# Model and retrieval contract

`app/models.py` makes one request per adapter invocation. The worker owns durable request reservations, global concurrency, retry limits, turn deadlines, and tool intent persistence. Both SDK clients disable automatic retries; they have independent credentials and endpoints. No provider or model fallback is implemented.

- Hypha: OpenAI-compatible Chat Completions at the configured Nemotron URL, `nemotron-builder`, tool schemas, sequential tool calls, `max_tokens=2048`, `temperature=0`, and `chat_template_kwargs.enable_thinking=false`. The adapter rejects malformed, unknown, duplicate-ID, and excessive tool calls. The backend validates each tool's arguments and authorization before mutation, and counts every proposed tool call against the turn budget, including invalid arguments.
- Sprout answers: OpenAI Responses, `gpt-5.6-luna`, `reasoning={"effort":"low"}`, `store=false`, `max_output_tokens=2048`, and no tools. Refusal, incomplete, and empty responses fail explicitly. Sprout Q&A does not access Nemotron.
- Prompts distinguish current Markdown notes from instructions, require note-name/heading citations and abstention, and forbid management and history reset. These are soft grounding requirements; mocked outputs do not establish factual accuracy.

Inputs use a conservative UTF-8 byte bound, plus message/schema framing allowances, within 12,000 tokens. This avoids using an OpenAI tokenizer to estimate Nemotron input. Earlier Hypha turns are removed as complete message/tool groups. Sprout history and retrieved context are trimmed before the latest question; an oversized current request is rejected before generation.

`app/retrieval.py` uses one deterministic splitter for saving quotas and indexing: approximately 300 words with 50-word overlap, separate Markdown headings, and at most 8,000 UTF-8 bytes per chunk (a conservative Nemotron token bound). Note-name and heading metadata survive retrieval. Vectors are tagged with `nvidia/Nemotron-3-Embed-1B-BF16`, configured dimensions (default 1,536), and `markdown-bytes-v2`; incompatible vectors are excluded from cosine scoring. The caller must supply only the selected Sprout’s current, ready note versions and recheck those versions before dispatch.

Ranking is descending cosine similarity between the current question embedding and
each eligible chunk embedding; the highest five are passed toward answer generation.
Scores are similarity values, not calibrated confidence, and are not included in the
model's source JSON. There is no relevance cutoff, keyword/hybrid search, reranker,
clarity/quality scoring, or conversational query rewrite. Only chunk text is embedded;
note-name/heading metadata is supplied to the answer model for citations. Context
trimming can reduce the five selected chunks. Citation labels and abstention are
prompted, with no independent citation-entailment or factual-support validator.

Hypha's basic welcome/help, explicit command list, common notes requests, and share
requests are deterministic and do not consume a model generation. `/edit` enters
editing; `/builder` is removed. Notes-link tool traces expose only a readiness flag;
the server appends the fresh full HTTPS editing URL outside model context. Historical
failure notices and old ID-only editing replies are excluded from model context while
remaining in the stored conversation audit.

Hypha may choose one to three texts per model reply, separating them internally
with `<next_message>` on its own line. Sprouts default to one text; their
saved instructions can encourage up to three short texts or require one message.
Hypha sees the current instructions and can update this style through
`update_instructions`, preserving the Sprout’s other preferences. This is a model
preference, while the server enforces the maximum of three messages. Ordinary
paragraphs do not cause splitting. Excess segments are merged into the third
message without losing content. Separators never reach Photon; every bubble
still passes through plain-text formatting. Only delivered bubbles enter future
model context. Each factual bubble should carry its own source citation.

`NemotronEmbeddings` sends authenticated requests only to `EMBEDDING_BASE_URL`,
the private Brev service. It uses the existing compatible SDK as a transport;
it does not call OpenAI for embeddings. `input_type=passage` indexes notes and
`input_type=query` embeds questions. The service adds the model's corresponding
`passage: ` / `query: ` prefixes, performs mean pooling through Sentence Transformers,
and normalizes the requested vector dimensions after truncation. Inputs are never
silently truncated. Requests contain at most 32 chunks; the worker batches 16.
SDK retries are disabled, with budgets/retries owned by the worker. Actual tokenizer
usage is recorded by the service; reservation budgets use UTF-8 byte upper bounds.
Responses must contain exactly one finite, nonzero vector of the expected dimension
per input, with unique indices; shuffled results are reordered before storage.

On startup, ready notes with a different model, dimensions, or preprocessing version
get a new version and indexing job. Their previous vectors are removed atomically;
old queued answers fail the existing source-version checks. Pending notes are indexed
with the new splitter, and their chunk counts are updated on completion. Repeating
startup does not recreate the migration jobs.

The self-hosted service and model revisions are pinned in
[`deploy/embeddings`](../deploy/embeddings/README.md). The backend needs its own
`EMBEDDING_API_KEY`; it must not reuse a public-provider credential.

Authoritative contracts consulted:

- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) and [reasoning settings](https://developers.openai.com/api/docs/guides/reasoning).
- [Responses creation](https://developers.openai.com/api/reference/resources/responses/methods/create).
- [NVIDIA Nemotron embedding model and local inference](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16).

The offline implementation was verified with mocks and local transports. Separate live Nemotron verification recorded in [the deployment README](../deploy/nemotron/README.md) established the non-thinking, temperature-zero tool-call contract used here. At the user's subsequent live-setup request, the application adapters passed a Nemotron tool call, a GPT-5.6 Luna answer with low reasoning, and actual document/query embeddings. A real inbound iMessage then completed Sprout creation and notes-link issuance through Nemotron, with Photon confirming the reply delivered. Maximum-notes retrieval timing, representative grounding/abstention, dashboard upload/Q&A, and second-participant delivery remain to be verified.

The earlier full journey used the previous OpenAI embedding adapter. On 2026-11-08,
the self-hosted Nemotron embedding replacement passed authenticated synthetic
passage/query ranking checks on a dedicated Brev A4000 and was deployed to the
live backend. The existing saved note was reindexed successfully in SpacetimeDB.
Measurements and verification limits are recorded in the
[embedding deployment README](../deploy/embeddings/README.md); representative
grounding and a full user journey after this switch remain unverified.
