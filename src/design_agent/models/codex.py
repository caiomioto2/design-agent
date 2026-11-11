"""OpenAI chat transport for a Codex subscription endpoint."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_openai import ChatOpenAI


class CodexChatOpenAI(ChatOpenAI):
    """Codex accepts developer instructions rather than system input messages."""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = super()._get_request_payload(input_, stop=stop, **kwargs)
        for item in payload.get("input", []):
            if item.get("role") == "system":
                item["role"] = "developer"
        return payload
