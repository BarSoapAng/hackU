# iMessage agent builder

FastAPI handles verified Photon messages, a Nemotron builder, OpenAI project Q&A,
and a project-scoped Markdown editor. Application state lives in a private
SpacetimeDB module; the browser talks only to FastAPI.

The first journey is implemented and covered by mocked integration tests. The
live two-person journey is **not complete**. See [verification status](docs/verification.md)
for the remaining integration gates and deferred extensions. In particular,
the database currently uses an integration-stage snapshot: partitioned records,
full domain reducers, and measured capacity remain required before production.

## Local checks

Use Python 3.12 or newer and a supported Node version from [db/package.json](db/package.json).
The shell examples use Bash. Installing dependencies and running these tests
does not start the application or call paid providers.

```bash
python -m venv .venv
# Windows Git Bash: source .venv/Scripts/activate
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pytest -q
cd db
npm ci --ignore-scripts
npm run typecheck
npm test
```

The tokenizer may download its public vocabulary on first use. Haystack telemetry
is disabled in the test suite and container. Unit and integration tests use
in-process handlers and mocked transports, including the two-user journey.
There is no development database or in-memory fallback in production.

## Configuration

Copy `.env.example` to an untracked `.env`. Export its values through your secret
manager or use the local runner below when running outside Compose. Supply separate
Nemotron and OpenAI credentials. The database runtime token must belong to the
identity configured in the module, independently of the CLI's management login.
Use the Photon webhook secret for payload version `2026-07-01`.

The local runner reads `.env` without executing it as shell code and preserves
quoted JSON. After stopping any existing backend, start one process with:

```bash
python -m tools.local uvicorn app.main:production_app --factory --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

Use the same runner for explicitly requested live probes:

```bash
python -m tools.local tools.verify_integrations database --live
python -m tools.local tools.verify_integrations models --live
```

`ENCRYPTION_KEYS` is a JSON map of key IDs to URL-safe base64-encoded 32-byte keys.
Generate a key locally, keep it outside the database, and preserve it across
restarts. `ACTIVE_KEY` selects the key for new ciphertext; retain old keys until
their pending capability/outbox payloads have expired or been delivered.
Do not log credentials, tokens, request bodies, or vault URL fragments.

Defaults are in [app/config.py](app/config.py). `LIMIT_<UPPERCASE_FIELD>` can lower
a limit; increasing the starting ceilings requires reviewing database policy
and live capacity. `LIMIT_DEDUP_DAYS` can be increased and must be at least 30.

## Runtime and deployment

Follow [the deployment runbook](docs/deployment.md) at the explicitly requested
live milestone. Building, publishing, application startup, Photon enrollment,
and provider smoke tests are separate operator actions. The supplied Dockerfile
and Compose configuration have not been built or started by this implementation.

The backend must run as **one process with no overlapping replacement**. Its
per-user dispatch gates serialize navigation against the bounded Photon send.
Durable leases and claims protect against stale job commits. Scaling replicas
requires replacing the process-local dispatch gates first.

Acorn greets users with a short capabilities overview. `/help` shows that same
overview; ask about slash commands or send `/commands` for the separate command list.
Commands are `/projects`, `/edit` (return to creating/editing), `/edit CODE`,
`/use CODE`, `/vault`, `/share [CODE]`, `/help`, and `/commands`. File CRUD and imports
use the mobile vault editor. History reset is unsupported. The initial builder
tools create/list/select owned projects, update instructions, and issue links;
later tools remain behind the live journey gate.

`/share` provides a public `/a/CODE` link, separate from the private vault link.
First-time recipients enter their iMessage number and explicitly connect it;
existing Photon assignments are reused and new ones respect the shared 25-seat cap.
The page opens Messages with `/use CODE` prefilled and provides a copy fallback.
The person still taps Send. An encrypted, HTTP-only browser cookie remembers the
connection for 30 days. Exact app-opening/prefill behavior needs a real device check.

Provider details: [Photon](docs/photon-contract.md),
[models and retrieval](docs/model-contract.md), and
[SpacetimeDB](docs/database-contract.md).

For local, read-only account/project inspection, see the
[internal dashboard](docs/internal-dashboard.md). It runs separately from the
public application and never starts messaging workers.
