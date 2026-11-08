import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import DatabaseUnavailable
from app.models import ModelGateway, ProviderError
from app.retrieval import NemotronEmbeddings
from app.service import DomainError
from app.telemetry import request_context, tracked_call
from test_models import clients, response
from test_service import create_project, environment, inbound


async def observe(env, job, call, provider='openai', operation='answer', model='test-model', **kwargs):
    context = request_context.set((env.service, job['id'], job['claim']))
    try:
        return await tracked_call(call, provider, operation, model, **kwargs)
    finally:
        request_context.reset(context)


async def records(env, job):
    return (await env.service.snapshot())['jobs'][job['id']].get('requests', [])


@pytest.mark.parametrize(('usage', 'operation', 'expected'), [
    ({'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15,
      'prompt_tokens_details': {'cached_tokens': 4}}, 'builder', (12, 3, 4, 15)),
    ({'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25,
      'input_tokens_details': {'cached_tokens': 8}}, 'answer', (20, 5, 8, 25)),
    ({'prompt_tokens': 9, 'total_tokens': 9}, 'query_embedding', (9, 0, None, 9)),
    ({'prompt_tokens': 18, 'total_tokens': 18}, 'document_embedding', (18, 0, None, 18)),
    ({}, 'answer', (None, None, None, None)),
    ({'input_tokens': True, 'output_tokens': -1, 'total_tokens': float('nan')},
     'answer', (None, None, None, None)),
    ({'input_tokens': 3, 'input_tokens_details': {'cached_tokens': 4}},
     'answer', (3, None, None, None)),
])
async def test_usage_shapes_preserve_unknowns(usage, operation, expected):
    env = await environment()
    job, project, _ = await create_project(env)
    result = {'meta': {'usage': usage}} if operation.endswith('_embedding') else SimpleNamespace(usage=usage)
    call = AsyncMock(return_value=result)
    assert await observe(env, job, call, operation=operation) is result
    row, = await records(env, job)
    assert tuple(row[k] for k in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens')) == expected
    assert row['status'] == 'received' and row['duration_ms'] >= 0
    assert row['project_id'] == project['id']  # Current focus, not the original claimed job copy.
    assert row['account_id'] == row['participant_id'] == job['user']
    assert row['conversation_id'] == job['user'] + ':builder'


async def test_reservation_failure_prevents_paid_call():
    env = await environment()
    job, _, _ = await create_project(env)
    env.service.store.transaction = AsyncMock(side_effect=DatabaseUnavailable('private connection data'))
    call = AsyncMock()
    with pytest.raises(DatabaseUnavailable):
        await observe(env, job, call)
    call.assert_not_awaited()


async def test_final_write_failure_returns_received_response_without_retry(caplog):
    env = await environment()
    job, _, _ = await create_project(env)
    transaction = env.service.store.transaction
    writes = 0
    async def fail_final_write(fn):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise DatabaseUnavailable('secret database credential')
        return await transaction(fn)
    env.service.store.transaction = fail_final_write
    result = {'usage': {'input_tokens': 8, 'output_tokens': 2}}
    call = AsyncMock(return_value=result)
    assert await observe(env, job, call) is result
    assert call.await_count == 1
    row, = await records(env, job)
    assert row['status'] == 'pending' and row['input_tokens'] is None
    assert 'secret database credential' not in caplog.text
    env.service.store.transaction = transaction
    await env.worker.recover()
    row, = await records(env, job)
    assert row['status'] == 'unknown' and row['error_type'] == 'Interrupted'


async def test_cas_retry_reuses_attempt_id_without_repeating_provider_call():
    env = await environment()
    job, _, _ = await create_project(env)
    compare = env.service.store.compare_and_swap
    proposed = []
    async def conflict_once(revision, state):
        proposed.append(state['jobs'][job['id']]['requests'][0]['id'])
        if len(proposed) == 1:
            return False
        return await compare(revision, state)
    env.service.store.compare_and_swap = conflict_once
    call = AsyncMock(return_value={})
    await observe(env, job, call)
    assert call.await_count == 1 and len(set(proposed)) == 1
    assert len(await records(env, job)) == 1


async def test_contexts_do_not_cross_between_concurrent_users():
    env = await environment()
    for event, guest in [('owner', False), ('guest', True)]:
        await env.service.admit(inbound(env, event, 'Make a project', guest=guest))
    jobs = [await env.worker.claim('turn'), await env.worker.claim('turn')]
    entered = asyncio.Event()
    active = 0
    async def call(**kwargs):
        nonlocal active
        active += 1
        if active == 2:
            entered.set()
        await entered.wait()
        return {'usage': {'input_tokens': kwargs['tokens']}}
    await asyncio.gather(*(observe(env, job, call, tokens=index + 1) for index, job in enumerate(jobs)))
    for index, job in enumerate(jobs):
        row, = await records(env, job)
        assert row['account_id'] == job['user'] and row['input_tokens'] == index + 1
    assert request_context.get() is None


async def test_late_response_records_usage_without_restoring_superseded_job():
    env = await environment()
    job, _, _ = await create_project(env)
    entered, release = asyncio.Event(), asyncio.Event()
    async def call():
        entered.set()
        await release.wait()
        return {'usage': {'input_tokens': 7, 'output_tokens': 1}}
    task = asyncio.create_task(observe(env, job, call))
    await entered.wait()
    await env.service.store.transaction(lambda state: state['jobs'][job['id']].update(status='superseded', lease=0))
    before = copy.deepcopy((await env.service.snapshot())['jobs'][job['id']])
    release.set()
    await task
    after = (await env.service.snapshot())['jobs'][job['id']]
    assert after['requests'][0]['status'] == 'received' and after['requests'][0]['input_tokens'] == 7
    assert {k: v for k, v in after.items() if k != 'requests'} == {k: v for k, v in before.items() if k != 'requests'}


async def test_cancelled_call_remains_unknown_without_storing_error_body():
    env = await environment()
    job, _, _ = await create_project(env)
    call = AsyncMock(side_effect=asyncio.CancelledError('private prompt'))
    with pytest.raises(asyncio.CancelledError):
        await observe(env, job, call)
    row, = await records(env, job)
    assert row['status'] == 'unknown' and row['input_tokens'] is None
    assert row['error_type'] == 'CancelledError' and 'private prompt' not in str(row)
    assert request_context.get() is None


async def test_worker_gateway_records_distinct_provider_retries_and_resets_context():
    env = await environment()
    nemotron, openai = clients()
    class APITimeoutError(Exception):
        pass
    nemotron.chat.completions.create.side_effect = [APITimeoutError('secret prompt'), {
        'choices': [{'finish_reason': 'stop', 'message': {'content': 'Hello', 'tool_calls': []}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}]
    env.worker.models = ModelGateway(nemotron, openai)
    await env.service.admit(inbound(env, 'tracked', 'Please explain projects'))
    job = await env.worker.claim('turn')
    await env.worker.run_job(job)
    first, second = await records(env, job)
    assert first['status'] == 'unknown' and first['input_tokens'] is None
    assert second['status'] == 'received' and second['input_tokens'] == 10
    assert first['id'] != second['id'] and [first['generation'], second['generation']] == [1, 2]
    assert nemotron.chat.completions.create.await_count == 2
    assert (await env.service.snapshot())['jobs'][job['id']]['status'] == 'completed'
    assert request_context.get() is None


async def test_gateway_records_usage_before_rejecting_incomplete_answer():
    env = await environment()
    job, _, _ = await create_project(env)
    nemotron, openai = clients()
    openai.responses.create.return_value = dict(response(status='incomplete'),
                                                usage={'input_tokens': 15, 'output_tokens': 4})
    gateway = ModelGateway(nemotron, openai)
    token = request_context.set((env.service, job['id'], job['claim']))
    try:
        with pytest.raises(ProviderError, match='finish'):
            await gateway.answer('', [], [{'filename': 'guide.md', 'text': 'A fact'}], 'Question')
    finally:
        request_context.reset(token)
    row, = await records(env, job)
    assert row['status'] == 'received' and row['provider_status'] == 'incomplete'
    assert (row['input_tokens'], row['output_tokens']) == (15, 4)


async def test_embedding_adapters_record_provider_metadata_and_project_owner():
    env = await environment()
    job, project, _ = await create_project(env)
    await env.service.store.transaction(lambda state: state['jobs'].update({'index': {
        'id': 'index', 'kind': 'index', 'project': project['id'], 'status': 'running',
        'claim': 'index-claim', 'lease': env.clock.now + 60, 'deadline': env.clock.now + 120,
        'attempts': 1, 'created': env.clock.now}}))
    embedder = NemotronEmbeddings('test-key', dimensions=128)
    embedder.client.embeddings.create = AsyncMock(side_effect=[
        SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0] * 128)],
                        usage={'prompt_tokens': 8, 'total_tokens': 8}),
        SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0] * 128)],
                        usage={'prompt_tokens': 3, 'total_tokens': 3})])
    token = request_context.set((env.service, 'index', 'index-claim'))
    try:
        await embedder.embed_chunks([{'text': 'A small document'}])
        await embedder.embed_query('Question')
    finally:
        request_context.reset(token)
        await embedder.close()
    rows = (await env.service.snapshot())['jobs']['index']['requests']
    assert [r['input_tokens'] for r in rows] == [8, 3]
    assert all(r['provider'] == 'nvidia' and r['model'] == embedder.model for r in rows)
    assert all(r['account_id'] == job['user'] and r['project_id'] == project['id'] for r in rows)
    assert all(r['participant_id'] is None and r['conversation_id'] is None for r in rows)


async def test_unobserved_calls_keep_existing_adapter_behavior():
    call = AsyncMock(return_value={'value': 'unchanged'})
    assert await tracked_call(call, 'openai', 'answer', 'model', input='private') == {'value': 'unchanged'}
    call.assert_awaited_once_with(input='private')


async def test_customer_cost_belongs_to_project_owner_not_participant():
    env = await environment()
    owner_job, project, _ = await create_project(env)
    await env.service.admit(inbound(env, 'select', '/use ' + project['code'], guest=True))
    await env.service.admit(inbound(env, 'question', 'A question', guest=True))
    job = await env.worker.claim('turn')
    assert job['id'] == 'question'
    await observe(env, job, AsyncMock(return_value={}))
    row, = await records(env, job)
    assert row['account_id'] == owner_job['user'] != row['participant_id']
    assert row['participant_id'] == job['user'] and row['project_id'] == project['id']
    assert row['conversation_id'] == f'{job["user"]}:project:{project["id"]}'


async def test_stale_job_and_request_ceiling_prevent_dispatch():
    env = await environment()
    job, _, _ = await create_project(env)
    call = AsyncMock(return_value={})
    await env.service.store.transaction(lambda state: state['jobs'][job['id']].update(
        requests=[{} for _ in range(env.service.limits.generations_per_turn + 10)]))
    with pytest.raises(DomainError, match='request limit'):
        await observe(env, job, call)
    await env.service.store.transaction(lambda state: state['jobs'][job['id']].update(requests=[], lease=0))
    with pytest.raises(DomainError, match='lease'):
        await observe(env, job, call)
    call.assert_not_awaited()
    assert await records(env, job) == []


async def test_known_http_error_records_status_without_exception_body():
    env = await environment()
    job, _, _ = await create_project(env)
    class Rejected(Exception):
        status_code = 429
    call = AsyncMock(side_effect=Rejected('sensitive provider body'))
    with pytest.raises(Rejected):
        await observe(env, job, call)
    row, = await records(env, job)
    assert row['status'] == 'failed' and row['http_status'] == 429
    assert row['input_tokens'] is None and 'sensitive provider body' not in str(row)
