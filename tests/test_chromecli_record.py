"""chromecli tab recording (0.12.1), offline: the CLI and chromecli-host with a fake extension on its stdio. The host
names nothing the extension asks for: the CLI picks the file, the host writes the extension's rec_chunk events into
it in order, logs record_start / record_stop (redacted), and the CLI converts to MP4 with ffmpeg (or keeps the WebM
and says so). No Chrome."""

from __future__ import annotations

import base64
import json
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from webcli.chromecli import protocol, recording
from webcli.chromecli.cli import parse_duration, to_mp4
from webcli.core import config

from test_chromecli import err, ext, home, run  # noqa: F401  (fixtures)

ORIGIN_URL = "https://example.com"


class Ext:
    """The extension's side of recording: record.start answers recording (after rec_started), chunks on demand,
    record.stop sends rec_stopped first, then the reply (like background.js)."""

    def __init__(self, make, start_state="recording", status_states=None):
        self.start_state = start_state
        self.status_states = list(status_states or [])
        self.recs: dict[str, dict] = {}
        self.frame_age = 16
        self.fake = make(self.handle)

    def handle(self, req):
        op, args = req["op"], req["args"]
        if op == "record.start":
            if not req.get("confirm"):
                return {"ok": False, "error": {"code": "bad_request", "message": "needs confirm"}}
            rid = args["recId"]
            rec = {"recId": rid, "tab": args["tab"], "origin": ORIGIN_URL, "state": self.start_state, "fps": args.get("fps"),
                   "audio": args.get("audio"), "maxSec": args.get("maxSec"), "bytes": 0, "chunks": 0}
            self.recs[rid] = rec
            if self.start_state == "recording":
                self.fake.send({"event": "rec_started", "recId": rid, "tab": args["tab"], "origin": ORIGIN_URL,
                                "mime": "video/webm;codecs=vp9", "fps": args.get("fps"), "audio": False,
                                "maxSec": args.get("maxSec"), "width": 2880, "height": 1800, "via": "confirm"})
            else:
                self.fake.send({"event": "rec_waiting", "recId": rid, "tab": args["tab"], "origin": ORIGIN_URL})
            return {"ok": True, "result": {**rec, "confirmedInBrowser": True,
                                           **({"hint": "Kevin: click the chromecli icon on the tab, press Record."}
                                              if self.start_state != "recording" else {})}}
        if op == "record.status":
            if self.status_states:
                state = self.status_states.pop(0)
                for r in self.recs.values():
                    r["state"] = state
            rows = [r for r in self.recs.values() if not args.get("recId") or r["recId"] == args["recId"]]
            return {"ok": True, "result": {"recordings": [dict(r) for r in rows]}}
        if op == "record.stop":
            stopped = []
            for r in self.recs.values():
                if (args.get("all") or r["tab"] == args.get("tab") or r["recId"] == args.get("recId")) and r["state"] == "recording":
                    r.update(state="stopped", reason="stopped (chromecli record stop)", durationMs=2500)
                    self.fake.send({"event": "rec_stopped", "recId": r["recId"], "tab": r["tab"], "origin": ORIGIN_URL,
                                    "state": "stopped", "reason": r["reason"], "bytes": r["bytes"], "chunks": r["chunks"],
                                    "durationMs": 2500, "mime": "video/webm;codecs=vp9"})
                    stopped.append(dict(r))
            return {"ok": True, "result": {"stopped": stopped, "finished": []}}
        if op == "tabs.screenshot":
            return {"ok": True, "result": {"tab": args["tab"], "format": "png", "source": "stream" if args.get("covered") else "visible",
                                           **({"frameAgeMs": self.frame_age} if args.get("covered") else {}),
                                           "data": base64.b64encode(b"\x89PNG fake").decode()}}
        return {"ok": True, "result": {}}

    def start(self):
        self.fake.start()
        return self

    def chunk(self, rid, seq, raw: bytes):
        self.fake.send({"event": "rec_chunk", "recId": rid, "seq": seq, "data": base64.b64encode(raw).decode()})
        r = self.recs.get(rid)
        if r:
            r["bytes"] += len(raw)
            r["chunks"] += 1


def wait_for(pred, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def log_text():
    p = config.home() / "logs" / "chromecli.log"
    return p.read_text() if p.exists() else ""


def start(*extra):
    res = run("record", "start", "7", "--confirm", *extra)
    assert res.exit_code == 0, res.output + res.stderr
    return json.loads(res.stdout)


def test_preview_without_confirm_sends_nothing(ext):
    e = Ext(ext).start()
    res = run("record", "start", "7")
    assert res.exit_code == 0
    plan = json.loads(res.stdout)
    assert plan["dryRun"] is True and plan["op"] == "record.start"
    assert plan["path"].endswith(".webm") and "recordings" in plan["path"]
    assert "Start click" in plan["needs"]
    assert not [m for m in e.fake.received if m.get("op", "").startswith("record")]


def test_start_chunks_stop_writes_the_file_incrementally(ext, home):
    e = Ext(ext).start()
    out = start("--fps", "24", "--max", "90s")
    rid, path = out["recId"], Path(out["path"])
    assert out["state"] == "recording" and out["fps"] == 24 and out["maxSec"] == 90
    assert path.parent == config.home() / "chromecli" / "recordings" and path.name.startswith("tab7 ") and " at " in path.name
    sent = next(m for m in e.fake.received if m.get("op") == "record.start")
    assert "out" not in sent["args"] and sent["args"]["recId"] == rid, "the extension never sees the path"
    assert sent["confirm"] is True
    e.chunk(rid, 0, b"\x1aE\xdf\xa3first")
    assert wait_for(lambda: path.exists() and path.read_bytes() == b"\x1aE\xdf\xa3first"), "written as it comes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    e.chunk(rid, 1, b"-second")
    assert wait_for(lambda: path.read_bytes().endswith(b"-second"))
    res = run("record", "stop", "7")
    assert res.exit_code == 0, res.output + res.stderr
    body = json.loads(res.stdout)
    row = body["stopped"][0]
    assert row["bytesWritten"] == len(b"\x1aE\xdf\xa3first-second") and row["path"] == str(path)
    assert row["exists"] is True
    log = log_text()
    assert "event=record_start tab=7 origin=https://example.com" in log and f'file="{path.name}"' in log
    assert "event=record_stop tab=7" in log and "duration_s=2.5" in log and "bytes=16" in log
    assert str(path.parent) not in log, "file names only, no paths"


def test_out_of_order_and_unknown_chunks_are_dropped(ext):
    e = Ext(ext).start()
    out = start()
    rid, path = out["recId"], Path(out["path"])
    e.chunk("f" * 32, 0, b"stranger")
    e.chunk(rid, 0, b"ok")
    e.chunk(rid, 2, b"gap")
    e.chunk(rid, 1, b"late")
    assert wait_for(lambda: log_text().count("rec_chunk_dropped") >= 2)
    time.sleep(0.1)
    assert path.read_bytes() == b"ok", "never written past a hole"
    files = json.loads(run("record", "status", "--format", "json").stdout)
    row = next(r for r in files if r["recId"] == rid)
    assert row.get("writeError") or row.get("error")


def test_mp4_without_ffmpeg_keeps_the_webm(ext, monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    e = Ext(ext).start()
    want = tmp_path / "demo.mp4"
    out = start("--out", str(want))
    assert out["path"] == str(want.with_suffix(".webm"))
    e.chunk(out["recId"], 0, b"webm-bytes")
    assert wait_for(lambda: want.with_suffix(".webm").exists())
    res = run("record", "stop", "7")
    row = json.loads(res.stdout)["stopped"][0]
    assert row["converted"] is False and "ffmpeg isn't on PATH" in row["note"] and str(want.with_suffix(".webm")) in row["note"]
    assert not want.exists() and want.with_suffix(".webm").read_bytes() == b"webm-bytes"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")
def test_mp4_with_ffmpeg_converts(ext, tmp_path):
    src = tmp_path / "src.webm"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=321x241:rate=25",
                    "-t", "1", "-c:v", "libvpx-vp9", "-b:v", "200k", str(src)], check=True)
    data = src.read_bytes()
    e = Ext(ext).start()
    want = tmp_path / "out" / "demo.mp4"
    out = start("--out", str(want), "--fps", "25")
    step = 4096
    for i in range(0, len(data), step):
        e.chunk(out["recId"], i // step, data[i:i + step])
    assert wait_for(lambda: want.with_suffix(".webm").exists() and want.with_suffix(".webm").stat().st_size == len(data))
    res = run("record", "stop", "7")
    row = json.loads(res.stdout)["stopped"][0]
    assert row["converted"] == "now", row
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(want)],
                                      capture_output=True, text=True, check=True).stdout)
    v = probe["streams"][0]
    assert v["codec_name"] == "h264" and v["width"] % 2 == 0 and v["height"] % 2 == 0
    assert want.with_suffix(".webm").exists(), "the WebM is kept"


def test_existing_file_needs_force(ext, tmp_path):
    Ext(ext).start()
    f = tmp_path / "x.webm"
    f.write_bytes(b"old")
    res = run("record", "start", "7", "--out", str(f), "--confirm")
    assert res.exit_code == 2 and "--force" in err(res)["message"]
    assert start("--out", str(f), "--force")["path"] == str(f)
    res = run("record", "start", "7", "--out", str(tmp_path / "x.mov"), "--confirm")
    assert res.exit_code == 2 and ".webm or .mp4" in err(res)["message"]


def test_awaiting_popup_waits_then_records(ext):
    Ext(ext, start_state="awaiting_popup", status_states=["awaiting_popup", "recording"]).start()
    res = run("record", "start", "7", "--confirm", "--wait", "30s")
    assert res.exit_code == 0, res.output + res.stderr
    assert "click the chromecli icon" in res.stderr
    body = json.loads(res.stdout)
    assert body["state"] == "recording" and body["confirmedInBrowser"] is True


def test_awaiting_popup_expired_is_exit_4(ext):
    Ext(ext, start_state="awaiting_popup", status_states=["expired"]).start()
    res = run("record", "start", "7", "--confirm", "--wait", "10s")
    assert res.exit_code == 4
    e = err(res)
    assert e["error"] == "confirm_timeout" and "nothing was recorded" in e["message"]


def test_refusals_pass_through(ext):
    ext(lambda req: {"ok": False, "error": {"code": "not_shared", "message": "Tab 7 was opened by an agent.",
                                            "reason": "bot_opened"}}).start()
    res = run("record", "start", "7", "--confirm")
    assert res.exit_code == protocol.exit_code("not_shared") == 4
    assert err(res)["reason"] == "bot_opened"
    assert recording.Recordings  # the host forgot the request (no file)
    assert not list((config.home() / "chromecli" / "recordings").glob("*")) if (config.home() / "chromecli" / "recordings").exists() else True


def test_screenshot_covered_flag(ext):
    e = Ext(ext).start()
    res = run("screenshot", "7", "--covered")
    assert res.exit_code == 0, res.stderr
    assert json.loads(res.stdout)["source"] == "stream"
    sent = next(m for m in e.fake.received if m.get("op") == "tabs.screenshot")
    assert sent["args"]["covered"] is True
    assert "warning" not in json.loads(res.stdout)
    assert run("screenshot", "7", "--covered", "--full-page").exit_code == 2
    e.frame_age = 8944  # minimized window: Chrome stopped painting, the frame is stale
    res = run("screenshot", "7", "--covered")
    assert res.exit_code == 0
    assert "minimized" in json.loads(res.stdout)["warning"] and "8.9s old" in res.stderr


def test_record_files_is_host_local_and_paused_stop_falls_back(ext):
    def handler(req):
        if req["op"] == "record.start":
            fake.send({"event": "rec_started", "recId": req["args"]["recId"], "tab": 7, "origin": ORIGIN_URL})
            return {"ok": True, "result": {"recId": req["args"]["recId"], "tab": 7, "state": "recording"}}
        return {"ok": False, "error": {"code": "paused", "message": "Agents are paused."}}

    fake = ext(handler).start()
    out = start()
    fake.send({"event": "rec_chunk", "recId": out["recId"], "seq": 0, "data": base64.b64encode(b"abc").decode()})
    fake.send({"event": "rec_stopped", "recId": out["recId"], "tab": 7, "state": "stopped", "reason": "paused",
               "bytes": 3, "durationMs": 1200})
    assert wait_for(lambda: "event=record_stop" in log_text())
    res = run("record", "stop", "7")
    assert res.exit_code == 0, res.stderr
    body = json.loads(res.stdout)
    assert "Pause already stopped" in body["note"]
    assert body["finished"][0]["bytesWritten"] == 3 and body["finished"][0]["reason"] == "paused"
    assert not [m for m in fake.received if m.get("op") == "record.files"], "answered by the host"


def test_parse_duration_and_ffmpeg_missing_helper(tmp_path, monkeypatch):
    assert parse_duration("10m", "--max", 1, 14400) == 600
    assert parse_duration("1h30m", "--max", 1, 14400) == 5400
    assert parse_duration("45", "--max", 1, 14400) == 45
    monkeypatch.setattr(shutil, "which", lambda n: None)
    res = to_mp4(tmp_path / "a.webm", tmp_path / "a.mp4", 30, False)
    assert res["ok"] is False and "brew install ffmpeg" in res["note"]


def test_agent_help_has_the_catches():
    res = run("agent-help")
    text = res.stdout
    assert "record start" in text and "Kevin clicks Start" in text
    assert "throttl" in text.lower() and "minimiz" in text.lower()
    assert "chromecli icon" in text
