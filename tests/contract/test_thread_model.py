from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from design_agent.config import Settings
from design_agent.middleware.model_switch import NAMESPACE, ThreadModelMiddleware, select_model


async def test_choices_are_isolated_and_can_be_reset():
    store = InMemoryStore()
    assert "padrão" in await select_model(store, "one", "")
    await select_model(store, "one", "terra medium")
    await select_model(store, "two", "sol high")
    assert (await store.aget(NAMESPACE, "one")).value["model"] == "openai:gpt-6.1-terra"
    assert (await store.aget(NAMESPACE, "two")).value["effort"] == "high"
    with pytest.raises(ValueError):
        await select_model(store, "one", "unknown medium")
    await select_model(store, "one", "padrão")
    assert await store.aget(NAMESPACE, "one") is None
    assert await store.aget(NAMESPACE, "two") is not None


async def test_selected_model_reaches_model_request(monkeypatch, tmp_path):
    store = InMemoryStore()
    await select_model(store, "one", "terra medium")
    monkeypatch.setattr(
        "langgraph.config.get_config", lambda: {"configurable": {"thread_id": "one"}}
    )
    captured = {}

    def build(settings, role, project_root):
        captured.update(model=settings.model, effort=settings.model_effort)
        return "selected-model"

    monkeypatch.setattr("design_agent.middleware.model_switch.build_chat_model", build)

    class Request:
        runtime = SimpleNamespace(store=store)

        def override(self, **kwargs):
            captured.update(kwargs)
            return self

    async def handler(request):
        return request

    middleware = ThreadModelMiddleware(Settings(_env_file=None), tmp_path)
    await middleware.awrap_model_call(Request(), handler)
    assert captured == {"model": "selected-model", "effort": "medium"}


async def test_slack_owner_can_change_thread_model_but_other_people_cannot(tmp_path):
    from pathlib import Path

    from design_agent.runtime.local import build_local_runtime
    from design_agent.surfaces.slack.bolt_app import SlackAdapter, thread_id_for
    from design_agent.testing.fake_slack import BOT, CHANNEL, PERSON, TEAM, FakeSlack
    from design_agent.testing.fakes import folder_binding
    from design_agent.testing.scripted_model import RespondingChatModel, reply

    root = Path(__file__).resolve().parents[2]
    store = InMemoryStore()
    settings = Settings(_env_file=None, workspace_root=tmp_path / "ws", slack_owner_user_id=PERSON)
    runtime = build_local_runtime(
        settings,
        root,
        model=RespondingChatModel(respond=lambda _: reply("ok")),
        binding=folder_binding(tmp_path / "sandbox", root),
        store=store,
    )
    fake = FakeSlack()
    adapter = SlackAdapter(fake.client(), TEAM, BOT, runtime=runtime)
    event = {
        "user": PERSON,
        "team": TEAM,
        "channel": CHANNEL,
        "ts": "1712345678.000101",
        "text": "modelo terra medium",
    }
    try:
        await adapter.handle(event)
        thread = thread_id_for(TEAM, CHANNEL, event["ts"])
        assert (await store.aget(NAMESPACE, thread)).value["model"] == "openai:gpt-6.1-terra"
        await adapter.handle(
            {
                **event,
                "user": "U0TESTOTHER",
                "ts": "1712345678.000102",
                "thread_ts": event["ts"],
                "text": "modelo sol high",
            }
        )
        assert (await store.aget(NAMESPACE, thread)).value["model"] == "openai:gpt-6.1-terra"
    finally:
        await runtime.services.aclose()


async def test_shared_api_and_graph_use_selected_model(tmp_path, monkeypatch):
    from pathlib import Path

    import httpx

    from design_agent.runtime.local import build_local_runtime, final_text
    from design_agent.surfaces.shared import SharedGraph, create_shared_app
    from design_agent.testing.fakes import folder_binding
    from design_agent.testing.scripted_model import RespondingChatModel, reply

    root = Path(__file__).resolve().parents[2]
    store = InMemoryStore()
    settings = Settings(_env_file=None, workspace_root=tmp_path / "ws")
    base = RespondingChatModel(respond=lambda _: reply("base model reply"))
    selected = RespondingChatModel(respond=lambda _: reply("selected model reply"))
    monkeypatch.setattr(
        "design_agent.middleware.model_switch.build_chat_model", lambda *a, **k: selected
    )
    runtime = build_local_runtime(
        settings, root, model=base, binding=folder_binding(tmp_path / "sb", root), store=store
    )
    token = "test-shared-runtime-token-" + "x" * 32
    app = create_shared_app(runtime, store, token=token, thread="thread-a")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            graph = SharedGraph(client)
            assert "terra" in await graph.select_model("thread-a", "terra medium")
            assert "selected model reply" in final_text(
                await runtime.ask("hello", thread_id="thread-a")
            )
            assert "base model reply" in final_text(
                await runtime.ask("hello", thread_id="thread-b")
            )
    finally:
        await runtime.services.aclose()
