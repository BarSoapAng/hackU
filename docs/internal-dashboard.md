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

## Implementation phases

1. Account/project browsing and transcripts, with local-only API and React UI.
2. Sanitized agent activity and request-level usage/timing instrumentation.
3. Dated provider rates, cost estimates, shared overhead, and reconciliation.

Each phase receives a separate code review and style/UX review. Verification uses
mocked Python tests and frontend type checks; app startup and builds require an
explicit request. Runtime layout and browser behavior therefore remain unverified
until a requested preview.
