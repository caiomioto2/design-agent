"""Bounded reads and turns through the existing authenticated runtime; no direct database writes."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
MAX_TEXT = 64_000


def loopback(url: str) -> str:
    parsed = httpx.URL(url)
    if (
        parsed.scheme != "http"
        or parsed.host != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use a host-configured loopback HTTP endpoint")
    return str(parsed).rstrip("/")


class Backend:
    def __init__(self, root: Path, client: httpx.AsyncClient) -> None:
        self.root = root.resolve()
        self.client = client

    @classmethod
    def configured(cls, root: Path) -> Backend:
        env = dotenv_values(root / ".env")
        url = loopback(env.get("DESIGN_AGENT_SHARED_RUNTIME_URL") or "")
        token = env.get("DESIGN_AGENT_SHARED_RUNTIME_TOKEN") or ""
        if len(token) < 32:
            raise ValueError("Shared runtime token is missing from the host configuration")
        return cls(
            root,
            httpx.AsyncClient(
                base_url=url,
                headers={"Authorization": "Bearer " + token},
                timeout=httpx.Timeout(None, connect=10),
                follow_redirects=False,
            ),
        )

    async def get(self, path: str) -> Any:
        response = await self.client.get(path)
        response.raise_for_status()
        return response.json()

    async def conversations(self) -> list[dict[str, Any]]:
        return await self.get("/conversations")

    async def known(self, thread: str) -> None:
        if not IDENTIFIER.fullmatch(thread) or thread not in {
            r["id"] for r in await self.conversations()
        }:
            raise ValueError("Choose a conversation returned by list_conversations")

    async def snapshot(self, thread: str) -> dict[str, Any]:
        response = await self.client.post(
            "/state", json={"config": {"configurable": {"thread_id": thread}}}
        )
        response.raise_for_status()
        return response.json()["$snapshot"]

    @staticmethod
    def pending(state: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for task in state.get("tasks", {}).get("$tuple", []):
            for item in task.get("$task", {}).get("interrupts", {}).get("$tuple", []):
                result.append(item["$interrupt"])
        return result

    async def context(self, thread: str, limit: int = 12) -> dict[str, Any]:
        await self.known(thread)
        state = await self.snapshot(thread)
        messages = state.get("values", {}).get("messages", [])
        recent = []
        for message in messages[-max(1, min(limit, 50)) :]:
            data = message.get("$message", {})
            content = data.get("data", {}).get("content", "")
            rendered = (
                content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            )
            recent.append({"role": data.get("type"), "content": rendered[:8000]})
        # The checkpoint is reference data, never permission to execute its tool calls.
        return {
            "context_id": thread,
            "revision": state.get("created_at"),
            "message_count": len(messages),
            "messages": recent,
            "pending_approvals": self.pending(state),
        }

    async def projects(self) -> list[dict[str, Any]]:
        return await self.get("/projects")

    def project_roots(self, thread: str) -> dict[str, Path]:
        registry = self.root / "workspace/state/project-mounts.json"
        mounts = json.loads(registry.read_text()) if registry.exists() else {}
        volume = "design-agent-ws-" + hashlib.sha256(thread.encode()).hexdigest()[:16]
        result = {}
        project_base = (self.root / "workspace/projects").resolve()
        for target, source in mounts.items():
            path = Path(target)
            if volume not in Path(source).parts or path.is_symlink() or not path.is_dir():
                continue
            if path.parent.resolve() == project_base:
                result[path.name] = path
        return result

    def file_path(self, thread: str, area: str, relative: str) -> Path:
        if not IDENTIFIER.fullmatch(thread):
            raise ValueError("Invalid conversation id")
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p.startswith(".") for p in parts):
            raise ValueError("Use a relative artifact path without hidden components")
        if area == "deliverables":
            # Same normalization as LocalOutputAdapter.folder in Design Agent.
            folder = re.sub(r"[^A-Za-z0-9._ -]+", "-", thread)[:80]
            base = self.root / "workspace/out" / folder
        elif area in self.project_roots(thread):
            base = self.project_roots(thread)[area]
        else:
            raise ValueError("Choose a project belonging to this conversation")
        path = base.joinpath(*parts)
        if any(p.is_symlink() for p in [path, *path.parents] if p != base.parent):
            raise ValueError("Artifact symlinks are not exposed")
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(base.resolve())
            or not resolved.is_file()
            or resolved.name in {"delivery.key"}
        ):
            raise ValueError("Artifact does not exist in the selected conversation")
        return resolved

    async def artifacts(self, thread: str, limit: int = 100) -> list[dict[str, Any]]:
        await self.known(thread)
        folder = re.sub(r"[^A-Za-z0-9._ -]+", "-", thread)[:80]
        roots = {"deliverables": self.root / "workspace/out" / folder, **self.project_roots(thread)}
        result = []
        for area, root in roots.items():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if len(result) >= max(1, min(limit, 300)):
                    return result
                if not path.is_file():
                    continue
                relative = str(path.relative_to(root))
                try:
                    safe = self.file_path(thread, area, relative)
                except ValueError:
                    continue
                result.append(
                    {
                        "area": area,
                        "path": relative,
                        "host_path": str(safe),
                        "bytes": safe.stat().st_size,
                        "media_type": mimetypes.guess_type(safe.name)[0]
                        or "application/octet-stream",
                    }
                )
        return result

    async def read_artifact(self, thread: str, area: str, path: str) -> dict[str, Any]:
        await self.known(thread)
        safe = self.file_path(thread, area, path)
        result: dict[str, Any] = {"host_path": str(safe), "bytes": safe.stat().st_size}
        with safe.open("rb") as file:
            data = file.read(MAX_TEXT + 1)
        try:
            text = data[:MAX_TEXT].decode("utf-8")
            if "\x00" in text:
                raise UnicodeError
            result.update(text=text, truncated=len(data) > MAX_TEXT)
        except UnicodeError:
            result["binary"] = True
        return result

    async def turn(self, thread: str, text: str) -> dict[str, Any]:
        before = await self.snapshot(thread)
        if self.pending(before):
            return {
                "state": "input-required",
                "text": "Review the pending tools in the original Design Agent conversation. This bridge cannot approve them.",
            }
        # No Slack identity is forwarded; saved Slack grants and delivery routes are not inherited.
        async with self.client.stream(
            "POST",
            "/stream",
            json={
                "input": {"messages": [{"role": "user", "content": text}]},
                "config": {"configurable": {"thread_id": thread}},
                "kwargs": {"stream_mode": "updates", "subgraphs": False},
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line and "error" in json.loads(line):
                    raise RuntimeError(
                        "Design Agent turn failed; inspect its context before retrying"
                    )
        after = await self.snapshot(thread)
        if self.pending(after):
            return {
                "state": "input-required",
                "text": "Tool approval is pending in Design Agent. Inspect the context and review it with the person.",
            }
        messages = after.get("values", {}).get("messages", [])
        reply = next(
            (
                m["$message"]["data"].get("content", "")
                for m in reversed(messages)
                if m.get("$message", {}).get("type") == "ai"
            ),
            "Turn finished; inspect the context and artifacts.",
        )
        return {
            "state": "completed",
            "text": reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False),
        }
