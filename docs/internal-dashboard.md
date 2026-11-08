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

## Cost configuration

Cost reporting reads a local JSON file rather than calling billing APIs. Configure
your own rates and infrastructure amounts; the dashboard does not invent prices
or infer that a missing charge was free. Amounts use USD. From the repository root:

```bash
mkdir -p .local
cp dashboard-costs.example.json .local/dashboard-costs.json
```

Edit that ignored file in your editor. `DASHBOARD_COSTS_FILE` can select another
path. The dashboard reads configuration on refresh. Keep the copied example's
empty arrays until you have values to enter.

| Collection | Fields | Meaning |
| --- | --- | --- |
| `rates` | `model`, `effective_at`, `input_per_million`, `output_per_million`, `cached_input_per_million` | OpenAI USD per million tokens. Dates select the rate in force at request time. A null rate remains unknown. Include answer and embedding models separately. |
| `infrastructure` | `provider`, `start`, `end`, `amount_usd` | Estimated NVIDIA/Brev or SpacetimeDB cost for an interval. Provider is `nvidia` or `spacetimedb`. |
| `infrastructure` (GPU alternative) | `provider: "nvidia"`, `start`, `end`, `hourly_rate_usd` | A running GPU interval, including idle time. Use an hourly rate or an interval amount, not both. |
| `reported_bills` | `provider`, `start`, `end`, `amount_usd` | Manually entered provider billing totals, displayed separately from estimates. |

Configuration timestamps must use UTC (`Z` or `+00:00`), for example
`2026-11-08T00:00:00Z`. Intervals include their start and exclude their end.
Dashboard date filters include both selected calendar dates in UTC. Reported
bills are comparable only when their full period matches the selected deployment
period; they are never added to estimates or relabeled as actual per-user bills.

NVIDIA requests use shared GPU infrastructure costs, not token pricing. Allocation
uses recorded request windows, counting overlapping time once and splitting that
time's cost among concurrent requests. Idle or unmeasured time remains shared
overhead. Allocations cannot exceed the interval's total. SpacetimeDB
allocation uses each project's current logical storage footprint relative to the
whole snapshot; this is a current-footprint estimate, not historical metering.
Unattributed database storage stays in shared overhead.
Builder requests made before a project is selected remain account-level costs.
Account totals plus shared overhead reconcile with the deployment estimate;
project totals also require those account-level, unassigned costs to reconcile.

Unknown usage, missing rates, unmetered old jobs, and dates outside retained
history make totals incomplete. The known subtotal remains visible separately.
Cached input tokens are a subset of input tokens, and each provider retry counts
as a distinct request. Billing inputs, allocations, and estimates are all read only
in the dashboard.

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

Phase 3 passed independent code and style/UX reviews. Review fixes count overlapping
GPU time once, preserve cost filters through account/project drilldowns, and label
partial shared overhead and unconfigured GPU costs explicitly. Final checks passed:
96 dashboard/cost backend tests, 17 frontend tests, and TypeScript. The full Python
suite also passed at the end of phase 2 (283 tests and seven subtests).

```bash
python -m pytest tests/test_dashboard.py tests/test_dashboard_activity.py tests/test_dashboard_costs.py tests/test_dashboard_cost_api.py -q
cd dashboard
npm run typecheck
npm test
```
