# Deployment and recovery runbook

## Current hosted setup

On 2026-11-08, the user authorized migration and explicitly approved transferring
the existing credentials to `secure-amethyst-ferret`. The backend now runs in
`~/hypha` on that Brev server using
[`deploy/brev/compose.yaml`](../deploy/brev/compose.yaml). The laptop backend and
its ngrok process were stopped before the cloud worker started. The original
public hostname, Photon webhook, database, and encryption keys were preserved.

The user subsequently requested the dashboard on the same public hostname,
explicitly without authentication. `/dashboard/` and `/api/internal/` now route
through a gateway on loopback port 8011 to the separate dashboard on port 8010;
other routes still reach the messaging backend on port 8001. ngrok targets the
gateway. The dashboard deployment lives in `~/hypha-dashboard`, runs no messaging
worker, and exposes phone numbers and conversation history to anyone with the
URL. See [dashboard deployment](../deploy/brev/dashboard.md). Public page/assets,
overview/accounts/conversations/activity/costs, write rejection, and existing
backend routes passed checks. The earlier internal-API 404 below describes the
initial backend-only deployment, before this explicit exposure.

The backend binds to `127.0.0.1:8001` using Linux host networking. It reaches
Nemotron directly on port 8000 and speech directly on port 8002. The ngrok
system service supplies HTTPS; Docker and ngrok are enabled at boot, and all
three application/model containers use `restart: unless-stopped`. No laptop
or SSH forward is required. The Brev VM must remain running and funded.

Server `.env` and `ngrok.yml` files have mode 600 in a mode-700 directory.
The pre-migration server directory was backed up under `~/hypha-backups`.
Secrets and ngrok configuration are excluded from the Docker build context.
Use the [Brev operations guide](../deploy/brev/README.md) for updates and rollback;
always stop the old backend before starting a replacement.

Deployment checks passed: container dependencies/FFmpeg/tokenizer/temp storage,
exact Compose environment parsing, encryption-key round trip, authenticated
database read, Photon assignment lookup, Nemotron model discovery, and speech
readiness. Public `/healthz` and `/notes` passed; unsigned webhooks and notes API
requests without credentials returned 401. The internal dashboard API returned
404. A container file-permission issue found during the public notes check was
fixed by making packaged application files readable by the non-root app user.

A fresh real text/voice/notes journey with the laptop off remains the user's
acceptance check. No messages were sent by the migration checks. Database schema
and broader production gates in [verification status](verification.md) remain.
Docker restarts crashed processes, but a failed worker can leave Uvicorn alive
with `/healthz` returning 503; automatic recovery from that condition still needs
a health monitor or watchdog.

## Previous local setup (rollback reference)

The following describes the earlier laptop deployment, not the active host.
Do not start it while the cloud backend is running.

The user authorized live setup on 2026-11-07 (America/Winnipeg). Database and
model probes passed, and the owner’s real Sprout/notes-link exchange
was confirmed delivered. Windows runs one background backend and an ngrok
HTTPS tunnel; the existing WSL/Brev forward supplies Nemotron. The live URL and
all credentials are in ignored `.env`. This demo depends on the laptop and
tunnels staying online; it is not a hosted production deployment.

The builder and speech forwards now run together under
[`tools/inference_tunnel.sh`](../tools/inference_tunnel.sh). A persistent hidden
Windows `wsl.exe` process keeps WSL attached; SSH keepalives detect connection
loss and the script reconnects after three seconds. It binds only loopback:
local 8000 to Brev's builder 8000, and local 8003 to speech 8002. The local
dashboard keeps port 8002. Its Windows PID is recorded in
`%LOCALAPPDATA%/Temp/hackutd-inference-tunnels.pid`; timestamped diagnostics use
`hackutd-inference-tunnels-*.err.log` in the same directory. This is a running
demo process, not a Windows boot service.

For a later manual start from the repository in WSL, leave this command running:

```bash
bash tools/inference_tunnel.sh
```

The script rejects a second supervisor and stops its SSH child on Ctrl+C.

The listener PID and process logs are under `%LOCALAPPDATA%/Temp/` with names
`hackutd-backend-listener.pid`, `hackutd-backend.stderr.log`, and
`hackutd-ngrok.log`. The Python launcher can have a different PID from the actual
port-8001 listener. Stop the listener before starting a replacement; never
overlap workers. Access logs and ngrok request inspection are disabled.

The `tools.local` runner loads `.env` without shell evaluation. From an activated
environment, a later manual restart is:

```bash
python -m tools.local uvicorn app.main:production_app --factory --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

## Prerequisites and gates

Do not declare the MVP deployed from mocked success. First review
[verification status](verification.md). Obtain the backend database identity and
runtime token, OpenAI key with access to `gpt-5.6-luna` and embeddings, Photon API
key and webhook signing secret, and persistent delivery-encryption keys.

The existing Photon project is `hackUTD`; do not create another project or buy a
line. Read-only inspection found one active assignment, a 25-seat shared-line
capacity recorded in the plan, and no configured webhook destination.

The separate Nemotron deployment report records a successful live smoke test
from other work in this shared repository. See [that report](../deploy/nemotron/README.md).
This implementation did not repeat it. Application tool correctness, the
deployed private connection, and the other providers still need the live gate.

## Database

Use the pinned module and identity configuration described in
[the database contract](database-contract.md). Save the existing module/schema
identity before publishing and never use a data-clearing publish. The current
snapshot is a small integration scaffold; finish authenticated access verification
before replacing it with partitioned records and full domain reducers.

At the requested live step, after publishing with the backend identity and
exporting the environment, run the prepared probe with the application stopped:

```bash
python -m tools.verify_integrations database --live
```

Also export `DATABASE_UNRELATED_TOKEN` for the denial tests. The probe writes the
same state back at a new revision, reads it, and checks anonymous/unrelated reads
and writes are rejected. This is a real database mutation, not an offline test.
Direct SQL denial and stale-CAS rejection must also be recorded. Do not make
tables public to work around authorization failures.

## Provider checks and participant enrollment

After the requested private Nemotron connection is available and the keys are
exported, this command makes real inference and embedding requests:

```bash
python -m tools.verify_integrations models --live
```

It requires the exact configured models and checks real tool-call syntax; it
does not silently choose a substitute model. It does not create an application
project or send a message. Account/model errors stop the gate.

Preassign both demo participants before their first application message. Reuse
the owner's existing assignment. Preserve an operation ID for each enrollment:

```bash
python -m tools.enroll +15551234567 --operation-id demo-participant-2
```

This command can create a Photon assignment and consumes a seat. It lists first,
reuses an active mapping, checks capacity, and reconciles ambiguous creation by
listing before a retry. Only run it for the intended participant. Share each
participant's **returned destination**, which may differ from the owner's.
Require a new inbound text after assignment; earlier texts do not authorize a reply.
The application never creates an assignment in response to a webhook.
Assignments refresh at startup and every 60 seconds. Preassign demo participants
before startup; if someone is enrolled later, wait for the refresh before their
first text. A rejected receipt stays rejected, so they must send a fresh message
after the mapping is available.

## HTTPS and application startup

Configure a real HTTPS reverse proxy for `PUBLIC_URL`. Proxy the backend port
and do not log authorization headers, request/response bodies, or query strings.
The notes-editing link carries its token in the URL fragment; the browser removes it
from the address bar and uses an Authorization header. Set no caching and honor
the application's no-referrer and content-security headers.

Register `https://YOUR_HOST/webhooks/photon` in the existing Photon project,
using API version `2026-07-01` and the `message.received` event. Store the returned
secret outside source control. Verify the pinned payload and signature against
a real participant; the authenticated schema is preserved in
[photon-message-schema.json](photon-message-schema.json).

When building and starting have been explicitly requested:

```bash
docker compose build
docker compose up -d
```

Alternatively, for a requested local process:

```bash
uvicorn app.main:production_app --factory --host 127.0.0.1 --port 8001 --workers 1 --no-access-log
```

Compose publishes only `127.0.0.1:8001`; the HTTPS proxy owns public access.
Inside Docker, `127.0.0.1:8000` refers to that container, **not** a host SSH tunnel.
Set `NEMOTRON_BASE_URL` to a privately reachable service/tunnel destination for
the actual deployment. Do not expose the Brev inference port publicly.

`/healthz` checks database access and the background worker task. It does not
make model calls or prove end-to-end delivery. Startup loads current Photon
assignments, recovers claims, and starts one durable worker. Check health before
accepting traffic and alert on failures, queue age, failed indexing, and unknown
delivery counts without logging contents or secrets.

## Two-person acceptance checklist

1. Record both Photon assignment IDs and returned destinations privately.
2. Owner texts their assigned line after assignment, creates a Sprout with
   Nemotron, receives its stable code and a one-hour notes-editing link.
3. Upload Markdown, observe pending then ready, and verify conflicting saves
   return 409 rather than overwriting a newer version.
4. Second participant texts their own returned destination, sends `/use CODE`,
   and gets a cited OpenAI answer. Check a supported question, an unsupported
   question, malicious source instructions, and a Sprout without notes.
5. Exercise navigation during generation and both submission race orderings.
   Stop/restart after a tool mutation and after capability issuance, then during
   Photon submission. Confirm no duplicate mutation, token reissuance, or blind
   resend of an ambiguously accepted message.
6. Measure vector-fetch plus cosine scoring over a maximum-size notes collection across
   enough requests to report p95, excluding providers. Target <1 second. Reduce
   limits if needed; do not claim the singleton scaffold meets this target.
7. Record real OpenAI model access, source-answer behavior, webhook redelivery
   window, and Photon idempotency guarantees. Only then begin the extension phase.

## Recovery

Stop the old process before replacement; use no rolling overlap. A crash leaves
leases reclaimable with a new generation. Every tool mutation checks its current
claim and revision and returns its stored step result on replay. Deadlines,
attempt counts, quotas, and histories survive process replacement.

Capability issuance and its encrypted delivery material commit together. The
final encrypted outbox handoff and history/job completion are one transaction.
After delivery, expiry, revocation, or cancellation, no recoverable token copy
is retained. Losing the encryption key means pending links cannot be recovered;
do not regenerate capabilities by replaying a completed step. Revoke affected
pending work and request a fresh `/notes` turn.

An interrupted `submitting` record becomes `unknown`, never `pending`. Reconcile
only through a known Photon message ID. The adapter does not automatically resend
unknown messages because the provider's idempotency expiry/lookup-by-key guarantee
has not been verified. A later navigation cannot make an old reply retryable.
Known-message lookups have durable backoff and at most five attempts; unresolved
outcomes stay unknown for operator investigation. Transient storage failures
retry the worker loop up to five times, recovering uncertain submissions only
after the previous loop's tasks have stopped. Persistent failure makes health
unavailable and requires operator attention.

Index jobs commit only for the current note version. Changed or deleted notes
stop participating in retrieval immediately. Transient failures retry with
bounded backoff; budget exhaustion leaves current text intact and indexing pending.
Before the manual-retry extension, explicitly saving the note again creates a new
version/index job and still consumes the same hourly retry and embedding quotas.

Run cleanup routinely (the worker does so each minute). Histories retain at most
200 messages for 30 days; completed payloads retain seven days; compact event,
tool, and delivery deduplication records retain at least 30 days. Increase the
dedup window to cover the provider's verified redelivery policy. Expired events
are rejected even if delivered again with a fresh signature.
