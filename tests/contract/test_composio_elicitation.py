"""Legacy approvals must answer the original RPC exactly once, never replay a batch."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Context, FastMCP
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from design_agent.tools.composio import load_tools


@pytest.fixture(autouse=True)
def legacy_client(monkeypatch):
    from fastmcp import Client

    monkeypatch.setattr(
        "design_agent.tools.composio.Client",
        lambda transport, **kw: Client(transport, mode="legacy", **kw),
    )


def server():
    mcp = FastMCP("fixture")
    calls: list[str] = []
    responses: list[str] = []

    @mcp.tool
    async def execute(ctx: Context, value: str) -> str:
        calls.append(value)
        result = await ctx.session.elicit_form(
            message="Review operation",
            requested_schema={
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "oneOf": [
                            {"const": "allow_once"},
                            {"const": "allow_session"},
                            {"const": "deny"},
                        ],
                    }
                },
                "required": ["decision"],
            },
        )
        responses.append(result.action)
        if result.action == "accept":
            assert result.content == {"decision": "allow_once"}
        return result.action

    return mcp, calls, responses


async def graph_for(mcp):
    tools = await load_tools(mcp, "composio", 2)
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile(checkpointer=InMemorySaver(), store=InMemoryStore())


def initial(value="one"):
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"id": "call-fixture", "name": "execute", "args": {"value": value}}],
            )
        ]
    }


@pytest.mark.parametrize(("decision", "action"), [("approve", "accept"), ("reject", "decline")])
async def test_legacy_rpc_pauses_and_resumes_same_call(decision, action):
    mcp, calls, responses = server()
    graph = await graph_for(mcp)
    config = {"configurable": {"thread_id": "thread-fixture"}}
    state = await graph.ainvoke(initial(), config)
    question = state["__interrupt__"][0]
    assert question.value["type"] == "mcp_legacy_approval"
    assert calls == ["one"] and responses == []
    await graph.ainvoke(Command(resume={question.id: {"decisions": [{"type": decision}]}}), config)
    assert calls == ["one"]
    assert responses == [action]


async def test_no_graph_review_cancels_and_never_accepts():
    mcp, calls, responses = server()
    tools = await load_tools(mcp, "composio", 2)
    await tools[0].ainvoke({"value": "without-review"})
    assert calls == ["without-review"] and responses == ["cancel"]


async def test_parallel_threads_do_not_share_answers():
    mcp, calls, responses = server()
    graph = await graph_for(mcp)
    configs = [{"configurable": {"thread_id": t}} for t in ["thread-a", "thread-b"]]
    states = await asyncio.gather(
        *(graph.ainvoke(initial(str(i)), c) for i, c in enumerate(configs))
    )
    for state, config, decision in zip(states, configs, ["approve", "reject"], strict=True):
        q = state["__interrupt__"][0]
        await graph.ainvoke(Command(resume={q.id: {"decisions": [{"type": decision}]}}), config)
    assert sorted(calls) == ["0", "1"]
    assert sorted(responses) == ["accept", "decline"]


async def test_original_adapter_returns_error_without_graph_review():
    from fastmcp import Client
    from langchain.mcp import MCPAdapter

    mcp, calls, responses = server()
    async with MCPAdapter(Client(mcp, mode="legacy")) as adapter:
        tools = await adapter.list_tools()
        result = await tools[0].ainvoke({"value": "baseline"})
    assert calls == ["baseline"] and responses == []
    assert "legacy server-initiated" in str(result)


def test_mcp_card_never_offers_session_or_permanent_permission():
    from design_agent.surfaces.slack.approval import blocks, preview

    pending = {
        "id": "approval-fixture",
        "value": {
            "type": "mcp_legacy_approval",
            "action_requests": [{"name": "composio_execute", "args": {}}],
        },
    }
    card = blocks(pending)
    actions = card[-1]["elements"]
    assert {b["action_id"] for b in actions} == {"tool_approval_once", "tool_approval_reject"}
    assert "sempre" not in preview(pending) and "sessao" not in preview(pending)


async def test_lost_inflight_call_is_not_replayed():
    mcp, calls, responses = server()
    cp, store = InMemorySaver(), InMemoryStore()

    async def compile_again():
        tools = await load_tools(mcp, "composio", 0.05)
        return (
            StateGraph(MessagesState)
            .add_node("tools", ToolNode(tools))
            .add_edge(START, "tools")
            .add_edge("tools", END)
            .compile(checkpointer=cp, store=store)
        )

    graph = await compile_again()
    config = {"configurable": {"thread_id": "thread-lost"}}
    first = await graph.ainvoke(initial(), config)
    q = first["__interrupt__"][0]
    await asyncio.sleep(0.15)
    graph = await compile_again()
    result = await graph.ainvoke(
        Command(resume={q.id: {"decisions": [{"type": "approve"}]}}), config
    )
    assert calls == ["one"] and responses == ["cancel"]
    assert "uncertain" in str(result["messages"][-1].content)
