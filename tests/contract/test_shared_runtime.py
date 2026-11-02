"""Slack and the official terminal share one graph, sandbox and serialized turn queue."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Interrupt, StateSnapshot

from design_agent.config import Settings
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.shared import SharedGraph, create_shared_app, pack, unpack
from design_agent.surfaces.slack.bolt_app import SlackAdapter
from design_agent.testing.fake_slack import BOT, CHANNEL, PERSON, TEAM, FakeSlack
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import RespondingChatModel, reply

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-shared-runtime-token-" + "x" * 32


def test_wire_preserves_stream_chunks_and_approval_interrupts() -> None:
    values = (
        ((), "messages", (AIMessageChunk(content="token"), {"tag": "value"})),
        Interrupt({"action_requests": [{"name": "write_file"}]}, id="approval"),
        Command(resume={"decision": "approve"}),
    )
    decoded = unpack(pack(values))
    assert isinstance(decoded, tuple)
    assert isinstance(decoded[0][2][0], AIMessageChunk)
    assert decoded[0][2][0].content == "token"
    assert decoded[1].id == "approval"
    assert isinstance(decoded[2], Command)
    assert decoded[2].resume == {"decision": "approve"}


async def test_slack_and_terminal_read_one_history_and_queue_turns(tmp_path: Path) -> None:
    entered = []

    def model(messages):
        text = [message.text for message in messages if isinstance(message, HumanMessage)]
        entered.append(text)
        return reply("Shared reply " + str(len(entered)))

    store = InMemoryStore()
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=RespondingChatModel(respond=model),
        binding=folder_binding(tmp_path / "sandbox", ROOT),
        store=store,
    )
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=runtime)
    app = create_shared_app(runtime, store, token=TOKEN, thread="common-thread")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + TOKEN},
        timeout=30,
    ) as client:
        assert (await client.get("/info", headers={"Authorization": "wrong"})).status_code == 401
        graph = SharedGraph(client)
        events = [
            event
            async for event in graph.astream(
                {"messages": [HumanMessage("terminal context marker")]},
                config={"configurable": {"thread_id": "common-thread"}},
                stream_mode=["messages", "updates", "custom"],
                subgraphs=True,
            )
        ]
        assert events
        assert isinstance(events[0], tuple)
        event = {
            "type": "app_mention",
            "user": PERSON,
            "team": TEAM,
            "channel": CHANNEL,
            "ts": "1712345678.000100",
            "text": f"<@{BOT}> Slack followup",
        }
        adapter.conversations = runtime.conversations
        await runtime.conversations.touch("common-thread", owner=PERSON)
        native = f"slack-{TEAM}-{CHANNEL}-1712345678.000100"
        await runtime.conversations.link(native, "common-thread", owner=PERSON)
        await adapter.handle(event)
        unrelated = await graph.aget_state({"configurable": {"thread_id": "unrelated"}})
        assert not unrelated.values
        state = await graph.aget_state({"configurable": {"thread_id": "common-thread"}})
        assert isinstance(state, StateSnapshot)
        assert any(
            message.text == "terminal context marker" for message in state.values["messages"]
        )
        assert any(message.text == "Slack followup" for message in state.values["messages"])
        assert any("terminal context marker" in text for text in entered[-1])
        posts = [fields for method, fields in fake.calls if method == "chat.postMessage"]
        assert posts[-1]["channel"] == CHANNEL and posts[-1]["thread_ts"] == "1712345678.000100"
        assert posts[-1]["text"] == "Shared reply 2"
        # A terminal turn waits for the same lock as Slack and the background helpers.
        async with runtime._lock("common-thread"):

            async def terminal_turn():
                return [
                    event
                    async for event in graph.astream(
                        {"messages": [HumanMessage("queued")]}, config={}, stream_mode="updates"
                    )
                ]

            task = asyncio.create_task(terminal_turn())
            await asyncio.sleep(0.05)
            assert not task.done()
        assert await task
        await graph.aput_store_item(("approval",), "mode", {"auto": False})
        assert (await store.aget(("approval",), "mode")).value == {"auto": False}
    await runtime.services.aclose()


async def test_official_terminal_streams_from_the_shared_runtime(tmp_path: Path) -> None:
    import pytest

    pytest.importorskip("deepagents_code")
    from types import SimpleNamespace

    from deepagents_code.tui.widgets.messages import AssistantMessage

    from design_agent.surfaces.terminal import make_app, prepare

    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path / "workspace",
        shared_thread_id="common-thread",
        shared_runtime_url="http://127.0.0.1:8133",
    )
    _state, session = prepare(tmp_path, settings)
    store = InMemoryStore()
    runtime = build_local_runtime(
        settings,
        ROOT,
        model=RespondingChatModel(respond=lambda _messages: reply("Shared terminal ready.")),
        binding=folder_binding(tmp_path / "sandbox", ROOT),
        store=store,
    )
    api = create_shared_app(runtime, store, token=TOKEN, thread="common-thread")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + TOKEN},
        timeout=20,
    ) as client:
        graph = SharedGraph(client)
        lock = asyncio.Lock()
        proxy = SimpleNamespace(
            graph=graph,
            binding=runtime.binding,
            out_dir=runtime.out_dir,
            _lock=lambda _thread: lock,
        )
        terminal = make_app(
            proxy,
            SimpleNamespace(aput=graph.aput_store_item),
            "common-thread",
            ROOT,
            settings,
            session,
            prompt="Hello from the terminal.",
        )
        async with terminal.run_test(size=(120, 40)) as pilot:
            for _ in range(100):
                await pilot.pause(0.05)
                if any(
                    "Shared terminal ready." in message._content
                    for message in terminal.query(AssistantMessage)
                ):
                    break
            else:
                pytest.fail("No shared graph response in the official terminal")
            await terminal._handle_command("/threads")
            await terminal._handle_command("/new")
            await pilot.pause(0.15)
            assert terminal._lc_thread_id != "common-thread"
            created = terminal._lc_thread_id
            await terminal._handle_command("/resume common-thread")
            await pilot.pause(0.15)
            assert terminal._lc_thread_id == "common-thread"
            assert created != terminal._lc_thread_id
    await runtime.services.aclose()
