import asyncio
import json

import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import ProviderError
from app.retrieval import NemotronEmbeddings, cosine_retrieve, split_markdown, token_bound


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
    assert all(token_bound(c["text"]) <= 8000 for c in chunks)


@pytest.mark.parametrize(("name", "content"), [("../a.md", "a"), ("a.txt", "a"), ("a.md", "a\x00b")])
def test_invalid_markdown_rejected(name, content):
    with pytest.raises(ValueError):
        split_markdown(name, content)


def test_empty_markdown_has_no_chunks():
    assert split_markdown("empty.md", "\n   \n") == []


def test_byte_limited_chunks_keep_proportional_overlap_and_bounded_growth():
    chunks = split_markdown('long-words.md', ' '.join(['a' * 160] * 600))
    assert len(chunks) < 20
    assert all(token_bound(chunk['text']) <= 8000 for chunk in chunks)


def test_cosine_top_five_excludes_incompatible_or_invalid_vectors():
    rows = [{"filename": f"{i}.md", "embedding": [1, i / 10], "embedding_model": "nvidia/Nemotron-3-Embed-1B-BF16",
             "dimensions": 2, "preprocessing_version": "markdown-bytes-v2"} for i in range(8)]
    rows += [dict(rows[0], embedding_model="other"), dict(rows[0], preprocessing_version="old"),
             dict(rows[0], dimensions=3), dict(rows[0], embedding=[float("nan"), 1]), dict(rows[0], embedding=[0, 0])]
    results = cosine_retrieve([1, 0], rows, dimensions=2)
    assert [r["filename"] for r in results] == [f"{i}.md" for i in range(5)]
    assert results[0]["score"] == 1
    assert "score" not in rows[0]


def test_invalid_query_vector_does_not_score():
    with pytest.raises(ValueError):
        cosine_retrieve([float("inf"), 1], [], dimensions=2)


async def test_nvidia_transport_modes_ordering_and_configuration():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "http://127.0.0.1:8004/v1/embeddings"
        assert request.headers['authorization'] == 'Bearer nvidia-test-key'
        assert body['model'] == 'nvidia/Nemotron-3-Embed-1B-BF16'
        assert body['dimensions'] == 128
        assert body['encoding_format'] == 'float'
        assert body['truncate'] == 'NONE' and body['modality'] == 'text'
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [float(i + 1)] * 128, 'object': 'embedding'}
            for i in reversed(range(len(body['input'])))], 'model': body['model'],
            'object': 'list', 'usage': {'prompt_tokens': 8, 'total_tokens': 8}})
    embedder = NemotronEmbeddings('nvidia-test-key', dimensions=128,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    chunks = [{'text': 'Open at nine', 'filename': 'hours.md'}, {'text': 'Close at five', 'filename': 'hours.md'}]
    try:
        assert embedder.client.max_retries == 0
        assert embedder.client.timeout == 45
        rows = await embedder.embed_chunks(chunks)
        assert [row['embedding'][0] for row in rows] == [1, 2]
        assert all(row['embedding_model'] == embedder.model and row['dimensions'] == 128
                   and row['preprocessing_version'] == embedder.preprocessing_version for row in rows)
        assert 'embedding' not in chunks[0]
        assert await embedder.embed_query('When?') == [1.0] * 128
        assert [body['input_type'] for body in requests] == ['passage', 'query']
        assert requests[0]['input'] == ['Open at nine', 'Close at five']
        assert requests[1]['input'] == ['When?']
    finally:
        await embedder.close()


async def test_embedding_inputs_are_bounded_before_paid_work():
    embedder = NemotronEmbeddings('test-key')
    embedder.client.embeddings.create = AsyncMock()
    try:
        assert await embedder.embed_chunks([]) == []
        for chunks in ([{'text': 'hi'}] * 33, [{'text': ' '}], [{'text': 'a' * 8001}]):
            with pytest.raises(ValueError):
                await embedder.embed_chunks(chunks)
        for text in ('', ' ', 'a' * 8001, '\U0001f642' * 2001):
            with pytest.raises(ValueError):
                await embedder.embed_query(text)
        embedder.client.embeddings.create.assert_not_called()
    finally:
        await embedder.close()


@pytest.mark.parametrize('rows', [None, [], [SimpleNamespace(index=0, embedding=None)],
    [SimpleNamespace(index=0, embedding=[1, 0])], [SimpleNamespace(index=0, embedding=[0] * 128)],
    [SimpleNamespace(index=0, embedding=[float('nan')] * 128)],
    [SimpleNamespace(index=0, embedding=[True] * 128)], [SimpleNamespace(index=1, embedding=[1] * 128)],
    [SimpleNamespace(index=True, embedding=[1] * 128)], [SimpleNamespace(embedding=[1] * 128)]])
async def test_invalid_embedding_cannot_be_marked_ready(rows):
    embedder = NemotronEmbeddings('test-key', dimensions=128)
    embedder.client.embeddings.create = AsyncMock(return_value=SimpleNamespace(data=rows))
    try:
        with pytest.raises(ProviderError) as failure:
            await embedder.embed_chunks([{'text': 'hi'}])
        assert failure.value.kind == 'invalid_embedding'
    finally:
        await embedder.close()


@pytest.mark.parametrize('status,retryable', [(401, False), (429, True), (503, True), (302, False)])
async def test_provider_errors_are_sanitized_without_hidden_retries(status, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': 'private input and credential'}},
                              headers={'Location': 'https://other.example/embeddings'})
    embedder = NemotronEmbeddings('test-key', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(ProviderError) as failure:
            await embedder.embed_query('private question')
        assert failure.value.retryable == retryable
        assert 'private' not in str(failure.value)
        assert len(calls) == 1
    finally:
        await embedder.close()


def test_missing_nvidia_key_and_invalid_dimensions_fail_early():
    with pytest.raises(ValueError, match='EMBEDDING_API_KEY'):
        NemotronEmbeddings(' ')
    for dimensions in (0, 2, 2049):
        with pytest.raises(ValueError, match='dimensions'):
            NemotronEmbeddings('test-key', dimensions=dimensions)


@pytest.mark.parametrize('base_url', ['http://public.example/v1', 'https://user:secret@example.test/v1',
    'https://example.test/v1?key=secret', 'https://example.test/v1#secret'])
def test_embedding_endpoint_rejects_unsafe_credentials_or_transport(base_url):
    with pytest.raises(ValueError, match='HTTPS or loopback'):
        NemotronEmbeddings('test-key', base_url=base_url)
