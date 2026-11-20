"""Public Codex 0.167.1 App Server JSON-RPC over a host-registered WebSocket."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import websockets


class Rejected(RuntimeError):
    def __init__(self, error):
        # Provider messages may include private connection details. Never log them.
        super().__init__("App Server rejected the request")
        self.error = error

    def turn_race(self):
        message = str(self.error.get("message", "")).lower()
        return self.error.get("code") == -32600 and any(
            text in message
            for text in ("no active turn", "expected turn", "active turn id", "turn id mismatch")
        )


class Uncertain(RuntimeError):
    """Transmission may have happened; only history reconciliation can resolve this."""


def endpoint_url(url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path not in ("", "/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use an operator-configured ws://127.0.0.1:PORT endpoint or a secure local tunnel"
        )
    return url


class AppServer:
    def __init__(self, endpoint: str, *, token_file: Path | None = None, timeout: float = 15):
        self.endpoint = endpoint_url(endpoint)
        self.token_file, self.timeout = token_file, timeout
        self.socket = None
        self.reader = None
        self.serial = 0
        self.waiters = {}
        self.connect_lock = asyncio.Lock()

    async def connect(self):
        async with self.connect_lock:
            if self.reader and not self.reader.done():
                return
            auth = {}
            if self.token_file:
                if self.token_file.stat().st_mode & 0o077:
                    raise ValueError("App Server token file must be private")
                auth["Authorization"] = "Bearer " + self.token_file.read_text().strip()
            try:
                self.socket = await websockets.connect(
                    self.endpoint,
                    additional_headers=auth,
                    open_timeout=self.timeout,
                    max_size=16_000_000,
                    proxy=None,
                )
                self.reader = asyncio.create_task(self._read())
                await self.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "design_agent_callbacks", "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self.socket.send(json.dumps({"method": "initialized"}))
            except (websockets.WebSocketException, OSError, TimeoutError) as error:
                await self.close()
                raise ConnectionError("App Server handshake unavailable") from error
            except (Rejected, Uncertain, asyncio.CancelledError):
                await self.close()
                raise

    async def _read(self):
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                if "id" in message and "method" not in message:
                    waiter = self.waiters.get(message["id"])
                    if waiter and not waiter.done():
                        waiter.set_result(message)
                # Notifications need no response. Server requests (including approvals)
                # deliberately receive no automatic answer or permission grant.
        except (websockets.ConnectionClosed, OSError, ValueError, TypeError, KeyError):
            pass
        finally:
            for waiter in self.waiters.values():
                if not waiter.done():
                    waiter.set_exception(Uncertain("App Server disconnected"))

    async def request(self, method: str, params: dict):
        self.serial += 1
        identifier = self.serial
        future = asyncio.get_running_loop().create_future()
        self.waiters[identifier] = future
        try:
            await self.socket.send(
                json.dumps({"id": identifier, "method": method, "params": params})
            )
            response = await asyncio.wait_for(future, self.timeout)
            if "error" in response:
                raise Rejected(response["error"])
            return response["result"]
        except (TimeoutError, OSError, websockets.ConnectionClosed) as error:
            raise Uncertain("App Server acceptance is unknown") from error
        finally:
            self.waiters.pop(identifier, None)

    async def thread(self, thread: str, *, include_turns=True):
        return (
            await self.request("thread/read", {"threadId": thread, "includeTurns": include_turns})
        )["thread"]

    async def prepare(self, thread: str):
        state = await self.thread(thread, include_turns=False)
        if state["id"] != thread:
            raise ValueError("App Server returned another thread")
        if state["status"]["type"] == "notLoaded":
            cursor = None
            while True:
                params = {"archived": True, "modelProviders": [], "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                page = await self.request("thread/list", params)
                if any(item["id"] == thread for item in page["data"]):
                    raise ValueError("Thread is archived; human intervention is required")
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            # No thread/start and no overrides: resume exactly the authorized history.
            await self.request("thread/resume", {"threadId": thread})
            state = await self.thread(thread, include_turns=False)
        kind = state["status"]["type"]
        if kind == "active":
            turns = [t for t in state["turns"] if t["status"] == "inProgress"]
            if len(turns) != 1:
                # Some protocol histories paginate; ask for full live turn state.
                page = await self.request(
                    "thread/turns/list", {"threadId": thread, "limit": 100, "itemsView": "full"}
                )
                turns = [t for t in page["data"] if t["status"] == "inProgress"]
            if len(turns) != 1:
                raise ValueError("Cannot establish the current active turn")
            return "turn/steer", turns[0]["id"]
        if kind == "idle":
            return "turn/start", ""
        raise ValueError("Thread is unavailable")

    async def accepted(self, thread: str, event: str):
        # The client ID is a correlation key, not a server idempotency guarantee.
        cursor = None
        while True:
            params = {"threadId": thread, "limit": 100, "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            page = await self.request("thread/items/list", params)
            for entry in page["data"]:
                item = entry["item"]
                if item.get("type") != "userMessage":
                    continue
                if item.get("clientId") == event or any(
                    x.get("type") == "text"
                    and x.get("text", "").startswith(f"[design-agent-event:{event}]\n")
                    for x in item.get("content", [])
                ):
                    return {
                        "turn_id": entry["turnId"],
                        "input_id": item["id"],
                        "evidence": "thread/items/list",
                    }
            cursor = page.get("nextCursor")
            if not cursor:
                return None

    async def dispatch(self, thread: str, event, method: str, expected_turn: str):
        params = {
            "threadId": thread,
            "clientUserMessageId": event.event_id,
            "input": [{"type": "text", "text": event.external_input()}],
        }
        if method == "turn/steer":
            params["expectedTurnId"] = expected_turn
        result = await self.request(method, params)
        return {
            "turn_id": result.get("turnId") or result["turn"]["id"],
            "input_id": event.event_id,
            "evidence": method,
        }

    async def close(self):
        if self.socket:
            await self.socket.close()
        if self.reader:
            with suppress(asyncio.CancelledError):
                await self.reader
