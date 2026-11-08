import asyncio
import copy

import pytest

from app.photon import SendResult
from test_service import create_project, environment, inbound, issue_capability


async def completed_answer(env):
    job, project, _ = await create_project(env)
    token, _, _ = await issue_capability(env, job)
    await env.service.complete(job['id'], job['claim'], 'Created')
    file = await env.service.save_file(token, 'guide.md', '# Guide\nOld source.')
    await env.worker.run_job(await env.worker.claim('index'))
    await env.service.admit(inbound(env, 'select', '/use ' + project['code'], guest=True))
    await env.service.admit(inbound(env, 'question', 'What does the source say?', guest=True))
    await env.worker.run_job(await env.worker.claim('turn'))
    state = await env.service.snapshot()
    assert state['outbox']['question']['status'] == 'pending'
    assert state['outbox']['question']['versions'] == {file['id']: 1}
    return token, file


@pytest.mark.parametrize('change', ['save', 'delete'])
def test_sources_changed_before_first_submission_send_notice_with_same_key(change):
    async def scenario():
        env = await environment()
        token, file = await completed_answer(env)
        old_key = (await env.service.snapshot())['outbox']['question']['key']
        if change == 'save':
            await env.service.save_file(token, file['name'], '# Guide\nNew source.', file['id'], 1)
        else:
            await env.service.delete_file(token, file['id'], 1)
        await env.worker.dispatch('question')
        notice = 'Sources are updating. Please ask again shortly.'
        assert env.photon.send.call_args.args[2:] == (notice, old_key)
        state = await env.service.snapshot()
        assert state['outbox']['question']['versions'] == {}
        assert state['outbox']['question']['status'] == 'sent'
        history = env.service.history(state, state['jobs']['question'])
        assert any(m['role'] == 'assistant' and m['content'] == notice for m in history)
        assert not any(m['content'] == 'Supported. [guide.md > Guide]' for m in history)
        assert env.models.answer.await_count == 1
    asyncio.run(scenario())


def test_navigation_cancels_stale_source_reply_without_sending_updating_notice():
    async def scenario():
        env = await environment()
        token, file = await completed_answer(env)
        await env.service.save_file(token, file['name'], 'New source.', file['id'], 1)
        await env.service.admit(inbound(env, 'navigate', '/edit', guest=True))
        await env.worker.dispatch('question')
        assert (await env.service.snapshot())['outbox']['question']['status'] == 'cancelled'
        env.photon.send.assert_not_called()
    asyncio.run(scenario())


def test_ambiguous_submission_payload_is_never_rewritten_or_resent_after_source_change():
    async def scenario():
        env = await environment()
        token, file = await completed_answer(env)
        env.photon.send.return_value = SendResult('unknown', 'possibly-accepted')
        await env.worker.dispatch('question')
        old = copy.deepcopy((await env.service.snapshot())['outbox']['question'])
        await env.service.save_file(token, file['name'], 'New source.', file['id'], 1)
        await env.worker.dispatch('question')
        current = (await env.service.snapshot())['outbox']['question']
        assert env.photon.send.await_count == 1
        assert current['payload'] == old['payload']
        assert current['versions'] == old['versions']
        assert current['status'] == 'unknown'
    asyncio.run(scenario())


def test_prior_attempt_prevents_rewriting_even_if_record_is_pending():
    async def scenario():
        env = await environment()
        token, file = await completed_answer(env)
        await env.service.store.transaction(lambda state: state['outbox']['question'].update(attempts=1))
        await env.service.save_file(token, file['name'], 'New source.', file['id'], 1)
        await env.worker.dispatch('question')
        state = await env.service.snapshot()
        assert state['outbox']['question']['status'] == 'cancelled'
        assert state['outbox']['question']['versions'] == {file['id']: 1}
        env.photon.send.assert_not_called()
    asyncio.run(scenario())
