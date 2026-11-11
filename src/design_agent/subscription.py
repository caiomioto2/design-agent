"""Use the operator's installed mcodex runtime proxy; OAuth tokens stay in mcodex."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import click

from design_agent.config import Settings
from design_agent.envfile import read_env, write_env

PROXY_SCRIPT = """
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const config = JSON.parse(readFileSync(0, 'utf8'));
const { startRuntimeRotationProxy } = await import(pathToFileURL(config.module).href);
const server = await startRuntimeRotationProxy({
  host: '127.0.0.1', port: config.port, clientApiKey: config.key
});
process.stdout.write('mcodex proxy ready on loopback\\n');
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, async () => { await server.close(); process.exit(0); });
}
"""


def proxy_module() -> Path:
    npm = shutil.which("npm")
    if npm is None:
        raise click.ClickException("Install Node.js 22+ and codex-multi-auth@2.23.0 first.")
    result = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, check=True)  # noqa: S603 - resolved npm executable, fixed arguments
    package = Path(result.stdout.strip()) / "codex-multi-auth"
    metadata = json.loads((package / "package.json").read_text()) if package.exists() else {}
    module = package / "dist/lib/runtime-rotation-proxy.js"
    if metadata.get("version") != "2.23.0" or not module.is_file():
        raise click.ClickException(
            "Install the tested proxy: npm install -g codex-multi-auth@2.23.0"
        )
    return module


def configure(root: Path, *, model: str, port: int) -> None:
    proxy_module()
    existing = read_env(root)
    if existing.get("OPENAI_API_KEY") and existing.get("DESIGN_AGENT_MODEL_API_MODE") != "codex":
        raise click.ClickException(
            "OPENAI_API_KEY already holds another endpoint's key. Use a separate checkout for subscription setup."
        )
    endpoint = f"http://127.0.0.1:{port}/v1"
    write_env(
        root,
        {
            "DESIGN_AGENT_MODEL": "openai:" + model,
            "DESIGN_AGENT_MODEL_BASE_URL": endpoint,
            "DESIGN_AGENT_MODEL_API_MODE": "codex",
            "DESIGN_AGENT_MODEL_API_KEY_ENV": "OPENAI_API_KEY",
            "OPENAI_API_KEY": existing.get("OPENAI_API_KEY") or secrets.token_urlsafe(32),
            "DESIGN_AGENT_IMAGE_MODEL": "gpt-image-2",
            "DESIGN_AGENT_IMAGE_BASE_URL": endpoint,
            "DESIGN_AGENT_RUNTIME": "local",
            "DESIGN_AGENT_SANDBOX": "docker",
            "DESIGN_AGENT_ENABLE_TRACES": "0",
            "LANGSMITH_TRACING": "false",
            "DESIGN_AGENT_HELPER_MODEL": "",
            "DESIGN_AGENT_LOOK_MODEL": "",
            "DESIGN_AGENT_TOOL_SELECTOR_MODEL": "",
            "DESIGN_AGENT_LEARNING_MODEL": "",
        },
    )


def endpoint_port(settings: Settings) -> int:
    url = urlsplit(settings.model_base_url or "")
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.username
        or url.password
        or not url.port
    ):
        raise click.ClickException(
            "The managed subscription proxy must use http://127.0.0.1:PORT/v1."
        )
    return url.port


def run_proxy(root: Path) -> int:
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=True)
    settings = Settings(_env_file=root / ".env")
    if settings.model_api_mode != "codex" or settings.openai_api_key is None:
        raise click.ClickException("Run design-agent configure-chatgpt first.")
    node = shutil.which("node")
    if node is None:
        raise click.ClickException("Node.js 22+ is required.")
    config = {
        "module": str(proxy_module()),
        "port": endpoint_port(settings),
        "key": settings.openai_api_key.get_secret_value(),
    }
    child = subprocess.Popen(  # noqa: S603 - resolved Node executable, fixed program, secrets via stdin
        [node, "--input-type=module", "-e", PROXY_SCRIPT], stdin=subprocess.PIPE
    )
    if child.stdin is None:
        child.terminate()
        raise RuntimeError("The proxy configuration pipe is unavailable.")
    child.stdin.write(json.dumps(config).encode())
    child.stdin.close()
    previous = signal.signal(signal.SIGTERM, lambda _signum, _frame: child.terminate())
    try:
        return child.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@contextmanager
def ensure_proxy(root: Path, settings: Settings) -> Iterator[None]:
    if settings.model_api_mode != "codex" or listening(endpoint_port(settings)):
        yield
        return
    state = root / "workspace/state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "mcodex-proxy.log").open("a") as log:
        (state / "mcodex-proxy.log").chmod(0o600)
        child = subprocess.Popen(
            [sys.executable, "-m", "design_agent.subscription"],
            cwd=root,
            stdout=log,
            stderr=log,
            env=os.environ.copy(),
        )
        try:
            deadline = time.monotonic() + 20
            while not listening(endpoint_port(settings)):
                if child.poll() is not None or time.monotonic() > deadline:
                    raise click.ClickException(
                        "mcodex proxy did not start. Check workspace/state/mcodex-proxy.log."
                    )
                time.sleep(0.1)
            yield
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=15)


if __name__ == "__main__":
    from design_agent.config import project_root

    sys.exit(run_proxy(project_root(Path.cwd())))
