# Photon integration contract

Implementation phase: offline adapters and mocked validation completed. No message was sent, participant assigned, webhook destination created, or application started during implementation.

Subsequent authorized live setup: the existing Photon API key passed assignment
reads, a `2026-07-01` destination was created for `message.received`, and its
signing secret was saved privately in `.env`. The destination was enabled after
the public backend health check passed. The owner's real inbound text registered
their user and triggered project creation plus a vault link; Photon confirmed
the reply delivered after one submission. No additional participant was assigned.
The two-participant Q&A journey and provider retry-window guarantees remain pending.

Read-only inspection on 2026-11-07 confirmed `hackUTD` (`pho_prj_01m6vs9z63eh7sd0tfjyxq4qbd`), shared iMessage capacity of 25 assignments, one active assignment, and **no configured webhook destinations**. The owner assignment's destination is `+16283166196`; other participants must use their own returned destination. Participant phone numbers are not stored in this document.

## Pinned contracts

- Webhook payload version: `2026-07-01`, selectable preview; catalog reports `@photon-hq/contracts` `0.30.0`. The authenticated `message.received` JSON Schema is saved in [photon-message-schema.json](photon-message-schema.json).
- REST base: `https://api.photon.codes/v1`. The [official API OpenAPI schema](https://github.com/photon-hq/api/blob/ae9c7e032ce8955fdf11e0234521b73157dd649b/openapi/openapi.json) is pinned to commit `ae9c7e032ce8955fdf11e0234521b73157dd649b` (API info version `1.0.0`). Bearer project API key or authorized account service key is required. The adapter uses `httpx`; no Photon SDK retry policy is hidden underneath it.
- Verification follows the [official Photon Dify trigger](https://github.com/photon-hq/dify/blob/3083c38c41176bd06ccda062739b6029427c3086/trigger/provider/photon_trigger.py) at commit `3083c38c41176bd06ccda062739b6029427c3086`. A published [interoperability test vector](https://github.com/photon-hq/dify/blob/3083c38c41176bd06ccda062739b6029427c3086/trigger/tests/fixtures.py) independently tests our verifier. Older Spectrum and Advanced iMessage Kit header formats are not accepted.

## Inbound verification and normalization

Verify the raw bytes before parsing JSON. `webhook-id`, `webhook-timestamp`, and `webhook-signature` use Standard Webhooks. Strip `whsec_`, Base64-decode the secret, and HMAC-SHA256 the exact `id.timestamp.body` bytes. Compare Base64 `v1,` signatures in constant time and accept any matching signature during secret rotation. Reject timestamps more than 300 seconds in the past **or future**. Reject missing secrets and malformed signatures.

For supported events, read `data.object`. Require `type=message.received`, `source=imessage.photon`, `apiVersion=2026-07-01`, matching project IDs, `platform=imessage`, `status=received`, a `human` sender, an `agent` receiver, and a direct Space containing exactly those two participants. Text comes from `content.text` when `content.type=text`. Email handles, groups, outgoing messages, receipts, attachments, and events lacking a receiver are ignored before registration. Only normalize explicitly international phone numbers; do not infer country codes.

The durable deduplication identity is the body's event `id`; `message.id` is also retained. A delivery's `webhook-id` authenticates the signature but is not substituted for the event ID. `timestamps.receivedAt` is the authoritative receipt time preserved across replay and is used to check that the participant messaged **after** assignment. The envelope timestamp and current delivery timestamp do not prove this prerequisite.

The first journey must verify the schema against a real signed inbound event. The schema marks `receivedBy` optional; V1 intentionally ignores events without it because the correct sender line cannot safely be inferred.

## Multiple texts per reply

Photon supports separate text sends to an existing direct Space with an active
assignment. This uses the existing message-send API, not an assumed batch or
atomic-sequence API. Each reply can contain up to three messages. The first
retains the event's idempotency key; later bubbles use `EVENT:part:1` and
`EVENT:part:2`. The worker waits for `delivered`/`read` confirmation before
submitting the next bubble. `dispatched` and `accepted` trigger status lookups;
they are not delivery proof. Multi-text replies get two quick initial lookups,
then the existing bounded backoff. No uncertain submission is blindly replayed.

The encrypted outbox persists the remaining texts and delivered count. Each
bubble rechecks routing, assignment, source versions, and vault expiry before
sending. A failure, navigation change, or changed source stops the remaining
bubbles. A missing message ID after an ambiguous send leaves that sequence
stopped. Delivery checks for an earlier bubble cannot advance a later bubble.
Other replies wait behind an earlier active sequence; permanently unresolved
sequences do not block the conversation forever. Vault links are appended only
to the final bubble. Existing single-message outbox records remain compatible.

Mocked tests cover actual Photon request encoding, status polling, per-bubble
idempotency, restart recovery, partial history, cancellation, and stale lookup
races. No unsolicited live test messages were sent.

## Enrollment

Use `GET /projects/{id}/platforms/imessage/assignments`, following all pages and excluding released rows. Reuse an active normalized handle before creating. Count all active assignments, including unsupported email assignments, against the configured 25-seat cap. Photon remains the final capacity authority.

Explicit operator enrollment and share-page opt-in use the same method: serialize enrollment within one process, send a stable caller-provided `Idempotency-Key`, and use `POST` with `{"handle":"+..."}`. Store `assignmentId`, `assignedPhoneNumber`, and `createdAt`. A timeout/ambiguous server error triggers another assignment listing before any retry; if unresolved, report `PHOTON_ASSIGNMENT_UNKNOWN` without another write. Do not run multiple enrollment processes concurrently.

An agent's public `/a/CODE` page lets a recipient enter their own iMessage number and
explicitly connect before composing the first message. The API normalizes the number,
checks the public agent, limits connection attempts, reuses existing assignments,
and persists the returned destination before opening Messages. Connection and periodic
assignment refresh share a process-local gate. A secure encrypted browser cookie
remembers the recipient's routing for 30 days. A shared agent link never contains a
vault capability. No app user or conversation is created until a verified inbound text.
This demo still has 25 shared-line seats; the form does not prove control of a number,
so verified-phone onboarding/abuse controls remain necessary for a broad public rollout.
Messages links use the recipient's actual assigned destination and an encoded
`/use CODE` draft, with a visible copy fallback; device-specific launch and prefill
behavior still needs an actual Messages-client check.

Operator command, only when participant enrollment is requested:

```bash
python -m tools.enroll +14155550123 --operation-id demo-participant-2-enrollment-1
```

Set `PHOTON_PROJECT_ID` and `PHOTON_API_KEY` in the environment. Preserve the operation ID for retries of that enrollment. The command reports the assignment and destination and does not send messages. The application persists mappings at startup and refreshes them every 60 seconds; a refresh outage preserves the last known mapping. Preassign both demo participants before startup. If enrolling while the service runs, wait for a successful refresh before the participant texts; a message rejected before refresh stays rejected, so send a new message. Assignment creation does not establish reply eligibility; the participant must subsequently text that returned destination. Dispatch rechecks the stored assignment ID, destination, and creation time against the original inbound receipt.

## Outbound submission and reconciliation

After accepting a verified incoming text, a background task requests a read receipt using
`POST /projects/{id}/messages/{messageId}/read` with `Idempotency-Key: read:{messageId}`
and no body. The admission record claims the attempt atomically, so duplicate webhooks
do not submit it again. Receipt failures do not fail the webhook or interrupt the reply.
This is best effort: a process exit after claiming the attempt can lose the receipt;
ambiguous submissions are not retried. HTTP 202 means queued, and provider privacy
settings can suppress visibility. See [Photon's read-receipt contract](https://photon.codes/docs/beta/api-reference/messages/mark-a-message-as-read).

Persist the payload and idempotency key before calling `send`. `POST /projects/{id}/messages` receives `spaceId` from the inbound Space, `senderId` from `receivedBy.id`, and `{type: "text", format: "plain", text: ...}`. Do not derive the sender ID from a phone number or assume all participants share the owner's line.

The send endpoint returns HTTP 202 and a message representation. Preserve its exact status and provider ID: `dispatched` means submitted, `accepted` is channel acceptance without confirmed delivery, and `delivered` is confirmed delivery. Reconcile known IDs using `GET /projects/{id}/messages/{messageId}`. Only confirmed delivery should remove protected delivery material and make the reply visible in delivered model history.

**Unverified guarantees:** the fetched OpenAPI requires an idempotency key but does not establish its expiry or complete deduplication scope. No lookup by idempotency key is documented. The adapter therefore never automatically retries an ambiguous send. Transport failures, HTTP 408/409/5xx, and malformed success responses become `unknown`; no known ID means no safe automatic reconciliation. The worker must recover interrupted `submitting` records as `unknown`, never `pending`, and must keep reconciliation read-only after routing switches. Definitive HTTP errors are returned without logging provider bodies or text.

The exact webhook redelivery retention window also remains unverified. Retain compact event/outbound deduplication records for at least 30 days; confirm whether the provider's maximum retry window requires a longer period before enabling live traffic.

## Attachments and live gate

The REST specification exposes authenticated attachment metadata and content endpoints, but attachment import is a later extension. V1 ignores attachment events and uses vault UTF-8 Markdown upload. Before enabling Photon imports, verify authenticated download behavior, content limits, expected source event shape, and owner editing focus; never fetch an arbitrary model-supplied URL.

Pending live checklist: configure an HTTPS destination with the pinned payload version and `message.received`; securely configure its one-time signing secret; confirm two actual participant assignments and destinations; record signed inbound and outbound delivery evidence; verify post-assignment eligibility, provider idempotency scope/TTL, and redelivery window. All live writes and smoke tests require the explicitly requested milestone. Passing mocked tests is not proof of these provider behaviors.
