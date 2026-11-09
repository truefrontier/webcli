"""No page script carries a control character. A regex's \\b written in a plain Python string reaches Chrome as a
backspace, so the pattern silently never matches: google-photos-cli's info label regex did that from its first commit
(found by its overflow/gphotostests review, 2a975b4). webcli has none today; this keeps it that way."""

from __future__ import annotations

import importlib
import pkgutil

import webcli


def page_scripts() -> dict[str, str]:
    """Every module-level *_JS string in webcli, by module.name."""
    found = {}
    for info in pkgutil.walk_packages(webcli.__path__, "webcli."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if name.endswith("_JS") and isinstance(value, str):
                found[f"{info.name}.{name}"] = value
    return found


def test_no_page_script_carries_a_control_character():
    scripts = page_scripts()
    assert len(scripts) > 50  # the walk found the sites' scripts
    bad = {where: sorted({hex(ord(c)) for c in script if ord(c) < 0x20 and c not in "\n\t"})
           for where, script in scripts.items()}
    assert {where: chars for where, chars in bad.items() if chars} == {}
