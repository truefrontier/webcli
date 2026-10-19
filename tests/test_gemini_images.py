"""Offline tests for images.py: format and size from file headers, target paths, and never overwriting in a DIR."""

from __future__ import annotations

import os
import struct
import zlib

import pytest

from webcli.sites.gemini import images


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk + b"\x00\x00\x00\x00IEND\xaeB`\x82"


def jpeg(width: int, height: int, *, progressive: bool = False) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x01\x01,\x01,\x00\x00"
    sof = b"\xff" + (b"\xc2" if progressive else b"\xc0") + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + b"\xff\xff" + sof + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00" + b"\x00" * 8 + b"\xff\xd9"


def webp(kind: str, width: int, height: int) -> bytes:
    if kind == "VP8X":
        body = b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    elif kind == "VP8 ":
        body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 4
    else:  # VP8L
        bits = (width - 1) | ((height - 1) << 14)
        body = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 4
    chunk = kind.encode() + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


@pytest.mark.parametrize(("data", "expected"), [
    (png(2752, 1536), ("png", 2752, 1536)),
    (jpeg(2752, 1536), ("jpeg", 2752, 1536)),
    (jpeg(1024, 572, progressive=True), ("jpeg", 1024, 572)),
    (webp("VP8X", 2752, 1536), ("webp", 2752, 1536)),
    (webp("VP8 ", 1376, 768), ("webp", 1376, 768)),
    (webp("VP8L", 4096, 4096), ("webp", 4096, 4096)),
    (b"<HTML><HEAD>moved</HEAD></HTML>", (None, None, None)),  # a 302 page is not an image
    (b"", (None, None, None)),
    (b"\x89PNG\r\n\x1a\n", ("png", None, None)),  # cut off: the format is known, the size isn't
    (b"\xff\xd8\xff\xe0\x00", ("jpeg", None, None)),
])
def test_sniff_reads_format_and_size_from_the_header(data, expected):
    assert images.sniff(data) == expected


@pytest.mark.parametrize(("target", "is_dir", "fmt"), [
    ("out/", True, "png"),
    ("out", True, "png"),  # no image suffix: a directory to create
    ("out.v2", True, "png"),
    ("stars.png", False, "png"),
    ("stars.PNG", False, "png"),
    ("stars.jpg", False, "jpeg"),
    ("stars.jpeg", False, "jpeg"),
    ("stars.webp", False, "webp"),
])
def test_dir_or_file_target_and_the_format_to_ask_for(tmp_path, target, is_dir, fmt):
    path = str(tmp_path / target) + ("/" if target.endswith("/") else "")
    assert images.is_dir_target(path) is is_dir
    assert images.wanted_format(tmp_path / target) == fmt


def test_an_existing_directory_with_an_image_suffix_is_a_directory(tmp_path):
    (tmp_path / "shots.png").mkdir()
    assert images.is_dir_target(tmp_path / "shots.png") and images.wanted_format(tmp_path / "shots.png") == "png"


@pytest.mark.parametrize(("prompt", "stem"), [
    ("a seamless tileable pattern of small blue stars on white", "a-seamless-tileable-pattern-of-small-blue-stars"),
    ("Generate an image: Hot-rod flames!", "generate-an-image-hot-rod-flames"),
    ("🌟🌟", "gemini-image"),
    ("x" * 80, "x" * 48),
])
def test_slug(prompt, stem):
    assert images.slug(prompt) == stem


def item(data, kind="full_size"):
    return {"data": data, "source_url_kind": kind}


def test_save_into_a_dir_numbers_files_uses_the_real_extension_and_never_overwrites(tmp_path):
    out = tmp_path / "new" / "dir"
    first = images.save(out, "blue stars", [item(png(4, 3)), item(jpeg(2, 1), "preview")])
    assert first == [
        {"path": str((out / "blue-stars-1.png").resolve()), "width": 4, "height": 3, "bytes": len(png(4, 3)),
         "mime": "image/png", "source_url_kind": "full_size"},
        {"path": str((out / "blue-stars-2.jpg").resolve()), "width": 2, "height": 1, "bytes": len(jpeg(2, 1)),
         "mime": "image/jpeg", "source_url_kind": "preview"},
    ]
    (out / "blue-stars-3.png").write_bytes(b"someone else's file")
    again = images.save(out, "blue stars", [item(png(4, 3))])
    assert again[0]["path"] == str((out / "blue-stars-4.png").resolve())
    assert (out / "blue-stars-3.png").read_bytes() == b"someone else's file"
    assert sorted(p.name for p in out.iterdir()) == ["blue-stars-1.png", "blue-stars-2.jpg", "blue-stars-3.png",
                                                    "blue-stars-4.png"]  # no temp files left
    assert os.stat(out / "blue-stars-1.png").st_mode & 0o777 == 0o600


def test_save_to_a_file_writes_exactly_it_then_numbers_the_rest(tmp_path):
    target = tmp_path / "stars.png"
    target.write_bytes(b"old")
    saved = images.save(target, "ignored", [item(png(8, 8)), item(png(9, 9)), item(png(7, 7))])
    assert [s["path"] for s in saved] == [str((tmp_path / n).resolve()) for n in ("stars.png", "stars-2.png", "stars-3.png")]
    assert target.read_bytes() == png(8, 8)  # the file the caller named is replaced
    assert [(s["width"], s["height"]) for s in saved] == [(8, 8), (9, 9), (7, 7)]


def test_save_reports_an_unknown_format_honestly(tmp_path):
    saved = images.save(tmp_path / "x.png", "p", [item(b"not an image")])
    assert (saved[0]["mime"], saved[0]["width"], saved[0]["height"]) == ("application/octet-stream", None, None)


def test_a_lost_race_for_a_name_moves_on_without_clobbering(tmp_path, monkeypatch):
    real_link = os.link

    def racing_link(src, dst):
        if str(dst).endswith("p-1.png"):  # another writer takes the name between the check and the link
            with open(dst, "wb") as handle:
                handle.write(b"theirs")
        return real_link(src, dst)

    monkeypatch.setattr(images.os, "link", racing_link)
    saved = images.save(tmp_path, "p", [item(png(1, 1))])
    assert saved[0]["path"].endswith("p-2.png")
    assert (tmp_path / "p-1.png").read_bytes() == b"theirs"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["p-1.png", "p-2.png"]
