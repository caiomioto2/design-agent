"""Persistent conversation discovery and explicit navigation across local UI and Slack."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool

from design_agent.harness.meter import CURRENT_TASK_ID

THREAD_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
CATALOG = ("design_agent", "conversations")
BINDINGS = ("design_agent", "slack_conversations")
NAVIGATION = ("design_agent", "conversation_navigation")


def valid_thread(value: object) -> str:
    if not isinstance(value, str) or not THREAD_ID.fullmatch(value):
        raise ValueError("Pass a valid conversation id from list_conversations.")
    return value


class Conversations:
    def __init__(
        self, checkpointer: Any, store: Any, root: Path, *, operator: str | None = None
    ) -> None:
        self.checkpointer = checkpointer
        self.store = store
        self.root = root
        self.operator = operator
        self.runtime: Any = None
        self._bootstrapped = False
        self.surfaces: dict[str, str] = {}
        self.bindings: dict[str, str] = {}

    async def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        seen: set[str] = set()
        async for checkpoint in self.checkpointer.alist(None, limit=10000):
            config = checkpoint.config.get("configurable", {})
            thread = config.get("thread_id")
            if config.get("checkpoint_ns") or not isinstance(thread, str) or thread in seen:
                continue
            seen.add(thread)
        # SQLite's iterator holds its connection lock until exhausted. Read snapshots only
        # after releasing that iterator; nested graph reads would wait on the same lock.
        for thread in seen:
            await self.touch(thread)
        self._bootstrapped = True

    async def touch(
        self, thread: str, *, text: str = "", owner: str | None = None
    ) -> dict[str, Any]:
        valid_thread(thread)
        existing = await self.store.aget(CATALOG, thread)
        row = dict(existing.value) if existing else {"id": thread, "owner": owner or self.operator}
        state = await self.runtime.graph.aget_state({"configurable": {"thread_id": thread}})
        if not row.get("title") or row.get("title") in {thread, "Nova conversa"}:
            messages = state.values.get("messages", []) if state and state.values else []
            candidates = [
                message.text
                for message in messages
                if isinstance(message, HumanMessage)
                and not message.text.startswith(("[Today is", "[Runtime notice", "[Running in"))
            ]
            row["title"] = (text or next(iter(candidates), thread)).replace("\n", " ")[:140]
        row["updated_at"] = state.created_at if state else row.get("updated_at")
        if owner and not row.get("owner"):
            row["owner"] = owner
        await self.store.aput(CATALOG, thread, row)
        return row

    def visible(self, row: dict[str, Any], owner: str | None) -> bool:
        return owner is None or owner == self.operator or row.get("owner") == owner

    async def listing(self, *, owner: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        await self.bootstrap()
        rows = [
            dict(item.value)
            for item in await self.store.asearch(CATALOG, limit=1000)
            if self.visible(item.value, owner)
        ]
        return sorted(rows, key=lambda row: row.get("updated_at") or "", reverse=True)[
            : max(1, min(limit, 1000))
        ]

    async def resolve(self, selector: str, *, owner: str | None = None) -> str:
        selector = selector.strip().strip("`")
        if not selector:
            raise ValueError("Choose one conversation id or a unique title.")
        rows = await self.listing(owner=owner, limit=1000)
        exact = [row for row in rows if row["id"] == selector]
        found = exact or [
            row
            for row in rows
            if selector.casefold() in (row["id"] + " " + row["title"]).casefold()
        ]
        if len(found) != 1:
            raise ValueError(
                "Conversation not found or ambiguous. List conversations and choose one exact id."
            )
        return str(found[0]["id"])

    async def projects(self, *, owner: str | None = None) -> list[dict[str, Any]]:
        rows = await self.listing(owner=owner, limit=1000)
        registry = self.root / "workspace/state/project-mounts.json"
        mounts = json.loads(registry.read_text()) if registry.exists() else {}
        result = []
        for row in rows:
            digest = hashlib.sha256(row["id"].encode()).hexdigest()[:16]
            for target, source in mounts.items():
                if f"design-agent-ws-{digest}/" not in source:
                    continue
                path = Path(target)
                if path.is_dir():
                    result.append(
                        {
                            "project": path.name,
                            "thread_id": row["id"],
                            "title": row["title"],
                            "path": "/workspace/projects/" + Path(source).name,
                        }
                    )
        return result

    async def project_thread(self, selector: str, *, owner: str | None = None) -> str:
        rows = await self.projects(owner=owner)
        exact = [row for row in rows if row["project"] == selector]
        selected = exact or [
            row for row in rows if selector.casefold() in row["project"].casefold()
        ]
        threads = {row["thread_id"] for row in selected}
        if len(threads) != 1:
            raise ValueError(
                "Project not found or ambiguous. List previous projects and choose its conversation id."
            )
        return str(threads.pop())

    async def link(self, native: str, thread: str, *, owner: str) -> None:
        thread = await self.resolve(thread, owner=owner)
        old = await self.store.aget(BINDINGS, native)
        if old and old.value.get("owner") not in (None, owner) and owner != self.operator:
            raise ValueError("This Slack thread is already linked by another user.")
        await self.store.aput(BINDINGS, native, {"thread": thread, "owner": owner})
        self.bindings[native] = thread

    async def linked(self, native: str) -> str | None:
        item = await self.store.aget(BINDINGS, native)
        if item:
            self.bindings[native] = str(item.value["thread"])
            return self.bindings[native]
        return None

    async def consume(self, key: str) -> str | None:
        item = await self.store.aget(NAVIGATION, key)
        if not item:
            return None
        await self.store.adelete(NAVIGATION, key)
        return str(item.value["thread"])

    async def request_resume(self, selector: str, *, key: str, owner: str | None = None) -> str:
        target = await self.resolve(selector, owner=owner)
        if not key or CURRENT_TASK_ID.get() is not None:
            raise ValueError(
                "Resume is available only to the main conversation requested by the person."
            )
        await self.store.aput(NAVIGATION, key, {"thread": target})
        return json.dumps(
            {
                "resume_thread": target,
                "status": "will switch after this turn; histories remain separate",
            },
            ensure_ascii=False,
        )

    def tools(self) -> list[BaseTool]:
        def context(runtime: ToolRuntime) -> tuple[str | None, str]:
            config = runtime.config.get("configurable", {})
            event = config.get("slack_event") or {}
            return event.get("user"), str(config.get("navigation_key") or "")

        async def list_conversations(runtime: ToolRuntime, limit: int = 20) -> str:
            """List saved conversations the person can resume in Slack or the terminal, with ids and titles."""
            owner, _key = context(runtime)
            return json.dumps(await self.listing(owner=owner, limit=limit), ensure_ascii=False)

        async def resume_conversation(conversation: str, runtime: ToolRuntime) -> str:
            """Resume an existing conversation by exact id or unique title when the person asks. No history is merged."""
            owner, key = context(runtime)
            try:
                return await self.request_resume(conversation, key=key, owner=owner)
            except ValueError as error:
                return str(error)

        async def list_previous_projects(runtime: ToolRuntime) -> str:
            """Find previous projects across saved conversations, with the owning thread id to resume."""
            owner, _key = context(runtime)
            return json.dumps(await self.projects(owner=owner), ensure_ascii=False)

        async def open_previous_project(project: str, runtime: ToolRuntime) -> str:
            """Open a previous project by resuming its conversation, preserving its existing files and context."""
            owner, key = context(runtime)
            try:
                target = await self.project_thread(project, owner=owner)
                return await self.request_resume(target, key=key, owner=owner)
            except ValueError as error:
                return str(error)

        return [
            StructuredTool.from_function(
                coroutine=function,
                return_direct=function in {resume_conversation, open_previous_project},
            )
            for function in (
                list_conversations,
                resume_conversation,
                list_previous_projects,
                open_previous_project,
            )
        ]
