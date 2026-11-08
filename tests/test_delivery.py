"""Cross-layer delivery tests use the real Photon adapter with mocked HTTP only."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

import httpx

from app.database import DatabaseUnavailable, MemoryStore
from app.main import create_app
from app.photon import Inbound, PhotonClient
from app.security import Keyring
from app.service import Service, initialize
from app.worker import Worker


class TestDelivery(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [1767225600.0]
        state = {}
        initialize(state)
        state['projects']['p1'] = {'id': 'p1', 'name': 'Demo', 'code': 'DEMO', 'owner': '+14155550123',
                                   'instructions': 'Answer from sources.'}
        state['users']['+14155550123'] = {'id': '+14155550123', 'phone': '+14155550123', 'mode': 'builder',
                                         'focus': 'p1', 'selected': None, 'revision': 0, 'sequence': 0}
        state['assignments']['+14155550123'] = {'id': 'assignment-1', 'phone': '+14155550123',
                                               'destination': '+16283166196', 'created': self.now[0] - 10}
        self.service = Service(MemoryStore(state), Keyring({'test': b'x' * 32}, 'test'),
                               'https://hypha.example.test', clock=lambda: self.now[0])
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def worker(self, handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(http)
        return Worker(self.service, None, None, PhotonClient(http, 'project', 'synthetic-secret'))

    async def issue(self):
        receipt = await self.service.admit(Inbound('notes-turn', '+14155550123', '+16283166196', '/notes',
                                                   self.now[0], 'space-1', 'agent-1', 'message-1'))
        assert receipt['status'] == 'accepted'
        state = await self.service.snapshot()
        return next(iter(state['capabilities']))

    def assistant_history(self, state):
        return [item for item in self.service.history(state, state['jobs']['notes-turn']) if item['role'] == 'assistant']

    async def test_definitive_send_4xx_fails_and_revokes_capability(self):
        for status in [400, 401, 403, 422, 429]:
            with self.subTest(status=status):
                await self.asyncSetUp()
                methods = []
                async def handler(request):
                    methods.append(request.method)
                    return httpx.Response(status, json={'detail': 'Sensitive provider body must not be persisted.'})
                worker = self.worker(handler)
                capability = await self.issue()
                await worker.dispatch('notes-turn')
                await worker.dispatch('notes-turn')
                state = await self.service.snapshot()
                assert state['outbox']['notes-turn']['status'] == 'failed'
                assert 'payload' not in state['outbox']['notes-turn']
                assert state['capabilities'][capability]['revoked']
                assert not state['delivery_material'] and not self.assistant_history(state)
                assert methods == ['POST']
                await self.asyncTearDown()

    async def test_dispatched_and_accepted_are_excluded_until_confirmed_delivery(self):
        for provider_status in ['dispatched', 'accepted']:
            with self.subTest(status=provider_status):
                await self.asyncSetUp()
                requests = []
                async def handler(request):
                    requests.append(request)
                    return httpx.Response(202 if request.method == 'POST' else 200,
                        json={'id': 'sent-1', 'status': provider_status if request.method == 'POST' else 'delivered'})
                worker = self.worker(handler)
                capability = await self.issue()
                await worker.dispatch('notes-turn')
                state = await self.service.snapshot()
                assert state['outbox']['notes-turn']['status'] == 'unknown'
                assert state['outbox']['notes-turn']['message_id'] == 'sent-1'
                assert 'payload' in state['outbox']['notes-turn']
                assert not self.assistant_history(state)
                await worker.dispatch('notes-turn')
                await worker.reconcile('notes-turn')
                state = await self.service.snapshot()
                assert state['outbox']['notes-turn']['status'] == 'sent'
                assert 'payload' not in state['outbox']['notes-turn']
                assert state['capabilities'][capability]['delivered']
                assert not state['capabilities'][capability]['revoked']
                history = self.assistant_history(state)
                assert history and '[notes editing link issued]' in history[0]['content']
                assert '/notes#' not in history[0]['content']
                assert [request.method for request in requests] == ['POST', 'GET']
                assert requests[0].headers['Idempotency-Key'] == 'notes-turn'
                await self.asyncTearDown()

    async def test_delivered_submission_immediately_removes_all_ciphertext(self):
        async def handler(request):
            return httpx.Response(202, json={'id': 'sent-1', 'status': 'delivered'})
        worker = self.worker(handler)
        capability = await self.issue()
        await worker.dispatch('notes-turn')
        state = await self.service.snapshot()
        assert state['outbox']['notes-turn']['status'] == 'sent'
        assert 'payload' not in state['outbox']['notes-turn'] and not state['delivery_material']
        assert state['capabilities'][capability]['delivered']

    async def test_timeout_stays_unknown_across_recovery_and_is_not_retried(self):
        methods = []
        async def handler(request):
            methods.append(request.method)
            raise httpx.ReadTimeout('Provider may have accepted the request.')
        worker = self.worker(handler)
        capability = await self.issue()
        await worker.dispatch('notes-turn')
        await worker.recover()
        await worker.dispatch('notes-turn')
        await worker.reconcile('notes-turn')
        state = await self.service.snapshot()
        assert state['outbox']['notes-turn']['status'] == 'unknown'
        assert state['outbox']['notes-turn']['message_id'] is None
        assert not self.assistant_history(state) and methods == ['POST']
        self.now[0] += 3601
        await worker.cleanup()
        state = await self.service.snapshot()
        assert capability not in state['capabilities']
        assert 'payload' not in state['outbox']['notes-turn'] and not state['delivery_material']

    async def test_interrupted_submission_is_unknown_without_automatic_send(self):
        async def handler(request):
            raise AssertionError('A recovered submitting record must never send again.')
        worker = self.worker(handler)
        await self.issue()
        await self.service.store.transaction(lambda state: state['outbox']['notes-turn'].update(status='submitting'))
        await worker.recover()
        await worker.dispatch('notes-turn')
        state = await self.service.snapshot()
        assert state['outbox']['notes-turn']['status'] == 'unknown'

    async def test_navigation_after_ambiguous_acceptance_only_reconciles_read_only(self):
        methods = []
        async def handler(request):
            methods.append(request.method)
            return httpx.Response(202 if request.method == 'POST' else 200,
                                  json={'id': 'sent-1', 'status': 'accepted'})
        worker = self.worker(handler)
        capability = await self.issue()
        await worker.dispatch('notes-turn')
        await self.service.admit(Inbound('switch', '+14155550123', '+16283166196', '/use DEMO',
                                         self.now[0], 'space-1', 'agent-1', 'message-2'))
        await worker.dispatch('notes-turn')
        await worker.reconcile('notes-turn')
        state = await self.service.snapshot()
        assert state['capabilities'][capability]['revoked']
        assert 'payload' not in state['outbox']['notes-turn']
        assert methods == ['POST', 'GET']

    async def test_reconciliation_of_failed_delivery_revokes_capability(self):
        async def handler(request):
            return httpx.Response(202 if request.method == 'POST' else 200,
                json={'id': 'sent-1', 'status': 'dispatched' if request.method == 'POST' else 'failed'})
        worker = self.worker(handler)
        capability = await self.issue()
        await worker.dispatch('notes-turn')
        await worker.reconcile('notes-turn')
        state = await self.service.snapshot()
        assert state['outbox']['notes-turn']['status'] == 'failed'
        assert state['capabilities'][capability]['revoked']
        assert 'payload' not in state['outbox']['notes-turn']

    async def test_released_or_changed_assignment_cancels_pending_submission(self):
        async def handler(request):
            raise AssertionError('A stale assignment must not send.')
        worker = self.worker(handler)
        capability = await self.issue()
        await self.service.sync_assignments([{'id': 'new-assignment', 'phone': '+14155550123',
                                               'destination': '+16283166196', 'created': self.now[0] + 1}])
        await worker.dispatch('notes-turn')
        state = await self.service.snapshot()
        assert state['outbox']['notes-turn']['status'] == 'cancelled'
        assert state['capabilities'][capability]['revoked']

    async def test_same_destination_with_different_assignment_id_cannot_send_old_turn(self):
        async def handler(request):
            raise AssertionError('The originating assignment no longer exists.')
        worker = self.worker(handler)
        await self.issue()
        await self.service.sync_assignments([{'id': 'different-assignment', 'phone': '+14155550123',
                                               'destination': '+16283166196', 'created': self.now[0] - 5}])
        await worker.dispatch('notes-turn')
        assert (await self.service.snapshot())['outbox']['notes-turn']['status'] == 'cancelled'

    async def test_refresh_adds_preassigned_participant_and_failure_preserves_mapping(self):
        async def handler(request):
            assert request.method == 'GET'
            return httpx.Response(200, json={'assignments': [{
                'assignmentId': 'assignment-2', 'handle': '+14155550124', 'assignedPhoneNumber': '+16283166222',
                'createdAt': '2025-12-31T23:59:59Z', 'projectId': 'project', 'platform': 'imessage'}]})
        worker = self.worker(handler)
        await worker.refresh_assignments()
        state = await self.service.snapshot()
        assert list(state['assignments']) == ['+14155550124']
        result = await self.service.admit(Inbound('new-person', '+14155550124', '+16283166222', '/help',
                                                 self.now[0], 'other-space', 'other-agent', 'other-message'))
        assert result['status'] == 'accepted'
        async def unavailable(request):
            raise httpx.ConnectError('Synthetic outage')
        broken = self.worker(unavailable)
        with self.assertRaises(httpx.ConnectError):
            await broken.refresh_assignments()
        assert (await self.service.snapshot())['assignments'] == state['assignments']

    async def test_old_freshly_redelivered_event_cannot_recreate_expired_work(self):
        await self.service.store.transaction(lambda state: state['assignments']['+14155550123'].update(created=0))
        old = self.now[0] - 31 * 86400
        result = await self.service.admit(Inbound('ancient-event', '+14155550123', '+16283166196', '/help',
                                                 old, 'space-1', 'agent-1', 'message-old'))
        assert result['status'] == 'rejected'
        assert 'ancient-event' not in (await self.service.snapshot())['jobs']

    async def test_lifespan_failure_closes_clients_and_health_detects_dead_worker(self):
        calls = []
        async def unavailable():
            raise httpx.ConnectError('Synthetic startup outage')
        async def close():
            calls.append('closed')
        failed = SimpleNamespace(photon=SimpleNamespace(list_assignments=unavailable))
        app = create_app(self.service, failed, 'unused', 'project', run_worker=True)
        app.state.close = close
        with self.assertRaises(httpx.ConnectError):
            async with app.router.lifespan_context(app):
                raise AssertionError('Startup should not have succeeded.')
        assert calls == ['closed']
        async def completed():
            return
        task = asyncio.create_task(completed())
        await task
        app.state.worker_task = task
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
            assert (await client.get('/healthz')).status_code == 503

    async def test_storage_outage_returns_safe_health_error(self):
        class UnavailableStore:
            async def read(self):
                raise DatabaseUnavailable('Do not expose database exception details.')
        service = SimpleNamespace(store=UnavailableStore())
        app = create_app(service, None, 'unused', 'project')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://example.test') as client:
            response = await client.get('/healthz')
        assert response.status_code == 503
        assert 'exception details' not in response.text
        assert response.headers['cache-control'] == 'no-store'
