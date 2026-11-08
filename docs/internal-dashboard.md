# Internal dashboard

The dashboard is a local, read-only development tool. An account owns projects;
a participant messages a project's agent. The same user can be both. “View as”
changes the inspection scope, not the user's identity or messaging state.

## Local use

The existing `.env` supplies `DATABASE_URL`, `DATABASE_NAME`, and
`DATABASE_TOKEN`. Keep those credentials on the Python backend. From the
repository root, in separate Bash terminals:

```bash
# Windows Git Bash; use .venv/bin/activate on Linux/WSL.
source .venv/Scripts/activate
python -m tools.local uvicorn app.dashboard:local_app --factory --host 127.0.0.1 --port 8002 --no-access-log
```

```bash
cd dashboard
npm ci --ignore-scripts
npm run dev
```

Open `http://127.0.0.1:5173`. These are operator startup instructions; creating
this dashboard does not start either process. The local API has no worker,
webhook, agent editing, vault editing, or message sending routes. It is separate
from the public application and must not be exposed through a tunnel or proxy.

## Browsing

The overview lists registered users, project owners, active projects, and retained
conversations. Expand a user to inspect projects, or choose “View as” to open
their account. Accounts with no projects still appear in search and selection.
Builder conversations live at account level because they may span projects.
Project conversations are continuous threads per participant/project.

Account, project, and conversation scope is checked on the backend. Only safe
projections reach the browser; raw database snapshots, editing capabilities,
encrypted payloads, and provider credentials are excluded. Refreshes share a
short-lived backend snapshot cache. Lists and message pages are bounded.

Conversation pages show the latest messages first as a page, with each page in
chronological order. “Earlier messages” retrieves older pages. The source retains
at most 200 messages per thread within 30 days; detailed job traces expire after
seven days. Delivery status belongs to each reply, including multipart replies.

## Agent activity and usage

Activity follows the selected account and conversation. Each turn shows tool
outcomes and provider requests separately: receiving a provider response does not
mean the response was delivered to the participant. Queued first turns are visible
before their completed transcript exists. Tool arguments and results use explicit
field projections; vault-link tools expose only a readiness flag.

After the messaging backend is next restarted by the operator, request metadata
is recorded on its existing durable jobs. This includes NVIDIA builder requests,
OpenAI answers, and query/document embeddings. Each actual retry has its own
record. Records contain scope, model, timing, status, and provider-reported token
usage, without raw prompts, provider responses, or credentials. Document embedding
costs belong to the project and do not invent a participant conversation.

Recording the initial request must succeed before a provider is called. A failure
to finalize telemetry does not retry a paid request; its usage remains unknown.
Interrupted requests also have unknown usage. Missing historical data is labeled
“Not recorded,” never inferred as zero. Metadata follows job retention (normally
30 days); transcript and trace retention remain unchanged.

## Implementation phases

1. Account/project browsing and transcripts, with local-only API and React UI.
2. Sanitized agent activity and request-level usage/timing instrumentation.
3. Dated provider rates, cost estimates, shared overhead, and reconciliation.

Each phase receives a separate code review and style/UX review. Verification uses
mocked Python tests and frontend type checks; app startup and builds require an
explicit request. Runtime layout and browser behavior therefore remain unverified
until a requested preview.

Phase 1 passed independent backend/frontend code and style/UX reviews. Review
fixes keep pagination usable after history expires and identify the participant
in the transcript header. Its 24 mocked API tests and five frontend tests passed,
as did the TypeScript check.

```bash
python -m pytest tests/test_dashboard.py -q
cd dashboard
npm run typecheck
npm test
```

Phase 2 passed independent code and style/UX reviews. Review fixes preserve
request sequence and HTTP/provider status in the API and link each message to its
matching activity turn, including older pages. Ten frontend tests and 48 dashboard
API tests passed; telemetry, model, retrieval, and worker checks also passed.
The activity workspace has an optional 15-second live refresh that pauses while
the tab is hidden. “View activity” focuses and opens the matching turn; “Show all
turns” returns to the latest activity list.
