# iMessage Agent Builder MVP

## Summary

Build a deployed MVP where users text the existing Photon setup, create projects through conversation, and share immediately available Markdown-backed agents using project codes, within the configured messaging capacity and application quotas.

The only visual interface is a mobile-friendly, project-scoped vault editor. All builder conversations, project management, and agent configuration happen in iMessage.

Use:

- **Python / FastAPI:** webhooks, routing, authorization, agent execution, background work, and vault dashboard.
- **Photon:** the user's existing project and provisioned line, which supports multiple numbers. The user reports a current limit of 25 numbers, with one assigned for personal use through `+1 628-316-6196`. Verify how inbound numbers map to senders before implementing the adapter; application projects are selected by code rather than assigned their own numbers.
- **NVIDIA Nemotron:** powers only the builder agent through NVIDIA's hosted API and a small Python tool-calling loop. No NVIDIA agent framework is required.
- **OpenAI API:** project-agent responses use GPT-5.6 Luna with low reasoning effort; document and query embeddings also use OpenAI so project Q&A does not depend on NVIDIA.
- **Haystack:** document processing, OpenAI embeddings, and retrieval; pass retrieved context to the OpenAI Responses API for answer generation.
- **SpacetimeDB:** all durable application data, including Markdown and embeddings.

Builder model: `nvidia/nemotron-3-super-120b-a12b`, configurable through deployment settings to another Nemotron model. Call NVIDIA's hosted Chat Completions API at base URL `https://integrate.api.nvidia.com/v1`, with credentials from `NVIDIA_API_KEY`. Use the linked Nemotron developer repository as a reference for model usage; the MVP uses hosted inference and does not install its training stack. [NVIDIA model](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/playground), [Nemotron developer repository](https://github.com/NVIDIA-NeMo/nemotron)

Project-agent model: `gpt-5.6-luna` through the OpenAI Responses API, explicitly setting `reasoning={"effort": "low"}` and using `OPENAI_API_KEY`. Keep builder and project-agent model settings and clients separate. Model access must be verified with the deployment account; do not silently substitute another model or provider if access is unavailable. [Official OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [Reasoning settings](https://developers.openai.com/api/docs/guides/reasoning)

### Builder model integration

Implement a bounded Python loop that sends builder messages and tool schemas to Nemotron, validates returned tool calls, executes the backend's scoped operations, and returns their results to the model. Reconstruct each user's bounded builder context from SpacetimeDB. Keep ownership checks, durable tool-step records, and retry budgets in the backend; resume completed operations from their stored results rather than replaying mutations. Use separate NVIDIA and OpenAI clients even if both use the same Python SDK.

## Messaging and agent behavior

### Identity and routing

Register a user from their normalized sender phone number on their first verified inbound message. No onboarding or account setup in the application. V1 supports phone-number-based, one-to-one iMessage conversations.

Parse slash commands before invoking a model:

| Command | Behavior |
|---|---|
| `/builder` | Return to the user's builder and restore their editing focus |
| `/projects` | List projects owned by the user and their codes |
| `/edit <code>` | Enter builder mode focused on an owned project |
| `/use <code>` | Enter the selected project's customer-facing conversation |
| `/vault` | Issue a one-hour editing link for the currently focused, owned project |
| `/help` | Explain available commands |

Natural-language requests invoke the same validated operations. Clarify ambiguous names before switching. Invalid commands leave state unchanged.

History reset is not a capability: expose no reset command, tool, endpoint, or natural-language equivalent. Requests to reset receive an unsupported-operation response without changing state.

Keep editing focus separate from customer-agent selection. Only the backend changes routing state or grants access; model-supplied user IDs or ownership claims are never trusted. A customer agent may request navigation, but receives no project-management tools.

### Builder capabilities

For the first end-to-end milestone, expose narrowly scoped tools to create, list, and select owned projects, update agent instructions, and issue vault links. File editing and upload use the vault dashboard. After that milestone passes, extend the tools to:

- Create, list, rename, select, and delete owned projects.
- Read and update the project agent's configurable instructions.
- List, read, import, create, update, and delete Markdown files.
- Issue vault links and retry failed indexing.

Creation atomically initializes the project, empty vault, default agent configuration, and unique stable public code. It selects the new project for editing and returns its code immediately. Agents are live from creation; an empty vault produces a helpful "no sources available" response.

Project edits use the selected editing focus. Requests targeting a different project must explicitly select that project through the validated operation.

Deleting a project requires `delete <project-code>` within five minutes of a deletion request. Confirmation is bound to the requesting owner and exact project. Deletion atomically marks the project deleted, disables access, revokes vault links, and invalidates pending work. Idempotent cleanup then removes files, embeddings, agent settings, and conversation data; stale workers cannot recreate them. It does not delete the shared Photon project.

### Project agents

Give each project a default instruction template requiring concise, source-grounded answers with filename/heading citations.

Generate project answers with the configured OpenAI client and GPT-5.6 Luna at low reasoning effort. Supply the selected project's current retrieved chunks and bounded user/project history on each request; use `store=false` and manage application conversation state in SpacetimeDB. Send no management tools. Handle refusal, empty output, and incomplete responses explicitly rather than treating them as completed answers.

Owners may change tone, purpose, and domain instructions through iMessage. Application-enforced restrictions remain outside those editable instructions: project isolation, ownership checks, and no customer-agent management permissions. Source grounding, filename/heading citations, and abstention are soft requirements enforced through the system prompt for this MVP, not guarantees of factual correctness. Do not add a separate answer-verification model or evidence-validation pipeline for this milestone.

Treat Markdown and conversation quotations as content, never as authorization to call tools or switch modes. Never create a vault editing link for a customer seeking source citations.

Persist histories separately for the user's builder and each user/project Q&A conversation. Supply a bounded recent history to models. Histories expire under the retention policy below; users cannot reset them. Instruct the model that historical answers do not substitute for currently available sources.

## Storage, retrieval, and vault interface

### SpacetimeDB integration

Use a small TypeScript database module defining tables and transactional reducers. Keep application and agent logic in Python. Connect Python through authenticated HTTP query/reducer calls; the browser accesses only FastAPI.

The authenticated `spacetime` CLI is available after `source ~/.bashrc`. Read-only inspection on 2026-11-07 confirmed CLI version `2.14.1` and an existing Maincloud database named `hackutd-xmddq`. Use the CLI to inspect the existing schema and manage the module during implementation. CLI login confirms management access; deployed Python still needs an explicitly configured runtime identity and credentials.

SpacetimeDB supports TypeScript modules and HTTP access. Its HTTP interface is less optimized than SDK WebSocket connections, an acceptable initial tradeoff for this bounded MVP. [Modules](https://spacetimedb.com/docs/databases/), [HTTP API](https://spacetimedb.com/docs/http/database/)

Store these logical records:

- Users and routing state.
- Projects, ownership, stable public codes, and editable instructions.
- Versioned Markdown files and indexing status.
- Chunks, embeddings, source headings, and file-version references.
- Conversation messages.
- Hashed vault tokens and pending deletion confirmations.
- Inbound event receipts, durable jobs, and outbound delivery records.

Restrict database access to the backend service identity. Keep application tables private and expose the required reads through views or procedures that check the caller's identity. Every mutation reducer rejects callers other than the configured backend identity. Every user-facing operation also checks ownership or its project-specific vault capability in Python. Reducers enforce version checks, quotas, operation deduplication, and atomic updates. Before expanding the schema, verify the pinned module/API contracts support an authenticated Python read and reducer call while rejecting anonymous and unrelated identities; do not make tables public to bypass read-access problems.

### RAG pipeline

Use OpenAI `text-embedding-3-small` through Haystack's `OpenAIDocumentEmbedder` for chunks and `OpenAITextEmbedder` for queries, with the same model and dimensions for both. Store vectors in SpacetimeDB and compute cosine similarity in Python over the selected project's current, indexed chunks. This avoids introducing a second database; native vector indexing is not part of SpacetimeDB's documented index types. [Embedding model](https://developers.openai.com/api/docs/models/text-embedding-3-small), [Document embedder](https://docs.haystack.deepset.ai/docs/openaidocumentembedder), [Query embedder](https://docs.haystack.deepset.ai/docs/openaitextembedder), [SpacetimeDB indexes](https://spacetimedb.com/docs/tables/indexes/)

- Accept UTF-8 `.md` files through dashboard uploads first; add Photon attachments after the first end-to-end milestone.
- Start with 20 files per project, 128 KiB per file, 1 MiB total current text, and 1,000 chunks per project. Enforce quotas atomically across concurrent writes. Reject files whose chunk count would exceed the project limit before accepting the save.
- Preserve headings and filenames; split into approximately 300-word chunks with 50-word overlap, additionally respecting the pinned embedding model's token limit. Use the same deterministic splitter for quota checks and indexing.
- Store embedding model ID, dimensions, and preprocessing version. Never compare vectors from incompatible configurations; changing the embedding configuration requires reindexing.
- Retrieve the top five chunks and prompt GPT-5.6 Luna to answer only when they support the answer, otherwise abstaining, with low reasoning effort on every request.
- Save changes immediately and exclude changed files until their new embeddings are committed.
- Delete files from retrieval immediately.
- Discard indexing results for superseded file versions or deleted projects.
- Show pending, ready, and failed indexing states, with retry support.

Recheck project availability and source versions before dispatching a generated answer. If sources changed during generation, regenerate at most once within the turn budget or report that sources are updating. At the explicitly requested live verification milestone, measure retrieval at the maximum vault size; target a p95 below one second for vector fetch and cosine scoring, excluding provider calls. Lower vault/chunk limits if the HTTP scan cannot meet this target before adding caching or another database.

### Usage limits and retention

Treat these as configurable starting limits, enforced before paid work is queued:

- Five active projects per user and 100 across the deployment.
- Ten accepted conversation turns per minute and 100 per day per user; 1,000 generation requests per day across the deployment, shared by NVIDIA and OpenAI and counting retries. Replayed webhook events do not consume another turn allowance.
- At most four generation requests and three builder tool calls per turn, 12,000 input tokens per generation request, 2,048 output tokens per request, and a two-minute turn deadline. For OpenAI, use `max_output_tokens=2048`, which includes reasoning tokens; an incomplete response follows the failure path instead of sending an unfinished answer. Trim history and retrieved context to fit. Retry and regeneration attempts share these budgets, including calls made inside the builder loop.
- At most ten pending conversation jobs per user and 100 pending jobs overall. Serialize conversation work per user; permit two concurrent generation requests globally and one indexing job. Indexing uses bounded embedding batches, at most 100,000 embedding input tokens per deployment per day, and at most five indexing attempts per file per hour, including automatic and manual retries.
- Retain at most 200 messages per conversation for at most 30 days. Retain only current file contents and current embeddings; purge superseded embedding rows and failed staging data. Bound staging to one current indexing version per file and the same project chunk limit.
- Retain completed job payloads and outbound bodies for seven days. Keep compact event, mutation, and outbound deduplication records for at least 30 days and no less than the verified provider redelivery window; reject expired jobs instead of replaying them. A project-deletion tombstone survives for that window so delayed work cannot recreate it. Expired tokens and confirmations are cleaned up routinely.

Maintain quotas and job claims in SpacetimeDB so restarts do not reset them. Return a clear limit or temporary-capacity response without invoking a model. Deduplicate and rate-limit these notices too. Budget exhaustion leaves existing files intact and indexing visibly pending; it does not trigger unbounded retries.

### Vault dashboard and API

Serve lightweight HTML/CSS/JavaScript from FastAPI, without a separate frontend service. Provide a file list, Markdown textarea, explicit Save, create/import/delete actions, and indexing status. No builder chat, project settings, system-prompt editor, or project switching.

Use unguessable bearer links valid for one hour from issuance. Store only token hashes in the authorization records; enforce expiry and project scope on every request. The backend inserts the actual link into the outbound message. Tool results record its capability record ID and expiry, and conversation histories record a link-issued placeholder, never the raw link. For crash-safe delivery, encrypt the capability-bearing outbox payload using a deployment secret outside the database and remove it on confirmed delivery, expiry, or revocation; this exception takes precedence over ordinary outbound-body retention. Keep tokens out of server logs and referrers. Expired links cannot save changes or be resent; users request another through iMessage.

Core application endpoints:

- `POST /webhooks/photon` — verified event ingestion.
- `GET /vault` — editor shell; link capability passed through the URL fragment.
- `/api/vault/files` — list, create, and upload.
- `/api/vault/files/{id}` — read, update, and delete.
- `POST /api/vault/files/{id}/reindex` — retry indexing.
- `GET /healthz` — deployment health.

File updates include the expected version; stale writes return a conflict rather than overwriting newer content. Imports with duplicate names require explicit replacement. Attachment imports require an owned editing focus; otherwise the builder asks which project to edit.

## Reliability and implementation sequence

1. **Establish integration contracts:** inspect the provisioned Photon configuration and existing SpacetimeDB schema; pin dependencies and provider API versions. Record Photon event IDs, signature/replay verification, inbound sender and destination fields, outbound idempotency scope/expiry, delivery lookup, and attachment access. Configure separate `NVIDIA_API_KEY` and `OPENAI_API_KEY` credentials through environment variables. Prepare provider smoke tests; run them only when explicitly requested. The integration gate requires an inbound event and outbound reply on the existing setup, a Nemotron builder tool call through NVIDIA's hosted API, an OpenAI `gpt-5.6-luna` response with low reasoning effort, OpenAI document/query embeddings, and an authenticated Python database read/write round trip.
2. **Deliver the first complete journey:** text the number, create a project with the Nemotron builder, receive a vault link, upload Markdown in the dashboard, share the code, and have a second user receive a cited GPT-5.6 Luna answer at low reasoning effort. Implement ownership, expiry, quotas, scoped histories, versioning, and durable jobs as part of this path. Use one Python container containing FastAPI and the durable-job worker, plus the existing managed database. Verify the second user's actual Photon number mapping before declaring this journey complete.
3. **Verify the first journey:** mocked integration tests cover the failure cases below that apply to the implemented first-journey capabilities. At an explicitly requested live verification step, exercise the journey with two users, check retrieval latency and representative grounded/unsupported questions, and verify crash recovery. Mocked success alone does not complete the live integration gate.
4. **Extend after the first journey passes:** add project rename/deletion confirmation, conversational file editing, Photon attachment imports, manual indexing retries, and broader natural-language navigation. Add each extension's applicable tests as it lands. Keep each extension small and independently reviewable.
5. **Finish deployment documentation:** record HTTPS webhook configuration, environment variables, configured limits, database/module deployment, and worker recovery procedures. Record actual verification results and any unverified provider behavior.

Verify Photon webhook signatures and replay timestamps using the pinned API contract before registering users. Filter out outgoing messages, delivery receipts, groups, and unsupported sender identities before creating conversation jobs. Atomically persist an accepted event receipt and its durable job before acknowledgment. Duplicate receipts return acknowledgment without enqueuing another job. When admission limits reject an event, persist its rejected outcome so provider retries cannot turn it into a second request.

Use these recovery rules:

- Give every accepted user event a stable operation ID. Persist each validated tool intent, arguments, target project, and step ID before executing its mutation. A transactional reducer records the mutation result under that step ID in the same transaction as the write. Retrying returns the recorded result; never rerun a completed create/update/delete or regenerate a completed tool intent after a crash.
- Save a file's new version and enqueue its indexing job atomically. Commit embeddings only if the file version and project are still current. Retried or superseded jobs cannot restore removed content.
- Claim jobs transactionally with a 60-second lease, a unique claim generation, and a heartbeat every 20 seconds. Expired claims may be reclaimed; every commit checks the current claim generation so an old worker cannot commit after reclamation. Retry transient failures with bounded backoff, no more than five attempts, within applicable turn/provider budgets. Permanent failures become visible failed jobs.
- Persist the final outbound payload, destination, and stable provider idempotency key before sending. Reuse the same payload and key on every attempt; record provider message IDs and delivery state. If a send times out after possible acceptance, reconcile through provider lookup or retry within its verified idempotency window. If neither is supported, mark delivery unknown and do not blindly resend. Do not claim exactly-once delivery beyond the provider's guarantees.
- Atomically store a completed turn's history, outbox record, and job completion. A separate leased outbox dispatch step sends it; a crash after a tool mutation resumes from its stored result rather than replaying the mutation.

Order conversation events per user across builder and customer modes. Process deterministic navigation commands as ordered state transitions without waiting for an in-flight model call, incrementing a routing revision when routing changes. Bind each model job and mutation intent to the resolved project, conversation, and routing revision; reject stale mutation intents before committing them. Recheck routing revision and project availability before committing history or dispatching a reply. A mode switch invalidates pending replies from the previous context; it cannot retract a message already submitted to Photon.

Log operation IDs, failures, queue delays, and indexing status without recording secrets, vault links, or file bodies. Provide clear user-facing failure messages instead of claiming a failed mutation succeeded.

Make small commits containing only task-owned changes. After implementation, have a reviewer agent examine authorization, routing, expiry, deletion, and retry behavior.

## Validation and deployment assumptions

Automated tests use mocked providers and in-process handlers; do not build or start the application for verification without an explicit request.

Cover:

- First-message registration and duplicate webhook delivery.
- Exact commands and natural-language equivalents, ambiguity, and ownership rejection.
- Independent builder focus and customer selection.
- Provider routing: builder calls use NVIDIA's hosted Nemotron API; project answers use OpenAI `gpt-5.6-luna` with low reasoning effort, and document/query embeddings use OpenAI. Project Q&A remains usable during an NVIDIA outage. Verify unavailable-model errors, incomplete/empty responses, and per-provider retries within shared budgets.
- Builder-loop bounds and per-user context isolation, including recovery without duplicate tool mutations and rejection of unknown tools or invalid arguments.
- Cross-user/project isolation, including malicious Markdown instructions.
- Immediately live empty projects and editable agent instructions.
- Markdown validation, quota enforcement, duplicate names, and conflicting saves.
- Token expiry and project-deletion revocation.
- Vault links absent from histories/tool results, encrypted pending delivery, and sensitive-payload cleanup on delivery, expiry, or revocation.
- Changed-file exclusion, indexing failures, stale jobs, and immediate deletion.
- Grounding prompts, citation formatting, and unsupported-question instructions; assess actual model behavior during the explicitly requested live evaluation rather than treating mocked outputs as proof of factual correctness.
- Conversation persistence, unsupported reset requests without state changes, mode-switch races, and outbound retries.
- Project deletion confirmation expiry and cancellation of remaining work.
- Crash after mutation commit but before job completion; retry returns the original result without another mutation.
- Atomic receipt/job and file/index-job creation, expired worker leases, and rejection of commits from stale workers.
- Ambiguous outbound acceptance, reuse of provider idempotency keys, and unknown-delivery handling.
- Per-user/global quotas, bounded tool loops, indexing retry limits, and retention cleanup without removing active deduplication protection.
- Authenticated database reads and reducer calls, with anonymous/unrelated identities denied at the database boundary.

The user has already provisioned the Photon project and line and reports capacity for 25 numbers, with one currently used for their personal assignment. New line provisioning is not a prerequisite in this plan. Exact assignment/routing behavior and second-user access remain integration checks, not assumptions that the existing assignment is a shared public destination. SpacetimeDB CLI access and the existing Maincloud database are confirmed; hosted Nemotron access, OpenAI GPT-5.6 Luna/embedding access, and deployed runtime credentials still require verification.

V1 excludes history reset, NeMo Fabric, NeMo Agent Toolkit, model training/self-hosting, URL ingestion, non-Markdown resources, group chats, public agent discovery, collaborative project ownership, separate numbers per agent, and Hermes integration. Live provider smoke tests and application startup require an explicit request and are planned early enough to validate integrations before broadening the implementation.
