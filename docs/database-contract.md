# SpacetimeDB integration gate

The module in `db/` is pinned to `spacetimedb` **2.14.1**. On 2026-11-07,
read-only inspection of Maincloud `hackutd-xmddq` found no application tables,
views, or procedures, only empty `init`, `onConnect`, and `onDisconnect`
lifecycle reducers. The installed Windows CLI reports 2.14.1. It is available
from Git Bash after `source ~/.bashrc`; the inspected WSL Ubuntu PATH did not
contain it. No module was published and no database rows were changed.

## Boundary and transaction contract

`app_state` is private. The backend calls `read_state` and `commit_state` over
authenticated HTTP; the browser never receives database credentials. All
connections and exported operations compare the authenticated sender identity
with the required `BACKEND_IDENTITY` module environment value. There is no
first-caller registration or publicly readable application table. A database
owner retains platform administration privileges, as with other SpacetimeDB
private tables. See [table permissions](https://spacetimedb.com/docs/tables/access-permissions/).

The owner supplies `BACKEND_IDENTITY` at publish time. It is the 64-digit hex
identity belonging to the backend's separately supplied runtime bearer token,
not the database identity. Changing this module environment value is a
management operation; the backend exposes no identity-changing reducer.
Environment variables are declared in the module and configured through the
[publish-time environment contract](https://spacetimedb.com/docs/databases/environment-variables/).

The Python adapter uses these exact calls with `confirmed=true`:

- `POST /v1/database/{database}/call/read_state`, body `[]`: returns a JSON
  string containing `{"revision":"0","state":{}}`.
- `POST /v1/database/{database}/call/commit_state`, body `[expected_revision,
  serialized_state]`: commits only at the expected revision and increments it.

The read procedure uses one `withTx` transaction. The reducer rejects stale
revisions and malformed state without mutation. Missing singleton state reads
as revision zero, allowing a safe first write when updating the existing empty
module (an update does not rerun `init`). See [HTTP calls](https://spacetimedb.com/docs/http/database/)
and [procedure transactions](https://spacetimedb.com/docs/functions/procedures/).

The pinned [2.14.1 HTTP implementation](https://github.com/clockworklabs/SpacetimeDB/blob/v2.14.1/crates/client-api/src/routes/database.rs)
returns HTTP 200 with an empty body for successful reducers, HTTP 530 with the
reducer error for rejected reducers, and a JSON-encoded return value for
procedures. The adapter retries only an explicit `state revision conflict`.
Timeouts, authorization failures, unexpected responses, and unknown commit
outcomes fail closed. Retrying an uncertain application operation must use its
durable operation ID; the adapter does not blindly replay it.

`StateStore.transaction(fn)` reruns a pure synchronous callback on a fresh
snapshot after a CAS conflict. It commits the callback's entire resulting state
atomically and returns its result. Random IDs, encryption, provider calls, and
other side effects belong outside the callback. `MemoryStore` is an isolated
test double, never a production fallback.

## Scope limitation before the live gate

This deliberately small integration-gate schema stores all logical records in
one snapshot. Python transaction callbacks implement ownership, domain quotas,
leases, versions, and operation deduplication; the database enforces backend
identity, JSON-object shape, and atomic revision comparison. These are **not**
independently enforced domain reducers. A compromised backend can propose
invalid domain state. Whole-state reads/writes also transfer unrelated project
vectors and do not establish the plan's maximum-vault retrieval performance.

Before production or expanding beyond the integration gate, replace the
snapshot with partitioned private records and domain reducers, or explicitly
approve the reduced architecture and lower measured capacity. Passing the
mocked tests does not satisfy the authenticated live read/write/denial gate or
prove the requested sub-second p95. The existing database remains untouched
until live verification and publishing are explicitly requested.

## Offline checks and later deployment

The checks below do not build a module, start an application, or call providers:

```bash
python -m pytest tests/test_database.py -q
cd db
npm ci --ignore-scripts
npm run typecheck
npm test
```

Use a Node release accepted by the SDK's pinned engine range:
`^22.23.4 || ^24.22.1 || >=26.12.1`. Local TypeScript and policy checks passed
under the installed Node 22.14.0, but npm reported its unsupported engine; that
does not validate host execution. Eighteen mocked Python adapter tests and two
TypeScript policy tests passed; the module type check passed with no output.

At an explicitly requested live step, first obtain an explicit backend runtime
identity/token pair and preserve it outside source control. Configure the
module's `BACKEND_IDENTITY` through the declared shell environment or the
CLI's `spacetime.json` environment mapping, then publish without clearing data:

```bash
source ~/.bashrc
spacetime describe hackutd-xmddq --server maincloud --json
# Set BACKEND_IDENTITY to the backend token's verified identity before this step.
spacetime publish hackutd-xmddq --server maincloud --module-path db --delete-data=never
```

Publishing builds the module and has **not** been run. Record the schema and
module hash before and after publishing. Verify an authenticated Python read,
a CAS write/read round trip, a stale-CAS rejection, and read/write failures
using both no token and an unrelated identity. Confirm direct SQL access to
the private table is denied for those identities. Never make the table public
to resolve a failed probe. Record results without tokens or application data.
