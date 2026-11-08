# Photon integration contract

Implementation phase: offline adapters and mocked validation completed. No message was sent, participant assigned, webhook destination created, or application started during implementation.

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

## Enrollment

Use `GET /projects/{id}/platforms/imessage/assignments`, following all pages and excluding released rows. Reuse an active normalized handle before creating. Count all active assignments, including unsupported email assignments, against the configured 25-seat cap. Photon remains the final capacity authority.

The explicit operator method serializes enrollment within one process, sends a stable caller-provided `Idempotency-Key`, and uses `POST` with `{"handle":"+..."}`. Store `assignmentId`, `assignedPhoneNumber`, and `createdAt`. A timeout/ambiguous server error triggers another assignment listing before any retry; if unresolved, report `PHOTON_ASSIGNMENT_UNKNOWN` without another write. Do not run multiple enrollment processes concurrently.

Operator command, only when participant enrollment is requested:

```bash
python -m tools.enroll +14155550123 --operation-id demo-participant-2-enrollment-1
```

Set `PHOTON_PROJECT_ID` and `PHOTON_API_KEY` in the environment. Preserve the operation ID for retries of that enrollment. The command reports the assignment and destination and does not send messages. The application persists mappings at startup and refreshes them every 60 seconds; a refresh outage preserves the last known mapping. Preassign both demo participants before startup. If enrolling while the service runs, wait for a successful refresh before the participant texts; a message rejected before refresh stays rejected, so send a new message. Assignment creation does not establish reply eligibility; the participant must subsequently text that returned destination. Dispatch rechecks the stored assignment ID, destination, and creation time against the original inbound receipt.

## Outbound submission and reconciliation

Persist the payload and idempotency key before calling `send`. `POST /projects/{id}/messages` receives `spaceId` from the inbound Space, `senderId` from `receivedBy.id`, and `{type: "text", format: "plain", text: ...}`. Do not derive the sender ID from a phone number or assume all participants share the owner's line.

The send endpoint returns HTTP 202 and a message representation. Preserve its exact status and provider ID: `dispatched` means submitted, `accepted` is channel acceptance without confirmed delivery, and `delivered` is confirmed delivery. Reconcile known IDs using `GET /projects/{id}/messages/{messageId}`. Only confirmed delivery should remove protected delivery material and make the reply visible in delivered model history.

**Unverified guarantees:** the fetched OpenAPI requires an idempotency key but does not establish its expiry or complete deduplication scope. No lookup by idempotency key is documented. The adapter therefore never automatically retries an ambiguous send. Transport failures, HTTP 408/409/5xx, and malformed success responses become `unknown`; no known ID means no safe automatic reconciliation. The worker must recover interrupted `submitting` records as `unknown`, never `pending`, and must keep reconciliation read-only after routing switches. Definitive HTTP errors are returned without logging provider bodies or text.

The exact webhook redelivery retention window also remains unverified. Retain compact event/outbound deduplication records for at least 30 days; confirm whether the provider's maximum retry window requires a longer period before enabling live traffic.

## Attachments and live gate

The REST specification exposes authenticated attachment metadata and content endpoints, but attachment import is a later extension. V1 ignores attachment events and uses vault UTF-8 Markdown upload. Before enabling Photon imports, verify authenticated download behavior, content limits, expected source event shape, and owner editing focus; never fetch an arbitrary model-supplied URL.

Pending live checklist: configure an HTTPS destination with the pinned payload version and `message.received`; securely configure its one-time signing secret; confirm two actual participant assignments and destinations; record signed inbound and outbound delivery evidence; verify post-assignment eligibility, provider idempotency scope/TTL, and redelivery window. All live writes and smoke tests require the explicitly requested milestone. Passing mocked tests is not proof of these provider behaviors.
