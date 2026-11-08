import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.photon import PhotonClient, SendResult
from app.reactions import JevClient, JevError
from app.service import DomainError
from test_service import create_project, environment, inbound


@pytest.mark.parametrize('fits,social,meaning,emoji,only', [
    (.9, .95, 'acknowledgment', '👍', True),
    (.9, .95, 'warmth', '❤️', True),
    (.9, .95, 'taking_a_look', '👀', False),
    (.7, .95, 'warmth', None, False),
    (.2, .95, 'warmth', None, False),
    (.9, .7, 'acknowledgment', '👍', False),
    (.9, .1, 'acknowledgment', '👍', False),
])
async def test_jev_policy_and_request(fits, social, meaning, emoji, only):
    def handler(request):
        body = json.loads(request.content)
        assert request.url == 'https://api.typesafe.ai/v1/systemone'
        assert request.headers['Authorization'] == 'Bearer synthetic'
        assert body['model'] == 'jev-1.14.0'
        assert len(body['questions']) == 3
        assert 'secret' not in json.dumps(body)
        return httpx.Response(200, json={'answers': {
            'fits': {'noul': fits}, 'social': {'noul': social}, 'meaning': {'choice': meaning}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        decision = await JevClient(http, 'synthetic').decide('Thanks https://app/vault#secret',
            [{'role': 'assistant', 'content': 'https://app/vault#secret'}], {'name': 'Hypha'})
    assert decision == {'status': 'decided', 'emoji': emoji, 'only': only}


@pytest.mark.parametrize('response', [httpx.Response(429), httpx.Response(200, json={}),
    httpx.Response(200, json={'answers': {'fits': {'noul': True}, 'social': {'noul': .9}}})])
async def test_jev_errors_are_not_no_reaction(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(JevError):
            await JevClient(http, 'synthetic').decide('thanks', [], {'name': 'Hypha'})


@pytest.mark.parametrize('action', ['add', 'remove'])
async def test_photon_reaction_contract(action):
    def handler(request):
        assert request.method == 'POST' and request.url.path == '/v1/projects/project/messages'
        assert request.headers['Idempotency-Key'] == f'turn:reaction:{action}'
        assert json.loads(request.content) == {'spaceId': 'space', 'senderId': 'agent',
            'parentMessageId': 'incoming', 'content': {'type': 'reaction', 'reaction': '👀', 'action': action}}
        return httpx.Response(202, json={'id': 'reaction', 'status': 'accepted'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await PhotonClient(http, 'project', 'synthetic').react('space', 'agent', 'incoming',
            '👀', action, f'turn:reaction:{action}') == SendResult('accepted', 'reaction')


async def reaction_environment(emoji='❤️', only=True):
    env = await environment()
    env.worker.jev = SimpleNamespace(decide=AsyncMock(return_value={
        'status': 'decided', 'emoji': emoji, 'only': only}))
    env.photon.react = AsyncMock(return_value=SendResult('delivered', 'reaction-message'))
    env.models.builder.return_value = {'text': 'You are welcome!', 'tool_calls': []}
    return env


async def turn(env, identifier='thanks', text='Thank you so much'):
    await env.service.admit(inbound(env, identifier, text))
    job = await env.worker.claim('turn')
    await env.worker.run_job(job)
    return job


async def test_social_reaction_replaces_reply_and_records_history_without_duplicates():
    env = await reaction_environment()
    job = await turn(env)
    await env.service.admit(inbound(env, 'thanks', 'Thank you so much'))
    assert await env.worker.claim('turn') is None
    state = await env.service.snapshot()
    assert state['jobs']['thanks']['status'] == 'completed'
    assert 'thanks' not in state['outbox']
    assert env.service.history(state, job) == [
        {'role': 'user', 'content': 'Thank you so much'},
        {'role': 'assistant', 'content': '[Reacted ❤️ to the message]'}]
    env.models.builder.assert_not_awaited()
    assert env.photon.react.await_count == 1


@pytest.mark.parametrize('result', [SendResult('unknown'), SendResult('failed'), SendResult('accepted', 'reaction-message')])
async def test_unconfirmed_social_reaction_falls_back_to_text(result):
    env = await reaction_environment()
    env.photon.react.return_value = result
    env.photon.reconcile.return_value = SendResult('accepted', 'reaction-message')
    await turn(env)
    assert (await env.service.snapshot())['outbox']['thanks']['status'] == 'pending'
    assert env.models.builder.await_count == 1


@pytest.mark.parametrize('error', [JevError('synthetic'), TimeoutError()])
async def test_decision_error_preserved_and_normal_reply_continues(error):
    env = await reaction_environment()
    env.worker.jev.decide.side_effect = error
    await turn(env)
    state = await env.service.snapshot()
    assert state['jobs']['thanks']['reaction_decision']['status'] == 'error'
    assert 'thanks' in state['outbox']
    env.photon.react.assert_not_awaited()


async def test_eyes_removed_only_after_last_bubble_and_cleanup_survives_restart():
    env = await reaction_environment('👀', False)
    env.models.builder.return_value = {'text': 'First\n<next_message>\nSecond', 'tool_calls': []}
    await turn(env, text='Please look into this')
    await env.worker.cleanup_reactions()
    assert 'thanks:reaction:remove' not in (await env.service.snapshot())['outbox']
    await env.worker.dispatch('thanks')
    env.clock.now += 121  # Generation finished; pending later bubbles still retain eyes.
    await env.worker.cleanup_reactions()
    assert 'thanks:reaction:remove' not in (await env.service.snapshot())['outbox']
    await env.worker.dispatch('thanks')
    await env.worker.cleanup_reactions()
    await env.worker.recover()
    await env.worker.dispatch('thanks:reaction:remove')
    await env.worker.cleanup_reactions()
    await env.worker.dispatch('thanks:reaction:remove')
    assert env.photon.react.await_count == 2
    assert env.photon.react.call_args.args[2:] == ('message-thanks', '👀', 'remove', 'thanks:reaction:remove')


async def test_recovery_reuses_decision_and_never_replays_ambiguous_add():
    env = await reaction_environment('👀', False)
    await env.service.admit(inbound(env, 'work', 'Look into this'))
    job = await env.worker.claim('turn')
    env.photon.react.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await env.worker.react_to_turn(job)
    await env.worker.recover()
    job = await env.worker.claim('turn')
    await env.worker.run_job(job)
    assert env.worker.jev.decide.await_count == env.photon.react.await_count == 1
    assert (await env.service.snapshot())['outbox']['work:reaction:add']['status'] == 'unknown'


async def test_project_toggle_is_owner_only_and_disabled_by_default():
    env = await reaction_environment()
    job, project, _ = await create_project(env)
    step = await env.service.record_intent(job['id'], job['claim'], 'set_reactions', {'enabled': True})
    await env.service.execute_step(job['id'], job['claim'], step['id'])
    assert (await env.service.snapshot())['projects'][project['id']]['reactions_enabled'] is True
    await env.service.complete(job['id'], job['claim'], 'Created')
    await env.service.admit(inbound(env, 'guest', 'Change settings', guest=True))
    guest = await env.worker.claim('turn')
    await env.service.store.transaction(lambda s: s['jobs']['guest'].update(focus=project['id']))
    step = await env.service.record_intent(guest['id'], guest['claim'], 'set_reactions', {'enabled': False})
    with pytest.raises(DomainError) as error:
        await env.service.execute_step(guest['id'], guest['claim'], step['id'])
    assert error.value.status == 403


@pytest.mark.parametrize('enabled', [False, True])
async def test_project_opt_in_and_commands(enabled):
    env = await reaction_environment()
    job, project, _ = await create_project(env)
    assert not (await env.service.snapshot())['projects'][project['id']]['reactions_enabled']
    await env.service.complete(job['id'], job['claim'], 'Created')
    await env.service.store.transaction(lambda s: s['projects'][project['id']].update(reactions_enabled=enabled))
    await env.service.admit(inbound(env, 'navigate', f'/use {project["code"]}'))
    assert await env.worker.claim('turn') is None
    await turn(env)
    assert env.worker.jev.decide.await_count == int(enabled)


async def test_cleanup_after_navigation_uses_original_target_and_survives_more_navigation():
    env = await reaction_environment('👀', False)
    job, project, _ = await create_project(env)
    await env.worker.react_to_turn(job)
    await env.service.admit(inbound(env, 'navigate', f'/use {project["code"]}'))
    await env.worker.cleanup_reactions()
    await env.service.admit(inbound(env, 'back', '/edit'))
    await env.worker.dispatch('create:reaction:remove')
    assert env.photon.react.call_args.args[2:5] == ('message-create', '👀', 'remove')
