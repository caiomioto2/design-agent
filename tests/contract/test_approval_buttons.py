"""Real graph + fake Slack: scoped button grants, callback ownership and replay denial."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.store.memory import InMemoryStore

from design_agent.config import Settings
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.slack.approval import REQUESTERS, blocks
from design_agent.surfaces.slack.bolt_app import SlackAdapter, register, thread_id_for
from design_agent.testing.fake_slack import BOT, CHANNEL, PERSON, TEAM, FakeSlack
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call
from design_agent.tools.approvals import GRANTS, allowed, permission, principal, save
from design_agent.tools.mcp import McpServer

ROOT = Path(__file__).resolve().parents[2]
SERVER = McpServer("social", "https://actions.example.com/social", approval_required=True)


def build(tmp_path: Path, writes: list[str], store: Any):
    @tool
    def social_publish(caption: str) -> str:
        """Publish a caption."""
        writes.append(caption)
        return "published"

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return reply("Reviewed step finished.")
        humans = [m.text for m in messages if isinstance(m, HumanMessage)]
        return tool_call("social_publish", {"caption": humans[-1]})

    return build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=RespondingChatModel(respond=respond),
        binding=folder_binding(tmp_path / "sb", ROOT),
        out_dir=tmp_path / "out",
        store=store,
        mcp_servers=[SERVER],
        mcp_tools=[social_publish],
    )


def event(text="Publish one", *, ts="1712345678.000100", user=PERSON, root=None):
    result = {"user": user, "team": TEAM, "channel": CHANNEL, "ts": ts, "text": text}
    if root:
        result["thread_ts"] = root
    return result


def card(fake):
    return next(
        fields
        for method, fields in reversed(fake.calls)
        if method == "chat.postMessage" and fields.get("blocks")
    )


def click(fake, action, *, user=PERSON):
    request = card(fake)
    posted = next(p for p in reversed(list(fake.posted.values())) if p.blocks)
    value = next(
        b["value"]
        for block in request["blocks"]
        if block["type"] == "actions"
        for b in block["elements"]
        if b["action_id"] == action
    )
    return {
        "team": {"id": TEAM},
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {"ts": posted.ts, "thread_ts": request["thread_ts"], "user": BOT},
        "actions": [{"action_id": action, "value": value}],
    }


@pytest.mark.parametrize("scope", ["once", "session", "always"])
@pytest.mark.parametrize("via_text", [False, True])
async def test_button_scope_other_users_conversations_and_revocation(
    tmp_path: Path, scope: str, via_text: bool
):
    writes = []
    store = InMemoryStore()
    rt = build(tmp_path, writes, store)
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    first = event()
    thread = thread_id_for(TEAM, CHANNEL, first["ts"])
    try:
        await adapter.handle(first)
        request = card(fake)
        buttons = [
            b
            for block in request["blocks"]
            if block["type"] == "actions"
            for b in block["elements"]
        ]
        assert [b["text"]["text"] for b in buttons] == [
            "Aprovar uma vez",
            "Aprovar nesta sessão",
            "Aprovar sempre",
            "Rejeitar",
        ]
        for button in buttons:
            assert len(button["text"]["text"]) <= 75 and len(button["value"]) <= 2000
        callback = click(fake, "tool_approval_" + scope)
        await adapter.handle_approval_action({**callback, "user": {"id": "UOTHER"}})
        assert writes == []
        await adapter.handle_approval_action({**callback, "team": {"id": "TOTHER"}})
        assert writes == []
        if via_text:
            prefix = {"once": "aprovar", "session": "aprovar sessao", "always": "aprovar sempre"}[
                scope
            ]
            identifier = callback["actions"][0]["value"]
            command = f"{prefix} {identifier}"
            await adapter.handle(
                event(command, ts="1712345678.000200", root=first["ts"], user="UOTHER")
            )
            assert writes == []
            await adapter.handle(event(command, ts="1712345678.000300", root=first["ts"]))
        else:
            await adapter.handle_approval_action(callback)
        assert len(writes) == 1 and not await rt.pending_approvals(thread)
        await adapter.handle_approval_action(callback)
        assert len(writes) == 1
        await adapter.handle(event("Publish two", ts="1712345679.000100", root=first["ts"]))
        assert len(writes) == (1 if scope == "once" else 2)
        if scope == "once":
            await adapter.handle_approval_action(click(fake, "tool_approval_reject"))
        other = event("Other person", ts="1712345680.000100", root=first["ts"], user="UOTHER")
        await adapter.handle(other)
        assert await rt.pending_approvals(thread)
        await adapter.handle_approval_action(click(fake, "tool_approval_reject", user="UOTHER"))
        # Reload the runtime with the same durable grant store. Only "always" reaches a new conversation.
        await rt.services.aclose()
        rt = build(tmp_path / "restart", writes, store)
        adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
        count = len(writes)
        second = event("New conversation", ts="1712345700.000100")
        await adapter.handle(second)
        assert len(writes) == count + (scope == "always")
        if scope != "always":
            await adapter.handle_approval_action(click(fake, "tool_approval_reject"))
        await adapter.handle(event("revogar aprovacoes", ts="1712345701.000100", root=first["ts"]))
        await adapter.handle(event("After revoke", ts="1712345702.000100", root=first["ts"]))
        assert await rt.pending_approvals(thread)
        assert len(writes) == count + (scope == "always")
    finally:
        await rt.services.aclose()


async def test_double_click_grants_and_executes_only_once(tmp_path: Path):
    writes = []
    store = InMemoryStore()
    rt = build(tmp_path, writes, store)
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    try:
        await adapter.handle(event())
        callback = click(fake, "tool_approval_always")
        await asyncio.gather(
            adapter.handle_approval_action(callback), adapter.handle_approval_action(callback)
        )
        assert len(writes) == 1
        assert len(await store.asearch((*GRANTS, "always"))) == 1
    finally:
        await rt.services.aclose()


async def test_existing_pending_session_recovers_verified_owner(tmp_path: Path):
    writes = []
    store = InMemoryStore()
    rt = build(tmp_path, writes, store)
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    first = event()
    thread = thread_id_for(TEAM, CHANNEL, first["ts"])
    try:
        # The checkpoint and trusted conversation route predate the approval-card binding.
        await store.aput(("design_agent", "conversations"), thread, {"id": thread, "owner": PERSON})
        await store.aput(
            ("design_agent", "slack_conversations"), thread, {"thread": thread, "owner": PERSON}
        )
        await rt.ask(first["text"], thread_id=thread, configurable={"slack_event": first})
        # Another participant cannot claim the missing approval by asking for a card first.
        await adapter.handle(event("Go", ts="1712345679.000100", root=first["ts"], user="UOTHER"))
        await adapter.handle_approval_action(click(fake, "tool_approval_once", user="UOTHER"))
        assert writes == []
        await adapter.handle_approval_action(click(fake, "tool_approval_once"))
        assert len(writes) == 1
    finally:
        await rt.services.aclose()


async def test_previous_card_for_same_pending_request_still_works(tmp_path: Path):
    writes = []
    rt = build(tmp_path, writes, InMemoryStore())
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    first = event()
    try:
        await adapter.handle(first)
        original = click(fake, "tool_approval_once")
        await adapter.handle(event("Go", ts="1712345679.000100", root=first["ts"]))
        await adapter.handle_approval_action(original)
        assert len(writes) == 1
    finally:
        await rt.services.aclose()


@pytest.mark.parametrize("matching_owner", [True, False])
async def test_already_visible_legacy_card_recovers_only_matching_host_records(
    tmp_path: Path, matching_owner: bool
):
    writes = []
    store = InMemoryStore()
    rt = build(tmp_path, writes, store)
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    first = event()
    thread = thread_id_for(TEAM, CHANNEL, first["ts"])
    try:
        await store.aput(("design_agent", "conversations"), thread, {"id": thread, "owner": PERSON})
        await store.aput(
            ("design_agent", "slack_conversations"),
            thread,
            {"thread": thread, "owner": PERSON if matching_owner else "UOTHER"},
        )
        await rt.ask(first["text"], thread_id=thread, configurable={"slack_event": first})
        pending = (await rt.pending_approvals(thread))[0]
        await adapter.client.call(
            "chat.postMessage",
            {
                "channel": CHANNEL,
                "thread_ts": first["ts"],
                "text": "Legacy review",
                "blocks": blocks(pending),
            },
        )
        assert await store.aget(REQUESTERS, pending["id"]) is None
        callback = click(fake, "tool_approval_once")
        await adapter.handle_approval_action(callback)
        assert len(writes) == int(matching_owner)
        await adapter.handle_approval_action(callback)
        assert len(writes) == int(matching_owner)
    finally:
        await rt.services.aclose()


async def test_background_turn_cannot_inherit_slack_grant(tmp_path: Path):
    writes = []
    rt = build(tmp_path, writes, InMemoryStore())
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    thread = thread_id_for(TEAM, CHANNEL, event()["ts"])
    try:
        await adapter.handle(event())
        await adapter.handle_approval_action(click(fake, "tool_approval_always"))
        assert len(writes) == 1
        await rt.ask("Background turn with no verified person", thread_id=thread)
        assert len(writes) == 1
        assert await rt.pending_approvals(thread)
    finally:
        await rt.services.aclose()


async def test_inner_dispatch_and_schema_changes_do_not_inherit_approval():
    @tool
    def social_call_tool(tool_name: str, arguments_json: str) -> str:
        """Dispatch a selected operation."""
        return "ok"

    store = InMemoryStore()
    owner = principal(TEAM, PERSON)
    args = {"tool_name": "posts_create", "arguments_json": '{"caption":"one","slug":"one"}'}
    policy = permission([SERVER], [social_call_tool], "social_call_tool", args)
    await save(store, owner, "thread", [policy], "always")
    changed = permission(
        [SERVER],
        [social_call_tool],
        "social_call_tool",
        {**args, "arguments_json": '{"caption":"two","slug":"two"}'},
    )
    assert await allowed(store, owner, "other-thread", changed)
    different = permission(
        [SERVER], [social_call_tool], "social_call_tool", {**args, "tool_name": "posts_delete"}
    )
    assert not await allowed(store, owner, "thread", different)
    nested = permission(
        [SERVER],
        [social_call_tool],
        "social_call_tool",
        {"tool_name": "call_tool", "arguments_json": '{"name":"posts_delete","arguments":{}}'},
    )
    assert not await allowed(store, owner, "thread", nested)
    modified = social_call_tool.model_copy(
        update={"args_schema": {"type": "object", "properties": {"other": {"type": "string"}}}}
    )
    assert not await allowed(
        store, owner, "thread", permission([SERVER], [modified], "social_call_tool", args)
    )
    assert not await allowed(store, principal("TOTHER", PERSON), "thread", policy)
    assert not await allowed(store, "", "thread", policy)


async def test_bolt_acknowledges_before_review_processing():
    listeners = {}
    order = []

    class App:
        def event(self, name):
            return lambda handler: None

        def action(self, pattern):
            def register(handler):
                listeners["action"] = handler

            return register

    class Adapter:
        _tasks = set()

        async def handle_approval_action(self, body):
            order.append("review")

    adapter = Adapter()
    register(App(), adapter)

    async def ack():
        order.append("ack")

    await listeners["action"](ack, {"actions": []})
    assert order == ["ack"]
    await asyncio.gather(*adapter._tasks)
    assert order == ["ack", "review"]


async def test_nested_scripts_and_dispatchers_cannot_broaden_grants():
    @tool
    def social_call_tool(name: str, arguments: dict) -> str:
        """Dispatch a selected operation."""
        return "ok"

    store = InMemoryStore()
    owner = principal(TEAM, PERSON)
    args = {"name": "COMPOSIO_REMOTE_BASH_TOOL", "arguments": {"command": "pwd"}}
    policy = permission([SERVER], [social_call_tool], "social_call_tool", args)
    await save(store, owner, "thread", [policy], "always")
    changed = permission(
        [SERVER],
        [social_call_tool],
        "social_call_tool",
        {**args, "arguments": {"command": "different script"}},
    )
    assert not await allowed(store, owner, "thread", changed)
    with pytest.raises(ValueError, match="nested dispatcher"):
        permission(
            [SERVER],
            [social_call_tool],
            "social_call_tool",
            {"name": "call_tool", "arguments": {"unknown_selector": "posts_delete"}},
        )
    with pytest.raises(ValueError, match="explicit operation"):
        permission(
            [SERVER],
            [social_call_tool],
            "social_call_tool",
            {"name": "multi_execute_tool", "arguments": {"tools": [{}]}},
        )
