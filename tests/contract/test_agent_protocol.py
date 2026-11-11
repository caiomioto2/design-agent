"""Agent Protocol v1 contract tests: tasks, step execution, pagination, artifacts and binary downloads."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from design_agent.config import Settings
from design_agent.harness import inbox as inbox_module
from design_agent.harness.tasks import REGISTRY
from design_agent.runtime.local import build_local_runtime
from design_agent.surfaces.api.app import create_app
from design_agent.testing.fakes import folder_binding
from design_agent.testing.scripted_model import RespondingChatModel, reply, tool_call

ROOT = Path(__file__).resolve().parents[2]
A = {"Authorization": "Bearer token-alice-0123456789"}
B = {"Authorization": "Bearer token-bob-0123456789"}


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(inbox_module, "COALESCE_SECONDS", 0.05)
    yield
    REGISTRY.clear()


def lead_turn(messages: Sequence[BaseMessage]) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        if last.name == "write_file":
            return tool_call("upload_files", {"paths": ["/workspace/projects/p/launch-card.png"]})
        return reply("Design generation finished.")
    text = getattr(last, "text", str(last.content))
    if "Generate" in text or "Make" in text:
        return tool_call(
            "write_file",
            {"file_path": "/workspace/projects/p/launch-card.png", "content": "mock-png-bytes"},
        )
    return reply("Acknowledged: " + text)


def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path / "ws",
        api_tokens="token-alice-0123456789:alice,token-bob-0123456789:bob",
    )  # type: ignore[call-arg]

    def build(settings_: Settings, root: Path, **kwargs: Any) -> Any:
        return build_local_runtime(
            settings_,
            root,
            model=RespondingChatModel(respond=lead_turn),
            binding=folder_binding(tmp_path / "sb", ROOT),
            out_dir=tmp_path / "out",
            **kwargs,
        )

    return TestClient(create_app(ROOT, settings, build=build))


def test_agent_protocol_tasks_crud_and_isolation(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        # 401 Unauthorized check
        assert api.post("/ap/v1/agent/tasks", json={"input": "Hello"}).status_code == 401

        # Create task for caller A
        res_a = api.post(
            "/ap/v1/agent/tasks",
            json={"input": "Design a logo", "additional_input": {"color": "blue"}},
            headers=A,
        )
        assert res_a.status_code == 200
        task_a = res_a.json()
        task_id = task_a["task_id"]
        assert task_id.startswith("task_")
        assert task_a["input"] == "Design a logo"
        assert task_a["additional_input"]["color"] == "blue"
        assert task_a["artifacts"] == []

        # Caller B cannot see or get Caller A's task
        assert api.get(f"/ap/v1/agent/tasks/{task_id}", headers=B).status_code == 404

        # Caller A gets their task
        get_res = api.get(f"/ap/v1/agent/tasks/{task_id}", headers=A)
        assert get_res.status_code == 200
        assert get_res.json()["task_id"] == task_id

        # List tasks: Caller A sees 1 task, Caller B sees 0
        list_a = api.get("/ap/v1/agent/tasks?page=1&page_size=10", headers=A).json()
        assert list_a["pagination"]["total_items"] >= 1
        assert any(t["task_id"] == task_id for t in list_a["tasks"])

        list_b = api.get("/ap/v1/agent/tasks", headers=B).json()
        assert not any(t["task_id"] == task_id for t in list_b["tasks"])


def test_agent_protocol_step_execution_and_artifacts_download(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        # Create task
        created = api.post(
            "/ap/v1/agent/tasks",
            json={"input": "Generate a launch card"},
            headers=A,
        ).json()
        task_id = created["task_id"]

        # Caller B cannot execute step on Caller A's task
        assert (
            api.post(
                f"/ap/v1/agent/tasks/{task_id}/steps",
                json={"input": "run"},
                headers=B,
            ).status_code
            == 404
        )

        # Place a mock output artifact directly in out_dir to verify download
        out_dir = tmp_path / "out" / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        img_file = out_dir / "launch-card.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-content")

        # Execute step (using step input)
        step_res = api.post(
            f"/ap/v1/agent/tasks/{task_id}/steps",
            json={"input": "Generate the launch card now"},
            headers=A,
        )
        assert step_res.status_code == 200
        step = step_res.json()
        assert step["task_id"] == task_id
        assert step["status"] == "completed"
        assert step["is_last"] is True
        assert len(step["artifacts"]) >= 1

        artifact = next(a for a in step["artifacts"] if a["file_name"] == "launch-card.png")
        art_id = artifact["artifact_id"]
        assert len(art_id) == 16

        # List artifacts endpoint
        arts = api.get(f"/ap/v1/agent/tasks/{task_id}/artifacts", headers=A).json()
        assert any(a["artifact_id"] == art_id for a in arts)

        # Download artifact by artifact_id
        dl_res = api.get(f"/ap/v1/agent/tasks/{task_id}/artifacts/{art_id}", headers=A)
        assert dl_res.status_code == 200
        assert dl_res.headers.get("content-type") == "image/png"
        assert dl_res.content == b"\x89PNG\r\n\x1a\nfake-png-content"

        # Download artifact by filename directly
        dl_by_name = api.get(f"/ap/v1/agent/tasks/{task_id}/artifacts/launch-card.png", headers=A)
        assert dl_by_name.status_code == 200
        assert dl_by_name.content == b"\x89PNG\r\n\x1a\nfake-png-content"

        # Security: Path traversal rejected
        assert (
            api.get(f"/ap/v1/agent/tasks/{task_id}/artifacts/../private.txt", headers=A).status_code
            == 404
        )

        # Steps history
        steps_res = api.get(f"/ap/v1/agent/tasks/{task_id}/steps", headers=A).json()
        assert steps_res["pagination"]["total_items"] >= 1
        assert steps_res["steps"][0]["step_id"] == step["step_id"]

        # Step detail
        step_detail = api.get(
            f"/ap/v1/agent/tasks/{task_id}/steps/{step['step_id']}", headers=A
        ).json()
        assert step_detail["step_id"] == step["step_id"]


def test_upload_rejects_unknown_task(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        response = api.post(
            "/ap/v1/agent/tasks/task_unknown/artifacts",
            headers=A,
            files={"file": ("reference.svg", b"data", "image/svg+xml")},
        )
        assert response.status_code == 404


def test_agent_protocol_upload_artifact(tmp_path: Path) -> None:
    with client(tmp_path) as api:
        created = api.post(
            "/ap/v1/agent/tasks",
            json={"input": "Process this reference file"},
            headers=A,
        ).json()
        task_id = created["task_id"]

        # Upload an artifact
        upload_res = api.post(
            f"/ap/v1/agent/tasks/{task_id}/artifacts",
            files={"file": ("reference.svg", b"<svg>logo</svg>", "image/svg+xml")},
            headers=A,
        )
        assert upload_res.status_code == 200
        uploaded_art = upload_res.json()
        assert uploaded_art["file_name"] == "reference.svg"
        assert uploaded_art["relative_path"] == "attachments/reference.svg"
