"""Composio's legacy MCP approval RPC, held open across a graph interrupt.

Never replay a tools/call to answer a question: a batch may already have side effects.
An in-flight call lost on restart is uncertain and must be reconciled by reading the provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool, ToolException
from langgraph.types import interrupt
from mcp.types import ElicitResult

LOGGER = logging.getLogger(__name__)
CALLS = ("design_agent", "composio_calls")


@dataclass
class Question:
    request: dict[str, Any]
    answer: asyncio.Future[Any]


@dataclass
class PendingCall:
    task: asyncio.Task[Any] | None = None
    questions: list[Question] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class ApprovalBridge:
    """One registry per loaded server. Graph/tool IDs isolate concurrent callers."""

    def __init__(self, transport: Any, server: str, timeout: float) -> None:
        self.transport = transport
        self.server = server
        self.timeout = timeout
        self.pending: dict[str, PendingCall] = {}

    async def invoke(
        self, spec: Any, runtime: ToolRuntime | None, arguments: dict[str, Any]
    ) -> Any:
        from langchain.mcp import as_langchain_tool

        thread = str(runtime.config.get("configurable", {}).get("thread_id", "")) if runtime else ""
        raw_key = [thread, runtime.tool_call_id if runtime else "", spec.name, arguments]
        key = hashlib.sha256(json.dumps(raw_key, sort_keys=True).encode()).hexdigest()
        store = runtime.store if runtime else None
        persistent = bool(thread and runtime and runtime.tool_call_id and store is not None)
        call = self.pending.get(key) if persistent else None
        if call is None:
            if persistent and store is not None and await store.aget(CALLS, key) is not None:
                raise ToolException(
                    "MCP call outcome is uncertain after interruption or restart; "
                    "read provider state before retrying. No call was replayed."
                )
            call = PendingCall()

            async def elicit(message: str, _response_type: Any, params: Any, context: Any) -> Any:
                schema = getattr(params, "requested_schema", {})
                choices = schema.get("properties", {}).get("decision", {}).get("oneOf", [])
                supported = (
                    params.mode == "form"
                    and schema.get("required") == ["decision"]
                    and set(schema.get("properties", {})) == {"decision"}
                    and {c.get("const") for c in choices} == {"allow_once", "allow_session", "deny"}
                )
                request_id = hashlib.sha256(str(context.request_id).encode()).hexdigest()[:16]
                LOGGER.info(
                    "[mcp-approval] server=%s request=%s supported=%s",
                    self.server,
                    request_id,
                    supported,
                )
                if not persistent or not supported:
                    LOGGER.warning(
                        "[mcp-approval] server=%s request=%s action=cancel", self.server, request_id
                    )
                    return ElicitResult(action="cancel")
                question = Question(
                    {"request_id": request_id, "message": message, "requested_schema": schema},
                    asyncio.get_running_loop().create_future(),
                )
                call.questions.append(question)
                call.changed.set()
                try:
                    answer = await asyncio.wait_for(asyncio.shield(question.answer), self.timeout)
                except TimeoutError:
                    answer = ElicitResult(action="cancel")
                LOGGER.info(
                    "[mcp-approval] server=%s request=%s action=%s",
                    self.server,
                    request_id,
                    answer.action,
                )
                return answer

            async def execute() -> Any:
                try:
                    async with Client(self.transport, elicitation_handler=elicit) as client:
                        LOGGER.info(
                            "[mcp-approval] server=%s protocol=%s call=%s",
                            self.server,
                            client.protocol_version,
                            key[:16],
                        )
                        tool = await as_langchain_tool(spec, client)
                        if not isinstance(tool, StructuredTool) or tool.coroutine is None:
                            raise ToolException("MCP tool cannot be invoked safely")
                        async with asyncio.timeout(self.timeout + 30):
                            return await tool.coroutine(**arguments)
                finally:
                    call.changed.set()

            if persistent and store is not None:
                await store.aput(CALLS, key, {"status": "started"})
                self.pending[key] = call

            call.task = asyncio.create_task(execute())
        task = call.task
        if task is None:
            raise ToolException("MCP invocation is unavailable; no call was replayed")
        index = 0
        try:
            while True:
                while index < len(call.questions):
                    question = call.questions[index]
                    if not question.answer.done() and task.done():
                        raise ToolException(
                            "MCP approval expired; no call was replayed. Read provider state."
                        )
                    value = interrupt(
                        {
                            "type": "mcp_legacy_approval",
                            "action_requests": [
                                {
                                    "name": f"{self.server}_{spec.name}",
                                    "args": {
                                        "operation": arguments,
                                        "mcp_request": question.request,
                                    },
                                }
                            ],
                            "review_configs": [
                                {
                                    "action_name": f"{self.server}_{spec.name}",
                                    "allowed_decisions": ["approve", "reject"],
                                }
                            ],
                        }
                    )
                    decisions = value.get("decisions", []) if isinstance(value, dict) else []
                    if len(decisions) != 1 or decisions[0].get("type") not in {"approve", "reject"}:
                        raise ValueError(
                            "Provide one approve or reject decision for this MCP request"
                        )
                    if not question.answer.done():
                        accepted = decisions[0]["type"] == "approve"
                        question.answer.set_result(
                            ElicitResult(
                                action="accept" if accepted else "decline",
                                content={"decision": "allow_once"} if accepted else None,
                            )
                        )
                    index += 1
                if task.done():
                    result = task.result()
                    self.pending.pop(key, None)
                    return result
                call.changed.clear()
                await call.changed.wait()
        except asyncio.CancelledError:
            for question in call.questions:
                if not question.answer.done():
                    question.answer.set_result(ElicitResult(action="cancel"))
            raise


async def load_tools(transport: Any, server: str, timeout: float) -> list[Any]:
    """Use public FastMCP and LangChain APIs; modern servers retain their native adapter."""
    from langchain.mcp import MCPAdapter, as_langchain_tool

    async with Client(transport) as client:
        if client.protocol_version not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
            async with MCPAdapter(transport) as adapter:
                return await adapter.list_tools()
        specs = await client.list_tools()
        templates = [await as_langchain_tool(spec, client) for spec in specs]
    bridge = ApprovalBridge(transport, server, timeout)

    def bind(spec: Any, template: Any) -> Any:
        async def invoke(runtime: ToolRuntime | None = None, **arguments: Any) -> Any:
            return await bridge.invoke(spec, runtime, arguments)

        return template.model_copy(update={"coroutine": invoke, "handle_tool_error": True})

    return [bind(spec, template) for spec, template in zip(specs, templates, strict=True)]
