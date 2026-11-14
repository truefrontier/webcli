"""webcli/scripts/window_check.py compiles its sampler once into $TMPDIR/window-check-<uid>/ and runs it from there,
so it must never run one from a folder anyone else could have made or written to (with no $TMPDIR, that's /tmp)."""

from __future__ import annotations

import hashlib
import os

import pytest

from webcli.scripts import window_check


def plant(tmp_path, monkeypatch, *, mode: int = 0o700, link: bool = False):
    """A cached sampler binary, already in place in a temp dir's window-check-<uid>/."""
    monkeypatch.setattr(window_check.tempfile, "gettempdir", lambda: str(tmp_path))
    work = tmp_path / f"window-check-{os.getuid()}"
    real = tmp_path / "elsewhere" if link else work
    real.mkdir()
    real.chmod(mode)
    if link:
        work.symlink_to(real)
    binary = real / f"sampler-{hashlib.sha256(window_check.SAMPLER.encode()).hexdigest()[:12]}"
    binary.write_text("#!/bin/sh\necho planted\n")
    return work / binary.name


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o755])
def test_a_sampler_in_a_folder_others_can_write_or_read_is_never_run(tmp_path, monkeypatch, mode):
    plant(tmp_path, monkeypatch, mode=mode)
    with pytest.raises(SystemExit, match="isn't private to you"):
        window_check.sampler_binary()


def test_a_linked_folder_is_never_trusted(tmp_path, monkeypatch):
    plant(tmp_path, monkeypatch, link=True)
    with pytest.raises(SystemExit, match="isn't private to you"):
        window_check.sampler_binary()


def test_the_cached_sampler_in_its_private_folder_is_reused(tmp_path, monkeypatch):
    binary = plant(tmp_path, monkeypatch)
    assert window_check.sampler_binary() == binary
