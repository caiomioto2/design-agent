"""Transfer only validated sandbox rasters through Paper Desktop's native asset importer."""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import io
import json
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent
from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
TTL_SECONDS = 300
MAX_CACHE_BYTES = 64 * 1024 * 1024


class ImageAssets:
    """Private broker-created files; callers cannot choose or reference desktop paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._folder: tempfile.TemporaryDirectory[str] | None = None
        self._images: dict[Path, tuple[float, int]] = {}

    def add(self, data: bytes, mime: str) -> str:
        with self._lock:
            now = time.monotonic()
            for path, (expires, _size) in list(self._images.items()):
                if expires <= now:
                    path.unlink(missing_ok=True)
                    del self._images[path]
            if sum(size for _expires, size in self._images.values()) + len(data) > MAX_CACHE_BYTES:
                raise ValueError("Image transfer is busy; wait for earlier transfers to expire.")
            if self._folder is None:
                self._folder = tempfile.TemporaryDirectory(prefix="design-agent-paper-images-")
            extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            path = Path(self._folder.name) / (secrets.token_hex(16) + extension)
            with path.open("xb") as file:
                file.write(data)
            path.chmod(0o600)
            self._images[path] = (now + TTL_SECONDS, len(data))
            return "paper-asset:///" + quote(path.as_posix().lstrip("/"), safe="/:")

    def close(self) -> None:
        with self._lock:
            if self._folder is not None:
                self._folder.cleanup()
                self._folder = None
            self._images.clear()


def decode_raster(encoded: str) -> tuple[bytes, str]:
    """Validate the bytes without handing arbitrary SVG, HTML or local paths to Desktop."""
    if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ValueError("Paper image exceeds the 15 MiB limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(data)) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
                image.format or ""
            )
            if mime is None or image.width * image.height > MAX_PIXELS:
                raise ValueError("Use a PNG, JPEG or WebP raster up to 40 megapixels.")
            image.verify()
    except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("The image file is not a readable PNG, JPEG or WebP.") from None
    return data, mime


def matching_raster(result: Any, source: bytes) -> bool:
    def thumbnail(data: bytes) -> Image.Image:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("Image is too large")
            rgba = image.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            return white.convert("RGB").resize((64, 64))

    for block in result.content:
        if not isinstance(block, ImageContent) or len(block.data) > 30 * 1024 * 1024:
            continue
        try:
            actual = thumbnail(base64.b64decode(block.data, validate=True))
            expected = thumbnail(source)
            difference = ImageStat.Stat(ImageChops.difference(actual, expected))
            if sum(difference.mean) / 3 < 12:
                return True
        except (ValueError, OSError, Image.DecompressionBombError):
            continue
    return False


def add_image_tool(proxy: FastMCP[Any], upstream: Any, assets: ImageAssets) -> None:
    """The bridge policy checks the destination before this tool can reach Paper."""

    @proxy.tool(name="place_image")
    async def place_image(
        image_base64: str,
        fileId: str,
        targetNodeId: str,
        width: int,
        height: int,
        mode: Literal["insert-children", "replace"] = "insert-children",
    ) -> ToolResult:
        """Insert a workspace raster as a child of an existing Paper artboard or frame.

        The agent host supplies image_base64 from the sandbox path. Inspect the artboard with
        get_screenshot after insertion before reporting that the visual is complete.
        """
        if not 1 <= width <= 16384 or not 1 <= height <= 16384:
            raise ValueError("Image width and height must be between 1 and 16384 pixels.")
        data, mime = decode_raster(image_base64)
        url = assets.add(data, mime)
        markup = f'<img src="{html.escape(url, quote=True)}" layer-name="Generated image" style="width:{width}px;height:{height}px;object-fit:contain;flex-shrink:0" />'
        async with Client(upstream, mode="auto") as client:
            result = await client.call_tool(
                "write_html",
                {
                    "html": markup,
                    "fileId": fileId,
                    "targetNodeId": targetNodeId,
                    "mode": mode,
                },
                raise_on_error=False,
            )
            if result.is_error:
                return ToolResult(content=result.content, is_error=True)
            nodes: list[Any] = []
            for block in result.content:
                if isinstance(block, TextContent):
                    try:
                        value = json.loads(block.text)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        nodes.extend(value.get("createdNodes") or [])
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                    continue
                for delay in (0, 0.5, 1.5):
                    await asyncio.sleep(delay)
                    fill = await client.call_tool(
                        "get_fill_image",
                        {
                            "nodeId": node["id"],
                            "fileId": fileId,
                        },
                        raise_on_error=False,
                    )
                    if not fill.is_error and matching_raster(fill, data):
                        return ToolResult(
                            content=[
                                *result.content,
                                TextContent(
                                    type="text",
                                    text="Paper image pixels verified against the source. Inspect the artboard screenshot for layout.",
                                ),
                            ]
                        )
        return ToolResult(
            content=[
                *result.content,
                TextContent(
                    type="text",
                    text=(
                        "An image node was created, but its pixels could not be verified. Do not say it was saved successfully. "
                        "Use the returned node id to inspect it; do not repeat the insertion automatically."
                    ),
                ),
            ],
            is_error=True,
        )


class WithImageAssets:
    """Remove the broker's temporary files when the bridge's ASGI lifespan ends."""

    def __init__(self, app: Any, assets: ImageAssets) -> None:
        self.app = app
        self.assets = assets

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        finally:
            if scope["type"] == "lifespan":
                await asyncio.to_thread(self.assets.close)
