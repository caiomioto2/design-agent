# A2A completion in Codex

The optional local bridge in [`plugins/design-agent-a2a/`](../plugins/design-agent-a2a/) exposes
Design Agent context, artifact reads and asynchronous task submission over A2A 1.0 and MCP.
An operator can register completion callbacks to an existing Codex App Server thread. Neither
Orca nor an editor is required. Registration, starting these services and connecting an account
are operator actions. Installing the bridge does not enable callbacks or connect an account.

## What each state establishes

| State | Evidence |
| --- | --- |
| A2A `TASK_STATE_COMPLETED` | The delegated turn returned. Read `completion_scope` before claiming the deliverable finished. |
| Event `scope: turn` | Only turn completion is known. Helpers, render watches, approval or unknown runtime state can remain. The event includes the limitation. |
| Event `scope: work`, `state: completed` | The runtime has no outstanding helper, watch, lead turn, inbox delivery, check-in or persisted work record for the task's context. This establishes runtime completion, not creative quality. |
| Event `state: failed` | Execution failed, a helper/watch failed or the bridge restarted during uncertain execution. This is never presented as success. |
| Event `state: input-required` | A person must review the pending action in Design Agent. The callback cannot approve it. |
| Outbox `delivered` | The receiver committed the event and acknowledged it. Codex may still be unavailable. |
| Inbox `accepted` | App Server accepted the input, or its user-message history proves acceptance. This does not mean the resulting Codex turn completed successfully. |
| Inbox `uncertain` | Transmission may have happened. Only history reconciliation runs; no automatic generation retry occurs. |

Lifecycle events contain a version, stable event ID, submission/task/context identity, result
revision, completion scope, bounded response text and artifact references. Credentials, callback
URLs, App Server endpoints and Slack identities do not enter the envelope. The receiver sends the
envelope as explicitly untrusted external data. It supplies no model, sandbox, approval or other
thread configuration overrides.

The bridge reads authoritative runtime state without model calls. A completed turn and its event
and outbox row commit in one SQLite transaction. A monitor revisits unfinished work through
`GET /work/{context}`. If the runtime does not expose completion evidence, the event remains
turn-scoped. If that runtime restarts before work completion is established, the bridge retains
turn scope and reports the uncertainty. Overlapping later submissions on the same Design Agent
context can also prevent attribution of background completion to the earlier submission.
Use isolated contexts for independent jobs. The local waker currently exposes a process-wide
pending count, so unrelated queued wake-ups can conservatively delay work completion.

## Set up the bridge

Use Python 3.11 or newer on a Unix host and Codex **0.167.1**. App Server is experimental; the
acceptance suite pins this version. From the repository root:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --frozen --all-extras --dev
```

Keep the operator's configuration outside the checkout, with mode `600`. The default is
`~/.config/design-agent/a2a.env`; `--config /private/path/a2a.env` selects another file.
The producer uses these settings:

```dotenv
A2A_URL=http://127.0.0.1:8081
A2A_TOKEN=<private random bearer token, at least 32 characters>
A2A_STATE=/private/path/producer-state
DESIGN_AGENT_ROOT=/path/to/design-agent-runtime
```

`DESIGN_AGENT_ROOT` is the existing runtime checkout. Its private `.env` supplies
`DESIGN_AGENT_SHARED_RUNTIME_URL` and `DESIGN_AGENT_SHARED_RUNTIME_TOKEN`. The runtime must expose
the authenticated shared API. No database writes, Slack impersonation or approval grants cross
that connection. Start the producer with:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/server.py serve --config /private/path/a2a.env
```

The receiver's private config includes `A2A_URL`, `A2A_TOKEN` and:

```dotenv
CALLBACK_URL=http://127.0.0.1:8082
CALLBACK_STATE=/private/path/receiver.sqlite
# Optional, when App Server requires a WebSocket capability token:
CODEX_TOKEN_FILE=/private/path/app-server-token
```

The receiver connects to an operator-owned App Server, started separately using its existing
Codex home and configuration, for example `codex app-server --listen ws://127.0.0.1:8083`.
Use App Server authentication on shared hosts. The adapter supports its private bearer token
file. HTTP and WebSocket endpoints must be numeric loopback addresses without credentials,
paths, query strings or redirects. Use a secure local tunnel for a remote owner.

The receiver runs with:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/server.py receiver --config /private/path/receiver.env
```

Run one producer and one receiver worker per respective state database. Protect the state
directory: the registry contains callback keys. Database files have mode `600`, use SQLite WAL
and full synchronous commits. Back up the database and its WAL together using SQLite's backup
facilities. Keep state outside the repository and runtime sandbox.

## Register an authorized destination before submitting

Registration is a host-only CLI action. The MCP tool cannot select a callback URL, consumer,
App Server or Codex thread. Create a private random callback-key file with at least 32 characters
and mode `600`, then register the same consumer and key in both databases. The producer records
the receiver URL; the receiver records the App Server URL:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/producer-state/tasks.sqlite register --consumer designer --endpoint http://127.0.0.1:8082 --key-file /private/path/callback-key
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/receiver.sqlite register --consumer designer --endpoint ws://127.0.0.1:8083 --key-file /private/path/callback-key
```

Bind one stable submission ID to the existing Codex thread in both databases:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/producer-state/tasks.sqlite bind --submission design-request-1 --consumer designer --thread EXISTING_CODEX_THREAD
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/receiver.sqlite bind --submission design-request-1 --consumer designer --thread EXISTING_CODEX_THREAD
```

For an authenticated App Server, add `--codex-token-file /private/path/app-server-token` to the
receiver's `bind` command. It verifies the existing thread with `thread/read`; it never creates
one. Add `--context EXISTING_DESIGN_CONTEXT` to both bindings to constrain the Design Agent
context too. Without that option, the authenticated producer's first event fixes the context and
task IDs. Destinations and associations are immutable. Use a new consumer or submission for a new
destination, rather than rewriting an existing binding.

Now call the existing `send_task(message, request_id="design-request-1", context_id=...)` through
the MCP client, or `SendMessage` over A2A. Retries must preserve the original request ID, message
and context. Callback metadata in task content grants no authority. Unregistered submissions
continue to work with polling and produce no callback delivery.

The MCP manifest is in [`mcp.json`](../plugins/design-agent-a2a/mcp.json). Its standalone project
has a lockfile and a plugin-local virtual environment; do not inherit another project's
`UV_PROJECT_ENVIRONMENT`. No plugin or account is installed automatically by this repository.

## Delivery, recovery and revocation

The producer authenticates callbacks with HMAC-SHA256 over a current timestamp and the exact
body. The receiver rejects timestamps outside five minutes, oversized bodies, unknown consumers,
wrong context/task bindings and conflicting reuse of an event ID. Retries use a fresh signature
and the same event identity. The receiver commits the event before HTTP acknowledgement;
duplicates return its existing receipt.

Before dispatch the receiver reconciles the event with the authenticated producer's current
revision. Older events become `superseded`. A revoked producer binding prevents dispatch even if
the receiver already accepted its callback. The worker reads the authorized thread immediately
before sending. Active threads receive `turn/steer` with the observed `expectedTurnId`; idle
threads receive `turn/start`. An unloaded thread is checked for archival and resumed using only
its original ID. Unavailable or archived destinations are retained for inspection; there is no
fallback destination or new thread.

An explicit active-turn rejection permits another state read and a new choice of operation.
Before transmission the worker commits `dispatching`, operation and expected turn. A timeout,
disconnect or restart leaves `uncertain`. Reconciliation searches that thread's actual accepted
user messages using `clientUserMessageId` and the event marker. The client ID is a correlation
key, **not an App Server idempotency guarantee**. If history cannot prove acceptance, the input
remains uncertain. The system makes no universal exactly-once execution promise.

Inspect receipts without dumping credentials or task contents:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/receiver.sqlite receipts
```

Use the same command with the producer database for outbox attempts. Transient callback and
pre-dispatch failures use bounded exponential backoff and retain `exhausted` records after eight
attempts. Restore connectivity or credentials first. To retry callback transport only:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/producer-state/tasks.sqlite retry-callback --event EVENT_ID
```

To make an uncertain receiver record eligible for its next **read-only reconciliation** pass:

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run python plugins/design-agent-a2a/control.py --state /private/path/receiver.sqlite reconcile --event EVENT_ID
```

That command never changes uncertainty back to pending generation. For an exhausted pre-dispatch
receiver record, inspect the attempts and binding, restore connectivity and use
`retry-receiver --event EVENT_ID` against the receiver database. It only resets `exhausted`, never
`uncertain` or `dispatching`. Do not resubmit the original design job as a notification retry.
Unknown outcomes require a person's decision.

`revoke --submission SUBMISSION_ID` disables a binding. `disable-consumer --consumer CONSUMER_ID`
disables every destination use for that consumer. Apply revocation to both databases for immediate
local enforcement. A producer-only revocation is also enforced on the receiver's next authoritative
state read. Revocation prevents future dispatch, but cannot withdraw input already accepted by
Codex or cancel an in-flight design provider effect. Polling remains available.

The adapter never answers App Server approval requests. Review them through the owning Codex
client. Design Agent's own pending approvals must be reviewed in its original conversation.
Background hooks alone do not resume idle Codex conversations.

## Artifact reads and acceptance tests

Reads remain bounded to a known Design Agent conversation and its mounted projects. The bridge
rejects hidden paths and symlinks, opens each path component using directory descriptors without
following symlinks, and streams the opened descriptor. The runtime verifies unique ownership of
legacy normalized output folders before the bridge exposes deliverables. Ambiguous folders are
omitted; project reads remain available. An older runtime without ownership evidence cannot
expose legacy deliverable folders through this bridge.

Run the real-process suite with an explicit pinned binary. It starts isolated Codex homes,
actual HTTP services and a deterministic local Responses endpoint. It inherits no account
credentials and calls no paid model or media service:

```bash
DESIGN_AGENT_TEST_CODEX=/path/to/codex-0.167.1 UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run pytest -q plugins/design-agent-a2a/tests
```

Without that variable, the suite uses `codex` from PATH only if it is exactly 0.167.1; otherwise
these acceptance cases skip with an explicit reason. No App Server success is mocked. Faults
occur at real HTTP boundaries or around an actual protocol request. The suite covers active and
idle threads, turn races, duplicates, out-of-order results, helpers, approvals, failures,
revocation, outages, restarts, lost acknowledgements and distinct concurrent destinations.
Passing it establishes local protocol behavior, not a deployed callback or account connection.

Primary contracts: [App Server API](https://learn.chatgpt.com/docs/app-server#api-overview),
[active steering](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn),
[background hooks](https://learn.chatgpt.com/docs/hooks#how-background-hooks-run), and the schemas
emitted by `codex app-server generate-json-schema` from 0.167.1.
