import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Limits
from app.database import MemoryStore
from app.photon import Inbound, SendResult
from app.security import Keyring
from app.service import DomainError, Service
from app.worker import Worker


async def environment(limits=None):
    clock = SimpleNamespace(now=1_800_000_000.0)
    service = Service(MemoryStore(), Keyring({'test': b'k' * 32}, 'test'),
                      'https://vault.example', limits=limits, clock=lambda: clock.now)
    await service.sync_assignments([
        {'id': 'assignment-owner', 'phone': '+12045550101', 'destination': '+16285550101', 'created': clock.now - 60},
        {'id': 'assignment-guest', 'phone': '+12045550102', 'destination': '+16285550102', 'created': clock.now - 60},
    ])
    models = SimpleNamespace(builder=AsyncMock(), answer=AsyncMock(return_value='Supported. [guide.md > Guide]'))
    embeddings = SimpleNamespace(model='text-embedding-3-small', dimensions=2,
        preprocessing_version='markdown-v1', embed_query=AsyncMock(return_value=[1.0, 0.0]),
        embed_chunks=AsyncMock(side_effect=lambda chunks: [
            {**chunk, 'embedding': [1.0, 0.0], 'embedding_model': 'text-embedding-3-small',
             'dimensions': 2, 'preprocessing_version': 'markdown-v1'} for chunk in chunks]))
    photon = SimpleNamespace(send=AsyncMock(return_value=SendResult('delivered', 'message-1')),
                             reconcile=AsyncMock(return_value=SendResult('delivered', 'message-1')))
    worker = Worker(service, models, embeddings, photon)
    return SimpleNamespace(service=service, worker=worker, models=models, embeddings=embeddings,
                           photon=photon, clock=clock)


def inbound(env, event_id, text, *, guest=False, occurred_at=None):
    return Inbound(event_id, '+12045550102' if guest else '+12045550101',
        '+16285550102' if guest else '+16285550101', text,
        env.clock.now if occurred_at is None else occurred_at,
        'space-guest' if guest else 'space-owner', 'agent-1', f'message-{event_id}')


async def create_project(env, operation='create', name='Bakery', *, guest=False):
    await env.service.admit(inbound(env, operation, f'Create {name}', guest=guest))
    job = await env.worker.claim('turn')
    assert job['id'] == operation
    step = await env.service.record_intent(job['id'], job['claim'], 'create_project', {'name': name})
    project = await env.service.execute_step(job['id'], job['claim'], step['id'])
    return job, project, step


async def issue_capability(env, job):
    step = await env.service.record_intent(job['id'], job['claim'], 'issue_vault_link', {})
    result = await env.service.execute_step(job['id'], job['claim'], step['id'])
    state = await env.service.snapshot()
    cap = state['capabilities'][result['capability']]
    material = state['delivery_material'][cap['id']]
    token = env.service.keys.decrypt(material, f'cap:{cap["id"]}:{cap["project"]}:{cap["step"]}')['token']
    return token, cap, step


def test_registration_deduplication_and_preassignment_prerequisite():
    async def scenario():
        env = await environment()
        event = inbound(env, 'one', 'Hello')
        first = await env.service.admit(event)
        duplicate = await env.service.admit(event)
        assert first == duplicate and first['sequence'] == 1
        state = await env.service.snapshot()
        assert len(state['users']) == len(state['jobs']) == 1
        assert sum(v for k, v in state['usage'].items() if k.startswith('turn-minute:')) == 1
        rejected = await env.service.admit(inbound(env, 'old', 'Earlier text', guest=True, occurred_at=env.clock.now - 61))
        assert rejected['status'] == 'rejected'
        assert '+12045550102' not in (await env.service.snapshot())['users']
        assert await env.service.admit(inbound(env, 'old', 'Retry', guest=True)) == rejected
        unknown = Inbound('unknown', '+12045550999', event.destination, 'hello', env.clock.now, 'space', 'agent', 'message')
        assert (await env.service.admit(unknown))['status'] == 'rejected'
    asyncio.run(scenario())


def test_create_step_replay_restores_result_without_restoring_old_routing():
    async def scenario():
        env = await environment()
        job, project, step = await create_project(env)
        first = await env.service.snapshot()
        assert first['users'][job['user']]['revision'] == first['jobs'][job['id']]['revision'] == 1
        assert await env.service.execute_step(job['id'], job['claim'], step['id']) == project
        assert len((await env.service.snapshot())['projects']) == 1
        await env.service.admit(inbound(env, 'navigate', f'/use {project["code"]}'))
        with pytest.raises(DomainError):
            await env.service.execute_step(job['id'], job['claim'], step['id'])
        state = await env.service.snapshot()
        assert state['users'][job['user']]['revision'] == 2
        assert state['users'][job['user']]['mode'] == 'customer'
        assert state['jobs'][job['id']]['status'] == 'superseded'
        assert len(state['projects']) == 1
    asyncio.run(scenario())


def test_cross_user_ownership_and_independent_editing_focus():
    async def scenario():
        env = await environment()
        owner_job, owner_project, _ = await create_project(env)
        await env.service.complete(owner_job['id'], owner_job['claim'], 'Created')
        guest_job, guest_project, _ = await create_project(env, 'guest-create', guest=True)
        await env.service.complete(guest_job['id'], guest_job['claim'], 'Created')
        await env.service.admit(inbound(env, 'use-guest', f'/use {guest_project["code"]}'))
        state = await env.service.snapshot()
        owner = state['users'][owner_job['user']]
        assert owner['focus'] == owner_project['id'] and owner['selected'] == guest_project['id']
        before = copy.deepcopy(owner)
        await env.service.admit(inbound(env, 'edit-guest', f'/edit {guest_project["code"]}'))
        owner = (await env.service.snapshot())['users'][owner_job['user']]
        assert {k: owner[k] for k in ('mode', 'focus', 'selected', 'revision')} == {k: before[k] for k in ('mode', 'focus', 'selected', 'revision')}
        await env.service.admit(inbound(env, 'builder', '/builder'))
        owner = (await env.service.snapshot())['users'][owner_job['user']]
        assert owner['mode'] == 'builder' and owner['focus'] == owner_project['id']
        assert owner['selected'] == guest_project['id']
    asyncio.run(scenario())


def test_capability_crash_recovery_handoff_and_confirmed_delivery_cleanup():
    async def scenario():
        env = await environment()
        job, _, _ = await create_project(env)
        token, cap, step = await issue_capability(env, job)
        before = await env.service.snapshot()
        ciphertext = copy.deepcopy(before['delivery_material'][cap['id']])
        assert token not in json.dumps(before)
        await env.worker.recover()
        recovered = await env.worker.claim('turn')
        assert recovered['claim'] != job['claim']
        result = await env.service.execute_step(job['id'], recovered['claim'], step['id'])
        state = await env.service.snapshot()
        assert result == {'capability': cap['id'], 'expires': cap['expires']}
        assert state['delivery_material'][cap['id']] == ciphertext
        assert len(state['capabilities']) == 1
        await env.service.complete(job['id'], recovered['claim'], 'Your link is ready.')
        state = await env.service.snapshot()
        assert not state['delivery_material']
        payload = env.service.keys.decrypt(state['outbox'][job['id']]['payload'], f'outbox:{job["id"]}')
        assert payload['text'].endswith('/vault#' + token)
        assert token not in json.dumps(state['conversations']) and token not in json.dumps(state['jobs'])
        assert '[vault link issued]' in json.dumps(state['conversations'])
        assert not any(m['role'] == 'assistant' for m in env.service.history(state, state['jobs'][job['id']]))
        await env.worker.dispatch(job['id'])
        state = await env.service.snapshot()
        assert 'payload' not in state['outbox'][job['id']] and not state['delivery_material']
        assert state['capabilities'][cap['id']]['delivered']
        assert any(m['role'] == 'assistant' for m in env.service.history(state, state['jobs'][job['id']]))
    asyncio.run(scenario())


def test_navigation_revokes_unfinished_capability_and_replay_cannot_recreate_it():
    async def scenario():
        env = await environment()
        job, project, _ = await create_project(env)
        token, cap, step = await issue_capability(env, job)
        await env.service.admit(inbound(env, 'navigate', f'/use {project["code"]}'))
        state = await env.service.snapshot()
        assert state['capabilities'][cap['id']]['revoked'] and not state['delivery_material']
        with pytest.raises(DomainError):
            env.service.authorize(state, token)
        with pytest.raises(DomainError):
            await env.service.execute_step(job['id'], job['claim'], step['id'])
        assert len((await env.service.snapshot())['capabilities']) == 1
    asyncio.run(scenario())


def test_capability_expiry_cancels_pending_delivery_and_blocks_saves():
    async def scenario():
        env = await environment()
        job, _, _ = await create_project(env)
        token, cap, _ = await issue_capability(env, job)
        await env.service.complete(job['id'], job['claim'], 'Link ready')
        env.clock.now = cap['expires']
        with pytest.raises(DomainError, match='expired'):
            await env.service.save_file(token, 'late.md', 'Too late')
        await env.worker.cleanup()
        await env.worker.dispatch(job['id'])
        state = await env.service.snapshot()
        assert not state['delivery_material'] and 'payload' not in state['outbox'][job['id']]
        assert not state['files']
        env.photon.send.assert_not_called()
    asyncio.run(scenario())


def test_files_are_versioned_atomically_with_jobs_and_cross_project_access_is_denied():
    async def scenario():
        env = await environment()
        job, _, _ = await create_project(env)
        token, _, _ = await issue_capability(env, job)
        await env.service.complete(job['id'], job['claim'], 'Created')
        file = await env.service.save_file(token, 'guide.md', '# Guide\nInitial text')
        state = await env.service.snapshot()
        assert state['jobs'][f'index:{file["id"]}:1']['status'] == 'queued'
        with pytest.raises(DomainError, match='exists'):
            await env.service.save_file(token, 'GUIDE.md', 'duplicate')
        outcomes = await asyncio.gather(
            env.service.save_file(token, 'guide.md', 'first edit', file['id'], 1),
            env.service.save_file(token, 'guide.md', 'second edit', file['id'], 1), return_exceptions=True)
        assert sum(isinstance(result, DomainError) for result in outcomes) == 1
        state = await env.service.snapshot()
        assert state['files'][file['id']]['version'] == 2
        assert state['jobs'][f'index:{file["id"]}:1']['status'] == 'superseded'
        guest_job, _, _ = await create_project(env, 'guest', guest=True)
        guest_token, _, _ = await issue_capability(env, guest_job)
        with pytest.raises(DomainError, match='not found'):
            await env.service.read_file(guest_token, file['id'])
        with pytest.raises(DomainError, match='not found'):
            await env.service.save_file(guest_token, 'guide.md', 'stolen', file['id'], 2)
        await env.service.delete_file(token, file['id'], 2)
        state = await env.service.snapshot()
        assert file['id'] not in state['files'] and file['id'] not in state['chunks']
        assert state['jobs'][f'index:{file["id"]}:2']['status'] == 'superseded'
    asyncio.run(scenario())


def test_concurrent_file_quota_rejection_keeps_existing_file_and_job():
    async def scenario():
        env = await environment(Limits(files_per_project=1))
        job, _, _ = await create_project(env)
        token, _, _ = await issue_capability(env, job)
        outcomes = await asyncio.gather(env.service.save_file(token, 'one.md', 'one'),
            env.service.save_file(token, 'two.md', 'two'), return_exceptions=True)
        assert sum(isinstance(result, DomainError) for result in outcomes) == 1
        state = await env.service.snapshot()
        assert len(state['files']) == 1
        assert len([j for j in state['jobs'].values() if j['kind'] == 'index']) == 1
    asyncio.run(scenario())


def test_rejected_capacity_is_durable_and_navigation_does_not_wait_for_generation():
    async def scenario():
        env = await environment(Limits(pending_per_user=1))
        job, project, _ = await create_project(env)
        rejection = await env.service.admit(inbound(env, 'full', 'Another question'))
        assert rejection['status'] == 'rejected'
        await env.service.admit(inbound(env, 'switch', f'/use {project["code"]}'))
        assert await env.service.admit(inbound(env, 'full', 'Retry')) == rejection
        state = await env.service.snapshot()
        assert state['jobs'][job['id']]['status'] == 'superseded'
        assert state['users'][job['user']]['mode'] == 'customer'
        assert 'full' not in state['jobs']
    asyncio.run(scenario())


def test_reset_and_invalid_commands_preserve_route_and_tool_permissions():
    async def scenario():
        env = await environment()
        job, _, _ = await create_project(env)
        await env.service.complete(job['id'], job['claim'], 'Created')
        before = (await env.service.snapshot())['users'][job['user']]
        for index, command in enumerate(('/reset', 'forget all chat history', '/use MISSING', '/edit')):
            await env.service.admit(inbound(env, f'command-{index}', command))
        state = await env.service.snapshot()
        assert state['users'][job['user']]['revision'] == before['revision']
        assert state['users'][job['user']]['focus'] == before['focus']
        assert len(state['projects']) == 1
        assert all(not j['steps'] for j in state['jobs'].values() if j['id'].startswith('command-'))
        env.models.builder.assert_not_called()
    asyncio.run(scenario())
