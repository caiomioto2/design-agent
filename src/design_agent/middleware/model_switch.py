"""Per-conversation model selection, shared by the Slack and terminal surfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from design_agent.models.factory import build_chat_model

NAMESPACE = ("design_agent", "thread_model")
MODELS = {"terra": "openai:gpt-6.1-terra", "sol": "openai:gpt-6.1-sol"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


async def select_model(store: Any, thread: str, text: str) -> str:
    if store is None or not thread:
        raise ValueError("Esta conversa não possui armazenamento de modelo.")
    parts = text.lower().split()
    if not parts:
        entry = await store.aget(NAMESPACE, thread)
        return (
            ("Modelo desta conversa: " + entry.value["model"] + " · " + entry.value["effort"])
            if entry
            else "Esta conversa usa o modelo padrão do serviço."
        )
    if parts == ["padrao"] or parts == ["padrão"] or parts == ["default"]:
        await store.adelete(NAMESPACE, thread)
        return "Esta conversa voltou a usar o modelo padrão do serviço."
    if len(parts) > 2 or parts[0] not in MODELS or (len(parts) == 2 and parts[1] not in EFFORTS):
        raise ValueError("Use: modelo terra medium, modelo sol high ou modelo padrão.")
    effort = parts[1] if len(parts) == 2 else "medium"
    await store.aput(NAMESPACE, thread, {"model": MODELS[parts[0]], "effort": effort})
    return (
        f"Modelo desta conversa: {MODELS[parts[0]]} · {effort}. Vale a partir da próxima mensagem."
    )


class ThreadModelMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, settings: Any, root: Any) -> None:
        super().__init__()
        self.settings = settings
        self.root = root

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        from langgraph.config import get_config

        thread = str(get_config().get("configurable", {}).get("thread_id") or "")
        store = request.runtime.store
        entry = await store.aget(NAMESPACE, thread) if store is not None and thread else None
        if entry:
            settings = self.settings.model_copy(
                update={"model": entry.value["model"], "model_effort": entry.value["effort"]}
            )
            model = build_chat_model(settings, "lead", project_root=self.root)
            request = request.override(model=model)
        return await handler(request)
