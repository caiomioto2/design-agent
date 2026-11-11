"""A terminal delivery yields a link whose bytes can actually be opened."""

import re
from pathlib import Path

from starlette.testclient import TestClient

from design_agent.surfaces.files import create_files_app, download_url
from design_agent.tools.delivery import DeliveredFile, LocalOutputAdapter


def test_receipt_link_downloads_the_delivered_file(tmp_path: Path) -> None:
    out = tmp_path / "out"
    adapter = LocalOutputAdapter(out, base_url="http://testserver")
    receipt = adapter.deliver("thread-1", [DeliveredFile("image with spaces.png", b"png")])
    url = re.search(r"\]\((http[^)]+)\)", receipt)[1]
    with TestClient(create_files_app(out)) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == b"png"
        assert response.headers["content-disposition"].startswith("inline;")
        assert client.get(url.replace("thread-1", "thread-2")).status_code == 404
        assert client.get("/").status_code == 404
        assert client.get("/docs").status_code == 404


def test_signed_link_cannot_follow_a_symlink_outside_deliveries(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "thread").mkdir(parents=True)
    private = tmp_path / "private.txt"
    private.write_text("private")
    (out / "thread" / "file.txt").symlink_to(private)
    url = download_url(out, "http://testserver", "thread", "file.txt")
    with TestClient(create_files_app(out)) as client:
        assert client.get(url).status_code == 404
