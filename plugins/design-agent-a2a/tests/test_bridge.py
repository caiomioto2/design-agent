"""A2A wire, MCP client, durable deduplication and file boundaries without live model calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import Backend
from gateway import Store, create_app
from server import Peer, create_mcp

TOKEN = "test-local-a2a-token-" + "x" * 32
THREAD = "a2a-test-conversation"


def wire_state(pending=False):
    interrupts = (
        [
            {
                "$interrupt": {
                    "id": "review-test",
                    "value": {"action_requests": [{"name": "publish", "args": {}}]},
                }
            }
        ]
        if pending
        else []
    )
    return {
        "$snapshot": {
            "values": {
                "messages": [{"$message": {"type": "ai", "data": {"content": "Test result"}}}]
            },
            "tasks": {"$tuple": [{"$task": {"interrupts": {"$tuple": interrupts}}}]},
        }
    }


class Runtime:
    def __init__(self, root: Path):
        self.root, self.turns, self.pending, self.fail = root, 0, False, False
        self.seen = []
        self.threads = {THREAD}

    def route(self, request):
        if request.url.path == "/conversations":
            return httpx.Response(
                200, json=[{"id": t, "title": "Test conversation"} for t in self.threads]
            )
        if request.url.path == "/projects":
            return httpx.Response(200, json=[])
        if request.url.path == "/state":
            return httpx.Response(200, json=wire_state(self.pending))
        if request.url.path == "/stream":
            self.turns += 1
            data = json.loads(request.content)
            self.seen.append(data)
            self.threads.add(data["config"]["configurable"]["thread_id"])
            return httpx.Response(
                200,
                text=json.dumps({"error": "private secret error"} if self.fail else {"event": {}})
                + "\n",
            )
        return httpx.Response(404)

    def backend(self):
        return Backend(
            self.root,
            httpx.AsyncClient(base_url="http://runtime", transport=httpx.MockTransport(self.route)),
        )


def request(identifier="test-submission", text="Design a test", context=THREAD):
    message = {"messageId": identifier, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context:
        message["contextId"] = context
    return {
        "jsonrpc": "2.0",
        "id": "test-rpc",
        "method": "SendMessage",
        "params": {"message": message, "configuration": {"returnImmediately": True}},
    }


async def task_ready(client, task_id):
    for _ in range(100):
        response = await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": "get", "method": "GetTask", "params": {"id": task_id}},
        )
        data = response.json()
        if "error" in data:
            raise AssertionError(data)
        if data["result"]["status"]["state"] not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
            return data["result"]
        await asyncio.sleep(0.01)
    raise AssertionError("Task did not finish")


def client_for(backend, store):
    app = create_app(backend, store, TOKEN, "http://127.0.0.1:8134")
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:8134",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer " + TOKEN, "A2A-Version": "1.0"},
    )


async def test_a2a_protocol_durable_duplicate_and_mcp(tmp_path):
    runtime = Runtime(tmp_path)
    backend = runtime.backend()
    store = Store(tmp_path / "tasks.sqlite")
    async with client_for(backend, store) as client:
        assert (
            await client.get("/.well-known/agent-card.json", headers={"Authorization": "bad"})
        ).status_code == 401
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        first = (await client.post("/rpc", json=request())).json()
        assert "result" in first, first
        task_id = first["result"]["task"]["id"]
        task = await task_ready(client, task_id)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        duplicate = (await client.post("/rpc", json=request())).json()
        assert duplicate["result"]["task"]["id"] == task_id
        assert runtime.turns == 1
        assert "slack_event" not in runtime.seen[0]["config"]["configurable"]
        conflict = (await client.post("/rpc", json=request(text="Different task"))).json()
        assert "error" in conflict
        async with Client(create_mcp(Peer(client))) as mcp:
            catalog = await mcp.list_tools()
            assert len(catalog) == 8
            read = await mcp.call_tool("get_context", {"context_id": THREAD})
            assert not read.is_error
    store.db.close()
    # A new gateway process returns the original task when the same messageId is retried.
    second = Store(tmp_path / "tasks.sqlite")
    async with client_for(backend, second) as client:
        duplicate = (await client.post("/rpc", json=request())).json()
        assert duplicate["result"]["task"]["id"] == task_id
    assert runtime.turns == 1
    second.db.close()
    await backend.client.aclose()


async def test_pending_approval_blocks_turn_and_failure_is_not_replayed(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.pending = True
    backend = runtime.backend()
    store = Store(tmp_path / "tasks.sqlite")
    async with client_for(backend, store) as client:
        result = (await client.post("/rpc", json=request())).json()
        assert "result" in result, result
        task = await task_ready(client, result["result"]["task"]["id"])
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert runtime.turns == 0
        runtime.pending = False
        runtime.fail = True
        failed = (await client.post("/rpc", json=request(identifier="uncertain"))).json()
        task = await task_ready(client, failed["result"]["task"]["id"])
        assert task["status"]["state"] == "TASK_STATE_FAILED"
        assert "private secret" not in json.dumps(task)
        await client.post("/rpc", json=request(identifier="uncertain"))
        assert runtime.turns == 1
    store.db.close()
    await backend.client.aclose()


async def test_new_conversation_is_isolated_and_unknown_context_refused(tmp_path):
    runtime = Runtime(tmp_path)
    backend = runtime.backend()
    store = Store(tmp_path / "tasks.sqlite")
    async with client_for(backend, store) as client:
        bad = (await client.post("/rpc", json=request(context="unknown"))).json()
        assert "error" in bad
        fresh = (await client.post("/rpc", json=request(identifier="new", context=""))).json()
        assert "result" in fresh, fresh
        task = await task_ready(client, fresh["result"]["task"]["id"])
        assert task["contextId"].startswith("a2a-") and task["contextId"] != THREAD
        assert runtime.turns == 1
    store.db.close()
    await backend.client.aclose()


async def test_interrupted_task_does_not_restart(tmp_path):
    path = tmp_path / "tasks.sqlite"
    store = Store(path)
    await store.save(
        Task(
            id="test-task", context_id=THREAD, status=TaskStatus(state=TaskState.TASK_STATE_WORKING)
        ),
        ServerCallContext(),
    )
    store.db.close()
    store = Store(path)
    task = await store.get("test-task", ServerCallContext())
    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert task.metadata["outcome_unknown"]
    store.db.close()


async def test_artifact_boundaries_and_text_limit(tmp_path):
    runtime = Runtime(tmp_path)
    backend = runtime.backend()
    out = tmp_path / "workspace/out" / THREAD
    out.mkdir(parents=True)
    (out / "result.md").write_text("x" * 70_000)
    (out / ".env").write_text("private")
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (out / "outside.txt").symlink_to(outside)
    project = tmp_path / "workspace/projects/example"
    project.mkdir(parents=True)
    (project / "BRIEF.md").write_text("Approved brief")
    state = tmp_path / "workspace/state"
    state.mkdir()
    volume = "design-agent-ws-" + hashlib.sha256(THREAD.encode()).hexdigest()[:16]
    (state / "project-mounts.json").write_text(
        json.dumps({str(project): f"/var/lib/docker/volumes/{volume}/_data/projects/example"})
    )
    artifacts = await backend.artifacts(THREAD)
    assert {a["path"] for a in artifacts} == {"result.md", "BRIEF.md"}
    assert (await backend.read_artifact(THREAD, "deliverables", "result.md"))["truncated"]
    for path in ["../private.txt", ".env", "outside.txt", "/etc/passwd"]:
        with pytest.raises(ValueError):
            await backend.read_artifact(THREAD, "deliverables", path)
    with pytest.raises(ValueError):
        await backend.read_artifact("another-thread", "example", "BRIEF.md")
    assert (await backend.read_artifact(THREAD, "example", "BRIEF.md"))["text"] == "Approved brief"
    await backend.client.aclose()
