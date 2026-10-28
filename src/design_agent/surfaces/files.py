"""Read-only downloads for local deliveries, protected by per-file capability links."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from starlette.responses import FileResponse


def delivery_key(out_dir: Path) -> bytes:
    state = out_dir.parent / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "delivery.key"
    try:
        with path.open("xb") as file:
            path.chmod(0o600)
            file.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("The local delivery key is invalid.")
    return key


def download_url(out_dir: Path, base_url: str, thread: str, name: str) -> str:
    relative = f"{thread}/{name}"
    signature = hmac.new(delivery_key(out_dir), relative.encode(), hashlib.sha256).hexdigest()
    return (
        f"{base_url.rstrip('/')}/files/{signature}/{quote(thread, safe='')}/{quote(name, safe='')}"
    )


def create_files_app(out_dir: Path) -> FastAPI:
    """Serve only delivered files; no listings, provider access, or agent requests."""
    key = delivery_key(out_dir)
    root = out_dir.resolve()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/files/{signature}/{thread}/{name}")
    def download(signature: str, thread: str, name: str) -> FileResponse:
        relative = f"{thread}/{name}"
        expected = hmac.new(key, relative.encode(), hashlib.sha256).hexdigest()
        path = (root / thread / name).resolve()
        if (
            not hmac.compare_digest(signature, expected)
            or root not in path.parents
            or not path.is_file()
        ):
            raise HTTPException(status_code=404, detail="No such delivery")
        inline = path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        return FileResponse(
            path,
            filename=name,
            content_disposition_type="inline" if inline else "attachment",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    return app
