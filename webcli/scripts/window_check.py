#!/usr/bin/env python3
"""Live verification: prove a command shows no Chrome window and steals no focus (macOS). Needs a signed-in
identity to be meaningful, so it is not run by the tests; run it after `identity login`:

    python -m webcli.scripts.window_check -- llmcli gemini status
    python -m webcli.scripts.window_check -- llmcli chatgpt -p "say ok"
    python -m webcli.scripts.window_check --report /tmp/wc.json -- llmcli gemini chats list

Runs COMMAND while sampling, every --interval seconds, every on-screen window (CGWindowListCopyWindowInfo with
kCGWindowListOptionOnScreenOnly), the frontmost app (NSWorkspace), and each app activation as it happens. Windows
count as webcli's when their owner's command line names one of the --profile paths (default: the data home,
$WEBCLI_HOME or ~/.webcli; pid -> args via ps). A window counts as visible when its bounds overlap a display; one
parked off-screen is reported but not counted.

    python -m webcli.scripts.window_check [--profile PATH ...] [--interval 0.5] [--report FILE] -- COMMAND [ARG ...]

Prints a JSON report on stderr (and to --report) after COMMAND exits, and exits with COMMAND's status, or 1 when
the check failed (a visible profile window, or a profile Chrome became the active app). No pyobjc needed: the
sampler is a small Swift program compiled once into $TMPDIR. Nothing here reads page content or window titles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

def default_profiles() -> list[str]:
    from webcli.core.config import home

    return [str(home())]

SAMPLER = r"""
import AppKit
import CoreGraphics

setvbuf(stdout, nil, _IOLBF, 0)
let interval = Double(CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "0.5") ?? 0.5

func emit(_ obj: [String: Any]) {
  if let data = try? JSONSerialization.data(withJSONObject: obj), let line = String(data: data, encoding: .utf8) {
    print(line)
  }
}

func appInfo(_ app: NSRunningApplication?) -> Any {
  guard let app = app else { return NSNull() }
  return ["pid": Int(app.processIdentifier), "name": app.localizedName ?? "", "bundle": app.bundleIdentifier ?? ""]
}

func num(_ value: Any?) -> Double { (value as? NSNumber)?.doubleValue ?? 0 }

func displays() -> [[Double]] {
  var count: UInt32 = 0
  CGGetActiveDisplayList(0, nil, &count)
  var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
  CGGetActiveDisplayList(count, &ids, &count)
  return ids.map { let b = CGDisplayBounds($0); return [Double(b.origin.x), Double(b.origin.y), Double(b.width), Double(b.height)] }
}

NSWorkspace.shared.notificationCenter.addObserver(
  forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
) { note in
  emit(["event": "activate", "t": Date().timeIntervalSince1970,
        "app": appInfo(note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)])
}

while true {
  let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
    as? [[String: Any]] ?? []
  let windows: [[String: Any]] = info.map { w in
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    return ["pid": Int(num(w[kCGWindowOwnerPID as String])), "owner": w[kCGWindowOwnerName as String] as? String ?? "",
            "layer": Int(num(w[kCGWindowLayer as String])), "alpha": num(w[kCGWindowAlpha as String]),
            "x": num(b["X"]), "y": num(b["Y"]), "w": num(b["Width"]), "h": num(b["Height"])]
  }
  let chrome: [[String: Any]] = NSWorkspace.shared.runningApplications
    .filter { ($0.bundleIdentifier ?? "").hasPrefix("com.google.Chrome") }
    .map { ["pid": Int($0.processIdentifier), "policy": $0.activationPolicy.rawValue, "active": $0.isActive,
            "hidden": $0.isHidden] }
  emit(["event": "sample", "t": Date().timeIntervalSince1970, "front": appInfo(NSWorkspace.shared.frontmostApplication),
        "displays": displays(), "windows": windows, "chrome": chrome])
  RunLoop.current.run(until: Date(timeIntervalSinceNow: interval))
}
"""

POLICY = {0: "regular (Dock icon)", 1: "accessory", 2: "prohibited (background)"}


def sampler_binary() -> Path:
    digest = hashlib.sha256(SAMPLER.encode()).hexdigest()[:12]
    work = Path(tempfile.gettempdir()) / f"window-check-{os.getuid()}"
    work.mkdir(mode=0o700, exist_ok=True)
    binary = work / f"sampler-{digest}"
    if not binary.exists():
        source = work / f"sampler-{digest}.swift"
        source.write_text(SAMPLER)
        subprocess.run(["swiftc", "-O", "-o", str(binary), str(source)], check=True)
    return binary


def process_table() -> dict[int, str]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit():
            table[int(pid)] = command
    return table


def overlaps(win: dict, display: list[float]) -> bool:
    x, y, w, h = display
    return win["w"] > 1 and win["h"] > 1 and win["x"] < x + w and win["x"] + win["w"] > x \
        and win["y"] < y + h and win["y"] + win["h"] > y


class Recorder:
    def __init__(self, binary: Path, interval: float, profiles: list[str]) -> None:
        self.profiles = profiles
        self.samples: list[dict] = []
        self.activations: list[dict] = []
        self.owners: dict[int, str] = {}  # pid -> command line, for every pid seen in a profile
        self.proc = subprocess.Popen([str(binary), str(interval)], stdout=subprocess.PIPE, text=True)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _profile_pids(self) -> set[int]:
        pids = set()
        for pid, command in process_table().items():
            if any(p in command for p in self.profiles):
                pids.add(pid)
                self.owners.setdefault(pid, command)
        return pids

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event["event"] == "activate":
                event["profilePids"] = sorted(self._profile_pids())
                self.activations.append(event)
            else:
                event["profilePids"] = sorted(self._profile_pids())
                self.samples.append(event)

    def wait_first(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not self.samples and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.samples:
            raise SystemExit("window_check: the sampler produced no sample")

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.thread.join(timeout=5)


def browser_summary(command: str) -> dict:
    """The main Chrome process for a profile (no --type=): how it was launched. Flags only, no values."""
    flags = [arg.split("=", 1)[0] for arg in command.split() if arg.startswith("--")]
    return {"headless": any(f == "--headless" for f in flags),
            "offscreenFlag": "--window-position" in flags,
            "remoteDebugging": any(f.startswith("--remote-debugging") for f in flags)}


def _distinct(windows: list[dict]) -> list[dict]:
    return [dict(t) for t in dict.fromkeys(tuple(w.items()) for w in windows)][:10]


def report(rec: Recorder, started: float, ended: float, status: int, command: list[str]) -> dict:
    during = [s for s in rec.samples if started <= s["t"] <= ended + 1.5]
    before = next((s for s in reversed(rec.samples) if s["t"] < started), rec.samples[0])
    after = rec.samples[-1]
    visible_max, offscreen, shown = 0, [], []
    profile_browsers: dict[int, dict] = {}
    dock_icon, states = False, set()
    for s in during:
        pids = set(s["profilePids"])
        mine = [w for w in s["windows"] if w["pid"] in pids]
        visible = [w for w in mine if w["layer"] == 0 and w["alpha"] > 0 and any(overlaps(w, d) for d in s["displays"])]
        visible_max = max(visible_max, len(visible))
        shown += [{k: w[k] for k in ("pid", "layer", "x", "y", "w", "h")} for w in visible]
        for w in mine:
            if w not in visible:
                offscreen.append({k: w[k] for k in ("pid", "layer", "x", "y", "w", "h")})
        for app in s["chrome"]:
            if app["pid"] in pids:
                dock_icon = dock_icon or app["policy"] == 0
                states.add((POLICY.get(app["policy"], app["policy"]), "hidden" if app["hidden"] else "shown",
                            "active" if app["active"] else "inactive"))
        for pid in pids:
            command_line = rec.owners.get(pid, "")
            if "--type=" not in command_line and "Google Chrome" in command_line:
                profile_browsers[pid] = browser_summary(command_line)
    profile_pids = {pid for s in rec.samples for pid in s["profilePids"]} | set(rec.owners)
    window_activations = [a for a in rec.activations if started <= a["t"] <= ended + 1.5]
    own = [a for a in window_activations if a["app"] and (a["app"]["pid"] in set(a["profilePids"]) or
                                                          a["app"]["pid"] in profile_pids)]
    fronts = {(s["front"] or {}).get("name") for s in during}
    out = {
        "command": " ".join(command)[:200],
        "exit": status,
        "seconds": round(ended - started, 1),
        "samples": len(during),
        "maxVisibleProfileWindows": visible_max,
        "visibleProfileWindows": _distinct(shown),
        "offscreenProfileWindows": _distinct(offscreen),
        "profileChromeDockIcon": dock_icon,
        "profileChromeAppStates": sorted(" / ".join(t) for t in states),
        "profileBrowsers": list(profile_browsers.values()),
        "frontmostBefore": (before["front"] or {}).get("name"),
        "frontmostAfter": (after["front"] or {}).get("name"),
        "frontmostDuring": sorted(n for n in fronts if n),
        "frontmostChanged": len({n for n in fronts if n} | {(before["front"] or {}).get("name")}) > 1,
        "activations": [(a["app"] or {}).get("name") for a in window_activations],
        "profileChromeActivated": bool(own),
    }
    out["ok"] = visible_max == 0 and not own and not out["frontmostChanged"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", action="append", help="A profile path to watch (default: the data home).")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give a COMMAND after --")
    rec = Recorder(sampler_binary(), args.interval, [str(Path(p).expanduser()) for p in args.profile or default_profiles()])
    rec.wait_first()
    time.sleep(args.interval * 2)
    started = time.time()
    status = subprocess.run(command).returncode
    ended = time.time()
    time.sleep(1.5)
    rec.stop()
    result = report(rec, started, ended, status, command)
    text = json.dumps(result, indent=1)
    print(text, file=sys.stderr)
    if args.report:
        args.report.write_text(text + "\n")
    return status if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
