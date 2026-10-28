"""The official TUI renders the design graph and exposes real delivery links."""

from pathlib import Path

import pytest

pytest.importorskip("deepagents_code")

from deepagents_code.tui.widgets.messages import AssistantMessage
from langgraph.store.memory import InMemoryStore

from design_agent.config import Settings
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.terminal import make_app, prepare
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import ScriptedChatModel, reply
from design_agent.tools.delivery import DeliveredFile, LocalOutputAdapter

ROOT = Path(__file__).resolve().parents[2]


async def test_official_tui_runs_design_graph_and_lists_download_links(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, workspace_root=tmp_path / "workspace", delivery_base_url="http://testserver"
    )
    _state, session = prepare(tmp_path, settings)
    store = InMemoryStore()
    runtime = build_local_runtime(
        settings,
        ROOT,
        model=ScriptedChatModel(steps=[lambda _m: reply("Design ready.")]),
        binding=folder_binding(tmp_path / "sandbox", ROOT),
        store=store,
    )
    LocalOutputAdapter(runtime.out_dir).deliver(
        "terminal-test", [DeliveredFile("preview.png", b"png")]
    )
    app = make_app(runtime, store, "terminal-test", ROOT, settings, session, prompt="Hello.")
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(100):
                await pilot.pause(0.05)
                if any(
                    "Design ready." in message._content for message in app.query(AssistantMessage)
                ):
                    break
            else:
                pytest.fail("The official TUI did not display the graph reply")
            await app._handle_command("/files")
            assert any(
                "http://testserver/files/" in message._content and "preview.png" in message._content
                for message in app.query(AssistantMessage)
            )
            await app._handle_command("/auth")
            assert any(
                "commands supported by Design Agent" in message._content
                for message in app.query(AssistantMessage)
            )
    finally:
        await runtime.services.aclose()
