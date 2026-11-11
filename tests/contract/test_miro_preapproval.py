from design_agent.tools.miro_preapproval import miro_preapproved


def test_only_explicit_miro_account_and_unmixed_operations(monkeypatch):
    monkeypatch.setenv("DESIGN_AGENT_COMPOSIO_MIRO_PREAPPROVED_ACCOUNT", "miro-test")
    call = {"tool_slug": "MIRO_GET_BOARDS2", "account": "miro-test", "arguments": {}}
    assert miro_preapproved("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [call]})
    assert not miro_preapproved(
        "COMPOSIO_MULTI_EXECUTE_TOOL",
        {"tools": [call, {**call, "tool_slug": "SLACK_SEND_MESSAGE"}]},
    )
    assert not miro_preapproved(
        "COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{**call, "account": "other"}]}
    )
    assert not miro_preapproved("COMPOSIO_REMOTE_BASH_TOOL", {"tools": [call]})
    monkeypatch.delenv("DESIGN_AGENT_COMPOSIO_MIRO_PREAPPROVED_ACCOUNT")
    assert not miro_preapproved("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [call]})
