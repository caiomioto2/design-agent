"""Run the upstream Deep Agents Code TUI against the Design Agent graph."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from design_agent.config import Settings


def prepare(root: Path, settings: Settings) -> tuple[Path, Path]:
    from dotenv import load_dotenv

    from design_agent.runtime.persistence import workspace_of

    state = workspace_of(settings, root) / "state"
    profile = state / "deepagents-code-profile"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["DEEPAGENTS_HOME"] = str(profile)
    for name, value in {
        "AUTO_UPDATE": "false",
        "NO_UPDATE_CHECK": "true",
        "PRICES_AUTO_UPDATE": "false",
        "SHOW_REASONING": "false",
        "SHOW_HEADER": "true",
    }.items():
        os.environ["DEEPAGENTS_CODE_" + name] = value
    load_dotenv(root / ".env", override=True)
    os.environ["OPENAI_BASE_URL"] = settings.model_base_url or "https://api.openai.com/v1"
    return state, state / "design-agent-terminal-session.json"


def save_session(path: Path, thread: str) -> None:
    with path.open("w", encoding="utf-8") as file:
        path.chmod(0o600)
        file.write(json.dumps({"thread": thread}))


class DesignGraph:
    """Adapt the native TUI's Store interface to the local persistent runtime."""

    def __init__(self, runtime: Any, store: Any, session: Path) -> None:
        self.runtime = runtime
        self.store = store
        self.session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime.graph, name)

    async def aput_store_item(
        self, namespace: tuple[str, ...], key: str, value: dict[str, Any]
    ) -> None:
        await self.store.aput(tuple(namespace), key, value)

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        thread = config["configurable"]["thread_id"]
        save_session(self.session, thread)
        config = {**config, "recursion_limit": 9999}
        kwargs.pop("context", None)
        async with self.runtime._lock(thread):
            async for event in self.runtime.graph.astream(input, config=config, **kwargs):
                yield event


def make_app(
    runtime: Any,
    store: Any,
    thread: str,
    root: Path,
    settings: Settings,
    session: Path,
    prompt: str | None = None,
    *,
    mcp_info: list[Any] | None = None,
) -> Any:
    from deepagents_code.app import DeepAgentsApp
    from deepagents_code.config import runtime_state
    from deepagents_code.tui.widgets.messages import AssistantMessage

    from design_agent.models.factory import resolve_model

    resolved = resolve_model(settings)
    runtime_state.model_name = resolved.spec.model
    runtime_state.model_provider = resolved.spec.provider
    runtime_state.model_context_limit = resolved.caps.context_window

    class DesignTerminal(DeepAgentsApp):
        """Keep coding-specific remote commands from switching this local design graph."""

        CSS_PATH = str(files("deepagents_code").joinpath("app.tcss"))

        async def _handle_command(self, command: str) -> None:
            cmd = command.lower().strip()
            if cmd == "/files":
                from design_agent.surfaces.files import download_url
                from design_agent.tools.delivery import LocalOutputAdapter

                folder = LocalOutputAdapter(runtime.out_dir).folder(self._lc_thread_id or thread)
                rows = []
                if folder.is_dir():
                    for path in sorted(folder.iterdir()):
                        if path.is_file():
                            url = (
                                download_url(
                                    runtime.out_dir,
                                    settings.delivery_base_url,
                                    folder.name,
                                    path.name,
                                )
                                if settings.delivery_base_url
                                else str(path.resolve())
                            )
                            rows.append(f"- [{path.name}]({url})")
                await self._mount_message(
                    AssistantMessage("\n".join(rows) or "No files delivered in this thread yet.")
                )
            elif cmd in {"/help", "/model"}:
                await self._mount_message(
                    AssistantMessage(
                        f"Design Agent · {settings.model}\n\n"
                        "/files opens delivered files; /mcp shows integrations; /clear starts a new thread; /quit exits.\n\n"
                        "Continue a saved thread with `design-agent chat --thread ID`. Change the model in `.env` and restart."
                    )
                )
            elif cmd in {"/mcp", "/clear", "/quit", "/q"}:
                await super()._handle_command(command)
            else:
                await self._mount_message(
                    AssistantMessage("Use /help for the commands supported by Design Agent.")
                )

    return DesignTerminal(
        agent=cast(Any, DesignGraph(runtime, store, session)),
        assistant_id="design-agent",
        backend=runtime.binding.graph_backend,
        cwd=root,
        thread_id=thread,
        initial_prompt=prompt,
        mcp_server_info=mcp_info,
        title="Design Agent",
        sub_title="Deep Agents Code · ChatGPT",
    )


async def run(
    root: Path, settings: Settings, *, thread: str | None = None, new: bool = False
) -> None:
    state, session = prepare(root, settings)
    logging.basicConfig(filename=state / "design-agent-terminal.log", level=logging.WARNING)

    from deepagents_code.tui.widgets.messages import AssistantMessage

    from design_agent.runtime.local import build_local_runtime
    from design_agent.runtime.persistence import open_persistence, workspace_of
    from design_agent.tools.mcp import load_mcp_tools, servers_for_runtime
    from design_agent.tools.paper import PaperPolicy, admit_paper_tools

    os.chdir(root)
    if not thread and not new and session.exists():
        thread = json.loads(session.read_text()).get("thread")
    thread = thread or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", thread):
        raise SystemExit("Identificador de conversa inválido")
    servers = servers_for_runtime(root, settings, "local")
    loaded = await load_mcp_tools(servers)
    loaded.tools = admit_paper_tools(loaded.tools, PaperPolicy.from_settings(settings))
    from deepagents_code.mcp_tools import MCPServerInfo, MCPToolInfo

    mcp_info = [
        MCPServerInfo(
            name=server.name,
            transport=server.transport,
            status="error" if server.name in loaded.failed else "ok",
            error=loaded.failed.get(server.name),
            tools=tuple(
                MCPToolInfo(name=tool.name, description=tool.description)
                for tool in loaded.tools
                if tool.name.startswith(server.name + "_")
            )
            if server.name not in loaded.failed
            else (),
        )
        for server in servers
    ]
    servers = [s for s in servers if s.name not in loaded.failed]
    holder: list[Any] = []

    def post(_thread: str, text: str) -> None:
        if holder:
            app = holder[0]
            app.run_worker(app._mount_message(AssistantMessage(text)), exit_on_error=False)

    async with open_persistence(settings, workspace_of(settings, root)) as (checkpointer, store):
        runtime = build_local_runtime(
            settings,
            root,
            checkpointer=checkpointer,
            store=store,
            replies=post,
            mcp_servers=servers,
            mcp_tools=loaded.tools,
        )
        app = make_app(runtime, store, thread, root, settings, session, mcp_info=mcp_info)
        holder.append(app)
        await runtime.waker.restore(thread)
        save_session(session, thread)
        try:
            await app.run_async()
        finally:
            save_session(session, app._lc_thread_id or thread)
            await runtime.services.aclose()
            if runtime.binding.runner:
                await asyncio.to_thread(runtime.binding.runner.stop_all)
