"""Authenticated durable ingress and continuation worker, independent of any editor."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from callbacks import MAX_ATTEMPTS, backoff
from codex_adapter import AppServer, Rejected, Uncertain
from lifecycle import MAX_BODY, Event, authenticate


class Worker:
    def __init__(self, store, peer, *, adapter_factory=AppServer):
        self.store, self.peer, self.adapter_factory = store, peer, adapter_factory
        self.adapters = {}
        self.lock = asyncio.Lock()

    async def run_once(self):
        async with self.lock:
            rows = self.store.db.execute(
                "SELECT payload,status,attempts FROM inbox WHERE status IN ('pending','uncertain') AND next_at<=? ORDER BY rowid",
                (time.time(),),
            ).fetchall()
            for payload, status, attempts in rows:
                event = Event.model_validate_json(payload)
                try:
                    await self.process(event, status)
                except (
                    httpx.HTTPError,
                    OSError,
                    Uncertain,
                    Rejected,
                    ValueError,
                    KeyError,
                    TypeError,
                ):
                    # Pre-dispatch failures are retryable. Dispatch itself changes state
                    # before transmission, so an exception cannot erase uncertainty.
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE inbox SET attempts=?,next_at=?,status=CASE WHEN status='dispatching' THEN 'uncertain' WHEN status='pending' AND ?>=? THEN 'exhausted' ELSE status END WHERE event=?",
                            (
                                attempts + 1,
                                time.time() + backoff(attempts + 1),
                                attempts + 1,
                                MAX_ATTEMPTS,
                                event.event_id,
                            ),
                        )
                        self.store.audit(event.event_id, "unavailable-or-uncertain")

    async def process(self, event: Event, status: str):
        binding = self.store.binding(event.submission_id)
        target = self.store.consumer(binding[0]) if binding else None
        if not binding or not target:
            self.store.status(event.event_id, "revoked")
            return
        consumer, thread, context, task, _ = binding
        if context != event.context_id or task != event.task_id:
            self.store.status(event.event_id, "blocked", "binding-mismatch")
            return
        if consumer not in self.adapters:
            self.adapters[consumer] = self.adapter_factory(target[0])
        adapter = self.adapters[consumer]
        await adapter.connect()
        if status == "uncertain":
            accepted = await adapter.accepted(thread, event.event_id)
            if accepted:
                self.store.status(event.event_id, "accepted", json.dumps(accepted))
            else:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE inbox SET next_at=? WHERE event=?",
                        (time.time() + 30, event.event_id),
                    )
            return
        try:
            current = await self.peer.current(event.submission_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                self.store.status(event.event_id, "revoked", "producer-binding-unavailable")
                return
            raise
        if current.revision > event.revision:
            self.store.status(event.event_id, "superseded")
            return
        if current != event:
            self.store.status(event.event_id, "blocked", "authoritative-state-mismatch")
            return
        # Bound the state-change loop. Only an explicit protocol rejection can permit
        # another generation request. A timeout or disconnect never does.
        for _ in range(3):
            method, expected = await adapter.prepare(thread)
            if not self.store.binding(event.submission_id) or not self.store.consumer(consumer):
                self.store.status(event.event_id, "revoked")
                return
            with self.store.db:
                claimed = self.store.db.execute(
                    "UPDATE inbox SET status='dispatching',operation=?,expected_turn=? WHERE event=? AND status='pending'",
                    (method, expected, event.event_id),
                ).rowcount
                if not claimed:
                    return
                self.store.audit(event.event_id, "dispatch-intent", method)
            try:
                receipt = await adapter.dispatch(thread, event, method, expected)
            except Rejected as error:
                if error.turn_race():
                    self.store.status(event.event_id, "pending", "explicit-turn-race-rejection")
                    continue
                self.store.status(event.event_id, "blocked", "app-server-rejected")
                return
            except (Uncertain, OSError, asyncio.CancelledError):
                self.store.status(event.event_id, "uncertain")
                raise
            self.store.status(event.event_id, "accepted", json.dumps(receipt))
            return

    async def close(self):
        for adapter in self.adapters.values():
            await adapter.close()


def create_receiver(store, worker: Worker):
    async def receive(request):
        consumer = request.headers.get("X-A2A-Consumer", "")
        registered = store.consumer(consumer)
        if not registered:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "Payload too large"}, status_code=413)
        if not authenticate(
            registered[1],
            request.headers.get("X-A2A-Timestamp", ""),
            request.headers.get("X-A2A-Signature", ""),
            bytes(body),
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            event = Event.model_validate_json(bytes(body))
            binding = store.binding(event.submission_id)
            if not binding or binding[0] != consumer:
                return JSONResponse({"error": "Binding not authorized"}, status_code=403)
            receipt = store.accept(event, consumer)
        except (ValidationError, ValueError):
            return JSONResponse({"error": "Invalid event or binding"}, status_code=409)
        return JSONResponse(receipt)

    @asynccontextmanager
    async def lifespan(_app):
        async def work():
            while True:
                await worker.run_once()
                await asyncio.sleep(1)

        task = asyncio.create_task(work())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await worker.close()

    return Starlette(routes=[Route("/events", receive, methods=["POST"])], lifespan=lifespan)
