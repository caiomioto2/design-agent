"""Review external writes in the lead's graph; helpers cannot execute them.

The operator names read tools, never the remote server or model. Everything else on an opted-in
server requires review, so discovery, unknown tool names and generic dispatchers cannot bypass it.
Each model turn gets a fresh native HITL policy; no mutable approval map is shared across threads.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    ToolCallRequest,
)
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.types import Command

from design_agent.tools.approvals import allowed, permission, principal
from design_agent.tools.mcp import McpServer, server_of


class McpApprovalMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(
        self, servers: Sequence[McpServer], *, lead: bool, tools: Sequence[BaseTool] = ()
    ) -> None:
        super().__init__()
        self._servers = {s.name: s for s in servers if s.approval_required}
        self._lead = lead
        self._tools = tools

    def needs_review(self, name: str) -> bool:
        server = server_of(name, frozenset(self._servers))
        if server is None:
            return False
        return name[len(server) + 1 :] not in self._servers[server].read_tools

    async def aafter_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        if not self._lead or not state["messages"]:
            return None
        last = state["messages"][-1]
        if not isinstance(last, AIMessage):
            return None
        config = get_config().get("configurable", {})
        event = config.get("slack_event") or {}
        owner = principal(
            str(event.get("team") or event.get("user_team") or ""), str(event.get("user") or "")
        )
        interrupt_on: dict[str, bool | InterruptOnConfig] = {}
        for call in last.tool_calls:
            if not self.needs_review(call["name"]):
                continue
            try:
                policy = permission(
                    tuple(self._servers.values()), self._tools, call["name"], call["args"]
                )
                permitted = await allowed(
                    runtime.store, owner, str(config.get("thread_id") or ""), policy
                )
            except (ValueError, TypeError):
                permitted = False
            # If the same tool occurs twice and either call needs review, gate both.
            if not permitted:
                interrupt_on[call["name"]] = {"allowed_decisions": ["approve", "reject"]}
        review = HumanInTheLoopMiddleware(
            interrupt_on=interrupt_on,
            description_prefix="Review the destination, content and external effect before execution",
        )
        return await review.aafter_model(state, runtime)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if not self._lead and self.needs_review(request.tool_call["name"]):
            return ToolMessage(
                content="Return the prepared content to the lead for human review. Helpers cannot "
                "execute external writes, activate automations, publish or schedule content.",
                name=request.tool_call["name"],
                tool_call_id=request.tool_call.get("id") or "",
                status="error",
            )
        return await handler(request)
