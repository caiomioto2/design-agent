"""Recognize binary font downloads and select fonts from bounded, untrusted ZIP archives."""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath

MAX_ARCHIVE_FILES = 20000
MAX_FONT_BYTES = 100 * 1024 * 1024
FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2", ".ttc"}


class FontArchiveError(ValueError):
    """A safe explanation for a refused font package."""


def binary_asset_type(data: bytes) -> str | None:
    signatures = {
        b"\x00\x01\x00\x00": "font/ttf",
        b"true": "font/ttf",
        b"OTTO": "font/otf",
        b"wOFF": "font/woff",
        b"wOF2": "font/woff2",
        b"ttcf": "font/collection",
    }
    if len(data) >= 12 and (kind := signatures.get(data[:4])):
        return kind
    if zipfile.is_zipfile(io.BytesIO(data)):
        return "application/zip"
    return None


def font_archive(data: bytes) -> list[tuple[str, bytes]]:
    """Validate before extracting any bytes; return only font and license/readme files."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_FILES:
                raise FontArchiveError("the archive contains too many files")
            selected = []
            names: set[str] = set()
            total = 0
            for item in entries:
                path = PurePosixPath(item.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in item.filename
                    or any(":" in part for part in path.parts)
                    or len(item.filename) > 1000
                    or stat.S_ISLNK(item.external_attr >> 16)
                ):
                    raise FontArchiveError("the archive contains unsafe paths or links")
                if item.is_dir():
                    continue
                name = path.name.lower()
                font = path.suffix.lower() in FONT_SUFFIXES
                license_file = name.startswith(("license", "licence", "ofl", "readme"))
                if not font and not license_file:
                    continue
                if item.filename.casefold() in names:
                    raise FontArchiveError("the archive repeats a filename")
                names.add(item.filename.casefold())
                total += item.file_size
                if total > MAX_FONT_BYTES or (license_file and item.file_size > 1024 * 1024):
                    raise FontArchiveError("the extracted font package is too large")
                selected.append((item, font))
            result = []
            for item, is_font in selected:
                content = archive.read(item)
                if is_font and not (binary_asset_type(content) or "").startswith("font/"):
                    raise FontArchiveError("a font file has an invalid signature")
                result.append((item.filename, content))
            if not any(PurePosixPath(name).suffix.lower() in FONT_SUFFIXES for name, _ in result):
                raise FontArchiveError("the archive contains no supported font files")
            return result
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
        raise FontArchiveError("the font archive is damaged, encrypted or unsupported") from None
