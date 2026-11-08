import asyncio
from unittest.mock import AsyncMock

import pytest

from app.photon import PhotonError, SendResult
from app.database import DatabaseUnavailable
from test_service import environment, inbound


async def setup():
    env = await environment()
    env.photon.set_typing = AsyncMock(return_value=SendResult('dispatched', 'typing'))
    incoming = inbound(env, 'thinking', 'Help me make an agent')
    await env.service.admit(incoming)
    await env.worker.refresh_typing(incoming)
    return env, incoming


async def test_typing_covers_generation_and_send_then_stops(monkeypatch):
    env, incoming = await setup()
    env.photon.set_typing.assert_awaited_once_with('space-owner', 'agent-1', True)
    await env.worker.refresh_typing()
    assert env.photon.set_typing.await_count == 1
    last = next(iter(env.worker.typing.values()))
    monkeypatch.setattr('app.worker.time.monotonic', lambda: last + 5)
    await env.worker.refresh_typing()
    assert env.photon.set_typing.await_count == 2
    job = await env.worker.claim('turn')
    await env.service.complete(job['id'], job['claim'], 'Here is your reply')

    async def send(*args):
        assert env.photon.set_typing.call_args.args[-1] is True
        return SendResult('delivered', 'reply')
    env.photon.send.side_effect = send
    await env.worker.dispatch(job['id'])
    env.photon.set_typing.assert_awaited_with('space-owner', 'agent-1', False)
    assert not env.worker.typing
    count = env.photon.set_typing.await_count
    await env.worker.refresh_typing(incoming)
    assert env.photon.set_typing.await_count == count


@pytest.mark.parametrize('ending', ['cancelled', 'failed', 'expired', 'shutdown', 'unassigned'])
async def test_typing_stops_when_no_reply_is_pending(ending):
    env, incoming = await setup()
    if ending == 'expired':
        env.clock.now += env.service.limits.turn_seconds
    elif ending == 'unassigned':
        await env.service.sync_assignments([])
    elif ending != 'shutdown':
        await env.service.store.transaction(lambda state: state['jobs'][incoming.event_id].update(status=ending))
    await env.worker.refresh_typing(stop=ending == 'shutdown')
    env.photon.set_typing.assert_awaited_with('space-owner', 'agent-1', False)
    assert not env.worker.typing


async def test_typing_remains_for_another_pending_turn_in_same_space():
    env, incoming = await setup()
    await env.service.admit(inbound(env, 'second', 'Another question'))
    job = await env.worker.claim('turn')
    await env.service.complete(job['id'], job['claim'], 'First answer')
    await env.worker.dispatch(job['id'])
    assert env.worker.typing
    assert all(call.args[-1] for call in env.photon.set_typing.call_args_list)


@pytest.mark.parametrize('status', ['delivered', 'unknown', 'failed'])
async def test_typing_failures_do_not_block_reply_or_leave_refreshing(status):
    env, incoming = await setup()
    env.photon.set_typing.side_effect = PhotonError('PHOTON_HTTP_422', 422)
    env.worker.typing[next(iter(env.worker.typing))] = None
    await env.worker.refresh_typing()
    job = await env.worker.claim('turn')
    await env.service.complete(job['id'], job['claim'], 'Reply')
    env.photon.send.return_value = SendResult(status)
    await env.worker.dispatch(job['id'])
    env.photon.send.assert_awaited_once()
    assert not env.worker.typing


async def test_stalled_typing_request_is_bounded_and_reply_can_send():
    env, incoming = await setup()
    cancelled = asyncio.Event()

    async def stalled(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    env.photon.set_typing.side_effect = stalled
    env.worker.typing[next(iter(env.worker.typing))] = None
    await asyncio.wait_for(env.worker.refresh_typing(), 3)
    assert cancelled.is_set()
    env.photon.set_typing.side_effect = None
    job = await env.worker.claim('turn')
    await env.service.complete(job['id'], job['claim'], 'Reply')
    await env.worker.dispatch(job['id'])
    env.photon.send.assert_awaited_once()


async def test_typing_survives_worker_recovery_and_stops_at_exit():
    env, incoming = await setup()
    attempts = 0

    async def tick():
        nonlocal attempts
        attempts += 1
        assert env.worker.typing
        if attempts == 1:
            raise DatabaseUnavailable('temporary')

    env.worker._run = tick
    env.worker.stopping.wait = AsyncMock(side_effect=TimeoutError)
    await env.worker.run()
    assert attempts == 2
    assert not env.worker.typing
    env.photon.set_typing.assert_awaited_with('space-owner', 'agent-1', False)
