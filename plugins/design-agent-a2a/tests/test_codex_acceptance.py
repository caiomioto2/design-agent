"""Real A2A HTTP -> signed callback HTTP -> Codex 0.167.1 -> local synthetic Responses.

No Orca, account connection, real provider, App Server replacement or approval grant.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_bridge import TOKEN, request, task_ready

from backend import Backend
from callbacks import deliver
from codex_adapter import AppServer, Rejected
from delivery_store import DeliveryStore
from gateway import Store, create_app, reconcile_work
from lifecycle import headers
from receiver import Worker, create_receiver
from server import Peer


@asynccontextmanager
async def http_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise AssertionError("HTTP server stopped before startup")
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


class SyntheticModel:
    def __init__(self):
        self.requests = []
        self.hold = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def respond(self, request):
        body = await request.json()
        self.requests.append(body)
        identifier = f"resp_{len(self.requests)}"
        message = {
            "id": f"msg_{len(self.requests)}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "Synthetic completion.", "annotations": []}
            ],
        }
        response = {
            "id": identifier,
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "synthetic-codex",
            "output": [message],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

        async def events():
            def event(kind, **data):
                return "event: " + kind + "\ndata: " + json.dumps({"type": kind, **data}) + "\n\n"

            yield event(
                "response.created", response={**response, "status": "in_progress", "output": []}
            )
            if self.hold:
                self.entered.set()
                await self.release.wait()
                self.hold = False
            yield event(
                "response.output_item.added",
                output_index=0,
                item={**message, "status": "in_progress", "content": []},
            )
            yield event(
                "response.content_part.added",
                output_index=0,
                item_id=message["id"],
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            )
            yield event(
                "response.output_text.delta",
                output_index=0,
                item_id=message["id"],
                content_index=0,
                delta="Synthetic completion.",
            )
            yield event(
                "response.output_text.done",
                output_index=0,
                item_id=message["id"],
                content_index=0,
                text="Synthetic completion.",
            )
            yield event("response.output_item.done", output_index=0, item=message)
            yield event("response.completed", response=response)

        return StreamingResponse(events(), media_type="text/event-stream")

    def app(self):
        return Starlette(routes=[Route("/responses", self.respond, methods=["POST"])])


@pytest.fixture
async def codex(tmp_path):
    binary = os.environ.get("DESIGN_AGENT_TEST_CODEX") or shutil.which("codex")
    if not binary:
        pytest.skip("Install Codex 0.167.1 to run the real-process acceptance seam")
    version = await asyncio.create_subprocess_exec(
        binary, "--version", stdout=asyncio.subprocess.PIPE
    )
    output, _ = await version.communicate()
    if output.decode().strip() != "codex-cli 0.167.1":
        if os.environ.get("DESIGN_AGENT_TEST_CODEX"):
            pytest.fail("DESIGN_AGENT_TEST_CODEX must point to Codex 0.167.1")
        pytest.skip("Use DESIGN_AGENT_TEST_CODEX with the pinned Codex 0.167.1 executable")
    home = tmp_path / "codex-home"
    home.mkdir()
    work = tmp_path / "codex-work"
    work.mkdir()
    synthetic = SyntheticModel()
    async with http_server(synthetic.app()) as model_url:
        (home / "config.toml").write_text(f'''
model = "synthetic-codex"
model_provider = "synthetic"
approval_policy = "on-request"
sandbox_mode = "read-only"
[model_providers.synthetic]
name = "Synthetic local fixture"
base_url = "{model_url}"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
[analytics]
enabled = false
[feedback]
enabled = false
''')
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint = f"ws://127.0.0.1:{port}"
        log = (tmp_path / "app-server.log").open("wb")
        process = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            "--listen",
            endpoint,
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CODEX_HOME": str(home)},
            cwd=work,
            stdout=log,
            stderr=log,
        )
        adapter = AppServer(endpoint, timeout=3)
        try:
            for _ in range(200):
                if process.returncode is not None:
                    raise AssertionError((tmp_path / "app-server.log").read_text())
                try:
                    await adapter.connect()
                    break
                except OSError:
                    await asyncio.sleep(0.025)
            assert adapter.socket
            thread = (await adapter.request("thread/start", {"cwd": str(work)}))["thread"]["id"]

            async def restart(*extra_args):
                nonlocal process
                await adapter.close()
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)
                process = await asyncio.create_subprocess_exec(
                    binary,
                    "app-server",
                    "--listen",
                    endpoint,
                    *extra_args,
                    env={
                        "PATH": os.environ["PATH"],
                        "HOME": str(tmp_path),
                        "CODEX_HOME": str(home),
                    },
                    cwd=work,
                    stdout=log,
                    stderr=log,
                )
                for _ in range(200):
                    try:
                        await adapter.connect()
                        return
                    except OSError:
                        await asyncio.sleep(0.025)
                raise AssertionError("App Server restart did not reconnect")

            yield adapter, thread, synthetic, endpoint, process, restart
        finally:
            synthetic.release.set()
            await adapter.close()
            if process.returncode is None:
                process.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 10)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            log.close()


async def until_idle(adapter, thread):
    for _ in range(300):
        state = await adapter.thread(thread, include_turns=False)
        if state["status"]["type"] == "idle":
            try:
                return await adapter.thread(thread)
            except Rejected as error:
                # 0.167.1 can acknowledge a new turn before its first persisted
                # user message materializes the paginated history backend.
                if error.error.get("code") != -32601:
                    raise AssertionError(error.error) from error
        await asyncio.sleep(0.02)
    raise AssertionError("Real App Server did not become idle with readable history")


@pytest.fixture
async def flow(tmp_path, codex, request, monkeypatch):
    from design_agent.config import Settings
    from design_agent.harness import inbox as inbox_module
    from design_agent.runtime.local import build_local_runtime
    from design_agent.surfaces.shared import create_shared_app
    from design_agent.testing.fakes import folder_binding
    from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call
    from langchain_core.tools import tool
    from langgraph.store.memory import InMemoryStore
    from langgraph.types import interrupt

    monkeypatch.setattr(inbox_module, "COALESCE_SECONDS", 0.02)
    flags = {
        "mode": "normal",
        "started": False,
        "approved": 0,
        "http_down": False,
        "drop_ack": False,
    }
    release_helper = threading.Event()
    flags["release_helper"] = release_helper

    @tool
    def fixture_review() -> str:
        """Pause the synthetic operation for human review."""
        interrupt({"action_requests": [{"name": "fixture_review", "args": {}}]})
        flags["approved"] += 1
        return "Reviewed"

    def respond(_messages):
        if flags["mode"] == "failure":
            raise RuntimeError("synthetic-private-error-marker")
        if flags["mode"] == "approval":
            return tool_call("fixture_review", {})
        if flags["mode"] == "helper" and not flags["started"]:
            flags["started"] = True
            return tool_call(
                "start_task",
                {
                    "description": "Synthetic background work",
                    "effort": "low",
                    "label": "fixture-helper",
                },
            )
        return reply("Design fixture result")

    def helper(_messages):
        assert release_helper.wait(20), "Background fixture was not released"
        return reply("Background fixture complete")

    automatic = request.node.name == "test_autonomous_delivery_workers"
    root = Path(__file__).resolve().parents[3]
    store = InMemoryStore()
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        root,
        model=RespondingChatModel(respond=respond),
        role_models={"low": RespondingChatModel(respond=helper)},
        mcp_tools=[fixture_review],
        binding=folder_binding(tmp_path / "sandbox", root),
        store=store,
    )
    async with http_server(
        create_shared_app(runtime, store, token=TOKEN, thread="design-test")
    ) as runtime_url:
        backend = Backend(
            tmp_path,
            httpx.AsyncClient(base_url=runtime_url, headers={"Authorization": "Bearer " + TOKEN}),
        )
        producer = Store(tmp_path / "producer.sqlite")
        producer_app = create_app(backend, producer, TOKEN, "http://127.0.0.1:1")
        if not automatic:
            producer_app.wrapped.router.lifespan_context = no_worker
        async with http_server(producer_app) as producer_url:
            async with httpx.AsyncClient(
                base_url=producer_url,
                headers={"Authorization": "Bearer " + TOKEN, "A2A-Version": "1.0"},
            ) as peer_client:
                receiver = DeliveryStore.open(tmp_path / "receiver.sqlite")
                worker = Worker(receiver, Peer(peer_client))
                # Explicit passes make fault windows deterministic; HTTP ingress and App
                # Server remain real. Production runs the same pass from its lifespan.
                app = create_receiver(receiver, worker)
                if not automatic:
                    app.router.lifespan_context = no_worker

                class FaultBoundary:
                    async def __call__(self, scope, receive, send):
                        if scope["type"] == "http" and flags["http_down"]:
                            from starlette.responses import JSONResponse

                            await JSONResponse(
                                {"error": "temporary fixture outage"}, status_code=503
                            )(scope, receive, send)
                            return

                        async def drop_reply(message):
                            if flags["drop_ack"] and message["type"] == "http.response.start":
                                message = {**message, "status": 503}
                            await send(message)

                        await app(scope, receive, drop_reply)

                async with http_server(FaultBoundary()) as receiver_url:
                    async with httpx.AsyncClient() as network:
                        receiver.register("test-consumer", codex[3], TOKEN)
                        producer.delivery.register("test-consumer", receiver_url, TOKEN)
                        yield (
                            producer,
                            receiver,
                            worker,
                            peer_client,
                            network,
                            runtime,
                            backend,
                            flags,
                        )
                await worker.close()
                receiver.db.close()
        if not automatic:
            producer.db.close()
            await backend.client.aclose()
    release_helper.set()
    await runtime.services.aclose()
    if runtime.binding.runner:
        runtime.binding.runner.stop_all()


@asynccontextmanager
async def no_worker(app):
    yield


async def submit(flow, codex, identifier):
    producer, receiver, _, peer, *_ = flow
    for state in (producer.delivery, receiver):
        state.bind(identifier, "test-consumer", codex[1])
    result = (await peer.post("/rpc", json=request(identifier=identifier, context=""))).json()
    assert "result" in result, result
    task_id = result["result"]["task"]["id"]
    task = await task_ready(peer, task_id)
    event = producer.delivery.latest(identifier)
    assert event and event.task_id == task_id
    return task, event


async def test_idle_callback_and_duplicate_delivery(flow, codex):
    producer, receiver, worker, peer, network, *_ = flow
    task, event = await submit(flow, codex, "idle-completion")
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert event.scope == "work"
    await deliver(producer.delivery, network)
    assert receiver.db.execute("SELECT status FROM inbox").fetchone()[0] == "pending"
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("accepted",), (
        receiver.receipts()
    )
    await until_idle(codex[0], codex[1])
    receipt = json.loads(receiver.db.execute("SELECT receipt FROM inbox").fetchone()[0])
    assert receipt["evidence"] == "turn/start"
    assert await codex[0].accepted(codex[1], event.event_id)
    before = len((await codex[0].thread(codex[1]))["turns"])
    target = producer.delivery.consumer("test-consumer")[0]
    for _ in range(3):
        response = await network.post(
            target + "/events",
            content=event.body(),
            headers=headers(TOKEN, "test-consumer", event.body()),
        )
        assert response.status_code == 200
        await worker.run_once()
    assert len((await codex[0].thread(codex[1]))["turns"]) == before
    duplicate = (
        await peer.post("/rpc", json=request(identifier="idle-completion", context=""))
    ).json()
    assert duplicate["result"]["task"]["id"] == task["id"]
    assert any("design-agent-event:" in json.dumps(r) for r in codex[2].requests)


async def test_active_callback_steers_current_turn(flow, codex):
    producer, receiver, worker, _, network, *_ = flow
    adapter, thread, model, *_ = codex
    model.hold = True
    active = await adapter.request(
        "turn/start",
        {"threadId": thread, "input": [{"type": "text", "text": "Active fixture turn"}]},
    )
    await asyncio.wait_for(model.entered.wait(), 10)
    _, event = await submit(flow, codex, "active-completion")
    await deliver(producer.delivery, network)
    await worker.run_once()
    row = receiver.db.execute("SELECT status,operation,expected_turn,receipt FROM inbox").fetchone()
    assert row[:3] == ("accepted", "turn/steer", active["turn"]["id"]), row
    assert json.loads(row[3])["turn_id"] == active["turn"]["id"]
    model.release.set()
    state = await until_idle(adapter, thread)
    assert len(state["turns"]) == 1
    assert await adapter.accepted(thread, event.event_id)


async def test_autonomous_delivery_workers(flow, codex):
    _, receiver, *_ = flow
    _, event = await submit(flow, codex, "automatic-completion")
    for _ in range(300):
        row = receiver.db.execute(
            "SELECT status FROM inbox WHERE event=?", (event.event_id,)
        ).fetchone()
        if row and row[0] == "accepted":
            break
        await asyncio.sleep(0.02)
    assert row == ("accepted",)
    await until_idle(codex[0], codex[1])
    assert await codex[0].accepted(codex[1], event.event_id)


async def test_active_turn_ending_race_reconciles_real_rejection(flow, codex):
    producer, receiver, worker, _, network, *_ = flow
    adapter, thread, model, *_ = codex
    model.hold = True
    active = await adapter.request(
        "turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Race fixture"}]}
    )
    await asyncio.wait_for(model.entered.wait(), 10)
    _, event = await submit(flow, codex, "turn-ending-race")

    class EndingRace(AppServer):
        async def dispatch(self, *args):
            if args[2] == "turn/steer":
                model.release.set()
                await until_idle(adapter, thread)
            try:
                return await super().dispatch(*args)
            except Rejected as error:
                # Exact wire rejection is from the real process, not a simulated success.
                assert error.turn_race(), error.error
                raise

    worker.adapter_factory = EndingRace
    await deliver(producer.delivery, network)
    await worker.run_once()
    row = receiver.db.execute("SELECT status,operation FROM inbox").fetchone()
    assert row == ("accepted", "turn/start"), receiver.receipts()
    state = await until_idle(adapter, thread)
    assert len(state["turns"]) == 2
    assert state["turns"][0]["id"] == active["turn"]["id"]
    assert await adapter.accepted(thread, event.event_id)


@pytest.mark.parametrize("crash", [False, True])
async def test_lost_app_server_reply_reconciles_after_receiver_restart(
    flow, codex, tmp_path, crash
):
    producer, receiver, worker, peer, network, *_ = flow
    _, event = await submit(flow, codex, "lost-acceptance-response")

    class CrashSignal(BaseException):
        pass

    class LostReply(AppServer):
        async def dispatch(self, *args):
            from codex_adapter import Uncertain

            await super().dispatch(*args)
            await self.close()
            if crash:
                raise CrashSignal()
            raise Uncertain("Controlled disconnect after the real server accepted input")

    worker.adapter_factory = LostReply
    await deliver(producer.delivery, network)
    if crash:
        with pytest.raises(CrashSignal):
            await worker.run_once()
    else:
        await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == (
        "dispatching" if crash else "uncertain",
    )
    await until_idle(codex[0], codex[1])
    await worker.close()
    if crash:
        await codex[5]()
    restarted = DeliveryStore.open(tmp_path / "receiver.sqlite", recover=True)
    recovery = Worker(restarted, Peer(peer))
    try:
        restarted.db.execute("UPDATE inbox SET next_at=0")
        restarted.db.commit()
        await recovery.run_once()
        row = restarted.db.execute("SELECT status,receipt FROM inbox").fetchone()
        assert row[0] == "accepted", restarted.receipts()
        assert json.loads(row[1])["evidence"] == "thread/items/list"
        await recovery.run_once()
        state = await codex[0].thread(codex[1])
        assert len(state["turns"]) == 1
        assert await codex[0].accepted(codex[1], event.event_id)
    finally:
        await recovery.close()
        restarted.db.close()


async def test_unproven_acceptance_is_never_replayed(flow, codex, tmp_path):
    producer, receiver, worker, peer, network, *_ = flow
    from codex_adapter import Uncertain

    _, event = await submit(flow, codex, "unproven-acceptance")

    class Disconnected(AppServer):
        async def dispatch(self, *args):
            # Cut the actual transport at the intent boundary. The worker cannot
            # distinguish this from a lost acknowledgement and must not regenerate.
            await self.close()
            raise Uncertain("Transport cut at dispatch")

    worker.adapter_factory = Disconnected
    await deliver(producer.delivery, network)
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("uncertain",)
    await worker.close()
    restarted = DeliveryStore.open(tmp_path / "receiver.sqlite", recover=True)
    recovery = Worker(restarted, Peer(peer))
    try:
        for _ in range(3):
            restarted.db.execute("UPDATE inbox SET next_at=0")
            restarted.db.commit()
            await recovery.run_once()
        assert restarted.db.execute("SELECT status FROM inbox").fetchone() == ("uncertain",)
        assert codex[2].requests == []
    finally:
        await recovery.close()
        restarted.db.close()


async def test_consumer_outage_and_producer_restart_keep_delivery(flow, codex, tmp_path):
    producer, receiver, worker, _, network, *rest = flow
    flags = rest[-1]
    flags["http_down"] = True
    task, event = await submit(flow, codex, "callback-outage")
    await deliver(producer.delivery, network)
    assert producer.db.execute("SELECT status,attempts FROM outbox").fetchone() == ("pending", 1)
    assert receiver.db.execute("SELECT COUNT(*) FROM inbox").fetchone() == (0,)
    restarted = Store(tmp_path / "producer.sqlite")
    try:
        assert (await restarted.get(task["id"], None)).id == task["id"]
        flags["http_down"] = False
        restarted.db.execute("UPDATE outbox SET next_at=0")
        restarted.db.commit()
        await deliver(restarted.delivery, network)
        await worker.run_once()
        assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("accepted",)
        await until_idle(codex[0], codex[1])
        assert await codex[0].accepted(codex[1], event.event_id)
    finally:
        restarted.db.close()


async def test_running_helper_and_out_of_order_callbacks(flow, codex):
    producer, receiver, worker, _, network, runtime, backend, flags = flow
    flags["mode"] = "helper"
    _, turn_event = await submit(flow, codex, "background-completion")
    assert turn_event.scope == "turn"
    assert runtime.busy()
    assert producer.db.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
    await reconcile_work(backend, producer)
    assert producer.delivery.latest("background-completion") == turn_event
    flags["release_helper"].set()
    assert await runtime.settle(timeout_seconds=10, poll_seconds=0.02)
    await reconcile_work(backend, producer)
    work_event = producer.delivery.latest("background-completion")
    assert work_event.scope == "work" and work_event.revision == 2
    await deliver(producer.delivery, network)
    await worker.run_once()
    assert receiver.db.execute(
        "SELECT status FROM inbox WHERE event=?", (turn_event.event_id,)
    ).fetchone() == ("superseded",)
    assert receiver.db.execute(
        "SELECT status FROM inbox WHERE event=?", (work_event.event_id,)
    ).fetchone() == ("accepted",)
    await until_idle(codex[0], codex[1])
    assert len((await codex[0].thread(codex[1]))["turns"]) == 1
    assert await codex[0].accepted(codex[1], work_event.event_id)
    assert not await codex[0].accepted(codex[1], turn_event.event_id)


@pytest.mark.parametrize(
    ("mode", "outcome"), [("failure", "failed"), ("approval", "input-required")]
)
async def test_failed_and_approval_required_are_distinct_outcomes(flow, codex, mode, outcome):
    producer, receiver, worker, _, network, runtime, _, flags = flow
    flags["mode"] = mode
    task, event = await submit(flow, codex, "outcome-" + mode)
    assert event.state == outcome
    assert "synthetic-private-error-marker" not in event.text
    await deliver(producer.delivery, network)
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("accepted",)
    await until_idle(codex[0], codex[1])
    accepted = await codex[0].accepted(codex[1], event.event_id)
    assert accepted
    assert outcome in json.dumps(codex[2].requests)
    if mode == "approval":
        assert await runtime.pending_approvals(task["contextId"])
        assert flags["approved"] == 0


async def test_invalid_callbacks_revocation_and_destination_isolation(flow, codex):
    producer, receiver, worker, _, network, *_ = flow
    _, event = await submit(flow, codex, "authorized-only")
    destination = producer.delivery.consumer("test-consumer")[0]
    bad = await network.post(
        destination + "/events",
        content=event.body(),
        headers=headers("wrong-key", "test-consumer", event.body()),
    )
    assert bad.status_code == 401
    other = event.model_copy(update={"submission_id": "unregistered"})
    bad = await network.post(
        destination + "/events",
        content=other.body(),
        headers=headers(TOKEN, "test-consumer", other.body()),
    )
    assert bad.status_code == 403
    receiver.register("other-consumer", codex[3], "y" * 40)
    bad = await network.post(
        destination + "/events",
        content=event.body(),
        headers=headers("y" * 40, "other-consumer", event.body()),
    )
    assert bad.status_code == 403
    assert receiver.db.execute("SELECT COUNT(*) FROM inbox").fetchone() == (0,)
    await deliver(producer.delivery, network)
    receiver.revoke(event.submission_id)
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("revoked",)
    assert codex[2].requests == []


async def test_real_app_server_restart_resumes_same_thread_and_policy(flow, codex):
    producer, receiver, worker, _, network, *_ = flow
    adapter, thread, model, _, _, restart = codex
    # Persist a non-default selection on the existing thread, then stop its owner.
    await adapter.request(
        "turn/start",
        {
            "threadId": thread,
            "model": "synthetic-preserved-model",
            "approvalPolicy": "on-request",
            "sandboxPolicy": {"type": "readOnly"},
            "input": [{"type": "text", "text": "Initial thread context"}],
        },
    )
    await until_idle(adapter, thread)
    original = await adapter.request("thread/resume", {"threadId": thread})
    _, event = await submit(flow, codex, "app-server-restart")
    await deliver(producer.delivery, network)
    await restart()
    assert (await adapter.thread(thread, include_turns=False))["status"]["type"] == "notLoaded"
    calls = []

    class Recording(AppServer):
        async def request(self, method, params):
            calls.append((method, params))
            return await super().request(method, params)

    worker.adapter_factory = Recording
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("accepted",), (
        receiver.receipts()
    )
    state = await until_idle(adapter, thread)
    assert state["id"] == thread and len(state["turns"]) == 2
    assert await adapter.accepted(thread, event.event_id)
    resumed = await adapter.request("thread/resume", {"threadId": thread})
    for field in ("model", "modelProvider", "approvalPolicy", "sandbox", "cwd"):
        assert resumed[field] == original[field], (field, original[field], resumed[field])
    assert resumed["model"] == "synthetic-preserved-model"
    assert ("thread/resume", {"threadId": thread}) in calls
    assert not any(method == "thread/start" for method, _ in calls)
    start = next(params for method, params in calls if method == "turn/start")
    assert set(start) == {"threadId", "clientUserMessageId", "input"}
    assert all(body["model"] == "synthetic-preserved-model" for body in model.requests)


async def test_archived_thread_retains_pending_without_new_thread(flow, codex):
    producer, receiver, worker, _, network, *_ = flow
    adapter, thread, model, *_ = codex
    await adapter.request(
        "turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Archive fixture"}]}
    )
    await until_idle(adapter, thread)
    await adapter.request("thread/archive", {"threadId": thread})
    count = len(model.requests)
    _, event = await submit(flow, codex, "archived-destination")
    await deliver(producer.delivery, network)
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("pending",)
    assert len(model.requests) == count
    assert producer.delivery.latest(event.submission_id) == event


async def test_concurrent_tasks_keep_distinct_destinations(flow, codex):
    producer, receiver, worker, peer, network, *_ = flow
    adapter, thread, model, *_ = codex
    second = (await adapter.request("thread/start", {}))["thread"]["id"]
    for identifier, destination in [("concurrent-first", thread), ("concurrent-second", second)]:
        for state in (producer.delivery, receiver):
            state.bind(identifier, "test-consumer", destination)
    results = await asyncio.gather(
        *(
            peer.post("/rpc", json=request(identifier=identifier, context=""))
            for identifier in ["concurrent-first", "concurrent-second"]
        )
    )
    for result in results:
        await task_ready(peer, result.json()["result"]["task"]["id"])
    await deliver(producer.delivery, network)
    await asyncio.gather(worker.run_once(), worker.run_once())
    assert receiver.db.execute("SELECT COUNT(*) FROM inbox WHERE status='accepted'").fetchone() == (
        2,
    )
    await until_idle(adapter, thread)
    await until_idle(adapter, second)
    first_event = producer.delivery.latest("concurrent-first")
    second_event = producer.delivery.latest("concurrent-second")
    assert await adapter.accepted(thread, first_event.event_id)
    assert await adapter.accepted(second, second_event.event_id)
    assert not await adapter.accepted(thread, second_event.event_id)
    assert not await adapter.accepted(second, first_event.event_id)
    assert len(model.requests) == 2


async def test_callback_ack_loss_retries_existing_receipt(flow, codex):
    producer, receiver, worker, _, network, *rest = flow
    flags = rest[-1]
    _, event = await submit(flow, codex, "lost-http-ack")
    flags["drop_ack"] = True
    await deliver(producer.delivery, network)
    assert producer.db.execute("SELECT status FROM outbox").fetchone() == ("pending",)
    assert receiver.db.execute("SELECT COUNT(*) FROM inbox").fetchone() == (1,)
    await worker.run_once()
    await until_idle(codex[0], codex[1])
    flags["drop_ack"] = False
    producer.db.execute("UPDATE outbox SET next_at=0")
    producer.db.commit()
    await deliver(producer.delivery, network)
    await worker.run_once()
    assert producer.db.execute("SELECT status FROM outbox").fetchone() == ("delivered",)
    assert len((await codex[0].thread(codex[1]))["turns"]) == 1
    assert await codex[0].accepted(codex[1], event.event_id)


async def test_producer_binding_revoked_after_callback_blocks_continuation(flow, codex):
    producer, receiver, worker, _, network, *_ = flow
    _, event = await submit(flow, codex, "producer-revoked")
    await deliver(producer.delivery, network)
    producer.delivery.revoke(event.submission_id)
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("revoked",)
    assert codex[2].requests == []


async def test_rejected_websocket_auth_keeps_worker_recoverable(flow, codex, tmp_path):
    producer, receiver, worker, _, network, *_ = flow
    adapter, thread, model, _, _, restart = codex
    await adapter.request(
        "turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Auth fixture"}]}
    )
    await until_idle(adapter, thread)
    _, event = await submit(flow, codex, "auth-recovery")
    await deliver(producer.delivery, network)
    key_file = tmp_path / "app-server-token"
    key_file.write_text("test-app-server-capability-" + "z" * 32)
    key_file.chmod(0o600)
    adapter.token_file = key_file
    await restart("--ws-auth", "capability-token", "--ws-token-file", str(key_file))
    before = len(model.requests)
    for _ in range(2):
        receiver.db.execute("UPDATE inbox SET next_at=0")
        receiver.db.commit()
        await worker.run_once()
    assert receiver.db.execute("SELECT status,attempts FROM inbox").fetchone() == ("pending", 2)
    assert len(model.requests) == before
    worker.adapters["test-consumer"].token_file = key_file
    receiver.db.execute("UPDATE inbox SET next_at=0")
    receiver.db.commit()
    await worker.run_once()
    assert receiver.db.execute("SELECT status FROM inbox").fetchone() == ("accepted",), (
        receiver.receipts()
    )
    await until_idle(adapter, thread)
    assert await adapter.accepted(thread, event.event_id)


async def test_render_watch_failure_cannot_become_success(flow, codex):
    import time

    from design_agent.harness.watch import WatchRecord
    from design_agent.middleware.watch_tools import WATCHES

    producer, receiver, worker, _, network, runtime, backend, flags = flow
    flags["mode"] = "helper"
    _, event = await submit(flow, codex, "render-failed")
    WATCHES._records["failed-render-fixture"] = WatchRecord(
        id="failed-render-fixture",
        thread_id=event.context_id,
        label="Render fixture",
        started_at=time.time(),
        poll_seconds=1,
        stall_seconds=30,
        status="error",
    )
    try:
        flags["release_helper"].set()
        assert await runtime.settle(timeout_seconds=10, poll_seconds=0.02)
        await reconcile_work(backend, producer)
        failed = producer.delivery.latest(event.submission_id)
        assert failed.state == "failed"
        await deliver(producer.delivery, network)
        await worker.run_once()
        await until_idle(codex[0], codex[1])
        assert await codex[0].accepted(codex[1], failed.event_id)
        assert receiver.db.execute(
            "SELECT status FROM inbox WHERE event=?", (event.event_id,)
        ).fetchone() == ("superseded",)
    finally:
        WATCHES._records.pop("failed-render-fixture", None)


async def test_colliding_legacy_deliverable_folders_are_not_exposed(flow):
    _, _, _, _, _, runtime, backend, _ = flow
    await runtime.ask("First owner", thread_id="a:b")
    await runtime.ask("Second owner", thread_id="a-b")
    directory = backend.root / "workspace/out/a-b"
    directory.mkdir(parents=True)
    (directory / "result.txt").write_text("Ambiguous owner")
    for context in ("a:b", "a-b"):
        assert await backend.artifacts(context) == []
        with pytest.raises(ValueError, match="ambiguous"):
            await backend.read_artifact(context, "deliverables", "result.txt")
