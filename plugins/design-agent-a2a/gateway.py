"""A2A 1.0 sidecar backed by the existing runtime and a persistent SDK TaskStore."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

import httpx
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext, RequestContextBuilder
from a2a.server.id_generator import IDGenerator
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    ListTasksResponse,
    SecurityRequirement,
    SecurityScheme,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import InvalidParamsError, TaskNotCancelableError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from backend import Backend
from callbacks import deliver
from delivery_store import DeliveryStore
from lifecycle import MAX_BODY, Artifact, Event

TERMINAL = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


class Store(TaskStore):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS submissions (message TEXT PRIMARY KEY, fingerprint TEXT, task TEXT);
          CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, context TEXT, area TEXT, path TEXT);
        """)
        self.delivery = DeliveryStore(self.db)
        # A process restart never replays an uncertain external turn.
        for (blob,) in self.db.execute("SELECT payload FROM tasks").fetchall():
            task = Task.FromString(blob)
            if task.status.state in {TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING}:
                task.status.CopyFrom(
                    TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=new_text_message(
                            "Bridge restarted during execution. Outcome may be uncertain; inspect Design Agent context before submitting another task."
                        ),
                    )
                )
                task.metadata.update({"outcome_unknown": True})
                self._record(task)
        self.db.commit()

    async def save(self, task, context) -> None:  # noqa: ARG002 - public SDK interface
        with self.db:
            self._record(task)

    def _record(self, task):
        self.db.execute(
            "INSERT OR REPLACE INTO tasks VALUES (?,?)", (task.id, task.SerializeToString())
        )
        outcomes = {
            TaskState.TASK_STATE_COMPLETED: "completed",
            TaskState.TASK_STATE_FAILED: "failed",
            TaskState.TASK_STATE_INPUT_REQUIRED: "input-required",
        }
        if task.status.state not in outcomes:
            return
        row = self.db.execute("SELECT message FROM submissions WHERE task=?", (task.id,)).fetchone()
        if not row:
            return
        submission = row[0]
        self.delivery.associate(submission, task.context_id, task.id)
        previous = self.delivery.latest(submission)
        meta = MessageToDict(task.metadata)
        content = dict(
            submission_id=submission,
            task_id=task.id,
            context_id=task.context_id,
            state=outcomes[task.status.state],
            scope=meta.get("completion_scope", "turn"),
            result_revision=str(meta.get("result_revision", ""))[:256],
            text=get_message_text(task.status.message)[:8000],
            artifacts=[Artifact(**a) for a in meta.get("references", [])[:30]],
            limitation=str(
                meta.get("limitation")
                or ("Execution outcome may be uncertain." if meta.get("outcome_unknown") else "")
            )[:1000],
        )
        if previous and all(
            previous.model_dump()[k] == (v if k != "artifacts" else [a.model_dump() for a in v])
            for k, v in content.items()
        ):
            return
        revision = previous.revision + 1 if previous else 1
        event = Event(
            event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, task.id + ":" + str(revision))),
            revision=revision,
            **content,
        )
        while len(event.body()) > MAX_BODY and event.artifacts:
            event = event.model_copy(update={"artifacts": event.artifacts[:-1]})
        self.delivery.emit(event)

    async def get(self, task_id, context) -> Task | None:  # noqa: ARG002 - public SDK interface
        row = self.db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.FromString(row[0]) if row else None

    async def delete(self, task_id, context) -> None:  # noqa: ARG002 - public SDK interface
        self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.db.commit()

    async def list(self, params, context):  # noqa: ARG002 - public SDK interface
        tasks = [
            Task.FromString(row[0])
            for row in self.db.execute("SELECT payload FROM tasks ORDER BY rowid DESC")
        ]
        tasks = [
            t
            for t in tasks
            if (not params.context_id or t.context_id == params.context_id)
            and (not params.status or t.status.state == params.status)
        ]
        try:
            start = int(params.page_token or 0)
            if start < 0:
                raise ValueError
        except ValueError as error:
            raise InvalidParamsError(message="Invalid page token") from error
        size = max(1, min(params.page_size or 50, 100))
        selected = tasks[start : start + size]
        if not params.include_artifacts:
            for task in selected:
                task.ClearField("artifacts")
        return ListTasksResponse(
            tasks=selected,
            total_size=len(tasks),
            page_size=size,
            next_page_token=str(start + size) if start + size < len(tasks) else "",
        )

    def reserve(self, message: str, fingerprint: str, task: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint,task FROM submissions WHERE message=?", (message,)
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise InvalidParamsError(message="messageId already belongs to different content")
            return row[1]
        with self.db:
            self.db.execute("INSERT INTO submissions VALUES (?,?,?)", (message, fingerprint, task))
        return None

    def artifact(self, thread: str, area: str, path: str) -> str:
        identifier = hashlib.sha256(json.dumps([thread, area, path]).encode()).hexdigest()
        self.db.execute(
            "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?)", (identifier, thread, area, path)
        )
        self.db.commit()
        return identifier


class FixedID(IDGenerator):
    def __init__(self, value: str):
        self.value = value

    def generate(self, context) -> str:  # noqa: ARG002 - public SDK interface
        return self.value


class ContextBuilder(RequestContextBuilder):
    async def build(self, context, params=None, task_id=None, context_id=None, task=None):
        return RequestContext(
            context,
            params,
            task_id,
            context_id,
            task,
            task_id_generator=FixedID(
                str(uuid.uuid5(uuid.NAMESPACE_URL, "design-agent:" + params.message.message_id))
            ),
        )


class Executor(AgentExecutor):
    def __init__(self, backend: Backend, store: Store, url: str):
        self.backend, self.store, self.url = backend, store, url
        self.locks: dict[str, asyncio.Lock] = {}

    async def execute(self, context, event_queue) -> None:
        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        async with self.locks.setdefault(context.context_id, asyncio.Lock()):
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_text_message("Design Agent is processing the task."),
            )
            try:
                result = await self.backend.turn(
                    context.context_id, get_message_text(context.message)
                )
                await updater.add_artifact(
                    [new_text_part(result["text"][:64_000])], name="Design Agent response"
                )
                references = []
                for item in await self.backend.artifacts(context.context_id, limit=30):
                    references.append({"area": item["area"], "path": item["path"]})
                    identifier = self.store.artifact(context.context_id, item["area"], item["path"])
                    from a2a.types import Part

                    await updater.add_artifact(
                        [
                            Part(
                                url=f"{self.url}/artifacts/{identifier}",
                                filename=Path(item["path"]).name,
                                media_type=item["media_type"],
                            )
                        ],
                        name=item["area"] + "/" + item["path"],
                    )
                state = (
                    TaskState.TASK_STATE_INPUT_REQUIRED
                    if result["state"] == "input-required"
                    else TaskState.TASK_STATE_FAILED
                    if result["state"] == "failed"
                    else TaskState.TASK_STATE_COMPLETED
                )
                await updater.update_status(
                    state,
                    new_text_message(result["text"][:8000]),
                    metadata={
                        "completion_scope": result.get("scope", "turn"),
                        "work_since": result.get("work_since", time.time()),
                        "runtime_instance": result.get("runtime_instance", ""),
                        "monitor_work": result["state"] == "completed"
                        and result.get("scope") != "work",
                        "result_revision": result.get("result_revision", ""),
                        "limitation": result.get("limitation", ""),
                        "references": references,
                    },
                )
            except Exception as error:  # noqa: BLE001 - external failures must never expose credentials
                # Provider exceptions may contain credentials; return only the exception class.
                await updater.update_status(
                    TaskState.TASK_STATE_FAILED,
                    new_text_message(
                        f"Execution stopped ({type(error).__name__}). Inspect the existing context before retrying; an external effect may already have happened."
                    ),
                    metadata={"outcome_unknown": True},
                )

    async def cancel(self, context, event_queue) -> None:  # noqa: ARG002 - public SDK interface
        raise TaskNotCancelableError(
            message="This bridge cannot safely cancel provider work. Inspect the task and Design Agent context."
        )


class Handler(DefaultRequestHandler):
    def __init__(self, backend: Backend, store: Store, card: AgentCard, url: str):
        super().__init__(
            agent_executor=Executor(backend, store, url),
            task_store=store,
            agent_card=card,
            request_context_builder=ContextBuilder(),
        )
        self.backend, self.store = backend, store
        self.lock = asyncio.Lock()

    async def on_message_send_stream(self, params, context):  # noqa: ARG002 - public SDK interface
        raise UnsupportedOperationError(message="Use SendMessage and GetTask")
        yield  # pragma: no cover - the SDK interface is an async iterator

    async def on_message_send(self, params, context):
        message = params.message
        if (
            not message.message_id
            or len(message.message_id) > 128
            or not get_message_text(message).strip()
        ):
            raise InvalidParamsError(message="Provide a stable messageId and nonempty text")
        if len(get_message_text(message)) > 32_000 or any(
            p.WhichOneof("content") != "text" for p in message.parts
        ):
            raise InvalidParamsError(
                message="Only text inputs up to 32000 characters are supported"
            )
        # Retries of the exact same request return its original task instead of executing again.
        fingerprint = hashlib.sha256(message.SerializeToString(deterministic=True)).hexdigest()
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "design-agent:" + message.message_id))
        async with self.lock:
            prior = self.store.db.execute(
                "SELECT fingerprint,task FROM submissions WHERE message=?", (message.message_id,)
            ).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise InvalidParamsError(
                        message="messageId already belongs to different content"
                    )
                existing = await self.store.get(prior[1], context)
                if existing is None:
                    raise InvalidParamsError(
                        message="Previous submission outcome is uncertain; inspect context, do not resubmit"
                    )
                return existing
            if message.task_id:
                # Continuing a blocked approval through A2A must not impersonate the Slack person.
                raise InvalidParamsError(
                    message="Submit a new message with contextId; tool approvals stay in Design Agent"
                )
            if message.context_id:
                try:
                    await self.backend.known(message.context_id)
                except ValueError as error:
                    raise InvalidParamsError(message="Choose a known conversation") from error
            else:
                message.context_id = "a2a-" + str(uuid.uuid4())
            binding = self.store.delivery.binding(message.message_id, enabled=False)
            if binding and (not binding[4] or (binding[2] and binding[2] != message.context_id)):
                raise InvalidParamsError(
                    message="Submission binding is revoked or belongs to another context"
                )
            self.store.reserve(message.message_id, fingerprint, task_id)
            return await super().on_message_send(params, context)


def create_app(backend: Backend, store: Store, token: str, url: str):
    card = AgentCard(
        name="Design Agent",
        description="Design tasks and conversation context on the existing private runtime. Tool approvals remain with the person.",
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=url + "/rpc", protocol_version="1.0")
        ],
        skills=[
            AgentSkill(
                id="design",
                name="Design and deliverables",
                description="Prepare design tasks in an isolated or selected existing conversation.",
                tags=["design", "artifacts"],
            )
        ],
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearer": {}})],
    )
    handler = Handler(backend, store, card, url)

    async def data(request):
        try:
            kind = request.path_params["kind"]
            if kind == "conversations":
                result = await backend.conversations()
            elif kind == "projects":
                result = await backend.projects()
            elif kind == "context":
                result = await backend.context(
                    request.query_params["context_id"], int(request.query_params.get("limit", 12))
                )
            elif kind == "artifacts":
                result = await backend.artifacts(request.query_params["context_id"])
            elif kind == "read":
                result = await backend.read_artifact(
                    request.query_params["context_id"],
                    request.query_params["area"],
                    request.query_params["path"],
                )
            else:
                return JSONResponse({"error": "Unknown read operation"}, status_code=404)
            return JSONResponse(result)
        except (ValueError, KeyError):
            return JSONResponse(
                {"error": "Invalid conversation or artifact selector"}, status_code=400
            )
        except Exception as error:  # noqa: BLE001 - external failures must never expose credentials
            return JSONResponse({"error": type(error).__name__}, status_code=502)

    async def artifact(request):
        row = store.db.execute(
            "SELECT context,area,path FROM artifacts WHERE id=?", (request.path_params["id"],)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "Artifact not found"}, status_code=404)
        try:
            await backend.known(row[0])
            if row[1] == "deliverables":
                await backend.artifact_owner(row[0])
            path = backend.file_path(*row)
            file = backend.open_artifact(*row)

            def chunks():
                with file:
                    while chunk := file.read(64_000):
                        yield chunk

            return StreamingResponse(
                chunks(),
                media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.name)},
                background=BackgroundTask(file.close),
            )
        except (ValueError, httpx.HTTPError):
            return JSONResponse({"error": "Artifact not found"}, status_code=404)

    async def current(request):
        binding = store.delivery.binding(request.path_params["submission"])
        if not binding or not store.delivery.consumer(binding[0]):
            return JSONResponse({"error": "Binding not authorized"}, status_code=403)
        await reconcile_work(backend, store)
        event = store.delivery.latest(request.path_params["submission"])
        return JSONResponse(
            event.model_dump() if event else {"error": "Not found"},
            status_code=200 if event else 404,
        )

    @asynccontextmanager
    async def lifespan(_app):
        async def work():
            async with httpx.AsyncClient(follow_redirects=False) as client:
                while True:
                    try:
                        await reconcile_work(backend, store)
                        await deliver(store.delivery, client)
                    except (httpx.HTTPError, ValueError, OSError):
                        pass  # Durable pending rows remain available on the next pass.
                    await asyncio.sleep(2)

        worker = asyncio.create_task(work())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await backend.client.aclose()
            store.db.close()

    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, "/rpc"),
        Route("/callbacks/state/{submission}", current),
        Route("/read/{kind}", data),
        Route("/artifacts/{id}", artifact),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)

    class Authorized:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                authorization = dict(scope["headers"]).get(b"authorization", b"")
                if not hmac.compare_digest(authorization, ("Bearer " + token).encode()):
                    await JSONResponse({"error": "Unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
            await self.wrapped(scope, receive, send)

    return Authorized(app)


async def reconcile_work(backend, store):
    """Read runtime state, never run a model, and atomically advance a pending work result."""
    for (blob,) in store.db.execute("SELECT payload FROM tasks").fetchall():
        task = Task.FromString(blob)
        meta = MessageToDict(task.metadata)
        if not meta.get("monitor_work"):
            continue
        result = await backend.completion(task.context_id, float(meta["work_since"]))
        if meta.get("runtime_instance") and meta["runtime_instance"] != result.get(
            "runtime_instance"
        ):
            task.metadata.update(
                {
                    "monitor_work": False,
                    "limitation": "Runtime restarted before work completion was established. Inspect the Design Agent context; only turn completion is known.",
                }
            )
            await store.save(task, None)
            continue
        if result.get("scope") != "work" and result.get("state", "completed") == "completed":
            continue
        # Another submission on the same context makes attribution ambiguous.
        newer = store.db.execute(
            "SELECT b.task FROM submissions a JOIN submissions b ON b.rowid>a.rowid WHERE a.task=?",
            (task.id,),
        ).fetchall()
        peers = [await store.get(row[0], None) for row in newer]
        if any(t and t.context_id == task.context_id for t in peers):
            continue
        task.status.state = {
            "completed": TaskState.TASK_STATE_COMPLETED,
            "failed": TaskState.TASK_STATE_FAILED,
            "input-required": TaskState.TASK_STATE_INPUT_REQUIRED,
        }[result["state"]]
        task.status.message.CopyFrom(new_text_message(result["text"]))
        refs = [
            {"area": a["area"], "path": a["path"]}
            for a in await backend.artifacts(task.context_id, limit=30)
        ]
        task.metadata.update(
            {
                "completion_scope": result["scope"],
                "monitor_work": False,
                "result_revision": result["result_revision"],
                "limitation": result["limitation"],
                "references": refs,
            }
        )
        await store.save(task, None)
