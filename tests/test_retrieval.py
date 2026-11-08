import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import ProviderError
from app.retrieval import HaystackEmbeddings, cosine_retrieve, split_markdown, token_count


def test_splitter_keeps_headings_and_word_overlap_deterministically():
    content = "# Hours\n" + " ".join(f"word{i}" for i in range(650)) + "\n## Contact\nCall us."
    chunks = split_markdown("guide.md", content)
    assert chunks == split_markdown("guide.md", content)
    assert len(chunks) == 4
    assert all(c["filename"] == "guide.md" for c in chunks)
    assert [c["heading"] for c in chunks] == ["Hours", "Hours", "Hours", "Contact"]
    assert chunks[0]["text"].split()[-50:] == chunks[1]["text"].split()[:50]
    assert all(len(c["text"].split()) <= 300 for c in chunks)


def test_code_fence_hashes_do_not_change_source_heading():
    chunks = split_markdown("code.md", "# Actual\n```python\n# this is a comment\n```\n## Next\nhello")
    assert [c["heading"] for c in chunks] == ["Actual", "Next"]


def test_very_long_unicode_word_respects_embedding_limit_without_losing_text():
    word = "界🙂" * 4000
    chunks = split_markdown("unicode.md", word)
    assert len(chunks) > 1
    assert "".join(c["text"] for c in chunks) == word
    assert all(token_count(c["text"]) <= 8000 for c in chunks)


@pytest.mark.parametrize(("name", "content"), [("../a.md", "a"), ("a.txt", "a"), ("a.md", "a\x00b")])
def test_invalid_markdown_rejected(name, content):
    with pytest.raises(ValueError):
        split_markdown(name, content)


def test_empty_markdown_has_no_chunks():
    assert split_markdown("empty.md", "\n   \n") == []


def test_cosine_top_five_excludes_incompatible_or_invalid_vectors():
    rows = [{"filename": f"{i}.md", "embedding": [1, i / 10], "embedding_model": "text-embedding-3-small",
             "dimensions": 2, "preprocessing_version": "markdown-v1"} for i in range(8)]
    rows += [dict(rows[0], embedding_model="other"), dict(rows[0], preprocessing_version="old"),
             dict(rows[0], dimensions=3), dict(rows[0], embedding=[float("nan"), 1]), dict(rows[0], embedding=[0, 0])]
    results = cosine_retrieve([1, 0], rows, dimensions=2)
    assert [r["filename"] for r in results] == [f"{i}.md" for i in range(5)]
    assert results[0]["score"] == 1
    assert "score" not in rows[0]


def test_invalid_query_vector_does_not_score():
    with pytest.raises(ValueError):
        cosine_retrieve([float("inf"), 1], [], dimensions=2)


def test_real_haystack_configuration_and_mocked_embedding_requests():
    embedder = HaystackEmbeddings("test-key", dimensions=2)
    assert embedder.documents.model == embedder.queries.model == "text-embedding-3-small"
    assert embedder.documents.max_retries == embedder.queries.max_retries == 0
    assert embedder.documents.dimensions == embedder.queries.dimensions == 2
    assert embedder.documents.batch_size == 32
    assert embedder.documents.api_base_url == embedder.queries.api_base_url == "https://api.openai.com/v1"
    embedder.documents.run_async = AsyncMock(return_value={"documents": [SimpleNamespace(embedding=[1, 0])]})
    embedder.queries.run_async = AsyncMock(return_value={"embedding": [0, 1]})
    chunk = {"filename": "hours.md", "heading": "Hours", "text": "Open at nine", "index": 0}
    enriched = asyncio.run(embedder.embed_chunks([chunk]))
    assert enriched[0]["embedding"] == [1, 0]
    assert enriched[0]["embedding_model"] == "text-embedding-3-small"
    assert enriched[0]["preprocessing_version"] == "markdown-v1"
    assert asyncio.run(embedder.embed_query("When?")) == [0, 1]
    assert "embedding" not in chunk


def test_embedding_batch_is_bounded_before_paid_work():
    embedder = HaystackEmbeddings("test-key", dimensions=2)
    embedder.documents.run_async = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(embedder.embed_chunks([{"text": "hi"}] * 33))
    embedder.documents.run_async.assert_not_called()


def test_missing_embedding_cannot_be_marked_ready():
    embedder = HaystackEmbeddings("test-key", dimensions=2)
    embedder.documents.run_async = AsyncMock(return_value={"documents": [SimpleNamespace(embedding=None)]})
    with pytest.raises(ProviderError) as failure:
        asyncio.run(embedder.embed_chunks([{"text": "hi"}]))
    assert failure.value.kind == "invalid_embedding"
