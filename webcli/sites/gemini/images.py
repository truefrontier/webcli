"""Saving generated images: format and size from the file header, and where each image goes. No browser code.

`--image-out` names a directory or a file. A directory (one that exists, a path ending in "/", or a path
without an image suffix) gets <prompt-slug>-<n>.<ext>, with the extension of the real format and n the
next free number, so a file is never overwritten there. A file path (.png/.jpg/.jpeg/.webp) is written
exactly, overwriting it; a second image goes to <stem>-2<suffix>, a third to <stem>-3<suffix>, and so on.
"""

from __future__ import annotations

import os
import re
import struct
import tempfile
from pathlib import Path

SUFFIX_FORMAT = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
EXTENSION = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
# FIFE options that make the image server answer in a format: -rp PNG, -rw WebP, none for JPEG (what Gemini's
# own download button saves). PNG is the default: lossless, at the same full size.
FIFE_FORMAT = {"png": "-rp", "jpeg": "", "webp": "-rw"}
DEFAULT_FORMAT = "png"
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def sniff(data: bytes) -> tuple[str | None, int | None, int | None]:
    """(format, width, height) from the magic bytes and header: "png", "jpeg", or "webp", else (None, None, None).
    A size that can't be read is None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if data[12:16] == b"IHDR" and len(data) >= 24:
            return ("png", *struct.unpack(">II", data[16:24]))
        return "png", None, None
    if data[:3] == b"\xff\xd8\xff":
        return ("jpeg", *_jpeg_size(data))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", *_webp_size(data))
    return None, None, None


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    """Width and height from the first SOFn segment: walk the marker segments after SOI."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None, None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # no length field
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if marker in _JPEG_SOF and i + 9 <= len(data):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        if marker == 0xDA or length < 2:  # scan data starts: no frame header before it
            return None, None
        i += 2 + length
    return None, None


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None, None


def is_dir_target(out: Path | str) -> bool:
    text = str(out)
    path = Path(text)
    return path.is_dir() or text.endswith(("/", os.sep)) or path.suffix.lower() not in SUFFIX_FORMAT


def wanted_format(out: Path) -> str:
    """The format to ask Gemini's image server for: the file suffix's, else DEFAULT_FORMAT."""
    return DEFAULT_FORMAT if is_dir_target(out) else SUFFIX_FORMAT[out.suffix.lower()]


def slug(prompt: str, limit: int = 48) -> str:
    """A file-name stem from the prompt's first words: "A seamless tileable pattern…" -> "a-seamless-tileable-pattern"."""
    words = re.findall(r"[a-z0-9]+", prompt.casefold())
    out = ""
    for word in words:
        if len(out) + len(word) + (1 if out else 0) > limit:
            break
        out = f"{out}-{word}" if out else word
    return out or (words[0][:limit] if words else "gemini-image")


def _publish(path: Path, data: bytes, *, overwrite: bool) -> bool:
    """Write via a temp file in the same dir. overwrite=False links it into place, so an existing file (even
    one created a moment ago by someone else) is never replaced: False then. The file ends up mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if overwrite:
            os.replace(tmp, path)
            return True
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(out: Path, prompt: str, downloads: list[dict]) -> list[dict]:
    """Write each download ({data, source_url_kind}) under `out` (see the module doc). Returns one
    {path, width, height, bytes, mime, source_url_kind} per image, in order. OSError if a write fails."""
    directory = is_dir_target(out)
    if directory:
        out.mkdir(parents=True, exist_ok=True)
    stem = slug(prompt)
    number = 0
    saved = []
    for index, item in enumerate(downloads, 1):
        data = item["data"]
        fmt, width, height = sniff(data)
        if directory:
            while True:
                number += 1
                # A number is taken in any format. ponytail: two writers racing in one DIR could still get
                # x-1.png and x-1.jpg (never an overwrite); reserve numbers with O_EXCL if that ever matters.
                path = out / f"{stem}-{number}{EXTENSION.get(fmt, '.bin')}"
                if not any(out.glob(f"{stem}-{number}.*")) and _publish(path, data, overwrite=False):
                    break
        else:
            path = out if index == 1 else out.with_name(f"{out.stem}-{index}{out.suffix}")
            _publish(path, data, overwrite=True)
        saved.append({"path": str(path.resolve()), "width": width, "height": height, "bytes": len(data),
                      "mime": f"image/{fmt}" if fmt else "application/octet-stream",
                      "source_url_kind": item["source_url_kind"]})
    return saved
