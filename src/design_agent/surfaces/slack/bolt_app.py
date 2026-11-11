"""`design-agent slack`: the self-hosted Slack app, on Bolt, in one process with the agent.

What it does, mirroring the managed channel:

- A mention in a channel, or a direct message, starts a turn on the Slack thread's agent thread
  (`slack-<team>-<channel>-<thread ts>`). Turns on one thread queue behind each other; a follow-up
  that arrives while a turn runs is folded into it by the steering middleware.
- Slack delivers some events twice (an `app_mention` and a `message` for the same post, retries);
  each post starts at most one turn.
- Files attached to the message are fetched on the host with the bot token (Slack's own hosts
  only, size-capped) and put in the thread's `/workspace/attachments/`.
- The final reply posts to the thread unless it is `[quiet]`; wake-up replies (finished helpers,
  check-ins) post the same way. `upload_files` uploads finals to the thread and keeps a copy in
  `workspace/out/<thread>/` for the API.
- Live progress, the footer and steering come from the shared middleware, through a
  `BoltSlackResolver` that reads the Slack event this adapter puts on the run.

Only the installed workspace is served: an event from another team is ignored. Transport is
Socket Mode (`SLACK_APP_TOKEN`, no public URL) or signed HTTP (`SLACK_SIGNING_SECRET`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from design_agent.config import Settings
from design_agent.harness import notices
from design_agent.runtime.local import LocalRuntime, final_text
from design_agent.surfaces.slack.approval import (
    BUTTONS,
    REQUESTERS,
    blocks,
    preview,
    recover_requester,
    review_reply,
)
from design_agent.surfaces.slack.client import SlackClient, SlackError
from design_agent.tools.delivery import DeliveredFile, LocalOutputAdapter

_LOGGER = logging.getLogger(__name__)

THREAD = re.compile(r"^slack-(?P<team>[A-Z0-9]+)-(?P<channel>[A-Z0-9]+)-(?P<ts>\d+\.\d+)$")
_MENTION = re.compile(r"<@[A-Z0-9]+>")
MAX_FILES = 10
MAX_FILE_BYTES = 200 * 1024 * 1024
SEEN_KEPT = 2048


def thread_id_for(team: str, channel: str, thread_ts: str) -> str:
    return f"slack-{team}-{channel}-{thread_ts}"


def target_of(thread_id: str) -> tuple[str, str] | None:
    """The Slack channel and thread ts behind an agent thread id, or None for another surface."""
    match = THREAD.match(thread_id)
    return (match["channel"], match["ts"]) if match else None


class SlackFileDelivery:
    """`upload_files` for Slack threads: upload to the thread, and keep a local copy."""

    def __init__(
        self,
        token: str,
        local: LocalOutputAdapter,
        *,
        client: Any = None,
        target_for: Callable[[str], tuple[str, str] | None] = target_of,
    ) -> None:
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=token)
        self._client = client
        self._local = local
        self._target_for = target_for

    def deliver(self, thread_id: str, files: Sequence[DeliveredFile]) -> str | None:
        receipt = self._local.deliver(thread_id, files)
        target = self._target_for(thread_id)
        if target is None:
            return receipt
        channel, thread_ts = target
        self._client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            file_uploads=[
                {"content": f.content, "filename": f.name, "title": f.name} for f in files
            ],
        )
        return None


@dataclass
class SlackAdapter:
    """Turns Slack events into turns and replies (module doc)."""

    client: SlackClient
    team_id: str
    bot_user_id: str
    trigger_on_all_messages: bool = False
    runtime: LocalRuntime | None = None
    conversations: Any = None
    _active_threads: set[str] = field(default_factory=set)
    _routes: dict[str, tuple[str, str]] = field(default_factory=dict)
    _seen: OrderedDict[str, float] = field(default_factory=OrderedDict)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def accepts(self, event: Mapping[str, Any], *, mention: bool) -> bool:
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return False
        if str(event.get("user") or "") in ("", self.bot_user_id):
            return False
        team = str(event.get("team") or event.get("user_team") or self.team_id)
        if team != self.team_id:
            return False
        if mention:
            return True
        direct = event.get("channel_type") == "im"
        in_thread = bool(event.get("thread_ts"))
        native = thread_id_for(
            self.team_id,
            str(event.get("channel") or ""),
            str(event.get("thread_ts") or event.get("ts") or ""),
        )
        return (
            direct
            or (in_thread and native in self._active_threads)
            or (self.trigger_on_all_messages and in_thread)
        )

    def first_time(self, event: Mapping[str, Any]) -> bool:
        key = f"{event.get('channel')}:{event.get('ts')}"
        if key in self._seen:
            return False
        self._seen[key] = time.time()
        while len(self._seen) > SEEN_KEPT:
            self._seen.popitem(last=False)
        return True

    def dispatch(self, event: Mapping[str, Any], *, mention: bool) -> None:
        """Start handling an event without holding up Bolt's acknowledgement."""
        if not self.accepts(event, mention=mention) or not self.first_time(event):
            return
        native = thread_id_for(
            self.team_id, str(event["channel"]), str(event.get("thread_ts") or event["ts"])
        )
        self._active_threads.add(native)
        task = asyncio.get_running_loop().create_task(self.handle(event), name="slack turn")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle(self, event: Mapping[str, Any]) -> None:
        if self.runtime is None:
            raise RuntimeError("wire the runtime before serving")
        channel = str(event["channel"])
        thread_ts = str(event.get("thread_ts") or event["ts"])
        destination = thread_id_for(self.team_id, channel, thread_ts)
        owner = str(event.get("user") or "")
        thread = (
            await self.conversations.linked(destination) if self.conversations else None
        ) or destination
        self._routes[thread] = (channel, thread_ts)
        text = _MENTION.sub("", str(event.get("text") or "")).strip()
        if self.conversations:
            await self.conversations.touch(
                thread, text=text, owner=owner if thread == destination else None
            )
            if not await self.conversations.linked(destination):
                await self.conversations.link(destination, thread, owner=owner)
            command = text.split(maxsplit=1)
            action = command[0].lower().lstrip("!/") if command else ""
            if action in {"threads", "conversas", "projects", "projetos"}:
                rows = await (
                    self.conversations.projects(owner=owner)
                    if action in {"projects", "projetos"}
                    else self.conversations.listing(owner=owner)
                )
                lines = [
                    f"• {row.get('project') or row.get('title')} — `{row.get('thread_id') or row.get('id')}`"
                    for row in rows
                ]
                await self.post_direct(
                    destination,
                    "\n".join(lines) or "Nenhuma conversa ou projeto anterior encontrado.",
                )
                return
            if action in {"resume", "retomar", "project", "projeto"} and len(command) == 2:
                try:
                    target = await (
                        self.conversations.project_thread(command[1], owner=owner)
                        if action in {"project", "projeto"}
                        else self.conversations.resolve(command[1], owner=owner)
                    )
                    await self.conversations.link(destination, target, owner=owner)
                except ValueError as error:
                    await self.post_direct(destination, str(error))
                    return
                self._routes.pop(thread, None)
                self._routes[target] = (channel, thread_ts)
                await self.post_direct(
                    destination,
                    "Conversa retomada. Pode continuar neste fio sem @. Use `threads` ou `projetos` para consultar as anteriores.",
                )
                return
        review = await review_reply(
            self.runtime,
            thread=thread,
            requester=str(event.get("user") or ""),
            destination=thread_id_for(self.team_id, channel, thread_ts),
            text=text,
        )
        if review is not None:
            await self.post_review(thread, thread_id_for(self.team_id, channel, thread_ts), review)
            return
        try:
            await self.client.call(
                "reactions.add", {"channel": channel, "timestamp": event["ts"], "name": "eyes"}
            )
        except SlackError:
            pass  # presentation only
        attached = await self._attachments(thread, event)
        if attached:
            text = (text + "\n\nAttached: " + ", ".join(attached)).strip()
        try:
            added = await self.runtime.ask(
                text or "(no text)",
                thread_id=thread,
                configurable={
                    "slack_event": {**event, "team": self.team_id},
                    "navigation_key": "slack:" + destination,
                },
            )
        except Exception as error:  # noqa: BLE001 - the person hears that it failed, never silence
            _LOGGER.warning("[slack] the turn failed: %s", type(error).__name__)
            await self.post_direct(
                destination,
                "Something went wrong on my side; nothing was lost. Try again in a moment.",
            )
            return
        target = None
        if self.conversations:
            await self.conversations.touch(thread)
            target = await self.conversations.consume("slack:" + destination)
            if target:
                await self.conversations.link(destination, target, owner=owner)
                self._routes.pop(thread, None)
                self._routes[target] = (channel, thread_ts)
        response = await review_reply(
            self.runtime, thread=thread, requester=owner, destination=destination, new=True
        ) or final_text(added)
        if target:
            response = "Conversa retomada. Continue neste fio, sem precisar de @. O histórico e os arquivos anteriores estão disponíveis."
        await self.post_review(thread, destination, response)

    async def post_review(self, thread: str, destination: str, text: str) -> None:
        if self.runtime is None:
            return
        pending = await self.runtime.pending_approvals(thread)
        target = target_of(destination)
        if len(pending) != 1 or not target:
            await self.post_direct(destination, text)
            return
        binding = await recover_requester(self.runtime, thread, destination, pending[0]["id"])
        if binding is None:
            await self.post_direct(
                destination,
                "Não foi possível confirmar a origem desta aprovação. Revise esta conversa no terminal.",
            )
            return
        card = blocks(pending[0])
        if not card:
            await self.post_direct(destination, text)
            return
        if text != preview(pending[0]):
            card.insert(
                0,
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": text.split("\n", 1)[0][:2800]},
                },
            )
        try:
            result = await self.client.call(
                "chat.postMessage",
                {
                    "channel": target[0],
                    "thread_ts": target[1],
                    "text": text,
                    "blocks": card,
                },
            )
            store = self.runtime.graph.store
            if store and result.get("ts"):
                async with self.runtime._lock(thread):
                    binding = await store.aget(REQUESTERS, pending[0]["id"])
                    if binding is not None:
                        messages = [
                            *binding.value.get("message_timestamps", []),
                            binding.value.get("message_ts"),
                            result["ts"],
                        ]
                        await store.aput(
                            REQUESTERS,
                            pending[0]["id"],
                            {
                                **binding.value,
                                "message_ts": result["ts"],
                                "message_timestamps": list(dict.fromkeys(m for m in messages if m))[
                                    -50:
                                ],
                            },
                        )
        except SlackError as error:
            _LOGGER.warning("[slack] approval card failed: %s", error.code)

    async def handle_approval_action(self, body: Mapping[str, Any]) -> None:
        """Only Bolt-authenticated interactions from the original card and requester can decide."""
        if self.runtime is None or (body.get("team") or {}).get("id") != self.team_id:
            return
        actions = body.get("actions") or []
        if len(actions) != 1 or actions[0].get("action_id") not in BUTTONS:
            return
        action = actions[0]
        identifier = str(action.get("value") or "")
        store = self.runtime.graph.store
        binding = await store.aget(REQUESTERS, identifier) if store else None
        channel = str((body.get("channel") or {}).get("id") or "")
        message = body.get("message") or {}
        message_ts = str(message.get("ts") or "")
        requester = str((body.get("user") or {}).get("id") or "")
        destination = thread_id_for(
            self.team_id, channel, str(message.get("thread_ts") or message_ts)
        )
        if binding is None and store and message.get("user") == self.bot_user_id:
            route = await store.aget(("design_agent", "slack_conversations"), destination)
            if route is not None:
                binding = await recover_requester(
                    self.runtime, str(route.value["thread"]), destination, identifier
                )
        known_messages = (
            [*binding.value.get("message_timestamps", []), binding.value.get("message_ts")]
            if binding is not None
            else []
        )
        # Cards already visible before requester recovery have no saved message timestamps.
        legacy_card = not any(known_messages) and message.get("user") == self.bot_user_id
        if (
            binding is None
            or not message_ts
            or (message_ts not in known_messages and not legacy_card)
            or binding.value.get("requester") != requester
            or binding.value.get("destination") != destination
        ):
            await self.client.call(
                "chat.postEphemeral",
                {
                    "channel": channel,
                    "user": requester,
                    "text": "Este pedido não está disponível para você ou já foi concluído.",
                },
            )
            return
        _, command, scope = BUTTONS[action["action_id"]]
        thread = str(binding.value["thread"])
        response = await review_reply(
            self.runtime,
            thread=thread,
            requester=requester,
            destination=destination,
            text=f"{command} {identifier}",
            scope=scope,
        )
        pending = await self.runtime.pending_approvals(thread)
        if not any(item["id"] == identifier for item in pending):
            await self.client.call(
                "chat.update",
                {
                    "channel": channel,
                    "ts": message_ts,
                    "text": f"Revisão concluída: {BUTTONS[action['action_id']][0]}.",
                    "blocks": [],
                },
            )
        await self.post_review(thread, destination, response or "Revisão concluída.")

    async def _attachments(self, thread: str, event: Mapping[str, Any]) -> list[str]:
        if self.runtime is None:
            return []
        files: list[tuple[str, bytes]] = []
        for item in list(event.get("files") or [])[:MAX_FILES]:
            url = str(item.get("url_private_download") or item.get("url_private") or "")
            name = Path(str(item.get("name") or "attachment")).name
            try:
                files.append((name, await self.client.download(url, limit_bytes=MAX_FILE_BYTES)))
            except SlackError as error:
                _LOGGER.warning("[slack] an attachment was not fetched: %s", error.code)
        return await self.runtime.attach(thread, files) if files else []

    def delivery_target(self, thread: str) -> tuple[str, str] | None:
        from langgraph.config import get_config

        try:
            config = get_config().get("configurable", {})
        except RuntimeError:
            config = {}
        if config.get("interactive_surface") == "terminal":
            return None
        event = config.get("slack_event") or {}
        target = (
            (str(event["channel"]), str(event.get("thread_ts") or event["ts"]))
            if event.get("channel") and event.get("ts")
            else self._routes.get(thread) or target_of(thread)
        )
        if self.conversations and target:
            native = thread_id_for(self.team_id, target[0], target[1])
            if (
                self.conversations.bindings.get(native, thread) != thread
                or self.conversations.surfaces.get(thread) == "terminal"
            ):
                return None
        return target

    async def post_direct(self, destination: str, text: str) -> None:
        await self.post(destination, text, direct=True)

    async def post(self, thread: str, text: str, *, direct: bool = False) -> None:
        target = target_of(thread) if direct else self._routes.get(thread) or target_of(thread)
        if self.conversations and not direct and target is not None:
            native = thread_id_for(self.team_id, target[0], target[1])
            linked = await self.conversations.linked(native)
            if (linked is not None and linked != thread) or self.conversations.surfaces.get(
                thread
            ) == "terminal":
                return
        if target is None or not text or not notices.should_post(text):
            return
        channel, thread_ts = target
        try:
            await self.client.call(
                "chat.postMessage", {"channel": channel, "thread_ts": thread_ts, "text": text}
            )
        except SlackError as error:
            _LOGGER.warning("[slack] a reply was not posted: %s", error.code)

    def post_soon(self, thread: str, text: str) -> None:
        """The runtime's `replies` callback for wake-up turns (synchronous by contract)."""
        task = asyncio.get_running_loop().create_task(self.post(thread, text), name="slack reply")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def register(app: Any, adapter: SlackAdapter) -> None:
    """Bolt handlers: mentions everywhere, direct messages, and thread replies when configured."""

    async def on_mention(event: dict[str, Any]) -> None:
        adapter.dispatch(event, mention=True)

    async def on_message(event: dict[str, Any]) -> None:
        mentioned = f"<@{adapter.bot_user_id}>" in str(event.get("text") or "")
        adapter.dispatch(event, mention=mentioned)

    app.event("app_mention")(on_mention)
    app.event("message")(on_message)

    async def on_approval(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        _LOGGER.info("[slack] tool approval interaction received")

        async def process() -> None:
            try:
                await adapter.handle_approval_action(body)
            except Exception as error:  # noqa: BLE001 - report transport failure without secrets
                _LOGGER.warning("[slack] tool approval failed: %s", type(error).__name__)

        task = asyncio.create_task(process(), name="slack tool approval")
        adapter._tasks.add(task)
        task.add_done_callback(adapter._tasks.discard)

    app.action(re.compile(r"^tool_approval_(once|session|always|reject)$"))(on_approval)


async def serve(project_root: Path, settings: Settings) -> None:
    """Run the Slack app until stopped (module doc)."""
    from slack_bolt.async_app import AsyncApp

    from design_agent.runtime import maintenance
    from design_agent.runtime.local import build_local_runtime
    from design_agent.runtime.persistence import open_persistence, workspace_of
    from design_agent.surfaces.slack import BoltSlackResolver, SlackSurface
    from design_agent.tools.mcp import load_mcp_tools, servers_for_runtime
    from design_agent.tools.paper import PaperPolicy, admit_paper_tools

    if settings.slack_bot_token is None:
        raise SystemExit("SLACK_BOT_TOKEN is not set")
    token = settings.slack_bot_token.get_secret_value()
    client = SlackClient(token)
    identity = await client.identity()
    adapter = SlackAdapter(
        client,
        identity.team_id,
        identity.user_id,
        settings.slack_trigger_on_all_messages,
    )
    servers = servers_for_runtime(project_root, settings, "self_hosted")
    loaded = await load_mcp_tools(servers)
    servers = [s for s in servers if s.name not in loaded.failed]
    tools = admit_paper_tools(loaded.tools, PaperPolicy.from_settings(settings))
    workspace = workspace_of(settings, project_root)

    async with open_persistence(settings, workspace) as (checkpointer, store):
        from design_agent.surfaces.conversations import BINDINGS, Conversations

        directory = Conversations(
            checkpointer, store, project_root, operator=settings.slack_owner_user_id
        )
        runtime = build_local_runtime(
            settings,
            project_root,
            checkpointer=checkpointer,
            store=store,
            replies=adapter.post_soon,
            mcp_servers=servers,
            mcp_tools=[*tools, *directory.tools()],
            slack=SlackSurface(
                BoltSlackResolver(token),
                # The adapter admits only mentioned DMs/activated threads; follow-ups there
                # belong to this conversation even when the person omits the mention.
                mention_required=False,
            ),
            surface="slack",
            delivery=SlackFileDelivery(
                token,
                LocalOutputAdapter(workspace / "out", base_url=settings.delivery_base_url),
                target_for=adapter.delivery_target,
            ),
        )
        adapter.runtime = runtime
        adapter.conversations = directory
        runtime.conversations = directory
        directory.runtime = runtime
        await directory.bootstrap()
        for row in await directory.listing(limit=1000):
            if target_of(row["id"]):
                native = row["id"]
                if not await directory.linked(native):
                    await directory.link(
                        native, native, owner=row.get("owner") or settings.slack_owner_user_id or ""
                    )
        for item in await store.asearch(BINDINGS, limit=1000):
            directory.bindings[item.key] = item.value["thread"]
            adapter._active_threads.add(item.key)
            if target := target_of(item.key):
                adapter._routes[item.value["thread"]] = target
        await maintenance.start(runtime, store)
        keeper = asyncio.get_running_loop().create_task(
            maintenance.run(runtime, store), name="maintenance"
        )
        signing = (
            settings.slack_signing_secret.get_secret_value()
            if settings.slack_signing_secret
            else None
        )
        app = AsyncApp(
            token=token, signing_secret=signing, request_verification_enabled=signing is not None
        )
        register(app, adapter)
        shared_server = None
        shared_task = None
        if settings.shared_runtime_url:
            import uvicorn

            from design_agent.surfaces.shared import create_shared_app, local_endpoint

            if settings.shared_runtime_token is None:
                raise ValueError("The shared runtime token is missing")
            host, port = local_endpoint(settings.shared_runtime_url)
            info = [
                {
                    "name": server.name,
                    "transport": server.transport,
                    "status": "error" if server.name in loaded.failed else "ok",
                    "error": loaded.failed.get(server.name),
                    "tools": [
                        {"name": tool.name, "description": tool.description}
                        for tool in tools
                        if tool.name.startswith(server.name + "_")
                    ],
                }
                for server in servers_for_runtime(project_root, settings, "self_hosted")
            ]
            shared_app = create_shared_app(
                runtime,
                store,
                token=settings.shared_runtime_token.get_secret_value(),
                thread=settings.shared_thread_id or "default",
                mcp_info=info,
                conversations=directory,
            )
            shared_server = uvicorn.Server(
                uvicorn.Config(
                    shared_app, host=host, port=port, access_log=False, log_level="warning"
                )
            )
            shared_task = asyncio.create_task(shared_server.serve(), name="shared design runtime")
        try:
            await _serve_transport(app, settings)
        finally:
            if shared_server is not None:
                shared_server.should_exit = True
            if shared_task is not None:
                await shared_task
            keeper.cancel()
            await runtime.services.aclose()
            if runtime.binding.runner is not None:
                runtime.binding.runner.stop_all()
            await client.aclose()


async def _serve_transport(app: Any, settings: Settings) -> None:
    if settings.slack_transport == "socket_mode":
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        if settings.slack_app_token is None:
            raise SystemExit("Socket Mode needs SLACK_APP_TOKEN (or set SLACK_TRANSPORT=http)")
        socket: Any = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
        await socket.start_async()
        return
    if settings.slack_signing_secret is None:
        raise SystemExit("HTTP mode needs SLACK_SIGNING_SECRET to verify Slack's requests")
    import uvicorn
    from fastapi import FastAPI, Request
    from slack_bolt.adapter.starlette.async_handler import AsyncSlackRequestHandler

    handler = AsyncSlackRequestHandler(app)
    web = FastAPI(docs_url=None, redoc_url=None)

    @web.post("/slack/events")
    async def events(request: Request) -> Any:
        return await handler.handle(request)

    server = uvicorn.Server(
        uvicorn.Config(web, host=settings.api_host, port=settings.api_port, log_level="info")
    )
    await server.serve()
