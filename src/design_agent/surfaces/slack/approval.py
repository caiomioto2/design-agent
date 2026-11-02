"""Exact, durable approvals from the Slack person who requested the external action."""

from __future__ import annotations

import json
import re
from typing import Any

from design_agent.runtime.local import LocalRuntime
from design_agent.tools.approvals import Scope, permission, principal, revoke, save

REQUESTERS = ("design_agent", "slack_approvals")
DECISIONS = {"aprovar": "approve", "approve": "approve", "rejeitar": "reject", "reject": "reject"}
BUTTONS: dict[str, tuple[str, str, Scope]] = {
    "tool_approval_once": ("Aprovar uma vez", "aprovar", "once"),
    "tool_approval_session": ("Aprovar nesta sessão", "aprovar", "session"),
    "tool_approval_always": ("Aprovar sempre", "aprovar", "always"),
    "tool_approval_reject": ("Rejeitar", "rejeitar", "once"),
}


class SavedApprovalUnavailable(ValueError):
    """A tool cannot safely be covered by a saved permission."""


def owner_of(destination: str, requester: str) -> str:
    match = re.match(r"^slack-([A-Z0-9]+)-", destination)
    return principal(match[1], requester) if match else ""


async def recover_requester(
    runtime: LocalRuntime, thread: str, destination: str, identifier: str
) -> Any:
    """Recover an unbound legacy request only from matching host-owned conversation records."""
    store = runtime.graph.store
    if store is None:
        return None
    async with runtime._lock(thread):
        binding = await store.aget(REQUESTERS, identifier)
        if binding is not None:
            return binding
        pending = await runtime.pending_approvals(thread)
        if len(pending) != 1 or pending[0]["id"] != identifier:
            return None
        route = await store.aget(("design_agent", "slack_conversations"), destination)
        conversation = await store.aget(("design_agent", "conversations"), thread)
        if (
            route is None
            or conversation is None
            or route.value.get("thread") != thread
            or not route.value.get("owner")
            or route.value["owner"] != conversation.value.get("owner")
        ):
            return None
        await store.aput(
            REQUESTERS,
            identifier,
            {"thread": thread, "requester": route.value["owner"], "destination": destination},
        )
        return await store.aget(REQUESTERS, identifier)


def blocks(pending: dict[str, Any]) -> list[dict[str, Any]]:
    """Slack cards carry only the interrupt ID; arguments are loaded from the checkpoint."""
    details = json.dumps(pending["value"]["action_requests"], ensure_ascii=False, indent=2)
    if len(details) > 30_000:
        return []
    result: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "plain_text", "text": "Revise as ações antes de aprovar."},
        },
        *[
            {
                "type": "section",
                "text": {"type": "plain_text", "text": details[start : start + 2800]},
            }
            for start in range(0, len(details), 2800)
        ],
        {
            "type": "context",
            "elements": [
                {
                    "type": "plain_text",
                    "text": (
                        "Esta aprovação MCP vale somente para esta chamada."
                        if pending["value"].get("type") == "mcp_legacy_approval"
                        else "Sessão = esta conversa. Sempre = esta pessoa e operação em outras conversas. "
                        "Use ‘revogar aprovacoes’ para remover permissões salvas."
                    ),
                }
            ],
        },
    ]
    buttons = []
    for action, (label, _, scope) in BUTTONS.items():
        if pending["value"].get("type") == "mcp_legacy_approval" and scope != "once":
            continue
        button: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": action,
            "value": pending["id"],
        }
        if action == "tool_approval_once":
            button["style"] = "primary"
        if scope in {"session", "always"}:
            button["confirm"] = {
                "title": {"type": "plain_text", "text": label},
                "text": {
                    "type": "plain_text",
                    "text": "As operações exibidas poderão executar com outros argumentos, "
                    + (
                        "nesta conversa."
                        if scope == "session"
                        else "também em outras conversas suas, até revogar."
                    ),
                },
                "confirm": {"type": "plain_text", "text": "Permitir"},
                "deny": {"type": "plain_text", "text": "Cancelar"},
            }
        buttons.append(button)
    result.append({"type": "actions", "elements": buttons})
    return result


def preview(pending: dict[str, Any]) -> str:
    actions = pending["value"]["action_requests"]
    details = json.dumps(actions, ensure_ascii=False, indent=2)
    if len(details) > 30_000:
        return (
            "A revisão é grande demais para este fio. Abra esta conversa no terminal para revisar."
        )
    identifier = pending["id"]
    if pending["value"].get("type") == "mcp_legacy_approval":
        return (
            f"Revise esta chamada MCP:\n{details}\n"
            f"Responda `aprovar {identifier}` uma vez ou `rejeitar {identifier}`."
        )
    return (
        "Revise o destino, o conteúdo e o efeito destas ações antes de executar:\n"
        f"```{details}```\n"
        f"Para executar exatamente estas ações, responda `aprovar {identifier}`. "
        f"Para cancelar, responda `rejeitar {identifier}`.\n"
        "Para permitir estas operações com outros argumentos, use "
        f"`aprovar sessao {identifier}` nesta conversa ou "
        f"`aprovar sempre {identifier}` também nas suas outras conversas. "
        "Use `revogar aprovacoes` para remover permissões salvas."
    )


async def review_reply(
    runtime: LocalRuntime,
    *,
    thread: str,
    requester: str,
    destination: str,
    text: str = "",
    new: bool = False,
    scope: Scope = "once",
) -> str | None:
    """Consume an explicit review command or show the pending request; never infer consent."""
    owner = owner_of(destination, requester)
    if text.strip().casefold() in {"revogar aprovacoes", "revogar aprovações"}:
        if not owner or runtime.graph.store is None:
            return "Não foi possível identificar suas permissões salvas."
        async with runtime._lock(thread):
            count = await revoke(runtime.graph.store, owner, thread)
        return f"Permissões salvas revogadas: {count}. Novas ações voltarão a pedir aprovação."
    parts = text.strip().split()
    decision = DECISIONS.get(parts[0].lower()) if parts else None
    scopes: dict[str, Scope] = {"sessao": "session", "sessão": "session", "sempre": "always"}
    if decision == "approve" and len(parts) == 3 and parts[1].casefold() in scopes:
        scope = scopes[parts[1].casefold()]
        parts = [parts[0], parts[2]]
    pending = await runtime.pending_approvals(thread)
    if not pending:
        return (
            "Nenhuma aprovação pendente neste fio; nenhuma ação foi executada."
            if decision
            else None
        )
    if len(pending) != 1 or not isinstance(pending[0]["value"], dict):
        return "Esta conversa precisa de revisão no terminal antes de continuar."
    item = pending[0]
    if item["value"].get("type") == "mcp_legacy_approval" and scope != "once":
        return "Esta aprovação MCP vale somente para esta chamada. Use ‘Aprovar uma vez’."
    actions = item["value"].get("action_requests")
    if not actions:
        return "Esta conversa precisa de revisão no terminal antes de continuar."
    store = runtime.graph.store
    if store is None:
        return "A aprovação precisa de armazenamento persistente; revise esta conversa no terminal."
    binding = await store.aget(REQUESTERS, item["id"])
    if binding is None and not new:
        binding = await recover_requester(runtime, thread, destination, item["id"])
    if new and binding is None:
        await store.aput(
            REQUESTERS,
            item["id"],
            {"thread": thread, "requester": requester, "destination": destination},
        )
        return preview(item)
    if not decision:
        return preview(item)
    expected = {"thread": thread, "requester": requester, "destination": destination}
    if binding is None or any(binding.value.get(k) != v for k, v in expected.items()):
        return "Somente a pessoa que solicitou estas ações pode aprová-las, no fio original."
    if len(parts) != 2 or parts[1] != item["id"]:
        return "A aprovação não corresponde ao pedido pendente.\n" + preview(item)

    async def remember(actions: list[dict[str, Any]]) -> None:
        current = await store.aget(REQUESTERS, item["id"])
        if current is None or any(current.value.get(k) != v for k, v in expected.items()):
            raise ValueError("Approval requester changed")
        if scope != "once":
            try:
                policies = [
                    permission(
                        runtime.components.mcp_servers,
                        runtime.components.tools,
                        action["name"],
                        action["args"],
                    )
                    for action in actions
                ]
            except (ValueError, TypeError) as error:
                raise SavedApprovalUnavailable from error
            await save(store, owner, thread, policies, scope)

    if decision == "reject" and scope != "once":
        return "Uma rejeição não pode salvar permissões."
    try:
        added = await runtime.review(
            thread,
            interrupt_id=item["id"],
            decisions=[{"type": decision} for _ in actions],
            before_resume=remember,
            configurable={
                "slack_event": {"team": owner.split(":")[1] if owner else "", "user": requester}
            },
        )
    except SavedApprovalUnavailable:
        return "Esta operação exige revisão individual. Use o botão ‘Aprovar uma vez’."
    except ValueError:
        return "Este pedido de aprovação já mudou ou foi concluído. Nenhuma ação foi repetida."
    await store.adelete(REQUESTERS, item["id"])
    following = await review_reply(
        runtime, thread=thread, requester=requester, destination=destination, new=True
    )
    if following:
        return following
    from design_agent.runtime.local import final_text

    return final_text(added) or (
        "Revisão concluída." if decision == "approve" else "Ações canceladas."
    )
