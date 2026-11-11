"""Generated raster files reach Paper without exposing the host filesystem."""

import base64
import io
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent
from PIL import Image

from design_agent.paper_bridge.app import create_app
from design_agent.paper_bridge.images import ImageAssets, decode_raster
from design_agent.tools.paper import IMPORTS_REFUSED, PaperPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_bridge_places_real_raster_bytes_but_refuses_raw_local_paths(verified: bool) -> None:
    desktop = FastMCP("desktop")
    received = []
    image = io.BytesIO()
    Image.new("RGB", (24, 16), "cyan").save(image, format="PNG")

    @desktop.tool
    def get_fill_image(nodeId: str, fileId: str) -> ToolResult:
        if not verified:
            return ToolResult(
                content=[TextContent(type="text", text="No image fill")], is_error=True
            )
        return ToolResult(
            content=[
                ImageContent(
                    type="image",
                    data=base64.b64encode(image.getvalue()).decode(),
                    mimeType="image/png",
                )
            ]
        )

    @desktop.tool
    async def write_html(html: str, targetNodeId: str, mode: str, fileId: str) -> str:
        url = re.search(r'src="([^"]+)"', html)[1]
        assert url.startswith("paper-asset:///")
        path = Path(unquote(url.removeprefix("paper-asset://")))
        assert path.parent.name.startswith("design-agent-paper-images-")
        received.append((path.read_bytes(), targetNodeId, fileId))
        return '{"createdNodes": [{"id": "image-1"}]}'

    app = create_app(
        PaperPolicy(), token="x" * 43, upstream=desktop, hostnames=frozenset({"testserver"})
    )
    # Exercise the tool catalog and call through the actual HTTP bridge.
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        headers = {
            "Authorization": "Bearer " + "x" * 43,
            "Accept": "application/json, text/event-stream",
        }
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "place_image",
                    "arguments": {
                        "image_base64": base64.b64encode(image.getvalue()).decode(),
                        "fileId": "DRAFT",
                        "targetNodeId": "board-1",
                        "width": 24,
                        "height": 16,
                    },
                },
            },
        )
        result = response.json()["result"]
        assert bool(result.get("isError")) == (not verified), result
        if not verified:
            assert "pixels could not be verified" in str(result)
    assert received == [(image.getvalue(), "board-1", "DRAFT")]
    assert (
        PaperPolicy()
        .decide("write_html", {"fileId": "DRAFT", "html": '<img src="file:///secret.png">'})
        .refusal
        == IMPORTS_REFUSED
    )


@pytest.mark.parametrize("raw", [b"<svg></svg>", b"not an image", b"/etc/passwd"])
def test_raster_transfer_refuses_active_content_and_paths(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode_raster(base64.b64encode(raw).decode())


def test_image_transfer_writes_only_broker_owned_files_and_cleans_up() -> None:
    assets = ImageAssets()
    url = assets.add(b"raster", "image/png")
    assert url.endswith(".png")
    path = Path(unquote(url.removeprefix("paper-asset://")))
    assert path.read_bytes() == b"raster"
    assets.close()
    assert not path.exists()
