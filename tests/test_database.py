import asyncio
import copy
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from app.database import DatabaseUnavailable, MemoryStore, SpacetimeStore


def test_memory_transactions_isolate_snapshots_and_rollback_errors():
    async def scenario():
        store = MemoryStore({'count': 0, 'items': []})
        revision, snapshot = await store.read()
        snapshot['items'].append('not committed')
        assert (await store.read())[1]['items'] == []
        assert await store.compare_and_swap(revision, {'count': 1, 'items': []})
        assert not await store.compare_and_swap(revision, {'count': 99})

        def bad(state):
            state['count'] = 20
            raise ValueError('cancelled')

        with pytest.raises(ValueError):
            await store.transaction(bad)
        assert (await store.read())[1]['count'] == 1
    asyncio.run(scenario())


def test_conflicting_transactions_retry_without_lost_updates():
    class ConflictingStore(MemoryStore):
        async def compare_and_swap(self, expected, state):
            if expected == 0:
                await super().compare_and_swap(0, {'count': 4})
            return await super().compare_and_swap(expected, state)

    async def scenario():
        store = ConflictingStore({'count': 0})
        calls = []

        def increment(state):
            calls.append(state['count'])
            state['count'] += 1
            return state['count']

        assert await store.transaction(increment) == 5
        assert calls == [0, 4]
        assert (await store.read())[1]['count'] == 5
    asyncio.run(scenario())


def test_http_contract_uses_authenticated_confirmed_procedure_and_reducer():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer runtime-token'
        assert request.url.params['confirmed'] == 'true'
        if request.url.path.endswith('/read_state'):
            assert json.loads(request.content) == []
            return httpx.Response(200, json=json.dumps({'revision': '7', 'state': {'jobs': {}}}))
        expected, value = json.loads(request.content)
        assert expected == 7
        assert json.loads(value) == {'jobs': {'one': {'status': 'pending'}}}
        return httpx.Response(200)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = SpacetimeStore('https://database.example', 'hackutd-xmddq', 'runtime-token', client=client)
            revision, state = await store.read()
            state['jobs']['one'] = {'status': 'pending'}
            assert await store.compare_and_swap(revision, state)
        assert len(requests) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('status,body,conflict', [
    (530, 'state revision conflict', True),
    (530, 'backend identity required', False),
    (403, 'backend identity required', False),
    (401, 'invalid credential', False),
    (504, 'outcome unknown', False),
    (302, '', False),
])
def test_only_explicit_revision_conflicts_are_retried(status, body, conflict):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text=body)
        )) as client:
            store = SpacetimeStore('https://database.example', 'db', 'runtime-token', client=client)
            if conflict:
                assert not await store.compare_and_swap(0, {})
            else:
                with pytest.raises(DatabaseUnavailable):
                    await store.compare_and_swap(0, {})
    asyncio.run(scenario())


def test_ambiguous_commit_is_not_replayed_automatically():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/read_state'):
            return httpx.Response(200, json=json.dumps({'revision': '0', 'state': {}}))
        raise httpx.ReadTimeout('commit might have succeeded', request=request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = SpacetimeStore('https://database.example', 'db', 'runtime-token', client=client)
            with pytest.raises(DatabaseUnavailable, match='unknown'):
                await store.transaction(lambda state: state.update({'job': 'one'}))
        assert len(calls) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('url,token', [
    ('http://database.example', 'token'),
    ('https://database.example', ''),
    ('https://user:pass@database.example', 'token'),
    ('https://database.example?token=secret', 'token'),
])
def test_invalid_database_configuration_fails_closed(url, token):
    with pytest.raises(ValueError):
        SpacetimeStore(url, 'db', token)


@pytest.mark.parametrize('payload', [None, {}, 'not json', '{"revision":0,"state":[]}'])
def test_malformed_database_snapshot_is_rejected(payload):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        )) as client:
            store = SpacetimeStore('https://database.example', 'db', 'token', client=client)
            with pytest.raises(DatabaseUnavailable):
                await store.read()
    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for the cross-language database contract check')
def test_actual_journey_snapshots_pass_the_database_reducer_policy():
    from test_service import create_project, environment, inbound, issue_capability

    async def scenario():
        env = await environment()
        transitions = []
        compare = env.service.store.compare_and_swap

        async def checked(expected, state):
            _, before = await env.service.store.read()
            committed = await compare(expected, state)
            if committed:
                transitions.append({'before': before, 'after': copy.deepcopy(state), 'now': env.clock.now})
            return committed

        env.service.store.compare_and_swap = checked
        job, project, _ = await create_project(env)
        token, _, _ = await issue_capability(env, job)
        await env.service.complete(job['id'], job['claim'], 'Ready')
        await env.worker.dispatch(job['id'])
        file = await env.service.save_file(token, 'guide.md', '# Guide\nCurrent source')
        await env.worker.index(await env.worker.claim('index'))
        await env.service.admit(inbound(env, 'guest-use', '/use ' + project['code'], guest=True))
        await env.service.admit(inbound(env, 'question', 'What is current?', guest=True))
        await env.worker.answer(await env.worker.claim('turn'))
        await env.worker.dispatch('question')
        await env.service.save_file(token, 'guide.md', '# Guide\nNew source', file['id'], 1)
        await env.worker.index(await env.worker.claim('index'))
        env.clock.now += 31 * 86400
        await env.worker.cleanup()
        return transitions

    script = """
        import fs from 'node:fs';
        import { validateCommit } from './db/src/policy.ts';
        const rows = JSON.parse(fs.readFileSync(0, 'utf8'));
        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            try {
                validateCommit(0n, 0n, JSON.stringify(row.after), JSON.stringify(row.before), row.now);
            } catch (error) {
                throw new Error('Committed journey transition ' + i + ': ' + error.message);
            }
        }
    """
    result = subprocess.run(
        [shutil.which('node'), '--experimental-strip-types', '--input-type=module', '-e', script],
        input=json.dumps(asyncio.run(scenario())), capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1], timeout=15,
    )
    assert result.returncode == 0, result.stderr
