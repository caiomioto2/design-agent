"""Public GitHub font references work without granting the sandbox network access."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx

from design_agent.testing.fakes import FolderSandbox
from design_agent.tools.fetch import build_fetch_tool

ROOT = Path(__file__).resolve().parents[2]
FONT = (ROOT / "fonts/FragmentMono-Regular.ttf").read_bytes()


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("repo-version/fonts/Reference-Regular.ttf", FONT)
        z.writestr("repo-version/LICENCE", "OFL license fixture")
        z.writestr("repo-version/sources/build.py", 'raise RuntimeError("never run")')
    return output.getvalue()


async def test_github_repository_reference_extracts_fonts_and_license(tmp_path: Path) -> None:
    requested = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://codeload.github.com/example/reference/zip/main"}
            )
        return httpx.Response(200, content=archive(), headers={"content-type": "application/zip"})

    backend = FolderSandbox(tmp_path / "sandbox")
    tool = build_fetch_tool(
        lambda _r: backend, resolver=public_resolver, transport=httpx.MockTransport(serve)
    )
    result = await tool.coroutine(
        url="https://github.com/example/reference",
        sandbox_path="/workspace/projects/p/fonts",
        runtime=None,
        extract_fonts=True,
    )
    assert "font" in result.lower() and "not fetched" not in result
    files = list((tmp_path / "sandbox/workspace/projects/p/fonts").rglob("*"))
    assert any(p.name == "Reference-Regular.ttf" and p.read_bytes() == FONT for p in files)
    assert any(p.name == "LICENCE" for p in files)
    assert not any(p.name == "build.py" for p in files)
    manifest = json.loads(next(p for p in files if p.name == "font-download.json").read_text())
    assert manifest["source_url"] == "https://github.com/example/reference"
    assert manifest["files"][0]["sha256"]
    assert requested[0] == "https://api.github.com/repos/example/reference/zipball"


async def test_github_blob_font_url_uses_raw_file_and_accepts_binary_mime(tmp_path: Path) -> None:
    requested = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200, content=FONT, headers={"content-type": "application/octet-stream"}
        )

    backend = FolderSandbox(tmp_path / "sandbox")
    tool = build_fetch_tool(
        lambda _r: backend, resolver=public_resolver, transport=httpx.MockTransport(serve)
    )
    result = await tool.coroutine(
        url="https://github.com/example/reference/blob/main/Font.ttf",
        sandbox_path="/workspace/projects/p/Font.ttf",
        runtime=None,
    )
    assert "wrote" in result
    assert requested == ["https://raw.githubusercontent.com/example/reference/main/Font.ttf"]
    assert (tmp_path / "sandbox/workspace/projects/p/Font.ttf").read_bytes() == FONT


async def test_binary_response_is_not_permission_to_download_arbitrary_executables(
    tmp_path: Path,
) -> None:
    backend = FolderSandbox(tmp_path / "sandbox")
    tool = build_fetch_tool(
        lambda _r: backend,
        resolver=public_resolver,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                content=b"MZ" + b"bad" * 100,
                headers={"content-type": "application/octet-stream"},
            )
        ),
    )
    result = await tool.coroutine(
        url="https://example.com/fake.ttf",
        sandbox_path="/workspace/projects/p/fake.ttf",
        runtime=None,
    )
    assert "not a supported font or ZIP" in result
    assert not (tmp_path / "sandbox/workspace/projects").exists()


def test_font_archive_rejects_paths_that_leave_the_destination() -> None:
    import pytest

    from design_agent.tools.font_downloads import FontArchiveError, font_archive

    for name in ["../outside.ttf", "/absolute.ttf", "C:/outside.ttf", "dir\\outside.ttf"]:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as z:
            z.writestr(name, FONT)
        with pytest.raises(FontArchiveError, match="unsafe paths"):
            font_archive(output.getvalue())


def test_font_archive_refuses_expansion_beyond_its_budget(monkeypatch) -> None:
    import pytest

    from design_agent.tools import font_downloads

    monkeypatch.setattr(font_downloads, "MAX_FONT_BYTES", 8)
    with pytest.raises(font_downloads.FontArchiveError, match="too large"):
        font_downloads.font_archive(archive())


async def test_github_archive_redirect_still_refuses_private_network(tmp_path: Path) -> None:
    backend = FolderSandbox(tmp_path / "sandbox")
    tool = build_fetch_tool(
        lambda _r: backend,
        resolver=public_resolver,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(302, headers={"location": "http://127.0.0.1/secret.zip"})
        ),
    )
    result = await tool.coroutine(
        url="https://github.com/example/reference",
        sandbox_path="/workspace/projects/p/fonts",
        extract_fonts=True,
        runtime=None,
    )
    assert "not fetched" in result
    assert not (tmp_path / "sandbox/workspace/projects").exists()


def test_font_archive_refuses_symlinks_and_duplicate_targets() -> None:
    import stat

    import pytest

    from design_agent.tools.font_downloads import FontArchiveError, font_archive

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        entry = zipfile.ZipInfo("fonts/link.ttf")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(entry, "/etc/passwd")
    with pytest.raises(FontArchiveError, match="unsafe paths"):
        font_archive(output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("fonts/FONT.ttf", FONT)
        z.writestr("fonts/font.ttf", FONT)
    with pytest.raises(FontArchiveError, match="repeats"):
        font_archive(output.getvalue())
