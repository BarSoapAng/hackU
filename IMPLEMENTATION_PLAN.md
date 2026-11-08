# iMessage Agent Builder MVP

## Summary

Build a deployed MVP where users text the existing Photon setup, create projects through conversation, and share immediately available Markdown-backed agents using project codes, within the configured messaging capacity and application quotas.

The only visual interface is a mobile-friendly, project-scoped vault editor. All builder conversations, project management, and agent configuration happen in iMessage.

Use:

- **Python / FastAPI:** webhooks, routing, authorization, agent execution, background work, and vault dashboard.
- **Photon:** use the existing `hackUTD` project. Read-only inspection confirmed shared iMessage mode with 25 user-assignment seats and one active assignment through `+1 628-316-6196`. This is a per-user assignment limit, not 25 dedicated lines. Application projects are selected by code rather than assigned their own numbers.
- **NVIDIA Nemotron:** powers only the builder agent through an OpenAI-compatible inference endpoint on the user's Brev GPU instance and a small Python tool-calling loop. No NVIDIA agent framework is required.
- **OpenAI API:** project-agent responses use GPT-5.6 Luna with low reasoning effort; document and query embeddings also use OpenAI so project Q&A does not depend on NVIDIA.
- **Haystack:** document processing, OpenAI embeddings, and retrieval; pass retrieved context to the OpenAI Responses API for answer generation.
- **SpacetimeDB:** all durable application data, including Markdown and embeddings.

Builder model: **NVIDIA Nemotron 3.5 Lightning 30B A3B**, checkpoint `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4`, served as `nemotron-builder`. This explicitly replaces the larger Super model to fit the selected single L40S GPU. On Ada, serve the quantized weights as W4A16 through Marlin rather than relying on native Blackwell FP4 instructions. Pin the checkpoint revision and vLLM image in [deploy/nemotron/compose.yaml](deploy/nemotron/compose.yaml), following NVIDIA's [Ada deployment recipe](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent/blob/main/docker/docker-compose.nemotron35-lightning.yaml).

Selected deployment: Brev instance `secure-amethyst-ferret`, accessible through the user's authenticated WSL Brev CLI. Inspection confirmed one L40S with 46,068 MiB VRAM, about 144 GiB system RAM, Ubuntu 22.04, and Docker/Compose. The approved upgrade from NVIDIA driver `565.57.01` to `580.178.04` and reboot completed. The selected CUDA 13 image is running; R580 or newer remains a prerequisite on replacement instances. [Driver requirements](https://docs.vllm.ai/en/v0.31.0/getting_started/installation/gpu/#running-on-systems-with-older-cuda-drivers)

Use the [Brev setup instructions and verification results](deploy/nemotron/README.md). The backend-only `NEMOTRON_API_KEY` is generated on the instance; inference binds to loopback and uses a private SSH tunnel for local development. Configure the builder base URL, model ID, and key independently from OpenAI. A deployed FastAPI service will need a private connection or authenticated HTTPS proxy to the GPU service. Serving permits 16,384 total context tokens and two sequences. Live verification on 2026-11-07 passed authentication, text generation, automatic tool-call parsing and synthetic-result follow-up, and two concurrent 11,431-input-token requests through the WSL tunnel. These checks disabled reasoning with `chat_template_kwargs={"enable_thinking": false}`; use that setting for the initial builder integration. Application tool execution still requires its own integration tests. Model/provider changes remain explicit deployment decisions. Training is out of scope.

Project-agent model: `gpt-5.6-luna` through the OpenAI Responses API, explicitly setting `reasoning={"effort": "low"}` and using `OPENAI_API_KEY`. Keep builder and project-agent model settings and clients separate. Model access must be verified with the deployment account; do not silently substitute another model or provider if access is unavailable. [Official OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [Reasoning settings](https://developers.openai.com/api/docs/guides/reasoning)

### Builder model integration

Implement a bounded Python loop that sends builder messages and tool schemas to Nemotron, validates returned tool calls, executes the backend's scoped operations, and returns their results to the model. Reconstruct each user's bounded builder context from SpacetimeDB. Keep ownership checks, durable tool-step records, and retry budgets in the backend; resume completed operations from their stored results rather than replaying mutations. Use separate Nemotron and OpenAI clients even if both use the same Python SDK.

## Messaging and agent behavior

### Identity and routing

Register a user from their normalized sender phone number on their first verified inbound message. No onboarding or account setup in the application. V1 supports phone-number-based, one-to-one iMessage conversations.

Photon enrollment is separate from application registration. Its shared-line assignment API maps a user's phone number to a server-selected destination, and sending requires an active assignment plus an inbound message to that line **after** assignment. Creating an assignment in response to an earlier text does not establish that prerequisite, and delivery of unassigned inbound messages to this project is unverified. Do not rely on that message to bootstrap shared-line access. [Assignment contract](https://photon.codes/docs/beta/api-reference/shared-line-assignments/create-shared-line-assignment), [Send contract](https://photon.codes/docs/beta/api-reference/messages/send-a-message)

For the first journey, the operator preassigns each demo participant through Photon, reusing an existing active assignment for that normalized phone number. The owner shares the returned destination and project code with that participant; the participant texts that assigned destination, and FastAPI automatically creates their application user. Share a code with an already-enrolled participant without creating another assignment. Check the 25-seat cap, serialize assignment creation per phone number, and reconcile an ambiguous creation result by listing assignments before retrying. Record the assignment ID and destination; never assume every participant receives the owner's number. Automatic Photon provisioning on first contact is a later option only if verified ingress and post-assignment messaging behavior support it. A public dedicated line is another deployment option, not a prerequisite or a purchase made by this plan.

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

- Users, Photon assignment mappings, and routing state.
- Projects, ownership, stable public codes, and editable instructions.
- Versioned Markdown files and indexing status.
- Chunks, embeddings, source headings, and file-version references.
- Conversation messages.
- Hashed vault tokens, encrypted pending capability-delivery material, and pending deletion confirmations.
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
- Ten accepted conversation turns per minute and 100 per day per user; 1,000 generation requests per day across the deployment, shared by Nemotron inference and OpenAI and counting retries. Replayed webhook events do not consume another turn allowance.
- At most four generation requests and three builder tool calls per turn, 12,000 input tokens per generation request, 2,048 output tokens per request, and a two-minute turn deadline. For OpenAI, use `max_output_tokens=2048`, which includes reasoning tokens; an incomplete response follows the failure path instead of sending an unfinished answer. Trim history and retrieved context to fit. Retry and regeneration attempts share these budgets, including calls made inside the builder loop.
- At most ten pending conversation jobs per user and 100 pending jobs overall. Run at most one model turn per user; deterministic commands use the ordered routing transitions below without waiting for model generation. Permit two concurrent generation requests globally and one indexing job. Indexing uses bounded embedding batches, at most 100,000 embedding input tokens per deployment per day, and at most five indexing attempts per file per hour, including automatic and manual retries.
- Retain at most 200 messages per conversation for at most 30 days. Retain only current file contents and current embeddings; purge superseded embedding rows and failed staging data. Bound staging to one current indexing version per file and the same project chunk limit.
- Retain completed job payloads and outbound bodies for seven days. Keep compact event, mutation, and outbound deduplication records for at least 30 days and no less than the verified provider redelivery window; reject expired jobs instead of replaying them. A project-deletion tombstone survives for that window so delayed work cannot recreate it. Expired tokens and confirmations are cleaned up routinely.

Maintain quotas and job claims in SpacetimeDB so restarts do not reset them. Return a clear limit or temporary-capacity response without invoking a model. Deduplicate and rate-limit these notices too. Budget exhaustion leaves existing files intact and indexing visibly pending; it does not trigger unbounded retries.

### Vault dashboard and API

Serve lightweight HTML/CSS/JavaScript from FastAPI, without a separate frontend service. Provide a file list, Markdown textarea, explicit Save, create/import/delete actions, and indexing status. No builder chat, project settings, system-prompt editor, or project switching.

Use unguessable bearer links valid for one hour from issuance. Store only token hashes in the authorization records; enforce expiry and project scope on every request. The backend inserts the actual link into the outbound message. Tool results record its capability record ID and expiry, and conversation histories record a link-issued placeholder, never the raw link.

Before the issuance reducer runs, generate the random token and authenticated-encrypt its delivery material with a deployment secret outside the database, binding the ciphertext to its capability, project, and tool-step IDs. In **one transaction**, check ownership, routing revision, and job claim; persist the token hash, expiry, encrypted delivery material, and completed tool-step result. A duplicate step returns the original capability reference and preserves its original ciphertext and expiry. After a crash following this commit, the backend recovers the same link from that ciphertext; it does not create another token or attempt to recover one from its hash. Ciphertext stays in backend-only delivery records, never in model inputs or tool results.

When the turn completes, atomically create its encrypted final outbox payload, record history/job completion, and remove the earlier delivery-material record. Thus either the issuance ciphertext or the final outbox ciphertext survives a crash. Remove every encrypted copy on confirmed delivery, expiry, or revocation; cancel unsent delivery and revoke an undelivered capability when its originating turn fails or is superseded. This cleanup overrides ordinary outbound-body retention. Preserve encryption keys across restarts and retain any old key needed by an unexpired pending payload. Keep tokens out of server logs and referrers. Expired links cannot save changes or be resent; users request another through iMessage.

Core application endpoints:

- `POST /webhooks/photon` — verified event ingestion.
- `GET /vault` — editor shell; link capability passed through the URL fragment.
- `/api/vault/files` — list, create, and upload.
- `/api/vault/files/{id}` — read, update, and delete.
- `POST /api/vault/files/{id}/reindex` — retry indexing.
- `GET /healthz` — deployment health.

File updates include the expected version; stale writes return a conflict rather than overwriting newer content. Imports with duplicate names require explicit replacement. Attachment imports require an owned editing focus; otherwise the builder asks which project to edit.

## Reliability and implementation sequence

1. **Establish integration contracts:** inspect the provisioned Photon configuration and existing SpacetimeDB schema; pin dependencies and provider API versions. Record Photon event IDs, signature/replay verification, inbound sender and destination fields, assignment reconciliation, outbound idempotency scope/expiry, delivery lookup, and attachment access. Configure the Brev Nemotron endpoint and `NEMOTRON_API_KEY` separately from `OPENAI_API_KEY`. Prepare provider smoke tests; run them only when explicitly requested. The integration gate requires an inbound event and outbound reply from a preassigned participant on the existing setup, a Nemotron builder tool call through the selected inference endpoint, an OpenAI `gpt-5.6-luna` response with low reasoning effort, OpenAI document/query embeddings, and an authenticated Python database read/write round trip.
2. **Deliver the first complete journey:** preassign two participants in Photon, have the owner text their assigned destination, create a project with the Nemotron builder, receive a vault link, upload Markdown in the dashboard, share the code and second participant's assigned destination, and have that participant receive a cited GPT-5.6 Luna answer at low reasoning effort. Implement ownership, expiry, quotas, scoped histories, versioning, and durable jobs as part of this path. Use one Python container containing FastAPI and the durable-job worker, plus the existing managed database and the separate Brev inference service. Verify the second user's actual Photon number mapping before declaring this journey complete.
3. **Verify the first journey:** mocked integration tests cover the failure cases below that apply to the implemented first-journey capabilities. At an explicitly requested live verification step, exercise the journey with two users, check retrieval latency and representative grounded/unsupported questions, and verify crash recovery. Mocked success alone does not complete the live integration gate.
4. **Extend after the first journey passes:** add project rename/deletion confirmation, conversational file editing, Photon attachment imports, manual indexing retries, and broader natural-language navigation. Add each extension's applicable tests as it lands. Keep each extension small and independently reviewable.
5. **Finish deployment documentation:** record HTTPS webhook configuration, environment variables, configured limits, database/module deployment, and worker recovery procedures. Record actual verification results and any unverified provider behavior.

Verify Photon webhook signatures and replay timestamps using the pinned API contract before registering users. Filter out outgoing messages, delivery receipts, groups, and unsupported sender identities before creating conversation jobs. Atomically persist an accepted event receipt and its durable job before acknowledgment. Duplicate receipts return acknowledgment without enqueuing another job. When admission limits reject an event, persist its rejected outcome so provider retries cannot turn it into a second request.

Use these recovery rules:

- Give every accepted user event a stable operation ID. Persist each validated tool intent, arguments, target project, and step ID before executing its mutation. A transactional reducer records the mutation result under that step ID in the same transaction as the write. Retrying returns the recorded result; never rerun a completed create/update/delete or regenerate a completed tool intent after a crash.
- Save a file's new version and enqueue its indexing job atomically. Commit embeddings only if the file version and project are still current. Retried or superseded jobs cannot restore removed content.
- Claim jobs transactionally with a 60-second lease, a unique claim generation, and a heartbeat every 20 seconds. Expired claims may be reclaimed; every commit checks the current claim generation so an old worker cannot commit after reclamation. Retry transient failures with bounded backoff, no more than five attempts, within applicable turn/provider budgets. Permanent failures become visible failed jobs.
- Persist the final outbound payload, destination, and stable provider idempotency key before sending. Reuse the same payload and key on every attempt; record provider message IDs and delivery state. If a send times out after possible acceptance, reconcile through provider lookup or retry within its verified idempotency window. If neither is supported, mark delivery unknown and do not blindly resend. Do not claim exactly-once delivery beyond the provider's guarantees.
- Atomically store a completed turn's history, outbox record, and job completion. Assistant history rows reference their outbox and delivery state; exclude pending, cancelled, failed, and unknown replies from model-visible history while retaining audit records. A separate leased outbox dispatch step sends it; a crash after a tool mutation resumes from its stored result rather than replaying the mutation.

### Ordered routing and turn state

Use the following rules across builder and customer modes:

- Atomically assign each admitted event a per-user sequence number with its receipt and job. This defines backend acceptance order; duplicates retain the original sequence. A short per-user sequencer admits events in that order, applies deterministic commands, and binds model jobs to the resolved mode, editing focus, customer selection, conversation, and routing revision. It never waits for a model response. Workers run queued model jobs in sequence, at most one active per user.
- `/builder`, `/edit`, and `/use` apply validated routing changes in that sequencer, incrementing the revision only when state changes. Invalid commands leave state unchanged. A change marks queued/running jobs and unsent replies bound to older revisions superseded. Already committed mutations remain committed; cancellation prevents further mutations and replies. `/projects`, `/help`, and `/vault` do not change routing; `/vault` uses the same durable issuance protocol as the builder tool.
- Every new tool intent records its expected routing revision and explicit target. Reducers check that revision, ownership, and current job claim at mutation commit. A builder tool that creates or selects a project may itself change routing: atomically persist its mutation, expected and resulting revisions, resulting routing state, tool result, and the originating job's updated continuation binding. Exempt only that job from this transition's cancellation; other jobs bound to the old revision are superseded. Validate subsequent tool calls against the updated binding, in order, rather than executing a batch against a stale snapshot.
- Recovery loads the stored result and continuation binding without reapplying the routing change. If a later command has advanced the user's revision again, supersede the recovered job instead of restoring its older focus. For example, creation at revision 7 may commit revision 8 and continue to issue its vault link at revision 8; a later `/use` committing revision 9 prevents that creation turn from continuing or sending a stale reply.
- Before committing a completed turn, check the job claim, current routing revision, project availability, and source versions. Store history, outbox, and completion atomically. Superseded turns keep their event/tool audit records but do not append an assistant answer to model-visible history; revoke undelivered capabilities and remove their ciphertext. This is cancellation of an unfinished turn, not history reset.
- Serialize final reply submission and routing transitions through the same per-user dispatch gate. Recheck the outbox's revision and project/capability/source validity under that gate immediately before sending. Hold the gate only for the bounded Photon submission attempt, never for model generation or delivery polling. If navigation wins the gate, cancel the stale reply; if submission wins, navigation may wait for that bounded attempt and cannot retract an accepted or ambiguously accepted message. Reconciliation of an ambiguous send remains read-only after a switch; never retry an old-context payload into a new revision.
- Persist `submitting` under that gate before contacting Photon. An interrupted submission recovers as `unknown`, never automatically as `pending`; cancellation treats it as possibly sent. Reconcile through provider lookup, and retry only within verified provider idempotency guarantees while the routing revision is still current. For this MVP, run one active FastAPI/worker process and stop it before replacement, with no overlapping replicas, so the per-user gate covers every routing transition and submission and recovery cannot overlap a surviving dispatcher.

Log operation IDs, failures, queue delays, and indexing status without recording secrets, vault links, or file bodies. Provide clear user-facing failure messages instead of claiming a failed mutation succeeded.

Make small commits containing only task-owned changes. After implementation, have a reviewer agent examine authorization, routing, expiry, deletion, and retry behavior.

## Validation and deployment assumptions

Automated tests use mocked providers and in-process handlers; do not build or start the application for verification without an explicit request.

Cover:

- First-message application registration and duplicate webhook delivery; existing Photon assignment reuse, assignment-capacity failure, ambiguous assignment reconciliation, and no assumed reply eligibility from a pre-assignment text.
- Exact commands and natural-language equivalents, ambiguity, and ownership rejection.
- Independent builder focus and customer selection.
- Provider routing: builder calls use the explicitly configured Nemotron endpoint/model; project answers use OpenAI `gpt-5.6-luna` with low reasoning effort, and document/query embeddings use OpenAI. Project Q&A remains usable during a Nemotron-provider outage. Verify unavailable-model errors, incomplete/empty responses, and per-provider retries within shared budgets.
- Builder-loop bounds and per-user context isolation, including recovery without duplicate tool mutations and rejection of unknown tools or invalid arguments.
- Cross-user/project isolation, including malicious Markdown instructions.
- Immediately live empty projects and editable agent instructions.
- Markdown validation, quota enforcement, duplicate names, and conflicting saves.
- Token expiry and project-deletion revocation.
- Vault links absent from histories/tool results, encrypted pending delivery, and sensitive-payload cleanup on delivery, expiry, or revocation.
- Crash after capability/tool-result commit but before outbox creation: recover the same token from the issuance ciphertext, without a second capability or extended expiry. Crash during the atomic outbox handoff leaves exactly one recoverable delivery record. Cancellation/expiry removes all ciphertext, and replay cannot recreate a revoked capability.
- Changed-file exclusion, indexing failures, stale jobs, and immediate deletion.
- Grounding prompts, citation formatting, and unsupported-question instructions; assess actual model behavior during the explicitly requested live evaluation rather than treating mocked outputs as proof of factual correctness.
- Conversation persistence, unsupported reset requests without state changes, mode-switch races, and outbound retries.
- Ordered admission of commands and model jobs; navigation during generation supersedes old work without waiting for the model. Project creation/selection advances its own continuation revision without cancelling itself; replay after a later navigation never restores old routing. Exercise both navigation-first and submission-first dispatch races, including ambiguous acceptance and prohibition of stale retries. Crash before/after Photon acceptance recovers `submitting` as `unknown`; cancellation after history/outbox commit excludes the unsent reply from model context without deleting audit records or delivered history.
- Project deletion confirmation expiry and cancellation of remaining work.
- Crash after mutation commit but before job completion; retry returns the original result without another mutation.
- Atomic receipt/job and file/index-job creation, expired worker leases, and rejection of commits from stale workers.
- Ambiguous outbound acceptance, reuse of provider idempotency keys, and unknown-delivery handling.
- Per-user/global quotas, bounded tool loops, indexing retry limits, and retention cleanup without removing active deduplication protection.
- Authenticated database reads and reducer calls, with anonymous/unrelated identities denied at the database boundary.

Read-only Photon inspection confirmed shared iMessage mode, a maximum of 25 user assignments, and one active assignment for the owner. The first journey preassigns the second participant and verifies their inbound message and reply on the destination returned by Photon. Unassigned inbound delivery remains unverified; automatic application registration does not imply automatic Photon enrollment. New line provisioning is not a prerequisite. SpacetimeDB CLI access and the existing Maincloud database are confirmed. The Brev Nemotron endpoint passed live inference smoke tests; OpenAI GPT-5.6 Luna/embedding access, authenticated application database operations, and the complete messaging journey still require live verification.

V1 excludes history reset, NeMo Fabric, NeMo Agent Toolkit, model training, URL ingestion, non-Markdown resources, group chats, public agent discovery, collaborative project ownership, separate numbers per agent, and Hermes integration. The user selected GPU self-hosting through Brev. Live provider smoke tests and application startup require an explicit request and are planned early enough to validate integrations before broadening the implementation.
