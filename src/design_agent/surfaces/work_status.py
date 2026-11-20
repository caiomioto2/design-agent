"""Conservative, model-free completion evidence for the local A2A bridge."""

from __future__ import annotations

from typing import Any

from design_agent.harness import ledger, tasks, wakeups
from design_agent.harness.watch import WATCH_LEDGER
from design_agent.middleware.watch_tools import WATCHES
from design_agent.runtime.local import LocalRuntime
from design_agent.runtime.waker.local import CHECK_INS
from design_agent.surfaces.shared import pack

_FAILED = frozenset({"failed", "error", "timed_out", "cancelled", "interrupted"})


async def work_status(
    runtime: LocalRuntime, store: Any, thread: str, since: float
) -> dict[str, Any]:
    outstanding: set[str] = set()
    failed: set[str] = set()
    # Persisted records cover work not yet restored by maintenance after a restart.
    # Read all pages; a capped search must never be interpreted as an empty ledger.
    for namespace in (ledger.LEDGER, ledger.WAKEUP_LEDGER, WATCH_LEDGER, CHECK_INS):
        offset = 0
        while True:
            page = await store.asearch(namespace, limit=200, offset=offset)
            for item in page:
                value = item.value or {}
                if value.get("thread_id") != thread:
                    continue
                if float(value.get("started_at") or 0) >= since and value.get("status") in _FAILED:
                    failed.add(item.key)
                if namespace == WATCH_LEDGER and value.get("reported_at"):
                    continue
                outstanding.add(item.key)
            if len(page) < 200:
                break
            offset += len(page)
    snapshot = await runtime.graph.aget_state(runtime._config(thread))
    # No await separates the current snapshot from these in-process work checks.
    for record in tasks.REGISTRY.values():
        if record.thread_id != thread:
            continue
        if record.status == "running" or not record.delivered:
            outstanding.add(record.id)
        if record.started_at >= since and record.status in _FAILED:
            failed.add(record.id)
    for watch in WATCHES.list(thread):
        if watch.status == "watching":
            outstanding.add(watch.id)
        if watch.started_at >= since and watch.status in _FAILED:
            failed.add(watch.id)
    outstanding.update(w.id for w in wakeups.pending(thread))
    box = runtime.services.inbox.boxes.get(thread)
    if box and (box.now or box.held or (box.delivery and not box.delivery.done())):
        outstanding.add("lead-inbox")
    # The waker exposes a process-wide count. Wait conservatively rather than
    # declare completion during a queued lead wake-up whose thread is unknown.
    if runtime.waker.pending():
        outstanding.add("lead-wakeup")
    if runtime._lock(thread).locked() or snapshot.next:
        outstanding.add("lead-turn")
    pending = any(task.interrupts for task in snapshot.tasks)
    return {
        "version": 1,
        "settled": not outstanding and not pending,
        "outstanding": sorted(outstanding)[:100],
        "failed": sorted(failed)[:100],
        "pending_approval": pending,
        "snapshot": pack(snapshot),
    }
