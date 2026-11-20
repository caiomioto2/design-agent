"""Recoverable callback outbox. Retries never submit design work or follow redirects."""

from __future__ import annotations

import json
import time

import httpx

from lifecycle import Event, headers

MAX_ATTEMPTS = 8


def backoff(attempt: int) -> float:
    return min(300, 2 ** min(attempt, 9))


async def deliver(store, client: httpx.AsyncClient):
    rows = store.db.execute(
        "SELECT o.event,o.consumer,e.payload,o.attempts FROM outbox o JOIN events e ON e.id=o.event "
        "WHERE o.status='pending' AND o.next_at<=? ORDER BY e.rowid",
        (time.time(),),
    ).fetchall()
    for identifier, consumer, payload, attempts in rows:
        event = Event.model_validate_json(payload)
        binding = store.binding(event.submission_id)
        target = store.consumer(consumer)
        if not binding or binding[0] != consumer or not target:
            with store.db:
                store.db.execute("UPDATE outbox SET status='revoked' WHERE event=?", (identifier,))
            continue
        body = event.body()
        try:
            response = await client.post(
                target[0] + "/events",
                content=body,
                headers=headers(target[1], consumer, body),
                follow_redirects=False,
                timeout=10,
            )
            response.raise_for_status()
            receipt = response.json()
            if not isinstance(receipt, dict):
                raise ValueError("Invalid receiver receipt")
            if receipt.get("event_id") != identifier or receipt.get("status") not in {
                "pending",
                "dispatching",
                "accepted",
                "uncertain",
                "superseded",
                "revoked",
                "blocked",
                "exhausted",
            }:
                raise ValueError("Invalid receiver receipt")
        except (httpx.HTTPError, ValueError):
            attempts += 1
            with store.db:
                store.db.execute(
                    "UPDATE outbox SET attempts=?,next_at=?,status=? WHERE event=?",
                    (
                        attempts,
                        time.time() + backoff(attempts),
                        "exhausted" if attempts >= MAX_ATTEMPTS else "pending",
                        identifier,
                    ),
                )
                store.audit(identifier, "callback-unavailable")
        else:
            with store.db:
                store.db.execute(
                    "UPDATE outbox SET status='delivered',attempts=attempts+1,receipt=? WHERE event=?",
                    (json.dumps(receipt), identifier),
                )
                store.audit(identifier, "callback-recorded")
