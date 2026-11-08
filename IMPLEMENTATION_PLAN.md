# iMessage Agent Builder MVP

## Summary

Build a deployed MVP where anyone can text a dedicated Photon number, create projects through conversation, and share immediately available Markdown-backed agents using project codes.

The only visual interface is a mobile-friendly, project-scoped vault editor. All builder conversations, project management, and agent configuration happen in iMessage.

Use:

- **Python / FastAPI:** webhooks, routing, authorization, agent execution, background work, and vault dashboard.
- **Photon:** one project and one dedicated iMessage line serving all users and application projects.
- **NVIDIA:** Nemotron for both builder and project-agent responses, with NeMo Agent Toolkit managing the builder workflow.
- **Haystack:** document processing, embeddings, retrieval, and grounded response generation.
- **SpacetimeDB:** all durable application data, including Markdown and embeddings.

Default generation model: `nvidia/nemotron-3-super-120b-a12b`, configurable through deployment settings. Haystack provides NVIDIA generation and embedding integrations. [NVIDIA model](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/playground), [Haystack integration](https://docs.haystack.deepset.ai/docs/nvidiachatgenerator)

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
| `/reset` | Clear history for the current conversation |
| `/help` | Explain available commands |

Natural-language requests invoke the same validated operations. Clarify ambiguous names before switching. Invalid commands leave state unchanged.

Keep editing focus separate from customer-agent selection. Only the backend changes routing state or grants access; model-supplied user IDs or ownership claims are never trusted. A customer agent may request navigation, but receives no project-management tools.

### Builder capabilities

Expose narrowly scoped tools to:

- Create, list, rename, select, and delete owned projects.
- Read and update the project agent's configurable instructions.
- List, read, import, create, update, and delete Markdown files.
- Issue vault links and retry failed indexing.

Creation atomically initializes the project, empty vault, default agent configuration, and unique stable public code. It selects the new project for editing and returns its code immediately. Agents are live from creation; an empty vault produces a helpful "no sources available" response.

Project edits use the selected editing focus. Requests targeting a different project must explicitly select that project through the validated operation.

Deleting a project requires `delete <project-code>` within five minutes of a deletion request. Confirmation is bound to the requesting owner and exact project. Deletion immediately disables access, revokes vault links, cancels pending work, and removes its files, embeddings, agent settings, and conversation data. It does not delete the shared Photon project.

### Project agents

Give each project a default instruction template requiring concise, source-grounded answers with filename/heading citations.

Owners may change tone, purpose, and domain instructions through iMessage. Application-enforced restrictions remain outside those editable instructions: project isolation, source-only factual answers, and no management permissions.

Treat Markdown and conversation quotations as content, never as authorization to call tools or switch modes. Never create a vault editing link for a customer seeking source citations.

Persist histories separately for the user's builder and each user/project Q&A conversation. Supply a bounded recent history to models. `/reset` clears only the current history, preserving files, settings, and editing focus. Historical answers do not substitute for currently available sources.

## Storage, retrieval, and vault interface

### SpacetimeDB integration

Use a small TypeScript database module defining tables and transactional reducers. Keep application and agent logic in Python. Connect Python through authenticated HTTP query/reducer calls; the browser accesses only FastAPI.

SpacetimeDB supports TypeScript modules and HTTP access. Its HTTP interface is less optimized than SDK WebSocket connections, an acceptable initial tradeoff for this bounded MVP. [Modules](https://spacetimedb.com/docs/databases/), [HTTP API](https://spacetimedb.com/docs/http/database/)

Store these logical records:

- Users and routing state.
- Projects, ownership, stable public codes, and editable instructions.
- Versioned Markdown files and indexing status.
- Chunks, embeddings, source headings, and file-version references.
- Conversation messages.
- Hashed vault tokens and pending deletion confirmations.
- Inbound event receipts, durable jobs, and outbound delivery records.

Restrict database access to the backend service identity. Every user-facing operation checks ownership or its project-specific vault capability. Reducers enforce version checks and atomic updates.

### RAG pipeline

Use NVIDIA embeddings through Haystack, initially `nvidia/llama-3.2-nv-embedqa-1b-v2`. Store vectors in SpacetimeDB and compute cosine similarity in Python over the selected project's current, indexed chunks. This avoids introducing a second database; native vector indexing is not part of SpacetimeDB's documented index types. [Embedding integration](https://github.com/deepset-ai/haystack-integrations/blob/main/integrations/nvidia.md), [SpacetimeDB indexes](https://spacetimedb.com/docs/tables/indexes/)

- Accept UTF-8 `.md` files through dashboard uploads and Photon attachments.
- Enforce 100 files per project, 256 KiB per file, and 5 MiB total text.
- Preserve headings and filenames; split into approximately 300-word chunks with 50-word overlap.
- Retrieve the top five chunks, then generate an answer only when their contents support it.
- Save changes immediately and exclude changed files until their new embeddings are committed.
- Delete files from retrieval immediately.
- Discard indexing results for superseded file versions or deleted projects.
- Show pending, ready, and failed indexing states, with retry support.

Recheck source versions before sending a generated answer. If sources changed during generation, regenerate against the current vault or report that sources are updating.

### Vault dashboard and API

Serve lightweight HTML/CSS/JavaScript from FastAPI, without a separate frontend service. Provide a file list, Markdown textarea, explicit Save, create/import/delete actions, and indexing status. No builder chat, project settings, system-prompt editor, or project switching.

Use unguessable bearer links valid for one hour from issuance. Store only token hashes; enforce expiry and project scope on every request. Keep tokens out of server logs and referrers. Expired links cannot save changes; users request another through iMessage.

Core application endpoints:

- `POST /webhooks/photon` — verified event ingestion.
- `GET /vault` — editor shell; link capability passed through the URL fragment.
- `/api/vault/files` — list, create, and upload.
- `/api/vault/files/{id}` — read, update, and delete.
- `POST /api/vault/files/{id}/reindex` — retry indexing.
- `GET /healthz` — deployment health.

File updates include the expected version; stale writes return a conflict rather than overwriting newer content. Imports with duplicate names require explicit replacement. Attachment imports require an owned editing focus; otherwise the builder asks which project to edit.

## Reliability and implementation sequence

1. **Establish integrations:** pin dependencies and provider API versions; implement Photon, NVIDIA, and SpacetimeDB adapters using documented contracts. Configure credentials through environment variables.
2. **Implement state and routing:** users, project lifecycle, ownership checks, commands, natural-language routing tools, confirmation, and scoped history.
3. **Implement vault and RAG:** file operations, capability links, indexing jobs, retrieval, and citations.
4. **Connect workflows:** NeMo builder tools, project Q&A pipeline, Photon inbound/outbound messaging, and attachment import.
5. **Prepare deployment:** one Python container with a durable-job worker and managed SpacetimeDB; document HTTPS webhook configuration and required environment variables.

Verify Photon webhook signatures using its documented scheme before registering users. Persist accepted events before acknowledgment, deduplicate retries, serialize conversation work, and retry transient provider failures with bounded backoff. Store outbound idempotency identifiers so retries do not intentionally resend completed replies.

Capture project and conversation context when work begins. Mode switches invalidate stale replies rather than sending an answer from the previous agent after a switch.

Log operation IDs, failures, queue delays, and indexing status without recording secrets, vault links, or file bodies. Provide clear user-facing failure messages instead of claiming a failed mutation succeeded.

Make small commits containing only task-owned changes. After implementation, have a reviewer agent examine authorization, routing, expiry, deletion, and retry behavior.

## Validation and deployment assumptions

Automated tests use mocked providers and in-process handlers; do not build or start the application for verification without an explicit request.

Cover:

- First-message registration and duplicate webhook delivery.
- Exact commands and natural-language equivalents, ambiguity, and ownership rejection.
- Independent builder focus and customer selection.
- Cross-user/project isolation, including malicious Markdown instructions.
- Immediately live empty projects and editable agent instructions.
- Markdown validation, quota enforcement, duplicate names, and conflicting saves.
- Token expiry and project-deletion revocation.
- Changed-file exclusion, indexing failures, stale jobs, and immediate deletion.
- Source-supported answers, valid citations, and unsupported-question abstention.
- Conversation persistence, reset, mode-switch races, and outbound retries.
- Project deletion confirmation expiry and cancellation of remaining work.

Deployment requires a provisioned **dedicated Photon line** and suitable NVIDIA/SpacetimeDB access. The existing `+1 628-316-6196` shared assignment is not assumed to become that dedicated line. Provisioning and paid subscriptions are external prerequisites, not changes already made.

V1 excludes URL ingestion, non-Markdown resources, group chats, public agent discovery, collaborative project ownership, separate numbers per agent, and OpenAI/Hermes integration. Live provider smoke tests and application startup remain explicit follow-up verification steps.
