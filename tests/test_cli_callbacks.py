"""Every option and argument callback in webcli, given an edge value, accepts it or exits 2. Never "Unexpected error"
(exit 5): that's what --since 3000000w, 0001-01-01T00:00:00+14:00 and CHROMECLI_TIMEOUT=nan used to do. Callbacks
only check values; file-path options are left out because they create folders."""

from __future__ import annotations

import click
import pytest

from webcli.apps import webcli as webcli_app
from webcli.core import daemon
from webcli.sites import data

EDGE = ["", " ", "nan", "inf", "-1", "0", "9" * 30, "-" + "9" * 30, "1e309", "3000000w", "99999999999d",
        "0001-01-01", "9999-12-31", "9999-12-31T23:59", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00", "\x00", "‮", "a" * 5000, "%", "https://", "../../etc"]


def callbacks():
    def walk(cmd, path):
        yield path, cmd
        if isinstance(cmd, click.Group):
            ctx = click.Context(cmd)
            for name in cmd.list_commands(ctx):
                yield from walk(cmd.get_command(ctx, name), [*path, name])

    seen = {}
    for path, cmd in walk(webcli_app.build(), ["webcli"]):
        for param in cmd.params:
            cb = param.callback
            if cb is None or param.name in ("version", "help") or any(s in param.name for s in ("file", "out")):
                continue
            seen.setdefault((cb.__module__, cb.__qualname__, param.name), (" ".join(path), cmd, param))
    return list(seen.values())


CALLBACKS = callbacks()


def test_the_walk_finds_the_since_and_id_checks():
    found = {f"{path} {param.name}" for path, _, param in CALLBACKS}
    assert {"webcli whatsapp read since", "webcli linkedin feed since", "webcli youtube history since"} <= found
    assert len(CALLBACKS) >= 20


@pytest.mark.parametrize(("path", "cmd", "param"), CALLBACKS, ids=[f"{p} --{q.name}" for p, _, q in CALLBACKS])
def test_an_edge_value_is_taken_or_a_usage_error(path, cmd, param, monkeypatch, tmp_path):
    def never(*a, **k):
        raise AssertionError("a callback must not call a site")

    monkeypatch.setattr(daemon, "call", never)
    monkeypatch.setattr(data, "call", never)
    monkeypatch.chdir(tmp_path)
    ctx = click.Context(cmd, info_name=path.rsplit(" ", 1)[-1])
    for value in EDGE:
        try:
            param.callback(ctx, param, (value,) if param.multiple or param.nargs == -1 else value)
        except click.UsageError:  # BadParameter included: exit 2
            pass
        except SystemExit as exc:
            assert exc.code == 2, f"{path} --{param.name} {value[:40]!r}: exit {exc.code}"
