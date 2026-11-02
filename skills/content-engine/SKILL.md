---
name: content-engine
description: Prepare and publish blog posts, resources, Zernio social posts and comment automations through Action gateways, with human review and readback.
---

# Content engine

Complete the campaign in `/workspace/projects/<slug>/`. Use the connected Content Operations
and Social Action gateways. Tools are discovered at runtime; never invent an endpoint, schema,
account ID, public resource URL or successful publication. If a gateway is missing, finish the
local content and name the missing connection.

## Prepare the campaign

Read the thread, source article and brand pack. Use `content-brief` if the goal is unclear.
Write the article, resource, platform-specific captions, CTA keyword and automation reply as
one consistent campaign. Every claim needs a source. Produce and review the resource and media
with `media-production`; a local file or thread attachment is not a provider-accessible media URL.

Keep `CAMPAIGN.json` beside the brief. Record the source, intended CMS site, social profile and
accounts, resource slug, artifact paths and hashes, caption variants, CTA, keyword, requested
schedule with timezone, and each provider ID, state, verification time and public URL after a write.
Use separate entries for each destination. This is a receipt, not an exactly-once execution lock.

## Discover the actual contracts

Use tool search to find the connected gateways' catalog tools. For a Payload gateway, inspect
its CMS catalog before calling the dispatcher. For Zernio, inspect its full catalog and, when
the upstream uses deferred discovery, search its nested schemas. A generic dispatcher must use
an exact discovered name and validated arguments. Never treat a six-tool automation pilot as
the entire social publishing API.

If Composio is connected, use it to discover supporting tools and account connections for the
requested workflow. Inspect the exact returned schemas. Keep the campaign's governed CMS and
social Actions as its publishing path; another connector does not bypass approval, account
scope or readback. Composio's execution, workbench and remote shell tools require review even
when a nested request is described as a read. Do not connect an additional app unless asked.

Read the current site, profiles, accounts and existing records. Resolve the exact destination
from the request and live data. Search by slug or provider ID before creating anything. An
existing published resource is reused after verification; never overwrite it as a draft.

## Save and verify the resource and blog

1. Upload the actual downloadable file through the discovered host/CMS upload tool. Match the
   supported MIME type and size. An upload tool that accepts PDF cannot accept a renamed ZIP.
   Verify the protected asset exists and is linked to the resource.
2. Prepare the CMS resource as a draft, with its real title, slug, description, content, cover,
   delivery mode and protected asset. Use the provider's current schema. Read the saved draft
   back and compare fields. A draft or a missing protected asset is not a usable lead magnet.
3. Prepare the blog post with its source-backed copy, cover, metadata, and link to that resource.
   Read it back. Do not infer publication from a saved record or HTTP 200.
4. Show the person the exact drafts, destination and intended publication effect. After approval,
   publish through the connected gateway, then read the records and public pages. Test the
   resource's intended landing/download path. Do not expose a protected storage URL in a caption
   or DM. Use the verified public landing URL.

The host may require approval for draft writes as well as publication. A review request is a
checkpoint; do not bypass it with a different dispatcher, raw API, helper, shell or nested call.
In self-hosted Slack the person can approve once, for this conversation, or always for the same
operation. Only host-verified saved permission authorizes later calls. A request to remember
approval in chat does not create that permission. Continue checking destination and readback
under a saved grant. The person can revoke it with `revogar aprovacoes`; scheduled turns and
helpers cannot inherit it.
For a request already pending in Slack, use the existing review card or redisplay it in the same
thread. The host recovers its requester from verified conversation records; a new conversation
is not needed. A click on a completed request cannot repeat the external action.

## Publish the social campaign and automation

1. Prepare each post with the approved caption, media order, account IDs and exact schedule.
   Check platform media limits from the current contract. A provider media upload must finish
   before its URL or ID is used. Show the post and CTA together with the resource landing page.
2. Create or schedule the approved post once. Persist the Zernio ID immediately. Read it back;
   distinguish draft, scheduled, published and failed. A scheduled post is not already live.
3. Prepare the comment automation with the exact account/profile, keyword and matching rule,
   reply text, verified resource URL and post scope. Prefer the governed creation Action over
   its raw primitive. Inspect its trusted active-state policy: some deployments create paused,
   others active. If it creates active, approval must cover activation before calling it.
4. Read the saved automation back and compare scope, keywords, response channels and active
   state. Prefer a specific post scope; account-wide scope needs explicit approval. Activate a
   paused automation only after approval of that separate effect. Do not send a test DM or
   simulate a comment as part of setup without a separate instruction.

An Instagram comment campaign may deliver the resource by DM when supported. Other platforms
may only support public replies. Use the current platform/channel contract. Do not fabricate a
successful DM, a lead, conversion or revenue from an enabled automation.

If the requested launch needs automation ready before the post appears, use the provider's
supported pending-post binding or scheduling flow. If it cannot guarantee the ordering, show
that limitation before the public write. Do not claim an atomic campaign launch.

## Reconcile and report

A timeout or missing ID can follow a successful write. Mark the step uncertain, read existing
records, and reconcile before proceeding. Never retry an uncertain creation or publication
blindly. On partial failure, retain confirmed IDs and resume only missing steps. Do not delete
published content as an automatic rollback.

Report the verified resource/blog URLs, each social post's actual state, and the automation's
actual state. Name incomplete or unverified steps. Once requested, analytics can be read through
the same social gateway and used for the next brief; separate provider metrics from inferred
business outcomes. Background work and scheduled wake-ups may prepare and measure content;
they never approve a publication or activation.
