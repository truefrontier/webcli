"""chromecli: your real, signed-in Chrome tabs, read through the "chromecli" MV3 extension and a native messaging host.

    chromecli CMD  --1 JSON line-->  ~/.webcli/run/chromecli.sock (0600, run/ 0700)
                   <--1 JSON line--  chromecli-host (native messaging host, stdio, spawned by Chrome)
                                       <--4-byte LE framed JSON-->  chromecli extension (MV3 service worker)

No CDP, no TCP port, no launchd: Chrome starts the host when the extension connects, and the host exits (unlinking the
socket) when Chrome closes its stdin. The extension is the authority for shares, the origin policy, and Pause; the CLI
and host are untrusted callers. Config: [chromecli] in ~/.webcli/config.toml. Audit log: ~/.webcli/logs/chromecli.log.
"""

from __future__ import annotations

HOST_NAME = "com.truefrontier.chromecli"
# The stable extension ID, from the public key committed at extensions/chromecli/extension-key-pub.txt (manifest "key").
EXTENSION_ID = "ileggcomhbnofnapippaanhimdhchhci"
SOCKET_NAME = "chromecli.sock"
LOCK_NAME = "chromecli.lock"
LOG_NAME = "chromecli"
CONFIG_SECTION = "chromecli"
TIMEOUT_ENV = "CHROMECLI_TIMEOUT"
FORMAT_ENV = "CHROMECLI_FORMAT"


def extension_origin(extension_id: str = EXTENSION_ID) -> str:
    return f"chrome-extension://{extension_id}/"
