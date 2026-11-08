import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database import DatabaseUnavailable
from app.photon import SendResult
from test_service import create_project, environment


def test_worker_storage_retry_recovers_uncertain_submission_without_resend():
    async def scenario():
        env = await environment()
        job, _, _ = await create_project(env)
        await env.service.complete(job['id'], job['claim'], 'Created')
        calls = 0

        async def tick():
            nonlocal calls
            calls += 1
            if calls == 1:
                await env.service.store.transaction(lambda state: state['outbox'][job['id']].update(status='submitting'))
                raise DatabaseUnavailable('unknown commit')
            assert (await env.service.snapshot())['outbox'][job['id']]['status'] == 'unknown'

        env.worker._run = tick
        env.worker.stopping = SimpleNamespace(is_set=lambda: False, wait=AsyncMock())
        await env.worker.run()
        assert calls == 2
        env.photon.send.assert_not_awaited()
    asyncio.run(scenario())


def test_worker_storage_retry_is_bounded():
    async def scenario():
        env = await environment()
        env.worker._run = AsyncMock(side_effect=DatabaseUnavailable('offline'))
        env.worker.stopping = SimpleNamespace(is_set=lambda: False, wait=AsyncMock())
        with pytest.raises(DatabaseUnavailable):
            await env.worker.run()
        assert env.worker._run.await_count == 5
    asyncio.run(scenario())


def test_unknown_delivery_lookups_have_durable_backoff_and_attempt_limit():
    async def scenario():
        env = await environment()
        job, _, _ = await create_project(env)
        await env.service.complete(job['id'], job['claim'], 'Created')
        env.photon.send.return_value = SendResult('accepted', 'known-id')
        env.photon.reconcile.return_value = SendResult('accepted', 'known-id')
        await env.worker.dispatch(job['id'])
        for attempt in range(5):
            await env.worker.reconcile(job['id'])
            await env.worker.reconcile(job['id'])
            assert env.photon.reconcile.await_count == attempt + 1
            env.clock.now += 20 * 2 ** attempt
        await env.worker.recover()
        await env.worker.reconcile(job['id'])
        assert env.photon.reconcile.await_count == 5
        assert env.photon.send.await_count == 1
        assert (await env.service.snapshot())['outbox'][job['id']]['status'] == 'unknown'
    asyncio.run(scenario())
