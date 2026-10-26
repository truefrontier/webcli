"""`webcli feed schedule`: a user LaunchAgent (ai.truefrontier.webcli.feed) that runs `webcli feed pull` at set
times (StartCalendarInterval). install writes it and, unless --no-load, loads it; remove unloads and deletes it."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from webcli.core import config

LABEL = "ai.truefrontier.webcli.feed"
DEFAULT_TIMES = ("05:30", "12:30")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def parse_times(value: str | None) -> list[tuple[int, int]]:
    """"05:30,12:30" -> [(5, 30), (12, 30)], sorted, no repeats. ValueError when a time is bad."""
    out = set()
    for part in (value or ",".join(DEFAULT_TIMES)).split(","):
        m = _TIME.match(part.strip())
        if not m:
            raise ValueError(f"{part.strip()!r} is not a time: use HH:MM (24h), comma separated.")
        out.add((int(m.group(1)), int(m.group(2))))
    return sorted(out)


def program() -> list[str]:
    found = shutil.which("webcli")
    return [found, "feed", "pull", "--quiet"] if found else [sys.executable, "-m", "webcli.apps.webcli", "feed",
                                                            "pull", "--quiet"]


def plist(times: list[tuple[int, int]], prog: list[str] | None = None) -> dict:
    env = {"PATH": f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if os.environ.get(config.HOME_ENV):
        env[config.HOME_ENV] = str(config.home())
    return {"Label": LABEL, "ProgramArguments": prog or program(),
            "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times],
            "RunAtLoad": False, "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
            "WorkingDirectory": str(config.home()), "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(config.home() / "logs" / "feed-schedule.err"), "EnvironmentVariables": env}


def loaded() -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0


def install(times: list[tuple[int, int]], *, load: bool = True) -> dict:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    (config.home() / "logs").mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist(times)))
    if load:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        done = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip() or done.returncode}")
    return status()


def remove() -> dict:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    plist_path().unlink(missing_ok=True)
    return status()


def status() -> dict:
    path = plist_path()
    times = []
    if path.exists():
        try:
            data = plistlib.loads(path.read_bytes())
            times = [f"{t['Hour']:02d}:{t['Minute']:02d}" for t in data.get("StartCalendarInterval") or []]
        except Exception:
            times = []
    return {"label": LABEL, "plist": str(path), "installed": path.exists(), "loaded": loaded(), "times": times}
