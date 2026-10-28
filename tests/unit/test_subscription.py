"""Subscription setup keeps paid endpoint keys intact and credentials outside the sandbox."""

from pathlib import Path

import click
import pytest

from design_agent import subscription
from design_agent.config import Settings
from design_agent.envfile import read_env


def example(root: Path) -> None:
    source = Path(__file__).resolve().parents[2] / ".env.example"
    (root / ".env.example").write_bytes(source.read_bytes())


def test_setup_configures_text_and_images_without_an_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example(tmp_path)
    monkeypatch.setattr(subscription, "proxy_module", lambda: Path("/installed/proxy.js"))
    subscription.configure(tmp_path, model="gpt-6.1-sol", port=1456)
    env = read_env(tmp_path)
    assert env["DESIGN_AGENT_MODEL_API_MODE"] == "codex"
    assert env["DESIGN_AGENT_MODEL_BASE_URL"] == env["DESIGN_AGENT_IMAGE_BASE_URL"]
    assert len(env["OPENAI_API_KEY"]) >= 43
    assert env["DESIGN_AGENT_ENABLE_TRACES"] == "0"
    subscription.configure(tmp_path, model="gpt-6.1-sol", port=1456)
    assert read_env(tmp_path)["OPENAI_API_KEY"] == env["OPENAI_API_KEY"]


def test_setup_refuses_to_replace_an_api_account_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example(tmp_path)
    monkeypatch.setattr(subscription, "proxy_module", lambda: Path("/installed/proxy.js"))
    original = "OPENAI_API_KEY=test-other-endpoint\n"
    (tmp_path / ".env").write_text(original)
    with pytest.raises(click.ClickException, match="separate checkout"):
        subscription.configure(tmp_path, model="gpt-6.1-sol", port=1456)
    assert (tmp_path / ".env").read_text() == original


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:1456/v1",
        "https://example.com:443/v1",
        "http://user:password@127.0.0.1:1456/v1",
    ],
)
def test_managed_proxy_only_binds_loopback(url: str) -> None:
    with pytest.raises(click.ClickException, match="127.0.0.1"):
        subscription.endpoint_port(Settings(_env_file=None, model_base_url=url))


def test_doctor_recognizes_proxy_key_in_dotenv_when_process_key_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from design_agent.doctor import missing_model_key

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test-loopback-secret\n")
    settings = Settings(
        _env_file=None,
        model="openai:gpt-6.1-sol",
        model_base_url="http://127.0.0.1:1456/v1",
        model_api_key_env="OPENAI_API_KEY",
    )
    assert missing_model_key(settings) is None
