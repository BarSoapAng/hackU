import asyncio
import json

import httpx
import pytest

from app.photon import PhotonClient, SendResult
from app.service import split_messages
from test_service import create_project, environment, inbound, issue_capability


async def reply(env, text='First<next_message>Second<next_message>Third', event='reply'):
    await env.service.admit(inbound(env, event, 'Tell me a little about yourself'))
    job = await env.worker.claim('turn')
    await env.service.complete(job['id'], job['claim'], text)
    return job


def test_three_nonempty_messages_preserve_overflow_and_paragraphs():
    assert split_messages('one\n\ntwo') == ['one\n\ntwo']
    assert split_messages('<next_message>one<next_message><next_message>two<next_message>three<next_message>four') == [
        'one', 'two', 'three\n\nfour']


@pytest.mark.asyncio
async def test_real_photon_adapter_sends_separate_plain_texts_after_delivery_only():
    env = await environment()
    job = await reply(env, '**First**<next_message>Second<next_message>Third')
    sends, lookups = [], []

    def handler(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            sends.append((body['content'], request.headers['Idempotency-Key']))
            return httpx.Response(202, json={'id': f'message-{len(sends)}', 'status': 'dispatched'})
        lookups.append(request.url.path)
        return httpx.Response(200, json={'id': request.url.path.rsplit('/', 1)[1], 'status': 'delivered'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        env.worker.photon = PhotonClient(client, 'project', 'test-token')
        for i in range(3):
            await env.worker.dispatch(job['id'])
            await env.worker.dispatch(job['id'])
            assert len(sends) == i + 1
            state = await env.service.snapshot()
            assert len([m for m in env.service.history(state, job) if m['role'] == 'assistant']) == i
            await env.worker.reconcile(job['id'])
        await env.worker.dispatch(job['id'])
    assert [content for content, _ in sends] == [
        {'type': 'text', 'format': 'plain', 'text': text} for text in ['First', 'Second', 'Third']]
    assert [key for _, key in sends] == ['reply', 'reply:part:1', 'reply:part:2']
    assert len(lookups) == 3
    box = (await env.service.snapshot())['outbox'][job['id']]
    assert box['status'] == 'sent' and box['delivered_parts'] == 3 and 'payload' not in box


@pytest.mark.asyncio
async def test_partial_delivery_survives_restart_and_does_not_interleave_other_replies():
    env = await environment()
    job = await reply(env)
    await env.worker.dispatch(job['id'])
    other = await reply(env, 'Later reply', 'later')
    await env.worker.dispatch(other['id'])
    assert env.photon.send.await_count == 1
    await env.worker.recover()
    await env.worker.dispatch(job['id'])
    await env.worker.dispatch(job['id'])
    await env.worker.dispatch(other['id'])
    assert [call.args[2] for call in env.photon.send.call_args_list] == ['First', 'Second', 'Third', 'Later reply']


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [SendResult('failed'), SendResult('unknown')])
async def test_failed_or_ambiguous_second_send_stops_remaining_texts(result):
    env = await environment()
    env.photon.send.side_effect = [SendResult('delivered', 'first'), result]
    job = await reply(env)
    await env.worker.dispatch(job['id'])
    await env.worker.dispatch(job['id'])
    await env.worker.recover()
    await env.worker.dispatch(job['id'])
    await env.worker.reconcile(job['id'])
    state = await env.service.snapshot()
    assert [m['content'] for m in env.service.history(state, job) if m['role'] == 'assistant'] == ['First']
    assert env.photon.send.await_count == 2


@pytest.mark.asyncio
async def test_navigation_after_partial_delivery_cancels_remainder_and_vault_link():
    env = await environment()
    job, project, _ = await create_project(env)
    _, cap, _ = await issue_capability(env, job)
    await env.service.complete(job['id'], job['claim'], 'First<next_message>Second<next_message>Third')
    await env.worker.dispatch(job['id'])
    await env.service.admit(inbound(env, 'navigate', '/use ' + project['code']))
    await env.worker.dispatch(job['id'])
    state = await env.service.snapshot()
    assert state['outbox'][job['id']]['status'] == 'cancelled'
    assert state['capabilities'][cap['id']]['revoked']
    assert env.photon.send.await_count == 1
    assert '/notes#' not in env.photon.send.call_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize('trailing', ['', '<next_message>'])
@pytest.mark.parametrize('parts', [['First'], ['First', 'Second', 'Third']])
async def test_vault_link_is_in_final_text_and_never_in_model_history(trailing, parts):
    env = await environment()
    job, _, _ = await create_project(env)
    await env.service.complete(job['id'], job['claim'], 'Created')
    await env.worker.dispatch(job['id'])
    env.photon.send.reset_mock()
    await env.service.admit(inbound(env, 'editing', 'I want to change a note'))
    job = await env.worker.claim('turn')
    token, cap, _ = await issue_capability(env, job)
    await env.service.complete(job['id'], job['claim'], '<next_message>'.join(parts) + trailing)
    for i in range(len(parts)):
        await env.worker.dispatch(job['id'])
        state = await env.service.snapshot()
        assert bool(state['capabilities'][cap['id']].get('delivered')) == (i == len(parts) - 1)
        assert ('[notes editing link issued]' in json.dumps(env.service.history(state, job))) == (i == len(parts) - 1)
    texts = [call.args[2] for call in env.photon.send.call_args_list]
    assert len(texts) == len(parts)
    assert all('/notes#' not in text for text in texts[:-1])
    assert texts[-1].endswith('/notes#' + token)
    assert token not in json.dumps(env.service.history(state, job))


@pytest.mark.asyncio
async def test_late_lookup_cannot_confirm_a_different_part():
    env = await environment()
    env.photon.send.side_effect = [SendResult('dispatched', 'first'), SendResult('dispatched', 'second')]
    job = await reply(env)
    await env.worker.dispatch(job['id'])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def lookup(message_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return SendResult('delivered', message_id)

    env.photon.reconcile.side_effect = lookup
    late = asyncio.create_task(env.worker.reconcile(job['id']))
    await entered.wait()
    env.clock.now += 2
    await env.worker.reconcile(job['id'])
    await env.worker.dispatch(job['id'])
    release.set()
    await late
    box = (await env.service.snapshot())['outbox'][job['id']]
    assert box['status'] == 'unknown' and box['message_id'] == 'second' and box['delivered_parts'] == 1


@pytest.mark.asyncio
async def test_builder_can_set_project_messaging_style_and_keep_existing_preferences():
    env = await environment()
    job, project, _ = await create_project(env)
    await env.service.complete(job['id'], job['claim'], 'Created')
    await env.worker.dispatch(job['id'])
    original = (await env.service.snapshot())['projects'][project['id']]['instructions']
    preference = original + ' Use up to three short texts when natural.'
    env.models.builder.side_effect = [
        {'text': '', 'tool_calls': [{'id': 'style', 'name': 'update_instructions', 'arguments': {'instructions': preference}}]},
        {'text': 'Done!<next_message>They can send a few short texts now.', 'tool_calls': []},
    ]
    await env.service.admit(inbound(env, 'style', 'Let my agent reply in a few little texts'))
    await env.worker.builder(await env.worker.claim('turn'))
    state = await env.service.snapshot()
    assert state['projects'][project['id']]['instructions'] == preference
    assert state['outbox']['style']['parts'] == 2
    prompt = env.models.builder.call_args_list[0].args[0][0]['content']
    assert 'prefer two or three short texts over one large block' in prompt
    assert 'Match the user’s conversational energy' in prompt
    assert json.loads(prompt.split('Current editing focus: ', 1)[1])['instructions'] == original


@pytest.mark.asyncio
@pytest.mark.parametrize('first_sent', [False, True])
async def test_source_change_stops_stale_remaining_parts(first_sent):
    from test_dispatch_sources import completed_answer
    env = await environment()
    env.models.answer.return_value = 'First [guide.md > Guide]<next_message>Second [guide.md > Guide]'
    token, file = await completed_answer(env)
    if first_sent:
        await env.worker.dispatch('question')
    await env.service.save_file(token, file['name'], 'Changed source.', file['id'], 1)
    await env.worker.dispatch('question')
    assert env.photon.send.await_count == 1
    state = await env.service.snapshot()
    assert state['outbox']['question']['status'] == ('cancelled' if first_sent else 'sent')
    history = env.service.history(state, state['jobs']['question'])
    assert [m['content'] for m in history if m['role'] == 'assistant'] == [
        'First [guide.md > Guide]' if first_sent else 'Sources are updating. Please ask again shortly.']
