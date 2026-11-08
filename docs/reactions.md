# Message reactions

Set `TYPESAFE_API_KEY` on the backend to enable Jev decisions. Without the key,
ordinary text replies continue without reactions. The builder enables reactions
by default; existing and new projects default to disabled. Owners can ask the
builder to enable or disable reactions for the selected agent. This uses the
owner-checked `set_reactions(enabled: bool)` tool and a persisted
`reactions_enabled` project field, not an instruction-text preference.

The server sends the current text, up to 40 messages of existing conversation
context, and the agent's name/instructions to Jev 1.14.0. Vault capability links
are redacted; project documents, credentials, and routing identifiers are not
included. `JevClient.payload()` exposes the exact request for inspection without
logging private message content. Three bundled questions judge whether a reaction
fits, its meaning, and whether the message is purely social. The two yes/no gates
require probability greater than 0.7. API errors are recorded separately from a
valid no-reaction result and preserve normal reply processing. Decisions time out
after three seconds and are persisted across worker retries.

- 👀: taking a look / working on it, followed by a text reply.
- 👍: acknowledgment.
- ❤️: warmth or appreciation.
- No reaction: normal reply processing.

Only purely social messages can finish with a confirmed 👍 or ❤️ instead of a
text reply. Questions, instructions, and agreement to a proposed action still
need processing. A failed or unconfirmed reaction falls back to a text reply.
Commands, rejected messages, and voice-note jobs are excluded.

Photon reactions use the existing message endpoint with `parentMessageId` set to
the incoming message ID and content `{type: "reaction", reaction: emoji,
action: "add" | "remove"}`. Separate outbox records and stable keys
`EVENT:reaction:add` / `EVENT:reaction:remove` track each operation. Eyes removal
is scheduled after final text delivery, failure, cancellation, or an expired
active turn. Removal remains tied to the original message after navigation and
still checks the original assignment. Other reactions remain on the message.

Recovery never blindly repeats an uncertain write. Known provider message IDs
can be reconciled using the existing bounded read-only polling. An uncertain
addition without a provider ID cannot be safely confirmed or cleaned up;
uncertain/failed removal can leave eyes visible. A queued response is not proof
of visible delivery. These are the same provider uncertainty limits as text
delivery, and operation status stays in the outbox for inspection.

Mocked tests cover the decision policy, HTTP encoding, project ownership,
social-only history, fallback, deduplication, recovery, and eyes cleanup. No live
Jev or Photon requests were made for verification. Actual reaction visibility
and removal on the shared iMessage line remain a live verification step.

Contracts: [TypeSafe System One](https://docs.typesafe.ai/api/system-one),
[Noul](https://docs.typesafe.ai/primitives/noul),
[Choice](https://docs.typesafe.ai/primitives/choice), and
[Photon OpenAPI](https://github.com/photon-hq/api/blob/main/openapi/openapi.json).
