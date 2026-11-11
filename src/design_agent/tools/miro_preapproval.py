"""Operator-configured approval for one Composio Miro account."""

import os
import re
from collections.abc import Mapping
from typing import Any


def miro_preapproved(name: str, args: Mapping[str, Any]) -> bool:
    account = os.environ.get("DESIGN_AGENT_COMPOSIO_MIRO_PREAPPROVED_ACCOUNT", "")
    if not account or name.upper() != "COMPOSIO_MULTI_EXECUTE_TOOL":
        return False
    calls = args.get("tools")
    return bool(
        isinstance(calls, list)
        and calls
        and all(
            isinstance(call, dict)
            and call.get("account") == account
            and isinstance(call.get("tool_slug"), str)
            and re.fullmatch(r"MIRO_[A-Z0-9_]+", call["tool_slug"])
            and isinstance(call.get("arguments"), dict)
            for call in calls
        )
    )
