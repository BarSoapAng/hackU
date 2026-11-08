"""Deterministic Markdown chunks and version-compatible in-process retrieval."""

import math
import logging
import re
from functools import lru_cache

from app.models import ProviderError, provider_error
from app.telemetry import tracked_call


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def token_count(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def split_markdown(filename: str, content: str) -> list[dict]:
    """Keep heading context; use 300 words/50 overlap and <=8,000 tokens.

    Extremely long individual words are split by Unicode character boundaries,
    avoiding decoding partial UTF-8 token sequences. Quotas use this same function.
    """
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("Markdown must be UTF-8 text without NUL characters.")
    if not filename.lower().endswith(".md") or filename in (".md", "..md") or any(c in filename for c in "/\\\x00"):
        raise ValueError("Use a Markdown filename without a directory path.")
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
            while token_count(word) > 8000:
                lo, hi = 1, len(word)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if token_count(word[:mid]) <= 8000:
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
            while token_count(" ".join(words[start:end])) > 8000:
                end -= 1
            chunks.append({"filename": filename, "heading": heading,
                           "text": " ".join(words[start:end]), "index": len(chunks)})
            if end == len(words):
                break
            start = max(start + 1, end - 50)
    return chunks


def _valid_vector(vector, dimensions: int) -> bool:
    return isinstance(vector, list) and len(vector) == dimensions and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vector
    ) and any(vector)


def cosine_retrieve(query: list[float], chunks: list[dict], *, model: str = "text-embedding-3-small",
                    dimensions: int = 1536, preprocessing_version: str = "markdown-v1") -> list[dict]:
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


class HaystackEmbeddings:
    model = "text-embedding-3-small"
    preprocessing_version = "markdown-v1"

    def __init__(self, api_key: str, *, dimensions: int = 1536):
        if not 1 <= dimensions <= 1536:
            raise ValueError("Embedding dimensions must be between 1 and 1536.")
        from haystack.components.embedders import OpenAIDocumentEmbedder, OpenAITextEmbedder
        from haystack.utils import Secret

        # This component otherwise logs raw APIError bodies before raising.
        logging.getLogger("haystack.components.embedders.openai_document_embedder").disabled = True
        self.dimensions = dimensions
        settings = dict(api_key=Secret.from_token(api_key), model=self.model, dimensions=dimensions,
                        api_base_url="https://api.openai.com/v1", timeout=45, max_retries=0)
        self.documents = OpenAIDocumentEmbedder(**settings, batch_size=32, progress_bar=False,
                                               meta_fields_to_embed=[], raise_on_failure=True)
        self.queries = OpenAITextEmbedder(**settings)

    async def close(self):
        await self.documents.close_async()
        await self.queries.close_async()

    async def embed_chunks(self, chunks: list[dict]) -> list[dict]:
        from haystack import Document

        if not chunks:
            return []
        # Caller reserves tokens durably before every batch; no hidden batches/retries.
        if len(chunks) > 32 or any(not c["text"].strip() or token_count(c["text"]) > 8000 for c in chunks):
            raise ValueError("Embedding batches require 1-32 nonempty chunks of at most 8,000 tokens.")
        documents = [Document(id=str(i), content=c["text"]) for i, c in enumerate(chunks)]
        try:
            result = await tracked_call(self.documents.run_async, 'openai', 'document_embedding', self.model,
                                        documents=documents)
        except Exception as exc:
            raise provider_error(exc) from None
        enriched = result.get("documents", [])
        if len(enriched) != len(chunks) or any(not _valid_vector(d.embedding, self.dimensions) for d in enriched):
            raise ProviderError("The embedding service returned incomplete vectors.", kind="invalid_embedding")
        return [dict(chunk, embedding=doc.embedding, embedding_model=self.model, dimensions=self.dimensions,
                     preprocessing_version=self.preprocessing_version) for chunk, doc in zip(chunks, enriched)]

    async def embed_query(self, text: str) -> list[float]:
        if not text.strip() or token_count(text) > 8000:
            raise ValueError("Embedding queries require 1-8,000 tokens.")
        try:
            result = await tracked_call(self.queries.run_async, 'openai', 'query_embedding', self.model, text=text)
        except Exception as exc:
            raise provider_error(exc) from None
        vector = result.get("embedding")
        if not _valid_vector(vector, self.dimensions):
            raise ProviderError("The embedding service returned an invalid vector.", kind="invalid_embedding")
        return vector
