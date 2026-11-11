"""Host-owned grants for a verified Slack person, operation and current tool contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from langchain_core.tools import BaseTool

from design_agent.tools.mcp import McpServer, server_of

Scope = Literal["once", "session", "always"]
GRANTS = ("design_agent", "tool_approvals")
CODE_TOOLS = ("remote_bash", "remote_workbench")
DISPATCHERS = ("calltool", "call_tool", "multi_execute")


def principal(team: str, user: str) -> str:
    return f"slack:{team}:{user}" if team and user else ""


def operation(args: Mapping[str, Any], *, depth: int = 0) -> Any:
    """Dispatchers bind to their inner operation; arbitrary code is pinned to its exact args."""
    if depth > 4 or not isinstance(args, Mapping):
        raise ValueError("Nested tool dispatch is too deep for a saved approval")
    selected = args.get("tool_name") or args.get("tool_slug")
    if not selected and isinstance(args.get("name"), str) and "arguments" in args:
        selected = args["name"]
    if isinstance(args.get("tools"), list):
        selected_tools = [operation(item, depth=depth + 1) for item in args["tools"]]
        if not selected_tools or any(tool is None for tool in selected_tools):
            raise ValueError("Every dispatched tool needs an explicit operation")
        return selected_tools
    if selected:
        if not isinstance(selected, str):
            raise ValueError("A dispatched operation needs an exact tool name")
        inner = args.get("arguments", {})
        raw = args.get("arguments_json")
        if isinstance(raw, str):
            inner = json.loads(raw)
        child = operation(inner, depth=depth + 1)
        if any(part in selected.lower() for part in CODE_TOOLS):
            child = dict(inner)
        if any(part in selected.lower() for part in DISPATCHERS) and child is None:
            raise ValueError("Every nested dispatcher needs an explicit operation")
        return {"tool": selected, "inner": child}
    return None


def permission(
    servers: Sequence[McpServer], tools: Sequence[BaseTool], name: str, args: Mapping[str, Any]
) -> dict[str, str]:
    server_name = server_of(name, frozenset(s.name for s in servers))
    server = next((s for s in servers if s.name == server_name and s.approval_required), None)
    tool = next((t for t in tools if t.name == name), None)
    if server is None or tool is None:
        raise ValueError("Saved approval requires a configured tool contract")
    schema = tool.args_schema
    if schema is not None and not isinstance(schema, dict):
        exporter = getattr(schema, "model_json_schema", None)
        schema = exporter() if exporter is not None else schema.schema()
    selector = operation(args)
    # Remote code tools can dispatch anything; a saved permission covers only that exact script.
    if any(part in name.lower() for part in CODE_TOOLS):
        selector = dict(args)
    if any(part in name.lower() for part in DISPATCHERS) and not selector:
        raise ValueError("Select an exact inner operation before saving this approval")
    contract = {
        "server": server_name,
        "url": server.url,
        "command": server.command,
        "tool": name,
        "schema": schema,
        "operation": selector,
    }
    fingerprint = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    return {"id": fingerprint, "tool": name}


def grant_key(owner: str, thread: str, identifier: str, scope: str) -> str:
    return hashlib.sha256(
        json.dumps([owner, thread if scope == "session" else "", identifier]).encode()
    ).hexdigest()


async def allowed(store: Any, owner: str, thread: str, policy: dict[str, str]) -> bool:
    if store is None or not owner or not thread:
        return False
    for scope in ("session", "always"):
        item = await store.aget((*GRANTS, scope), grant_key(owner, thread, policy["id"], scope))
        if item and item.value == {
            "owner": owner,
            "policy": policy,
            "thread": thread if scope == "session" else "",
        }:
            return True
    return False


async def save(
    store: Any, owner: str, thread: str, policies: list[dict[str, str]], scope: Scope
) -> None:
    if scope not in ("session", "always") or not owner or not thread or store is None:
        raise ValueError("Saved approval requires an owner, conversation and persistent store")
    for policy in policies:
        await store.aput(
            (*GRANTS, scope),
            grant_key(owner, thread, policy["id"], scope),
            {"owner": owner, "policy": policy, "thread": thread if scope == "session" else ""},
        )


async def revoke(store: Any, owner: str, thread: str) -> int:
    """Withdraw the person's saved grants for this session and all conversations."""
    count = 0
    for scope in ("session", "always"):
        offset = 0
        keys: list[str] = []
        while True:
            rows = await store.asearch(
                (*GRANTS, scope), filter={"owner": owner}, limit=100, offset=offset
            )
            keys.extend(
                row.key for row in rows if scope == "always" or row.value.get("thread") == thread
            )
            if len(rows) < 100:
                break
            offset += len(rows)
        for key in keys:
            await store.adelete((*GRANTS, scope), key)
            count += 1
    return count
