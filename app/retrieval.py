"""Deterministic Markdown chunks and version-compatible in-process retrieval."""

import math
import re

from app.models import ProviderError, provider_error
from app.telemetry import tracked_call


def token_bound(text: str) -> int:
    """Bound Nemotron tokens with UTF-8 bytes plus prefix/special-token framing."""
    return len(text.encode("utf-8")) + 16


def split_markdown(filename: str, content: str) -> list[dict]:
    """Keep heading context; use 300 words/50 overlap and <=8,000 UTF-8 bytes.

    Extremely long individual words are split by Unicode character boundaries,
    avoiding decoding partial UTF-8 token sequences. Quotas use this same function.
    """
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("Markdown must be UTF-8 text without NUL characters.")
    if not filename.lower().endswith(".md") or filename in (".md", "..md") or any(c in filename for c in "/\\\x00"):
        raise ValueError("Use a Markdown note name without a directory path.")
    sections, lines, heading, fence = [], [], "", None
    for line in content.splitlines():
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if match:
            if lines:
                sections.append((heading, "\n".join(lines)))
            heading, lines = match.group(1), [line]
        else:
            lines.append(line)
    if lines:
        sections.append((heading, "\n".join(lines)))
    chunks = []
    for heading, section in sections:
        words = []
        for word in section.split():
            while token_bound(word) > 8000:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if token_bound(word[:mid]) <= 8000:
                        lo = mid
                    else:
                        hi = mid - 1
                words.append(word[:lo])
                word = word[lo:]
            if word:
                words.append(word)
        start = 0
        while start < len(words):
            end = min(start + 300, len(words))
            while token_bound(" ".join(words[start:end])) > 8000:
                end -= 1
            chunks.append({"filename": filename, "heading": heading,
                           "text": " ".join(words[start:end]), "index": len(chunks)})
            if end == len(words):
                break
            start = end - min(50, (end - start) // 6)
    return chunks


def _valid_vector(vector, dimensions: int) -> bool:
    return isinstance(vector, list) and len(vector) == dimensions and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vector
    ) and any(vector)


def cosine_retrieve(query: list[float], chunks: list[dict], *, model: str = "nvidia/Nemotron-3-Embed-1B-BF16",
                    dimensions: int = 1536, preprocessing_version: str = "markdown-bytes-v2") -> list[dict]:
    if not _valid_vector(query, dimensions):
        raise ValueError("Invalid query embedding.")
    norm = math.sqrt(sum(v * v for v in query))
    scored = []
    for chunk in chunks:
        if (chunk.get("embedding_model"), chunk.get("dimensions"), chunk.get("preprocessing_version")) != (
            model, dimensions, preprocessing_version
        ):
            continue
        vector = chunk.get("embedding")
        if not _valid_vector(vector, dimensions):
            continue
        denominator = norm * math.sqrt(sum(v * v for v in vector))
        score = sum(a * b for a, b in zip(query, vector)) / denominator
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [dict(chunk, score=score) for score, chunk in scored[:5]]


class NemotronEmbeddings:
    model = "nvidia/Nemotron-3-Embed-1B-BF16"
    preprocessing_version = "markdown-bytes-v2"

    def __init__(self, api_key: str, *, base_url: str = "http://127.0.0.1:8004/v1", dimensions: int = 1536, client=None):
        if not api_key.strip():
            raise ValueError("Set EMBEDDING_API_KEY for the private Brev embedding service.")
        if not 128 <= dimensions <= 2048:
            raise ValueError("Embedding dimensions must be between 128 and 2048.")
        import httpx
        from openai import AsyncOpenAI

        url = httpx.URL(base_url)
        if (url.username or url.password or url.query or url.fragment or not url.host
                or url.scheme != 'https' and not (url.scheme == 'http' and url.host in ('localhost', '127.0.0.1', '::1'))):
            raise ValueError("Embeddings require an HTTPS or loopback URL without credentials.")
        self.dimensions = dimensions
        # The SDK speaks our Brev service's compatible API; no requests go to OpenAI.
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url,
                                  timeout=45, max_retries=0, http_client=client or httpx.AsyncClient(follow_redirects=False))

    async def close(self):
        await self.client.close()

    async def _embed(self, texts, input_type):
        try:
            response = await tracked_call(self.client.embeddings.create, 'nvidia',
                'document_embedding' if input_type == 'passage' else 'query_embedding', self.model,
                model=self.model, input=texts, dimensions=self.dimensions, encoding_format='float',
                extra_body={'input_type': input_type, 'modality': 'text', 'truncate': 'NONE'})
        except Exception as exc:
            raise provider_error(exc) from None
        rows = getattr(response, 'data', None)
        if (not isinstance(rows, list) or len(rows) != len(texts)
                or any(type(getattr(row, 'index', None)) is not int or not _valid_vector(getattr(row, 'embedding', None), self.dimensions) for row in rows)
                or sorted(row.index for row in rows) != list(range(len(texts)))):
            raise ProviderError("The embedding service returned invalid vectors.", kind="invalid_embedding")
        return [row.embedding for row in sorted(rows, key=lambda row: row.index)]

    async def embed_chunks(self, chunks: list[dict]) -> list[dict]:
        if not chunks:
            return []
        # One bounded request; the worker reserves budgets and owns retries.
        if len(chunks) > 32 or any(not c["text"].strip() or token_bound(c["text"]) > 8000 for c in chunks):
            raise ValueError("Embedding batches require 1-32 nonempty chunks of at most 8,000 UTF-8 bytes.")
        vectors = await self._embed([c["text"] for c in chunks], 'passage')
        return [dict(chunk, embedding=vector, embedding_model=self.model, dimensions=self.dimensions,
                     preprocessing_version=self.preprocessing_version) for chunk, vector in zip(chunks, vectors)]

    async def embed_query(self, text: str) -> list[float]:
        if not text.strip() or token_bound(text) > 8000:
            raise ValueError("Embedding queries require 1-8,000 UTF-8 bytes.")
        return (await self._embed([text], 'query'))[0]
