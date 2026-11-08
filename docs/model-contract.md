# Model and retrieval contract

`app/models.py` makes one request per adapter invocation. The worker owns durable request reservations, global concurrency, retry limits, turn deadlines, and tool intent persistence. Both SDK clients disable automatic retries; they have independent credentials and endpoints. No provider or model fallback is implemented.

- Builder: OpenAI-compatible Chat Completions at the configured Nemotron URL, `nemotron-builder`, tool schemas, sequential tool calls, `max_tokens=2048`, `temperature=0`, and `chat_template_kwargs.enable_thinking=false`. The adapter rejects malformed, unknown, duplicate-ID, and excessive tool calls. The backend validates each tool's arguments and authorization before mutation, and counts every proposed tool call against the turn budget, including invalid arguments.
- Project answers: OpenAI Responses, `gpt-5.6-luna`, `reasoning={"effort":"low"}`, `store=false`, `max_output_tokens=2048`, and no tools. Refusal, incomplete, and empty responses fail explicitly. Project Q&A does not access Nemotron.
- Prompts distinguish current Markdown sources from instructions, require filename/heading citations and abstention, and forbid management and history reset. These are soft grounding requirements; mocked outputs do not establish factual accuracy.

Inputs use a conservative UTF-8 byte bound, plus message/schema framing allowances, within 12,000 tokens. This avoids using an OpenAI tokenizer to estimate Nemotron input. Earlier builder turns are removed as complete message/tool groups. Project history and retrieved context are trimmed before the latest question; an oversized current request is rejected before generation.

`app/retrieval.py` uses one deterministic splitter for saving quotas and indexing: approximately 300 words with 50-word overlap, separate Markdown headings, and at most 8,000 `cl100k_base` tokens per chunk. Filename and heading metadata survive retrieval. Vectors are tagged with `text-embedding-3-small`, configured dimensions (default 1,536), and `markdown-v1`; incompatible vectors are excluded from cosine scoring. The caller must supply only the selected project's current, ready file versions and recheck those versions before dispatch.

Ranking is descending cosine similarity between the current question embedding and
each eligible chunk embedding; the highest five are passed toward answer generation.
Scores are similarity values, not calibrated confidence, and are not included in the
model's source JSON. There is no relevance cutoff, keyword/hybrid search, reranker,
clarity/quality scoring, or conversational query rewrite. Only chunk text is embedded;
filename/heading metadata is supplied to the answer model for citations. Context
trimming can reduce the five selected chunks. Citation labels and abstention are
prompted, with no independent citation-entailment or factual-support validator.

Hypha's basic welcome/help, explicit command list, common vault requests, and share
requests are deterministic and do not consume a model generation. `/edit` enters
editing; `/builder` is removed. Vault tool traces expose only a readiness flag;
the server appends the fresh full HTTPS vault URL outside model context. Historical
failure notices and old ID-only vault replies are excluded from model context while
remaining in the stored conversation audit.

Hypha may choose one to three texts per model reply, separating them internally
with `<next_message>` on its own line. Project agents default to one text; their
saved instructions can encourage up to three short texts or require one message.
The builder sees the current instructions and can update this style through
`update_instructions`, preserving the agent's other preferences. This is a model
preference, while the server enforces the maximum of three messages. Ordinary
paragraphs do not cause splitting. Excess segments are merged into the third
message without losing content. Separators never reach Photon; every bubble
still passes through plain-text formatting. Only delivered bubbles enter future
model context. Each factual bubble should carry its own source citation.

Haystack's `OpenAIDocumentEmbedder` and `OpenAITextEmbedder` both use the OpenAI endpoint and the same dimensions. Document calls accept at most 32 chunks, so each adapter call makes one bounded embedding batch. The worker reserves each batch's tokens first. Query embedding tokens also count toward the deployment budget. Missing, nonfinite, zero, or wrong-dimension embeddings cannot be marked ready. Haystack's raw provider-error logger is disabled to avoid logging document bodies from error responses. Set `HAYSTACK_TELEMETRY_ENABLED=false` in the runtime environment. Tokenizer data must be cached during dependency preparation so indexing does not depend on a first-request download.

Pinned and tested Python dependencies: `openai==2.15.0`, `haystack-ai==3.4.1`, `tiktoken==0.14.0`.

Authoritative contracts consulted:

- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) and [reasoning settings](https://developers.openai.com/api/docs/guides/reasoning).
- [Responses creation](https://developers.openai.com/api/reference/resources/responses/methods/create).
- [Embedding model](https://developers.openai.com/api/docs/models/text-embedding-3-small).
- [Haystack document embedder](https://docs.haystack.deepset.ai/docs/openaidocumentembedder) and [query embedder](https://docs.haystack.deepset.ai/docs/openaitextembedder); constructor behavior was checked against the installed pinned package.

The offline implementation was verified with mocks and local transports. Separate live Nemotron verification recorded in [the deployment README](../deploy/nemotron/README.md) established the non-thinking, temperature-zero tool-call contract used here. At the user's subsequent live-setup request, the application adapters passed a Nemotron tool call, a GPT-5.6 Luna answer with low reasoning, and actual document/query embeddings. A real inbound iMessage then completed project creation and vault-link issuance through Nemotron, with Photon confirming the reply delivered. Maximum-vault retrieval timing, representative grounding/abstention, dashboard upload/Q&A, and second-participant delivery remain to be verified.
