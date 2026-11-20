"""Host-owned registration, outbox and inbox. SQLite commits precede network effects."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from lifecycle import Event


class DeliveryStore:
    def __init__(self, db: sqlite3.Connection, *, recover=False):
        self.db = db
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS consumers (
            id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
          );
          CREATE TABLE IF NOT EXISTS bindings (
            submission TEXT PRIMARY KEY, consumer TEXT NOT NULL, thread TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
          );
          CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, submission TEXT NOT NULL, revision INTEGER NOT NULL,
            payload TEXT NOT NULL, UNIQUE(submission, revision)
          );
          CREATE TABLE IF NOT EXISTS outbox (
            event TEXT PRIMARY KEY, consumer TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, receipt TEXT NOT NULL DEFAULT ''
          );
          CREATE TABLE IF NOT EXISTS inbox (
            event TEXT PRIMARY KEY, consumer TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', receipt TEXT NOT NULL DEFAULT '',
            operation TEXT NOT NULL DEFAULT '', expected_turn TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, event TEXT NOT NULL, at REAL NOT NULL,
            stage TEXT NOT NULL, detail TEXT NOT NULL
          );
        """)
        # A crash after dispatch intent has no safe automatic retry.
        if recover:
            self.db.execute("UPDATE inbox SET status='uncertain' WHERE status='dispatching'")
        self.db.commit()

    @classmethod
    def open(cls, path: Path, *, recover=False):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        return cls(sqlite3.connect(path), recover=recover)

    def register(self, consumer: str, endpoint: str, secret: str):
        from backend import IDENTIFIER, loopback

        if not IDENTIFIER.fullmatch(consumer) or len(secret) < 32:
            raise ValueError("Use a valid consumer ID and a private key of at least 32 characters")
        # Re-registration cannot silently change a destination or a key for existing work.
        existing = self.consumer(consumer, enabled=False)
        if existing and existing != (endpoint, secret, 1):
            raise ValueError("Use a new consumer ID for a new destination or credential")
        if endpoint.startswith("http"):
            loopback(endpoint)
        else:
            from codex_adapter import endpoint_url

            endpoint_url(endpoint)
        self.db.execute(
            "INSERT OR IGNORE INTO consumers VALUES (?,?,?,1)", (consumer, endpoint, secret)
        )
        self.db.commit()

    def consumer(self, consumer: str, *, enabled=True):
        return self.db.execute(
            "SELECT endpoint,secret,enabled FROM consumers WHERE id=? AND (?=0 OR enabled=1)",
            (consumer, int(enabled)),
        ).fetchone()

    def bind(self, submission: str, consumer: str, thread: str, context: str = ""):
        from backend import IDENTIFIER

        if not all(IDENTIFIER.fullmatch(x) for x in (submission, consumer, thread)) or (
            context and not IDENTIFIER.fullmatch(context)
        ):
            raise ValueError("Invalid binding identity")
        if not self.consumer(consumer):
            raise ValueError("Register the consumer first")
        row = self.binding(submission, enabled=False)
        if row and row != (consumer, thread, context, "", 1):
            raise ValueError("A submission binding is immutable; use a new submission")
        self.db.execute(
            "INSERT OR IGNORE INTO bindings(submission,consumer,thread,context) VALUES (?,?,?,?)",
            (submission, consumer, thread, context),
        )
        self.db.commit()

    def binding(self, submission: str, *, enabled=True):
        return self.db.execute(
            "SELECT consumer,thread,context,task,enabled FROM bindings WHERE submission=? AND (?=0 OR enabled=1)",
            (submission, int(enabled)),
        ).fetchone()

    def revoke(self, submission: str):
        self.db.execute("UPDATE bindings SET enabled=0 WHERE submission=?", (submission,))
        self.db.commit()

    def disable_consumer(self, consumer: str):
        self.db.execute("UPDATE consumers SET enabled=0 WHERE id=?", (consumer,))
        self.db.commit()

    def associate(self, submission: str, context: str, task: str):
        row = self.binding(submission)
        if row:
            if (row[2] and row[2] != context) or (row[3] and row[3] != task):
                raise ValueError("Binding context/task mismatch")
            self.db.execute(
                "UPDATE bindings SET context=?,task=? WHERE submission=?",
                (context, task, submission),
            )

    def latest(self, submission: str) -> Event | None:
        row = self.db.execute(
            "SELECT payload FROM events WHERE submission=? ORDER BY revision DESC LIMIT 1",
            (submission,),
        ).fetchone()
        return Event.model_validate_json(row[0]) if row else None

    def emit(self, event: Event):
        # The caller commits task state, event and delivery together.
        self.db.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (event.event_id, event.submission_id, event.revision, event.body().decode()),
        )
        binding = self.binding(event.submission_id)
        if binding and self.consumer(binding[0]):
            self.db.execute(
                "INSERT INTO outbox(event,consumer) VALUES (?,?)", (event.event_id, binding[0])
            )

    def audit(self, event: str, stage: str, detail: str = ""):
        self.db.execute(
            "INSERT INTO attempts(event,at,stage,detail) VALUES (?,?,?,?)",
            (event, time.time(), stage, detail),
        )

    def accept(self, event: Event, consumer: str) -> dict:
        existing = self.db.execute(
            "SELECT consumer,payload,status,receipt FROM inbox WHERE event=?", (event.event_id,)
        ).fetchone()
        if existing:
            if existing[0] != consumer or existing[1] != event.body().decode():
                raise ValueError("Conflicting event identity")
            return {"event_id": event.event_id, "status": existing[2], "receipt": existing[3]}
        with self.db:
            self.associate(event.submission_id, event.context_id, event.task_id)
            self.db.execute(
                "INSERT INTO inbox(event,consumer,payload) VALUES (?,?,?)",
                (event.event_id, consumer, event.body().decode()),
            )
            self.audit(event.event_id, "recorded")
        return {"event_id": event.event_id, "status": "pending", "receipt": ""}

    def status(self, event: str, status: str, receipt: str = ""):
        with self.db:
            self.db.execute(
                "UPDATE inbox SET status=?,receipt=? WHERE event=?", (status, receipt, event)
            )
            self.audit(event, status, receipt)

    def receipts(self):
        # Never expose the registry, credentials or raw result data in an operator listing.
        return {
            "outbox": self.db.execute(
                "SELECT event,consumer,status,attempts,next_at,receipt FROM outbox"
            ).fetchall(),
            "inbox": self.db.execute(
                "SELECT event,consumer,status,attempts,operation,expected_turn,receipt FROM inbox"
            ).fetchall(),
            "attempts": self.db.execute(
                "SELECT event,at,stage,detail FROM attempts ORDER BY id"
            ).fetchall(),
        }
