"""Real graph approval and owner-bound API acceptance, without any provider writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool

from design_agent.config import Settings
from design_agent.middleware.mcp_approval import McpApprovalMiddleware
from design_agent.runtime.local import ApprovalPending, build_local_runtime
from design_agent.surfaces.api.app import create_app
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import ScriptedChatModel, reply, tool_call
from design_agent.tools.mcp import McpServer, declared_servers

ROOT = Path(__file__).resolve().parents[2]
SERVER = McpServer(
    "social",
    "https://actions.example.com/mcp/social",
    approval_required=True,
    read_tools=("accounts_list",),
)


def runtime(tmp_path: Path, calls: list[str], **kwargs: Any) -> Any:
    @tool
    def social_publish(caption: str) -> str:
        """Publish the exact caption to the selected account."""
        calls.append(caption)
        return "published"

    @tool
    def social_accounts_list() -> str:
        """List connected accounts."""
        return "example-account"

    return build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path / "ws"),
        ROOT,
        model=ScriptedChatModel(
            steps=[
                lambda _: tool_call("social_accounts_list", {}),
                lambda _: tool_call("social_publish", {"caption": "The reviewed caption"}),
                lambda _: reply("Finished the requested step."),
            ]
        ),
        binding=folder_binding(tmp_path / "sb", ROOT),
        out_dir=tmp_path / "out",
        mcp_servers=[SERVER],
        mcp_tools=[social_publish, social_accounts_list],
        **kwargs,
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_reads_run_but_writes_wait_for_exact_review(tmp_path: Path, decision: str) -> None:
    calls: list[str] = []
    rt = runtime(tmp_path, calls)
    try:
        await rt.ask("Publish the campaign", thread_id="campaign")
        assert calls == []
        pending = await rt.pending_approvals("campaign")
        assert len(pending) == 1
        assert pending[0]["value"]["action_requests"][0]["name"] == "social_publish"
        with pytest.raises(ApprovalPending):
            await rt.ask("approved", thread_id="campaign")
        with pytest.raises(ValueError):
            await rt.review("campaign", interrupt_id="stale", decisions=[{"type": "approve"}])
        with pytest.raises(ValueError):
            await rt.review("campaign", interrupt_id=pending[0]["id"], decisions=[])
        await rt.review("campaign", interrupt_id=pending[0]["id"], decisions=[{"type": decision}])
        assert calls == (["The reviewed caption"] if decision == "approve" else [])
        assert await rt.pending_approvals("campaign") == []
        with pytest.raises(ValueError):
            await rt.review(
                "campaign", interrupt_id=pending[0]["id"], decisions=[{"type": "approve"}]
            )
        assert len(calls) <= 1
    finally:
        await rt.services.aclose()


async def test_helpers_cannot_bypass_the_review_with_a_dispatcher() -> None:
    middleware = McpApprovalMiddleware([SERVER], lead=False)
    assert not middleware.needs_review("social_accounts_list")
    assert middleware.needs_review("social_unknown_new_tool")
    assert middleware.needs_review("social_call_tool")
    assert not middleware.needs_review("paper_export")
    from langchain.agents.middleware import ToolCallRequest

    dispatched = []

    async def handler(request: Any) -> Any:
        dispatched.append(request)
        raise AssertionError("The helper must not dispatch a write")

    request = ToolCallRequest(
        tool_call={"name": "social_call_tool", "args": {}, "id": "c1", "type": "tool_call"},
        tool=None,
        state={"messages": []},
        runtime=None,  # type: ignore[arg-type]
    )
    result = await middleware.awrap_tool_call(request, handler)
    assert result.status == "error"
    assert dispatched == []


def test_api_approval_is_owned_and_stale_review_does_not_republish(tmp_path: Path) -> None:
    calls: list[str] = []
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path / "api-ws",
        api_tokens="alice-token-0123456789:alice,bob-token-0123456789:bob",
    )

    def build(_settings: Any, _root: Path, **kwargs: Any) -> Any:
        kwargs.pop("mcp_servers", None)
        kwargs.pop("mcp_tools", None)
        return runtime(tmp_path, calls, **kwargs)

    alice = {"Authorization": "Bearer alice-token-0123456789"}
    bob = {"Authorization": "Bearer bob-token-0123456789"}
    with TestClient(create_app(ROOT, settings, build=build)) as api:
        result = api.post("/threads/c/messages", headers=alice, json={"text": "Publish"})
        assert result.status_code == 200
        pending = result.json()["approvals"]
        assert calls == [] and len(pending) == 1
        assert api.get("/threads/c/approvals", headers=bob).status_code == 404
        body = {"interrupt_id": pending[0]["id"], "decisions": [{"type": "approve"}]}
        assert api.post("/threads/c/approvals", headers=bob, json=body).status_code == 404
        assert (
            api.post("/threads/c/messages", headers=alice, json={"text": "approved"}).status_code
            == 409
        )
        assert api.post("/threads/c/approvals", headers=alice, json=body).status_code == 200
        assert calls == ["The reviewed caption"]
        assert api.post("/threads/c/approvals", headers=alice, json=body).status_code == 409
        assert calls == ["The reviewed caption"]


def test_mcp_configuration_validates_host_owned_approval(tmp_path: Path) -> None:
    folder = tmp_path / "config"
    folder.mkdir()
    path = folder / "mcp.toml"
    path.write_text(
        '[servers.social]\nurl="https://actions.example.com/mcp/social"\n'
        'approval_required=true\nread_tools=["accounts_list"]\n'
    )
    configured = declared_servers(tmp_path)[0]
    assert configured.approval_required and configured.read_tools == ("accounts_list",)
    assert configured.include_tools is None
    path.write_text(path.read_text().replace("true", '"yes"'))
    with pytest.raises(ValueError, match="approval_required"):
        declared_servers(tmp_path)


@pytest.mark.parametrize("decision", ["aprovar", "rejeitar"])
async def test_slack_review_is_exact_owned_and_not_replayed(tmp_path: Path, decision: str) -> None:
    from design_agent.surfaces.slack.bolt_app import SlackAdapter, thread_id_for
    from design_agent.testing.fake_slack import BOT, CHANNEL, PERSON, TEAM, FakeSlack

    calls: list[str] = []
    rt = runtime(tmp_path, calls)
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
    event = {
        "user": PERSON,
        "team": TEAM,
        "channel": CHANNEL,
        "ts": "1712345678.000100",
        "text": f"<@{BOT}> Publish the campaign",
    }
    thread = thread_id_for(TEAM, CHANNEL, event["ts"])
    try:
        await adapter.handle(event)
        assert calls == []
        pending = await rt.pending_approvals(thread)
        assert len(pending) == 1
        identifier = pending[0]["id"]
        review = {**event, "thread_ts": event["ts"], "ts": "1712345679.000100"}
        # The requester's binding is in the graph's store, not the adapter's memory.
        adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=rt)
        await adapter.handle({**review, "user": "UOTHER", "text": f"aprovar {identifier}"})
        assert calls == [] and await rt.pending_approvals(thread)
        await adapter.handle({**review, "text": "aprovar stale-id"})
        assert calls == [] and await rt.pending_approvals(thread)
        await adapter.handle({**review, "text": f"{decision} {identifier}"})
        expected = ["The reviewed caption"] if decision == "aprovar" else []
        assert calls == expected and not await rt.pending_approvals(thread)
        await adapter.handle({**review, "text": f"aprovar {identifier}"})
        assert calls == expected
        posts = [fields["text"] for method, fields in fake.calls if method == "chat.postMessage"]
        assert any("Revise o destino" in text for text in posts)
        assert any("Somente a pessoa" in text for text in posts)
        assert "Nenhuma aprovação pendente" in posts[-1]
    finally:
        await rt.services.aclose()
