# Model and retrieval contract

`app/models.py` makes one request per adapter invocation. The worker owns durable request reservations, global concurrency, retry limits, turn deadlines, and tool intent persistence. Both SDK clients disable automatic retries; they have independent credentials and endpoints. No provider or model fallback is implemented.

- Builder: OpenAI-compatible Chat Completions at the configured Nemotron URL, `nemotron-builder`, tool schemas, sequential tool calls, `max_tokens=2048`, `temperature=0`, and `chat_template_kwargs.enable_thinking=false`. The adapter rejects malformed, unknown, duplicate-ID, and excessive tool calls. The backend validates each tool's arguments and authorization before mutation, and counts every proposed tool call against the turn budget, including invalid arguments.
- Project answers: OpenAI Responses, `gpt-5.6-luna`, `reasoning={"effort":"low"}`, `store=false`, `max_output_tokens=2048`, and no tools. Refusal, incomplete, and empty responses fail explicitly. Project Q&A does not access Nemotron.
- Prompts distinguish current Markdown sources from instructions, require filename/heading citations and abstention, and forbid management and history reset. These are soft grounding requirements; mocked outputs do not establish factual accuracy.

Inputs use a conservative UTF-8 byte bound, plus message/schema framing allowances, within 12,000 tokens. This avoids using an OpenAI tokenizer to estimate Nemotron input. Earlier builder turns are removed as complete message/tool groups. Project history and retrieved context are trimmed before the latest question; an oversized current request is rejected before generation.

`app/retrieval.py` uses one deterministic splitter for saving quotas and indexing: approximately 300 words with 50-word overlap, separate Markdown headings, and at most 8,000 `cl100k_base` tokens per chunk. Filename and heading metadata survive retrieval. Vectors are tagged with `text-embedding-3-small`, configured dimensions (default 1,536), and `markdown-v1`; incompatible vectors are excluded from cosine scoring. The caller must supply only the selected project's current, ready file versions and recheck those versions before dispatch.

Haystack's `OpenAIDocumentEmbedder` and `OpenAITextEmbedder` both use the OpenAI endpoint and the same dimensions. Document calls accept at most 32 chunks, so each adapter call makes one bounded embedding batch. The worker reserves each batch's tokens first. Query embedding tokens also count toward the deployment budget. Missing, nonfinite, zero, or wrong-dimension embeddings cannot be marked ready. Haystack's raw provider-error logger is disabled to avoid logging document bodies from error responses. Set `HAYSTACK_TELEMETRY_ENABLED=false` in the runtime environment. Tokenizer data must be cached during dependency preparation so indexing does not depend on a first-request download.

Pinned and tested Python dependencies: `openai==2.15.0`, `haystack-ai==3.4.1`, `tiktoken==0.14.0`.

Authoritative contracts consulted:

- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) and [reasoning settings](https://developers.openai.com/api/docs/guides/reasoning).
- [Responses creation](https://developers.openai.com/api/reference/resources/responses/methods/create).
- [Embedding model](https://developers.openai.com/api/docs/models/text-embedding-3-small).
- [Haystack document embedder](https://docs.haystack.deepset.ai/docs/openaidocumentembedder) and [query embedder](https://docs.haystack.deepset.ai/docs/openaitextembedder); constructor behavior was checked against the installed pinned package.

This implementation's verification uses mocks and a local HTTP transport only. Separate live Nemotron verification recorded in [the deployment README](../deploy/nemotron/README.md) established the non-thinking, temperature-zero tool-call contract now used here. This implementation run did not invoke live providers or start the app. The remaining deployment gate requires a GPT-5.6 Luna answer with low reasoning, actual document/query embeddings, and maximum-vault retrieval timing, plus the database and two-user messaging checks.
