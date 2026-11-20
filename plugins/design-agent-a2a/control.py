"""Private host registration and receipt inspection. Never exposed as model tools."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from codex_adapter import AppServer
from delivery_store import DeliveryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    register = commands.add_parser("register")
    register.add_argument("--consumer", required=True)
    register.add_argument("--endpoint", required=True)
    register.add_argument("--key-file", type=Path, required=True)
    bind = commands.add_parser("bind")
    bind.add_argument("--submission", required=True)
    bind.add_argument("--consumer", required=True)
    bind.add_argument("--thread", required=True)
    bind.add_argument("--context", default="")
    bind.add_argument("--codex-token-file", type=Path)
    disable = commands.add_parser("disable-consumer")
    disable.add_argument("--consumer", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--submission", required=True)
    retry = commands.add_parser("retry-callback")
    retry.add_argument("--event", required=True)
    retry_receiver = commands.add_parser("retry-receiver")
    retry_receiver.add_argument("--event", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--event", required=True)
    commands.add_parser("receipts")
    args = parser.parse_args()
    store = DeliveryStore.open(args.state)
    try:
        if args.action == "register":
            if args.key_file.stat().st_mode & 0o077:
                raise ValueError("Key file must be private (chmod 600)")
            store.register(args.consumer, args.endpoint, args.key_file.read_text().strip())
        elif args.action == "bind":
            target = store.consumer(args.consumer)
            if target and target[0].startswith("ws:"):

                async def verify():
                    adapter = AppServer(target[0], token_file=args.codex_token_file)
                    try:
                        await adapter.connect()
                        state = await adapter.thread(args.thread, include_turns=False)
                        if state["id"] != args.thread:
                            raise ValueError("Thread mismatch")
                    finally:
                        await adapter.close()

                asyncio.run(verify())
            store.bind(args.submission, args.consumer, args.thread, args.context)
        elif args.action == "disable-consumer":
            store.disable_consumer(args.consumer)
        elif args.action == "revoke":
            store.revoke(args.submission)
        elif args.action == "retry-callback":
            with store.db:
                store.db.execute(
                    "UPDATE outbox SET status='pending',attempts=0,next_at=0 WHERE event=? AND status IN ('pending','exhausted')",
                    (args.event,),
                )
        elif args.action == "retry-receiver":
            with store.db:
                store.db.execute(
                    "UPDATE inbox SET status='pending',attempts=0,next_at=0 WHERE event=? AND status='exhausted'",
                    (args.event,),
                )
        elif args.action == "reconcile":
            # This never returns an uncertain generation request to pending.
            with store.db:
                store.db.execute(
                    "UPDATE inbox SET next_at=0 WHERE event=? AND status='uncertain'", (args.event,)
                )
        print(json.dumps(store.receipts()))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
