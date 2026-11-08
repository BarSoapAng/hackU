# Implementation and verification status

The work is phased against [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
This report distinguishes mocked behavior from deployed behavior.

| Phase | Status |
| --- | --- |
| Integration contracts | Implemented; primary contracts recorded, existing Photon and database configuration inspected read-only |
| First-journey implementation | Implemented: verified ingestion, routing, initial Hypha tools, encrypted notes-editing capabilities, editor, indexing, Sprout Q&A, budgets, durable jobs/outbox |
| First-journey mocked verification | Passing; independent subagents wrote provider, API, journey, authorization, race, and recovery tests |
| Live integration and two-person journey | Provider/database checks and the owner’s real Sprout/notes-link exchange passed; note upload/Q&A and second-participant checks remain pending |
| Voice notes | Implemented behind a disabled flag; offline voice/service tests and reviewer checks pass. FFmpeg decoding with real iMessage audio, NeMo GPU startup/capacity, and the real voice journey remain rollout gates |
| Extensions after the live journey | Deferred as required by the plan: Sprout rename/deletion, conversational note tools, general Photon attachment imports, explicit reindex endpoint, broader natural-language navigation |
| Deployment | Local Windows backend and ngrok HTTPS tunnel running; Photon webhook enabled; production deployment remains pending |

## Evidence

- At the user's explicit live-setup request on 2026-11-07 (America/Winnipeg),
  the configured database passed authenticated read/write, stale-CAS rejection,
  anonymous/unrelated read/write denial, and private SQL denial (HTTP 403).
  The user had published the module before these checks; no clearing publish
  was performed during setup.
- Live Nemotron tool calling, an OpenAI `gpt-5.6-luna` response at low reasoning,
  and Haystack document/query embeddings passed with the configured credentials.
- The local and HTTPS `/healthz` and `/notes` endpoints returned HTTP 200;
  unsigned webhook requests and unauthenticated notes API requests returned 401.
  One Photon destination was created and enabled for `message.received` using
  API version `2026-07-01`. Its signing secret is stored only in ignored `.env`.
- A real message from the preassigned owner registered their application user,
  created the Demo Sprout, and issued a notes-editing capability. Hypha completed
  in three generation requests and one worker attempt. Photon confirmed the
  reply delivered after one submission; no duplicate Sprout or reply was created.
- The backend runs as one Windows process, with a background ngrok tunnel and
  the existing WSL/Brev inference forward. Keep the laptop awake for this local
  demo. Two targeted tests cover the new `.env` runner's quoting and secret
  preservation; no environment secrets were committed.
- Final offline checks: **164 Python tests and 7 subtests passed**, **11 TypeScript
  policy tests passed**, and TypeScript `--noEmit` checking passed. The Python
  suite includes actual application transitions replayed through the TypeScript
  proposal validator. The editor JavaScript passed `node --check`; pinned Python
  dependencies passed `pip check`.
- Initial Photon read-only inspection confirmed `hackUTD`, payload API `2026-07-01`,
  `@photon-hq/contracts` 0.30.0, one active assignment, and no webhook destination.
  No assignment or destination was created during the offline phase. Tests include an independently
  published signature vector and the authenticated event schema.
- Initial SpacetimeDB CLI 2.14.1 inspection found only empty lifecycle reducers in
  `hackutd-xmddq`. The module was subsequently published by the user. Python HTTP and TypeScript
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
unrelated Sprout vectors and cannot establish acceptable maximum-capacity p95.
The authenticated live read/write/denial gate has passed. Finish partitioning
and domain reducers before production deployment.

Real inbound/outbound Photon delivery for the second participant, dashboard
upload followed by Q&A, real model grounding/abstention, maximum
notes retrieval p95, and deployed crash recovery remain unverified. The live
checks in `tools/verify_integrations.py` require `--live` and are not collected
by the test suite. No model/provider fallback is configured.

Photon's exact outbound idempotency expiry and redelivery window remain
unverified. Unknown sends are retained for read-only reconciliation, never
blindly retried. The initial deployment requires one active process; horizontal
or overlapping replicas are unsupported.

There is no Sprout-deletion capability before the extension gate. Existing
checks reject deleted Sprouts if a tombstone is present, but that is not proof
of a completed deletion workflow. The same distinction applies to attachments
and manual indexing retries, which are intentionally not exposed yet.

Re-run `python -m pytest -q`, `npm --prefix db run typecheck`, and
`npm --prefix db test` after changes. Record live evidence separately; mocked
citations cannot prove factual correctness.
