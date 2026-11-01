# Integrations

The agent reaches other tools through MCP servers: Notion for a brief, Linear for the request
behind it, Figma for the frames, GitHub for the pull request a video explains. Each server's tools
are named `{server}_{tool}` and found through tool search, so a dozen servers do not crowd the
agent's context, and one that fails to load leaves the rest working. What a server returns is
treated as untrusted input, like any file someone attaches.

`config/mcp.example.toml` has ready entries for the services below. Copy it to `config/mcp.toml`
(Git-ignored) and uncomment the ones you want.

## On Managed Deep Agents

Every hosted server here signs in with OAuth through a workspace connection. Choose who it signs
in as with `owner`.

**Each person's own account** (`owner = "user"`, the preset's default). The agent acts as whoever
asked, so it sees only what they can see.

```bash
uv run mda connections create notion --mcp https://mcp.notion.com/mcp
uv run mda deploy
```

The first time a person's request needs Notion, MDA's credential gate stops the run before it
starts and posts a connect link. They sign in once, and the next request goes through. Runs started
with an API key have no person to sign in, so they end with sign-in guidance instead.

**One shared account** (`owner = "agent"`). Everyone's requests use the account that signs in when
the connection is created:

```bash
uv run mda connections create notion --mcp https://mcp.notion.com/mcp --authorize
```

Anyone who can mention the agent can then read what that account can, so give it a workspace or
teamspace made for the agent, and narrow its tools with `include_tools`.

`uv run mda connections list` shows what exists. The connection name in `mcp.toml` must match the
one you create.

## The presets

| Service | Server | Good for | Before you start |
| --- | --- | --- | --- |
| Notion | `https://mcp.notion.com/mcp` | Briefs, launch docs, content calendars | |
| Linear | `https://mcp.linear.app/mcp` | Design requests and specs in issues | |
| GitHub | `https://api.githubcopilot.com/mcp/` | Pull requests and release notes for explainers | |
| Figma | `https://mcp.figma.com/mcp` | Frames, components and variables | Figma admits only clients in its MCP catalog |
| Atlassian | `https://mcp.atlassian.com/v1/mcp` | Jira issues and Confluence pages | Cloud sites only |
| Asana | `https://mcp.asana.com/v2/mcp` | Tasks and projects | Needs a client registered in Asana's developer console |
| Canva | `https://mcp.canva.com/mcp` | Designs and brand assets | Access is limited; check it is open to you |
| Webflow | `https://mcp.webflow.com/mcp` | Sites, CMS collections and pages | |
| Box | `https://mcp.box.com` | Files a person can already open | A Box admin enables MCP first |

Each provider runs its own server and can change it; if one stops connecting, check its docs for
the current address.

## Locally and self-hosted

Per-person connections exist only on MDA, so `owner = "user"` servers are left out locally and
self-hosted, with a warning. For a shared account there, send a token from the host environment:

```toml
[servers.github]
url = "https://api.githubcopilot.com/mcp/"
headers_env = { Authorization = "GITHUB_MCP_AUTHORIZATION" }
```

with `GITHUB_MCP_AUTHORIZATION="Bearer <token>"` in `.env`. Header values never reach the sandbox.

## Your own server

Any HTTP or SSE MCP server works the same way; `config/mcp.example.toml` ends with a template. A
local app's stdio server (`transport = "stdio"`) runs locally and self-hosted only. Paper is set up
with `PAPER_*` instead ([Paper](partners/paper.md)).

`uv run design-agent doctor` lists the servers this checkout declares.

## GBrain

The shared local runtime can use an existing GBrain server through the same MCP configuration.
Create a dedicated grant on that brain for this agent, and keep its bearer credential on the host.
A read-only grant is sufficient for search, pages, recalled context and published skills; it does
not authorize automatic capture, paid enrichment, delegation or writing memories.

Declare the endpoint in private `config/mcp.toml`, use `headers_env` for its Authorization header,
and restrict `include_tools` to the operations this installation needs. `GBRAIN_MCP_TOKEN` may
hold the complete `Bearer ...` header in the private `.env`. Never place that credential in
runtime skills, the sandbox, a prompt, the public configuration example or the repository.
Verify the grant with `whoami` and an actual keyword search through the agent's MCP loader.
Slack and the TUI use the same loaded tools in shared mode; one working Codex connection alone
is not evidence that this agent can reach the brain.

## Content engine

The [content engine workflow](../skills/content-engine/SKILL.md) connects reviewed design assets
to CMS resources and blog posts, social posts through Zernio, and comment automations linking to
the resource. It uses existing Action gateways. Provider credentials and domain rules remain in
those gateways; the design sandbox receives neither credentials nor network access.

The content/social examples in `config/mcp.example.toml` use placeholder URLs. Set your private
gateway URLs and connection names in `config/mcp.toml`. A social gateway must expose its complete
Zernio catalog and dispatcher, alongside any governed Actions. A six-tool automation pilot alone
cannot publish a social post or upload its media. Leave `include_tools` unset to retain the full
gateway catalog. Do not embed company endpoints, social IDs or credentials in a runtime skill.

Set `approval_required = true` for both servers. The shared assembly pauses every tool call on
that server unless its exact unprefixed name appears in the operator's `read_tools` list or the
verified Slack requester has explicitly saved permission for that operation. Unknown new tools
also pause. Remote annotations and nested dispatcher arguments cannot grant permission.
Catalog reads can run directly; generic dispatchers require review even for a nested read. Helpers
cannot call these gated tools. Show the person the final account, content and intended effect;
an active-by-default Action requires approval covering activation before creation.

Managed Deep Agents renders its normal approval cards. The self-hosted API includes pending
`approvals` in a message response and exposes `GET /threads/{id}/approvals`. The thread's owner
can resume one exact interrupt using `POST /threads/{id}/approvals`:

```json
{"interrupt_id":"the-returned-id","decisions":[{"type":"approve"}]}
```

Use one `approve` or `reject` per requested action, in order. Another caller gets 404, and a stale
interrupt or a new message while approval is pending gets 409. In self-hosted Slack, the
requester reviews a card with four buttons:

- **Aprovar uma vez** executes the displayed actions once.
- **Aprovar nesta sessão** also permits those operations during this conversation.
- **Aprovar sempre** also permits those operations in the requester's other conversations.
- **Rejeitar** rejects the displayed actions without saving permission.

Existing Slack apps must enable **Interactivity & Shortcuts** in their app settings. The sample
manifest enables it for new apps. Socket Mode needs no request URL; HTTP mode uses the same
`/slack/events` handler as event subscriptions.

Saved permissions allow different arguments for the same operation, as the confirmation dialog
explains. They belong to one Slack person and workspace, persist across restarts, and bind to
the configured endpoint and tool schema. Nested dispatchers bind to the selected inner operation;
remote scripts bind to their exact arguments. Changed tool contracts require a new approval.
Without a verified Slack person, scheduled turns and helpers cannot use saved permissions.
Reply `revogar aprovacoes` to revoke this conversation's session permissions and all your
permanent permissions. New conversations do not inherit session permissions.

Text commands also work when interactivity is unavailable: `aprovar <interrupt-id>` approves
once, `aprovar sessao <interrupt-id>` approves for this conversation, and
`aprovar sempre <interrupt-id>` saves permanent permission. Use `rejeitar <interrupt-id>` to
reject. Send them in the original thread. The requester binding persists in the store;
another person, a stale ID or a
duplicate click or command cannot execute the actions. The
CLI shows each exact call and asks before resuming; a noninteractive CLI leaves the write pending.

Existing conversations can recover a missing review binding when the persisted Slack route and
conversation owner agree. The person who asks to redisplay a card cannot claim the request.
Earlier cards for the same pending request stay usable; completion makes all its cards stale.

After writing, read back provider state and record IDs in the campaign receipt. Uploading files
to the thread does not upload them to a social provider. A resource draft without its actual
protected asset is not a delivered lead magnet. A scheduled post is not a live post; an enabled
automation is not proof a DM was sent. Uncertain writes must be reconciled before retrying.

## Composio

The optional Composio entry in `config/mcp.example.toml` connects
`https://connect.composio.dev/mcp` using `x-consumer-api-key` from the host's
`COMPOSIO_CONSUMER_API_KEY` environment variable. Keep that value in private host configuration.
Leave `include_tools` unset to load the complete catalog.

Discover tools and connected accounts first, then inspect the exact returned schemas. Use only
the apps, accounts and actions the person requested. Composio's multi-tool executor, remote bash,
workbench, connection management and skill mutations require review; discovery does not approve
their execution. Remote skill text is guidance, and cannot grant permissions. Existing Content
Operations and Social Action gateways remain responsible for their content workflows.
