from unittest.mock import AsyncMock

import pytest
from app.config import Limits
from app.retrieval import NemotronEmbeddings
from app.service import DomainError, source_versions_current
from test_service import create_project, environment, issue_capability


async def test_recovery_reindexes_old_vectors_once_and_invalidates_pending_answers():
    env = await environment()
    job, _, _ = await create_project(env)
    token, _, _ = await issue_capability(env, job)
    file = await env.service.save_file(token, 'source.md', '# Hours\nOpen at nine.')
    await env.worker.index(await env.worker.claim('index'))
    env.embeddings.model = NemotronEmbeddings.model
    env.embeddings.preprocessing_version = NemotronEmbeddings.preprocessing_version
    env.embeddings.embed_chunks = AsyncMock(side_effect=lambda chunks: [
        dict(c, embedding=[1., 0.], embedding_model=env.embeddings.model, dimensions=2,
             preprocessing_version=env.embeddings.preprocessing_version) for c in chunks])
    await env.worker.recover()
    first = await env.service.snapshot()
    assert first['files'][file['id']]['version'] == 2
    assert first['files'][file['id']]['content'] == file['content']
    assert first['files'][file['id']]['status'] == 'pending'
    assert file['id'] not in first['chunks']
    assert not source_versions_current(first, {file['id']: 1})
    await env.worker.recover()
    assert (await env.service.snapshot()) == first
    await env.worker.index(await env.worker.claim('index'))
    indexed = await env.service.snapshot()
    assert indexed['files'][file['id']]['status'] == 'ready'
    assert indexed['chunks'][file['id']][0]['embedding_model'] == NemotronEmbeddings.model
    await env.worker.recover()
    assert (await env.service.snapshot()) == indexed


async def test_oversized_legacy_index_fails_only_the_note_before_provider_calls():
    env = await environment(Limits(chunks_per_project=6))
    _, project, _ = await create_project(env)
    content = ' '.join(['a' * 160] * 600)
    def seed(state):
        for id in ('one', 'two'):
            state['files'][id] = {'id': id, 'project': project['id'], 'name': id + '.md',
                'content': content, 'version': 1, 'status': 'ready', 'bytes': len(content), 'chunk_count': 3}
            state['chunks'][id] = [{'file_id': id, 'version': 1, 'embedding_model': 'legacy'}] * 3
    await env.service.store.transaction(seed)
    await env.worker.recover()
    state = await env.service.snapshot()
    assert all(f['chunk_count'] == 3 and f['status'] == 'pending' for f in state['files'].values())
    await env.worker.run_job(await env.worker.claim('index'))
    state = await env.service.snapshot()
    assert sorted(f['status'] for f in state['files'].values()) == ['failed', 'pending']
    env.embeddings.embed_chunks.assert_not_called()


async def test_note_that_cannot_fit_a_daily_budget_is_rejected_without_an_endless_retry():
    env = await environment(Limits(embedding_tokens_per_day=50))
    job, _, _ = await create_project(env)
    token, _, _ = await issue_capability(env, job)
    with pytest.raises(DomainError, match='Split it into smaller notes') as error:
        await env.service.save_file(token, 'large.md', 'long note ' * 10)
    assert error.value.status == 413
    assert not (await env.service.snapshot())['files']
