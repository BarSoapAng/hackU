# Implementation and verification status

The work is phased against [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
This report distinguishes mocked behavior from deployed behavior.

| Phase | Status |
| --- | --- |
| Integration contracts | Implemented; primary contracts recorded, existing Photon and database configuration inspected read-only |
| First-journey implementation | Implemented: verified ingestion, routing, initial builder tools, encrypted vault capabilities, editor, indexing, project Q&A, budgets, durable jobs/outbox |
| First-journey mocked verification | Passing; independent subagents wrote provider, API, journey, authorization, race, and recovery tests |
| Live integration and two-person journey | Pending an explicitly requested live verification step and runtime configuration |
| Extensions after the live journey | Deferred as required by the plan: project rename/deletion, conversational file tools, Photon attachments, explicit reindex endpoint, broader natural-language navigation |
| Deployment | Configuration and runbook prepared; no application build, startup, publish, enrollment, or messaging performed by this implementation |

## Evidence

- Final offline checks: **164 Python tests and 7 subtests passed**, **11 TypeScript
  policy tests passed**, and TypeScript `--noEmit` checking passed. The Python
  suite includes actual application transitions replayed through the TypeScript
  proposal validator. The editor JavaScript passed `node --check`; pinned Python
  dependencies passed `pip check`.
- Photon read-only inspection confirmed `hackUTD`, payload API `2026-07-01`,
  `@photon-hq/contracts` 0.30.0, one active assignment, and no webhook destination.
  No assignment or destination was created. Tests include an independently
  published signature vector and the authenticated event schema.
- SpacetimeDB CLI 2.14.1 inspection found only empty lifecycle reducers in
  `hackutd-xmddq`. The new module has not been published. Python HTTP and TypeScript
  policy tests do not establish real runtime identity authorization.
- A separate concurrent repository commit, `3b0c43a`, records successful Nemotron
  inference/tool smoke tests on Brev. Its files were preserved. Those results do
  not establish application tools or the complete integration gate.
- The Python suite exercises a two-user journey using distinct assigned Photon
  destinations, actual application operations, and mocked provider transports.
  It verifies the requested OpenAI model, low reasoning, `store=false`, source
  metadata, absence of customer management tools, and provider isolation.
- Recovery tests exercise mutation replay, capability issuance/outbox handoff,
  stale claims, source updates, navigation races, unknown outbound acceptance,
  expiry, quotas, and retention. No server process is launched by these tests.

## Remaining gates and limitations

The current database is a private snapshot integration scaffold, not the final
partitioned domain schema. Reducer proposal checks improve defense in depth but
do not replace the planned domain reducers. Whole-state transfer includes
unrelated project vectors and cannot establish acceptable maximum-capacity p95.
Complete the authenticated live read/write/denial gate before expanding it;
finish partitioning and domain reducers before production deployment.

OpenAI account access, deployed Python database identity, real inbound/outbound
Photon delivery for both participants, real model grounding/abstention, maximum
vault retrieval p95, and deployed crash recovery remain unverified. The live
checks in `tools/verify_integrations.py` require `--live` and are not collected
by the test suite. No model/provider fallback is configured.

Photon's exact outbound idempotency expiry and redelivery window remain
unverified. Unknown sends are retained for read-only reconciliation, never
blindly retried. The initial deployment requires one active process; horizontal
or overlapping replicas are unsupported.

There is no project-deletion capability before the extension gate. Existing
checks reject deleted projects if a tombstone is present, but that is not proof
of a completed deletion workflow. The same distinction applies to attachments
and manual indexing retries, which are intentionally not exposed yet.

Re-run `python -m pytest -q`, `npm --prefix db run typecheck`, and
`npm --prefix db test` after changes. Record live evidence separately; mocked
citations cannot prove factual correctness.
