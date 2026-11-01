"""Conversation bindings stay isolated and survive a runtime restart."""

import asyncio
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage
from langgraph.store.memory import InMemoryStore

from design_agent.config import Settings
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.conversations import Conversations
from design_agent.surfaces.shared import SharedGraph, create_shared_app
from design_agent.surfaces.slack.bolt_app import SlackAdapter, thread_id_for
from design_agent.testing.fake_slack import BOT, CHANNEL, PERSON, TEAM, FakeSlack
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import RespondingChatModel, reply

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-conversation-service-" + ("x" * 32)


async def test_threads_isolate_history_and_followups_need_mention_only_once(tmp_path: Path) -> None:
    fake = FakeSlack()
    store = InMemoryStore()
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=RespondingChatModel(respond=lambda _messages: reply("ok")),
        binding=folder_binding(tmp_path / "sandbox", ROOT),
        store=store,
    )
    directory = Conversations(runtime.graph.checkpointer, store, ROOT, operator=PERSON)
    directory.runtime = runtime
    runtime.conversations = directory
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=runtime, conversations=directory)
    first = {
        "type": "app_mention",
        "user": PERSON,
        "team": TEAM,
        "channel": CHANNEL,
        "ts": "1712345678.000100",
        "text": f"<@{BOT}> Create alpha",
    }
    follow = {
        **first,
        "type": "message",
        "thread_ts": first["ts"],
        "ts": "1712345679.000100",
        "text": "alpha continuation",
    }
    untouched = {**follow, "thread_ts": "1712345699.000100", "text": "unrelated humans talking"}
    assert not adapter.accepts(follow, mention=False)
    adapter.dispatch(first, mention=True)
    while adapter._tasks:
        await asyncio.sleep(0.01)
    assert adapter.accepts(follow, mention=False)
    assert not adapter.accepts(untouched, mention=False)
    await adapter.handle(follow)
    second = {**first, "ts": "1712345680.000100", "text": f"<@{BOT}> Create beta"}
    await adapter.handle(second)
    alpha = thread_id_for(TEAM, CHANNEL, first["ts"])
    beta = thread_id_for(TEAM, CHANNEL, second["ts"])
    a = await runtime.graph.aget_state(runtime._config(alpha))
    b = await runtime.graph.aget_state(runtime._config(beta))
    assert any(m.text == "alpha continuation" for m in a.values["messages"])
    assert not any("alpha" in m.text for m in b.values["messages"] if isinstance(m, HumanMessage))
    # A deliberate resume changes this Slack binding, preserving both saved histories.
    await adapter.handle({**follow, "text": "resume " + beta})
    assert await directory.linked(alpha) == beta
    await adapter.handle({**follow, "text": "beta resumed here"})
    b = await runtime.graph.aget_state(runtime._config(beta))
    a = await runtime.graph.aget_state(runtime._config(alpha))
    assert any(m.text == "beta resumed here" for m in b.values["messages"])
    assert not any(m.text == "beta resumed here" for m in a.values["messages"])
    sent_before = len([1 for method, _fields in fake.calls if method == "chat.postMessage"])
    await adapter.post(alpha, "old alpha background result")
    assert adapter.delivery_target(alpha) is None
    assert len([1 for method, _fields in fake.calls if method == "chat.postMessage"]) == sent_before
    assert not any(
        fields.get("text") == "old alpha background result"
        for method, fields in fake.calls
        if method == "chat.postMessage"
    )
    other = "U0TESTUSER2"
    assert not await directory.listing(owner=other)
    await runtime.services.aclose()


async def test_agent_resume_tool_navigates_without_copying_messages(tmp_path: Path) -> None:
    store = InMemoryStore()
    directory = Conversations(None, store, ROOT, operator=PERSON)
    from design_agent.testing.scripted_model import ScriptedChatModel, tool_call

    model = ScriptedChatModel(
        steps=[lambda _m: tool_call("resume_conversation", {"conversation": "old-thread"})]
    )
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=model,
        binding=folder_binding(tmp_path / "sandbox", ROOT),
        store=store,
        mcp_tools=directory.tools(),
    )
    directory.runtime = runtime
    directory.checkpointer = runtime.graph.checkpointer
    runtime.conversations = directory
    await runtime.graph.aupdate_state(
        runtime._config("old-thread"), {"messages": [HumanMessage("old project secret")]}
    )
    await directory.touch("old-thread", owner=PERSON)
    api = create_shared_app(runtime, store, token=TOKEN, thread="default", conversations=directory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        graph = SharedGraph(client)
        _ = [
            event
            async for event in graph.astream(
                {"messages": [HumanMessage("resume old-thread")]},
                config=runtime._config("new-thread"),
                stream_mode="updates",
            )
        ]
        assert graph.pending_thread == "old-thread"
        old = await graph.aget_state(runtime._config("old-thread"))
        new = await graph.aget_state(runtime._config("new-thread"))
        assert any(m.text == "old project secret" for m in old.values["messages"])
        assert not any("old project secret" in m.text for m in new.values["messages"])
    await runtime.services.aclose()


async def test_bound_followup_remains_active_after_adapter_restart(tmp_path: Path) -> None:
    fake = FakeSlack()
    store = InMemoryStore()
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=RespondingChatModel(respond=lambda _m: reply("ok")),
        binding=folder_binding(tmp_path / "sb", ROOT),
        store=store,
    )
    directory = Conversations(runtime.graph.checkpointer, store, ROOT, operator=PERSON)
    directory.runtime = runtime
    native = thread_id_for(TEAM, CHANNEL, "1712345678.000100")
    await directory.touch(native, owner=PERSON)
    await directory.link(native, native, owner=PERSON)
    restarted = SlackAdapter(fake.client(), TEAM, BOT, runtime=runtime, conversations=directory)
    from design_agent.surfaces.conversations import BINDINGS

    for item in await store.asearch(BINDINGS, limit=1000):
        restarted._active_threads.add(item.key)
    event = {
        "user": PERSON,
        "team": TEAM,
        "channel": CHANNEL,
        "thread_ts": "1712345678.000100",
        "ts": "1712345679.000100",
        "text": "continue without mention",
    }
    assert restarted.accepts(event, mention=False)
    assert not restarted.accepts({**event, "bot_id": "B0TESTBOT1"}, mention=False)
    await runtime.services.aclose()


async def test_thread_navigation_refuses_other_users_and_background_helpers(tmp_path: Path) -> None:
    import pytest

    from design_agent.harness.meter import CURRENT_TASK_ID

    store = InMemoryStore()
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=RespondingChatModel(respond=lambda _m: reply("ok")),
        binding=folder_binding(tmp_path / "sb", ROOT),
        store=store,
    )
    directory = Conversations(runtime.graph.checkpointer, store, ROOT, operator=PERSON)
    directory.runtime = runtime
    await directory.touch("private-thread", text="Private design project", owner=PERSON)
    with pytest.raises(ValueError, match="not found"):
        await directory.resolve("private-thread", owner="U0TESTUSER2")
    context = CURRENT_TASK_ID.set("background-helper")
    try:
        with pytest.raises(ValueError, match="main conversation"):
            await directory.request_resume("private-thread", key="helper", owner=PERSON)
    finally:
        CURRENT_TASK_ID.reset(context)
    assert await directory.resolve("`private-thread`", owner=PERSON) == "private-thread"
    await runtime.services.aclose()


async def test_catalog_bootstrap_does_not_nest_sqlite_checkpoint_locks(tmp_path: Path) -> None:
    from design_agent.runtime.persistence import open_persistence

    settings = Settings(_env_file=None, workspace_root=tmp_path / "ws")
    async with open_persistence(settings, tmp_path / "ws") as (cp, store):
        runtime = build_local_runtime(
            settings,
            ROOT,
            model=RespondingChatModel(respond=lambda _m: reply("ok")),
            binding=folder_binding(tmp_path / "sb", ROOT),
            checkpointer=cp,
            store=store,
        )
        await runtime.graph.aupdate_state(
            runtime._config("saved-a"), {"messages": [HumanMessage("project a")]}
        )
        await runtime.graph.aupdate_state(
            runtime._config("saved-b"), {"messages": [HumanMessage("project b")]}
        )
        directory = Conversations(cp, store, ROOT, operator=PERSON)
        directory.runtime = runtime
        rows = await asyncio.wait_for(directory.listing(owner=PERSON), timeout=3)
        assert {row["id"] for row in rows} == {"saved-a", "saved-b"}
        await runtime.services.aclose()
