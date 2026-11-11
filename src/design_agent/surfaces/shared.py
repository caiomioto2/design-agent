"""One local runtime for Slack and the terminal, with authenticated streamed graph access."""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict
from langgraph.types import Command, Interrupt, PregelTask, StateSnapshot

from design_agent.config import Settings
from design_agent.runtime.local import DEFAULT_RECURSION_LIMIT, LocalRuntime


def pack(value: Any) -> Any:
    """Only the public graph types needed by the official terminal; no executable serialization."""
    if isinstance(value, BaseMessage):
        return {"$message": message_to_dict(value)}
    if isinstance(value, Interrupt):
        return {"$interrupt": {"value": pack(value.value), "id": value.id}}
    if isinstance(value, StateSnapshot):
        return {"$snapshot": {key: pack(item) for key, item in value._asdict().items()}}
    if isinstance(value, PregelTask):
        return {"$task": {key: pack(item) for key, item in value._asdict().items()}}
    if isinstance(value, Command):
        return {
            "$command": {
                "graph": value.graph,
                "update": pack(value.update),
                "resume": pack(value.resume),
                "goto": pack(value.goto),
            }
        }
    if isinstance(value, tuple):
        return {"$tuple": [pack(item) for item in value]}
    if isinstance(value, list):
        return [pack(item) for item in value]
    if isinstance(value, dict):
        return {str(key): pack(item) for key, item in value.items()}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Exception):
        return type(value).__name__
    raise TypeError(f"Unsupported graph wire type: {type(value).__name__}")


def unpack(value: Any) -> Any:
    if isinstance(value, list):
        return [unpack(item) for item in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        if "$message" in value:
            return messages_from_dict([value["$message"]])[0]
        if "$tuple" in value:
            return tuple(unpack(item) for item in value["$tuple"])
        if "$interrupt" in value:
            return Interrupt(**unpack(value["$interrupt"]))
        if "$snapshot" in value:
            return StateSnapshot(**unpack(value["$snapshot"]))
        if "$task" in value:
            return PregelTask(**unpack(value["$task"]))
        if "$command" in value:
            return Command(**unpack(value["$command"]))
    return {key: unpack(item) for key, item in value.items()}


def local_endpoint(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or not parts.port
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
    ):
        raise ValueError("The shared runtime URL must be http://127.0.0.1:PORT")
    return "127.0.0.1", parts.port


def create_shared_app(
    runtime: LocalRuntime,
    store: Any,
    *,
    token: str,
    thread: str,
    mcp_info: list[dict[str, Any]] | None = None,
    conversations: Any = None,
) -> FastAPI:
    if len(token) < 32:
        raise ValueError(
            "The shared runtime needs a private random token of at least 32 characters"
        )
    from design_agent.surfaces.conversations import Conversations, valid_thread

    directory = conversations or Conversations(
        runtime.graph.checkpointer,
        store,
        runtime.project_root,
        operator=runtime.settings.slack_owner_user_id,
    )
    directory.runtime = runtime
    runtime.conversations = directory
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request: Request) -> None:
        expected = "Bearer " + token
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="Unauthorized")

    app.router.dependencies.append(Depends(authorized))

    def config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = raw or {}
        configurable = dict(raw.get("configurable", {}))
        configurable.pop("slack_event", None)
        try:
            selected = valid_thread(configurable.get("thread_id") or thread)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        return {
            **raw,
            "configurable": {
                **configurable,
                "thread_id": selected,
                "interactive_surface": "terminal",
            },
            "recursion_limit": DEFAULT_RECURSION_LIMIT,
        }

    @app.get("/info")
    async def info(thread_id: str | None = None) -> dict[str, Any]:
        selected = config({"configurable": {"thread_id": thread_id}})
        current = selected["configurable"]["thread_id"]
        snapshot = await runtime.graph.aget_state(selected)
        return {
            "revision": snapshot.created_at,
            "pending": bool(snapshot.next),
            "thread": current,
            "model": runtime.settings.model,
            "mcp": mcp_info or [],
            "busy": runtime._lock(current).locked(),
            "active_turns": sum(lock.locked() for lock in runtime._locks.values()),
            "background_work": runtime.busy(),
            "content_engine": "content-engine" in runtime.components.system_prompt,
            "tool_count": len(runtime.components.tools),
            "approval_servers": [
                server.name for server in runtime.components.mcp_servers if server.approval_required
            ],
            "approval_scopes": ["once", "session", "always"],
            "one_runtime": True,
        }

    @app.get("/conversations")
    async def conversation_list() -> list[dict[str, Any]]:
        return await directory.listing(limit=100)

    @app.get("/projects")
    async def project_list() -> list[dict[str, Any]]:
        return await directory.projects()

    @app.post("/resolve")
    async def resolve(body: dict[str, Any]) -> dict[str, str]:
        try:
            target = (
                await directory.project_thread(body["selector"])
                if body.get("project")
                else await directory.resolve(body["selector"])
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        return {"thread": target}

    @app.post("/new")
    async def new_conversation() -> dict[str, str]:
        selected = str(uuid.uuid4())
        await directory.touch(selected)
        return {"thread": selected}

    @app.post("/state")
    async def state(body: dict[str, Any]) -> Any:
        snapshot = await runtime.graph.aget_state(config(unpack(body.get("config"))))
        return pack(snapshot)

    @app.post("/update")
    async def update(body: dict[str, Any]) -> Any:
        selected = config(unpack(body.get("config")))
        async with runtime._lock(selected["configurable"]["thread_id"]):
            result = await runtime.graph.aupdate_state(
                selected,
                unpack(body.get("values")),
                as_node=body.get("as_node"),
            )
        return pack(result)

    @app.post("/store")
    async def put_store(body: dict[str, Any]) -> dict[str, bool]:
        await store.aput(tuple(body["namespace"]), body["key"], unpack(body["value"]))
        return {"ok": True}

    @app.post("/stream")
    async def stream(body: dict[str, Any]) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                selected = config(unpack(body.get("config")))
                current = selected["configurable"]["thread_id"]
                navigation_key = uuid.uuid4().hex
                selected["configurable"]["navigation_key"] = navigation_key
                async with runtime._lock(current):
                    directory.surfaces[current] = "terminal"
                    value = unpack(body.get("input"))
                    user_text = ""
                    if isinstance(value, dict):
                        messages = value.get("messages") or []
                        if messages:
                            last = messages[-1]
                            user_text = (
                                last.text
                                if isinstance(last, BaseMessage)
                                else str(last.get("content", ""))
                            )
                    await directory.touch(current, text=user_text)
                    kwargs = unpack(body.get("kwargs", {}))
                    kwargs.pop("context", None)
                    async for event in runtime.graph.astream(
                        unpack(body.get("input")),
                        config=selected,
                        **kwargs,
                    ):
                        yield json.dumps({"event": pack(event)}, ensure_ascii=False) + "\n"
                    await directory.touch(current)
                    target = await directory.consume(navigation_key)
                    if target:
                        yield json.dumps({"navigate": target}) + "\n"
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - provider errors can contain credentials
                yield json.dumps({"error": type(error).__name__}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    return app


class SharedGraph:
    """The native TUI talks to the exact graph that the Slack service owns."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.pending_thread: str | None = None

    @staticmethod
    def _config(config: Any) -> Any:
        # Callbacks belong to the UI process and cannot cross a JSON connection.
        return {
            key: value
            for key, value in (config or {}).items()
            if key not in {"callbacks", "run_manager"}
        }

    async def revision(self, thread: str | None = None) -> dict[str, Any]:
        response = await self.client.get("/info", params={"thread_id": thread} if thread else {})
        response.raise_for_status()
        return dict(response.json())

    async def conversations(self, *, projects: bool = False) -> list[dict[str, Any]]:
        response = await self.client.get("/projects" if projects else "/conversations")
        response.raise_for_status()
        return list(response.json())

    async def resolve(self, selector: str, *, project: bool = False) -> str:
        response = await self.client.post(
            "/resolve", json={"selector": selector, "project": project}
        )
        if response.status_code == 400:
            raise ValueError(response.json()["detail"])
        response.raise_for_status()
        return str(response.json()["thread"])

    async def new_thread(self) -> str:
        response = await self.client.post("/new")
        response.raise_for_status()
        return str(response.json()["thread"])

    async def aget_state(self, config: Any, **_kwargs: Any) -> Any:
        response = await self.client.post("/state", json={"config": pack(self._config(config))})
        response.raise_for_status()
        return unpack(response.json())

    async def aupdate_state(
        self, config: Any, values: Any, *, as_node: str | None = None, **_kwargs: Any
    ) -> Any:
        response = await self.client.post(
            "/update",
            json={"config": pack(self._config(config)), "values": pack(values), "as_node": as_node},
        )
        response.raise_for_status()
        return unpack(response.json())

    async def aput_store_item(
        self, namespace: tuple[str, ...], key: str, value: dict[str, Any]
    ) -> None:
        response = await self.client.post(
            "/store", json={"namespace": namespace, "key": key, "value": pack(value)}
        )
        response.raise_for_status()

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        kwargs.pop("context", None)
        async with self.client.stream(
            "POST",
            "/stream",
            json={
                "input": pack(input),
                "config": pack(self._config(config)),
                "kwargs": pack(kwargs),
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                item = json.loads(line)
                if "navigate" in item:
                    self.pending_thread = str(item["navigate"])
                    continue
                if "error" in item:
                    raise RuntimeError("Shared design runtime: " + item["error"])
                yield unpack(item["event"])


async def run_terminal(
    root: Path, settings: Settings, *, thread: str | None = None, new: bool = False
) -> None:
    """Attach the official UI without creating a second agent or helper runner."""
    from design_agent.runtime.sandbox import select_binding
    from design_agent.surfaces.terminal import make_app, prepare

    if not settings.shared_runtime_url or not settings.shared_runtime_token:
        raise ValueError("Configure the shared runtime URL and token")
    local_endpoint(settings.shared_runtime_url)
    _state, session = prepare(root, settings)
    async with httpx.AsyncClient(
        base_url=settings.shared_runtime_url,
        headers={"Authorization": "Bearer " + settings.shared_runtime_token.get_secret_value()},
        timeout=httpx.Timeout(None, connect=10),
    ) as client:
        response = await client.get("/info")
        response.raise_for_status()
        info = response.json()
        graph = SharedGraph(client)
        if new:
            selected = await graph.new_thread()
        elif thread:
            selected = await graph.resolve(thread)
        elif session.exists():
            selected = json.loads(session.read_text()).get("thread") or info["thread"]
        else:
            selected = info["thread"]
        locks: dict[str, asyncio.Lock] = {}
        runtime = SimpleNamespace(
            graph=graph,
            binding=select_binding(settings, root),
            out_dir=(
                settings.workspace_root
                if settings.workspace_root.is_absolute()
                else root / settings.workspace_root
            )
            / "out",
            _lock=lambda thread: locks.setdefault(thread, asyncio.Lock()),
        )
        store = SimpleNamespace(aput=graph.aput_store_item)
        from deepagents_code.mcp_tools import MCPServerInfo, MCPToolInfo

        mcp_info = [
            MCPServerInfo(
                name=item["name"],
                transport=item["transport"],
                status=item["status"],
                error=item.get("error"),
                tools=tuple(MCPToolInfo(**tool) for tool in item.get("tools", [])),
            )
            for item in info["mcp"]
        ]
        app = make_app(runtime, store, selected, root, settings, session, mcp_info=mcp_info)
        await app.run_async()
