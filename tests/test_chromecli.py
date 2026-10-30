"""chromecli offline (spec section 10, P1): framing, the socket's modes, the host's origin check and flock, the
error -> exit table, the policy (shared cases with the extension's node tests), audit redaction, bridge_down, the
CLI over a real host with a fake extension on its stdio, install-host, doctor, and agent-help. No Chrome."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from webcli.chromecli import EXTENSION_ID, extension_origin, host, install, policy, protocol
from webcli.chromecli.cli import group
from webcli.core import config
from webcli.core.errors import ConfigError

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "extensions" / "chromecli"
CASES = json.loads((EXT / "test" / "policy_cases.json").read_text())
ORIGIN = extension_origin()


@pytest.fixture
def home(monkeypatch):
    """A short WEBCLI_HOME (unix socket paths must stay under 104 bytes) and a temp NativeMessagingHosts dir."""
    work = Path(tempfile.mkdtemp(prefix="cc-", dir="/tmp"))
    monkeypatch.setenv("WEBCLI_HOME", str(work / "h"))
    monkeypatch.setattr(install, "nmh_dir", lambda: work / "nmh")
    monkeypatch.setattr(host, "POLL_SECONDS", 0.1)
    yield work / "h"
    shutil.rmtree(work, ignore_errors=True)


def run(*args):
    return CliRunner().invoke(group, list(args))


def err(result) -> dict:
    return json.loads(result.stderr.strip().splitlines()[-1])


class FakeExtension:
    """A chromecli-host on os pipes, with this object playing the extension: `handler(request) -> reply`."""

    def __init__(self, handler=None, argv_origin: str = ORIGIN) -> None:
        self.handler = handler or (lambda req: {"ok": True, "result": {"echo": req["op"]}})
        self.received: list[dict] = []
        r1, self.to_host = os.pipe()
        self.from_host, w2 = os.pipe()
        self.host_in, self.host_out = os.fdopen(r1, "rb", 0), os.fdopen(w2, "wb", 0)
        self.reader = os.fdopen(self.from_host, "rb", 0)
        self.rc = None
        self.thread = threading.Thread(target=self._host, args=(argv_origin,), daemon=True)
        self.ext_thread = threading.Thread(target=self._ext, daemon=True)

    def _host(self, origin):
        self.rc = host.main(["chromecli-host", origin], stdin=self.host_in, stdout=self.host_out)
        self.host_out.close()

    def _ext(self):
        while True:
            try:
                msg = protocol.read_frame(self.reader)
            except (protocol.ProtocolError, OSError, ValueError):
                return
            if msg is None:
                return
            self.received.append(msg)
            if msg.get("id") and msg.get("op"):
                answer = self.handler(msg)
                if answer is not None:  # None: never answer (timeouts)
                    self.send({"v": 1, "id": msg["id"], **answer})

    def send(self, msg):
        os.write(self.to_host, protocol.encode_frame(msg, protocol.EXT_TO_HOST_MAX))

    def start(self, wait_socket=True):
        self.thread.start()
        self.ext_thread.start()
        if wait_socket:
            sock = config.home() / "run" / "chromecli.sock"
            for _ in range(200):
                if sock.exists():
                    break
                time.sleep(0.01)
            assert sock.exists()
        return self

    def stop(self):
        try:
            os.close(self.to_host)
        except OSError:
            pass
        self.thread.join(5)
        self.ext_thread.join(5)


@pytest.fixture
def ext(home):
    made = []

    def make(handler=None, **kw):
        made.append(FakeExtension(handler, **kw))
        return made[-1]

    yield make
    for fake in made:
        fake.stop()


# framing

def test_frame_round_trip():
    msg = {"v": 1, "op": "tabs.read", "args": {"text": "héllo ✓"}}
    frame = protocol.encode_frame(msg)
    assert struct.unpack("<I", frame[:4])[0] == len(frame) - 4
    assert protocol.read_frame(io.BytesIO(frame)) == msg
    assert protocol.read_frame(io.BytesIO(b"")) is None


def test_frame_limits():
    big = {"data": "x" * protocol.HOST_TO_EXT_MAX}
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.encode_frame(big)
    assert exc.value.code == "too_large" and protocol.exit_code("too_large") == 2
    just = {"d": "x" * (protocol.HOST_TO_EXT_MAX - 10)}
    assert len(protocol.encode_frame(just)) - 4 <= protocol.HOST_TO_EXT_MAX
    over = struct.pack("<I", protocol.EXT_TO_HOST_MAX + 1)
    with pytest.raises(protocol.ProtocolError):
        protocol.read_frame(io.BytesIO(over))
    with pytest.raises(protocol.ProtocolError):
        protocol.read_frame(io.BytesIO(struct.pack("<I", 3) + b"{x}"))


def test_validate_request():
    assert protocol.validate_request({"v": 1, "op": "tabs.list"})["args"] == {}
    for bad in ({"v": 2, "op": "status"}, {"v": 1, "op": "page.elements"}, {"v": 1, "op": "x"},
                {"v": 1, "op": "status", "args": []}, [], {"v": 1, "op": "status", "confirm": "yes"}):
        with pytest.raises(protocol.ProtocolError) as exc:
            protocol.validate_request(bad)
        assert exc.value.code == "bad_request"


# error table

SPEC_CODES = {
    2: ["bad_request", "too_large", "bad_target", "missing_expect", "confirm_conflict"],
    3: ["tab_not_found", "element_not_found", "ambiguous_target", "no_history"],
    4: ["not_shared", "not_act_shared", "origin_blocked", "paused", "declined", "confirm_timeout", "not_visible",
        "permission_needed", "policy_pending"],
    5: ["bridge_down", "host_busy", "timeout", "url_mismatch", "script_error", "jev_error"],
    7: ["config_error", "host_not_installed", "jev_not_configured"],
    8: ["sent_unverified"],
}


def test_every_spec_error_code_maps_to_its_exit():
    for exit_, codes in SPEC_CODES.items():
        for code in codes:
            assert protocol.exit_code(code) == exit_, code
    assert protocol.exit_code("something_new") == 5


@pytest.mark.parametrize("code", [c for codes in SPEC_CODES.values() for c in codes])
def test_cli_exits_with_the_table_code(ext, code):
    ext(lambda req: {"ok": False, "error": {"code": code, "message": f"m {code}", "tab": 7}}).start()
    result = run("list")
    assert result.exit_code == protocol.ERROR_EXIT[code]
    e = err(result)
    assert e["error"] == code and e["code"] == protocol.ERROR_EXIT[code] and e["tab"] == 7


# socket, origin check, flock

def test_socket_is_0600_in_a_0700_dir(ext, home):
    ext().start()
    sock = home / "run" / "chromecli.sock"
    assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
    assert stat.S_ISSOCK(sock.stat().st_mode)


def test_socket_is_removed_on_stdin_eof(ext, home):
    fake = ext().start()
    fake.stop()
    assert fake.rc == 0
    assert not (home / "run" / "chromecli.sock").exists()
    assert run("list").exit_code == 5


def test_host_refuses_a_wrong_origin(home):
    out = io.BytesIO()
    rc = host.main(["chromecli-host", "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"], stdin=io.BytesIO(),
                   stdout=out)
    assert rc == 1
    msg = protocol.read_frame(io.BytesIO(out.getvalue()))
    assert msg["event"] == "error" and msg["code"] == "bad_origin"
    assert not (home / "run" / "chromecli.sock").exists()
    rc = host.main(["chromecli-host"], stdin=io.BytesIO(), stdout=io.BytesIO())
    assert rc == 1


def test_host_accepts_the_installed_manifest_origin(home):
    other = "abcdefghijklmnopabcdefghijklmnop"
    install.write(install.render(other, Path("/bin/true")))
    assert extension_origin(other) in install.allowed_origins()


def test_second_host_is_host_busy(ext, home):
    ext().start()
    out = io.BytesIO()
    rc = host.main(["chromecli-host", ORIGIN], stdin=io.BytesIO(), stdout=out)
    assert rc == 1
    msg = protocol.read_frame(io.BytesIO(out.getvalue()))
    assert msg == {"event": "error", "code": "host_busy", "message": "Another chromecli-host is already running."}
    assert protocol.exit_code("host_busy") == 5
    assert (home / "run" / "chromecli.sock").exists()  # the first host keeps serving


# bridge_down

def test_bridge_down_when_no_socket(home):
    result = run("list")
    assert result.exit_code == 5
    e = err(result)
    assert e["error"] == "bridge_down" and "install-host" in e["hint"]


def test_status_when_down_prints_state_and_exits_5(home):
    result = run("status")
    assert result.exit_code == 5
    out = json.loads(result.stdout)
    assert out["connected"] is False and out["expectedExtensionId"] == EXTENSION_ID
    assert err(result)["error"] == "bridge_down"


def test_stale_socket_is_bridge_down(home):
    (home / "run").mkdir(parents=True, exist_ok=True)
    import socket as s

    sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    sock.bind(str(home / "run" / "chromecli.sock"))
    sock.close()  # bound, nothing listening
    assert run("list").exit_code == 5


# the CLI over a real host

def fake_tabs(req):
    op, args = req["op"], req["args"]
    if op == "status":
        return {"ok": True, "result": {"extVersion": "0.7.0", "extensionId": EXTENSION_ID, "paused": False,
                                       "shares": [{"tab": 1, "origin": "https://example.com", "level": "read"}],
                                       "jevConfigured": False, "policy": {"hash": "x", "pending": False}}}
    if op == "tabs.list":
        return {"ok": True, "result": [{"id": 1, "title": "Ex", "url": "https://example.com/a", "origin":
                                        "https://example.com", "share": "read", "active": True, "windowId": 9,
                                        "botOpened": False}]}
    if op == "tabs.read":
        if args.get("tab") != 1:
            return {"ok": False, "error": {"code": "not_shared", "message": "Tab 2 is not shared.", "tab": 2}}
        return {"ok": True, "result": {"tab": 1, "url": "https://example.com/a?access_token=abcd1234efgh5678ijkl9012mnop",
                                       "origin": "https://example.com", "title": "Ex", "as": args.get("as"),
                                       "content": "# Hello", "chars": 7, "truncated": False}}
    if op == "tabs.screenshot":
        return {"ok": True, "result": {"tab": 1, "format": "png", "origin": "https://example.com",
                                       "data": base64.b64encode(b"\x89PNG fake").decode()}}
    if op == "history.search":
        return {"ok": True, "result": [{"title": "X", "url": "https://example.com/x", "lastVisit": "2026-10-22T08:00:00-05:00",
                                        "visitCount": 2}][: args.get("limit", 50)]}
    return {"ok": False, "error": {"code": "bad_request", "message": "?"}}


def test_status_list_read_history_screenshot(ext, home, tmp_path):
    fake = ext(fake_tabs).start()
    status = run("status", "--format", "json")
    assert status.exit_code == 0, status.output
    st = json.loads(status.stdout)
    assert st["connected"] is True and st["extensionIdMatches"] is True
    assert st["host"]["pid"] == os.getpid() and st["host"]["policyHash"]
    rows = json.loads(run("list", "--format", "json").stdout)
    assert rows[0]["share"] == "read" and rows[0]["id"] == 1
    read = run("read", "1", "--as", "md", "--format", "json")
    assert read.exit_code == 0 and json.loads(read.stdout)["content"] == "# Hello"
    text = run("read", "1", "--format", "text")
    head, _, body = text.stdout.partition("\n\n")
    assert body.startswith("# Hello")
    assert head.startswith("Title: Ex\nURL: https://example.com/a")
    assert "abcd1234efgh5678ijkl9012mnop" not in text.stdout, "a token in the URL reached the terminal"
    not_shared = run("read", "2")
    assert not_shared.exit_code == 4 and err(not_shared)["error"] == "not_shared"
    hist = run("history", "--limit", "5", "--query", "secret words", "--format", "json")
    assert hist.exit_code == 0 and json.loads(hist.stdout)[0]["visitCount"] == 2
    shot = run("screenshot", "1", "-o", str(tmp_path / "t.png"))
    assert shot.exit_code == 0 and (tmp_path / "t.png").read_bytes() == b"\x89PNG fake"
    assert json.loads(shot.stdout)["bytes"] == 9 and "data" not in json.loads(shot.stdout)
    sent = [m for m in fake.received if m.get("op") == "tabs.read"]
    assert sent[0]["args"] == {"tab": 1, "as": "md", "maxChars": 200000, "elements": False}
    assert sent[0]["confirm"] is False
    # the host pushed the policy on start
    pushed = [m for m in fake.received if m.get("event") == "policy"]
    assert pushed and pushed[0]["policy"]["deny"] == list(policy.DEFAULT_DENY)


def test_target_is_tab_or_active(ext):
    ext(fake_tabs).start()
    assert run("read").exit_code == 2
    assert run("read", "1", "--active").exit_code == 2
    assert err(run("show"))["error"] == "bad_target"


def test_audit_log_is_redacted(ext, home):
    ext(fake_tabs).start()
    assert run("read", "1").exit_code == 0
    assert run("history", "--query", "my private query").exit_code == 0
    log = (home / "logs" / "chromecli.log").read_text()
    assert "op=tabs.read" in log and "outcome=ok" in log
    assert "abcd1234efgh5678ijkl9012mnop" not in log and "<redacted>" in log
    assert "my private query" not in log and "sha256:" in log
    assert stat.S_IMODE((home / "logs" / "chromecli.log").stat().st_mode) == 0o600


def test_host_timeout(ext, monkeypatch):
    ext(lambda req: None).start()
    monkeypatch.setenv("CHROMECLI_TIMEOUT", "1")
    result = run("list")
    assert result.exit_code == 5 and err(result)["error"] == "timeout"


def test_config_error_blocks_ops_but_not_status(ext, home):
    config.ensure_home()
    (home / "config.toml").write_text('[chromecli]\nimplicit_active = "act"\n')
    ext(fake_tabs).start()
    result = run("list")
    assert result.exit_code == 7 and err(result)["error"] == "config_error"
    st = json.loads(run("status", "--format", "json").stdout)
    assert "implicit_active" in st["host"]["configError"]


def test_config_change_pushes_a_new_policy(ext, home):
    fake = ext(fake_tabs).start()
    config.ensure_home()
    (home / "config.toml").write_text('[chromecli]\nhistory = false\n')
    for _ in range(100):
        pushes = [m for m in fake.received if m.get("event") == "policy"]
        if len(pushes) >= 2:
            break
        time.sleep(0.05)
    assert pushes[-1]["policy"]["history"] is False


# policy (shared cases with extensions/chromecli/test/policy.test.mjs)

@pytest.mark.parametrize(("pattern", "kind"), CASES["kinds"])
def test_pattern_kinds(pattern, kind):
    assert policy.pattern_kind(pattern) == kind


@pytest.mark.parametrize(("pattern", "url", "want"), CASES["match"])
def test_pattern_matching(pattern, url, want):
    assert policy.matches(pattern, url) is want


@pytest.mark.parametrize(("url", "want"), CASES["denied_default"])
def test_default_deny(url, want):
    assert bool(policy.denied(url, policy.default_policy())) is want


@pytest.mark.parametrize(("url", "want"), CASES["builtin_with_empty_deny"])
def test_builtin_deny_is_not_removable(url, want):
    p = policy.parse({"deny": []})
    assert p["deny"] == []
    assert bool(policy.denied(url, p)) is want


def _build(over):
    over = dict(over)
    deny = list(policy.DEFAULT_DENY)
    deny += over.pop("deny_add", [])
    remove = over.pop("deny_remove", [])
    return policy.parse({"deny": [d for d in deny if d not in remove], **over})


@pytest.mark.parametrize("case", CASES["loosening"], ids=[c["name"] for c in CASES["loosening"]])
def test_loosening_is_flagged(case):
    kinds = [x["kind"] for x in policy.loosening(_build(case["old"]), _build(case["new"]))]
    assert kinds == case["kinds"]


def test_tighten_and_config_validation(home):
    old, new = _build({}), _build({"deny_remove": ["*.chase.com"], "deny_add": ["*.mybank.com"],
                                    "read_allow": ["https://docs.python.org"]})
    t = policy.tighten(old, new)
    assert "*.chase.com" in t["deny"] and "*.mybank.com" in t["deny"] and t["read_allow"] == []
    for bad in ({"implicit_active": "act"}, {"history": "yes"}, {"deny": ["chrome://*"]}, {"read_allow": ["path:/x"]},
                {"confirm_timeout": 5}, {"act_allow": "https://a.com"}):
        with pytest.raises(ConfigError):
            policy.parse(bad)
    # 0.11.4: an unknown key is a warning and ignored, not an error (tests/test_chromecli_config_warnings.py)
    assert policy.parse_checked({"nope": 1}) == (policy.default_policy(), policy.unknown_key_warnings({"nope": 1}))
    assert policy.parse(None)["hash"] == policy.default_policy()["hash"]
    config.ensure_home()
    (home / "config.toml").write_text('[chromecli]\nread_allow = ["https://Docs.Python.org"]\n')
    assert policy.load()["read_allow"] == ["https://docs.python.org"]


# install-host, doctor, agent-help, extension ID

def test_install_host_previews_then_writes(home, monkeypatch):
    monkeypatch.setattr(install, "host_path", lambda: Path("/usr/local/bin/chromecli-host"))
    preview = run("install-host")
    assert preview.exit_code == 0
    plan = json.loads(preview.stdout)
    assert plan["dryRun"] is True and not install.manifest_path().exists()
    assert plan["manifest"]["allowed_origins"] == [ORIGIN]
    written = run("install-host", "--confirm")
    assert written.exit_code == 0
    data = json.loads(install.manifest_path().read_text())
    assert data == {"name": "com.truefrontier.chromecli", "description": data["description"],
                    "path": "/usr/local/bin/chromecli-host", "type": "stdio", "allowed_origins": [ORIGIN]}
    assert run("install-host", "--confirm", "--dry-run").exit_code == 0
    assert run("install-host", "--extension-id", "NOTANID").exit_code == 2
    assert run("uninstall-host").exit_code == 0 and install.manifest_path().exists()
    assert json.loads(run("uninstall-host", "--confirm").stdout)["removed"] is True
    assert not install.manifest_path().exists()


def test_install_host_without_the_script_is_exit_7(home, monkeypatch):
    monkeypatch.setattr(install, "host_path", lambda: None)
    result = run("install-host", "--confirm")
    assert result.exit_code == 7 and err(result)["error"] == "host_not_installed"


def test_doctor_row(ext, home, monkeypatch):
    from webcli.apps import webcli as webcli_app

    monkeypatch.setattr(install, "host_path", lambda: Path("/bin/sh"))
    install.write(install.render())
    ext(fake_tabs).start()
    row = json.loads(run("doctor").stdout)
    assert row["host"]["manifestPresent"] and row["host"]["idMatches"] and row["connected"]
    assert row["extensionIdMatches"] and row["socket"]["mode"] == "0o600" and row["ok"]
    full = json.loads(CliRunner().invoke(webcli_app.build(), ["doctor", "--no-browser"]).stdout)
    assert full["chromecli"]["connected"] is True


def test_agent_help_lists_p1_commands():
    result = run("agent-help", "--format", "json")
    assert result.exit_code == 0
    ref = json.loads(result.stdout)
    names = {c["name"] for c in ref["commands"]}
    assert {"status", "list", "show", "read", "screenshot", "history", "install-host", "uninstall-host",
            "doctor"} <= names
    assert "find" in names  # P4 (Jev)
    assert any("bridge_down 5" in n for n in ref["notes"])


def test_extension_id_matches_the_committed_key():
    pub = (EXT / "extension-key-pub.txt").read_text().strip()
    manifest = json.loads((EXT / "manifest.json").read_text())
    assert manifest["key"] == pub
    digest = hashlib.sha256(base64.b64decode(pub)).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == EXTENSION_ID
    assert (EXT / "EXTENSION_ID").read_text().strip() == EXTENSION_ID
    # TODO: 0.17.2 intentionally left the extension manifest at 0.17.1.
    # Remove this once Kevin chooses: bump the extension, or drop the equality check.
    package = __import__("webcli").__version__
    if manifest["version"] != package:
        pytest.xfail(
            "TODO: extension manifest version "
            f"{manifest['version']} != package {package} "
            "(intentional since 0.17.2; Kevin has not chosen the fix)"
        )
    assert manifest["version"] == package


def test_extension_is_not_in_the_wheel():
    text = (REPO / "pyproject.toml").read_text()
    assert 'packages = ["webcli"]' in text and 'chromecli = "webcli.apps.chromecli:main"' in text
    assert 'chromecli-host = "webcli.chromecli.host:entry"' in text
