import copy
from urllib.parse import quote
from unittest.mock import AsyncMock

import httpx
import pytest

from app.dashboard import create_dashboard_app
from app.database import DatabaseUnavailable, MemoryStore


@pytest.fixture
def dashboard():
    now = 1800000000.0
    state = {
        'users': {key: {'id': key, 'phone': key, 'mode': 'builder', 'focus': 'p1'}
                  for key in ('+1001', '+1002', '+1003', '+1004')},
        'projects': {'p1': {'id': 'p1', 'name': 'First', 'code': 'FIRST', 'owner': '+1001'},
                     'p2': {'id': 'p2', 'name': 'Second', 'code': 'SECOND', 'owner': '+1002'},
                     'deleted': {'id': 'deleted', 'name': 'Deleted', 'code': 'DELETED', 'owner': '+1001', 'deleted': True}},
        'conversations': {
            '+1001:builder': [{'role': 'user', 'content': 'Make a project', 'created': now - 9},
                              {'role': 'assistant', 'content': 'Open https://example.test/vault#secret-token',
                               'created': now - 8, 'outbox': 'b1'}],
            '+1002:builder': [{'role': 'user', 'content': 'Private builder', 'created': now - 9}],
            '+1003:project:p1': [{'role': 'user', 'content': 'Question', 'created': now - 6},
                                 {'role': 'assistant', 'content': 'Part one', 'created': now - 5, 'outbox': 'j1', 'part': 0},
                                 {'role': 'assistant', 'content': 'Part two', 'created': now - 5, 'outbox': 'j1', 'part': 1}],
            '+1003:project:p2': [{'role': 'user', 'content': 'Other project', 'created': now - 4}],
            '+1003:project:deleted': [{'role': 'user', 'content': 'Deleted project', 'created': now - 3}],
            '+1004:builder': [{'role': 'user', 'content': 'Expired', 'created': now - 31 * 86400}],
        },
        'outbox': {'j1': {'id': 'j1', 'status': 'unknown', 'delivered_parts': 1,
                          'payload': {'ciphertext': 'do-not-return'}}, 'b1': {'status': 'sent'}},
        'capabilities': {'secret': {'hash': 'do-not-return'}},
    }
    store = MemoryStore(state)
    store.read = AsyncMock(wraps=store.read)
    app = create_dashboard_app(store, clock=lambda: now)
    return app, store, state, now


def client(app, *, peer='127.0.0.1', base_url='http://127.0.0.1:8002'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url=base_url)


def conversation_path(account='+1001', conversation='+1003:project:p1'):
    return f'/api/internal/accounts/{quote(account, safe="")}/conversations/{quote(conversation, safe="")}'


async def test_overview_search_account_and_project_are_read_only(dashboard):
    app, store, state, now = dashboard
    before = copy.deepcopy(state)
    async with client(app) as http:
        overview = (await http.get('/api/internal/overview')).json()
        assert overview == {'users': 4, 'owners': 2, 'projects': 2, 'conversations': 4, 'updated_at': now}
        users = (await http.get('/api/internal/users?q=1001')).json()
        assert users['total'] == 1 and users['items'][0]['project_count'] == 1
        account = (await http.get('/api/internal/accounts/%2B1001')).json()
        assert [p['id'] for p in account['projects']] == ['p1']
        assert account['builder_conversation']['id'] == '+1001:builder'
        project = (await http.get('/api/internal/accounts/%2B1001/projects/p1')).json()
        assert project['project']['participant_count'] == 1
        assert project['participants'][0]['id'] == '+1003'
        empty = (await http.get('/api/internal/accounts/%2B1004')).json()
        assert empty['projects'] == [] and empty['builder_conversation'] is None
    assert store.read.await_count == 1
    assert (await store.read())[1] == before


@pytest.mark.parametrize('path', [
    '/api/internal/accounts/%2B1001/projects/p2',
    '/api/internal/accounts/%2B1001/projects/deleted',
    '/api/internal/accounts/%2B9999',
    conversation_path(conversation='+1002:builder'),
    conversation_path(conversation='+1003:project:p2'),
    conversation_path(conversation='+1003:project:deleted'),
    conversation_path(account='+1003'),
])
async def test_account_scope_cannot_be_changed_by_conversation_or_project_id(dashboard, path):
    async with client(dashboard[0]) as http:
        assert (await http.get(path)).status_code == 404


async def test_transcript_redacts_links_and_preserves_multipart_delivery(dashboard):
    async with client(dashboard[0]) as http:
        result = (await http.get(conversation_path())).json()
        assert [m['delivery_status'] for m in result['messages']] == [None, 'sent', 'unknown']
        assert result['messages'][1]['job_id'] == 'j1'
        builder = await http.get(conversation_path(conversation='+1001:builder'))
        assert builder.json()['messages'][-1]['content'] == 'Open [vault link omitted]'
        assert 'secret-token' not in builder.text and 'ciphertext' not in builder.text
        assert builder.headers['cache-control'] == 'no-store'
        assert builder.headers['x-frame-options'] == 'DENY'


async def test_pagination_returns_latest_retained_messages_then_earlier_pages(dashboard):
    _, _, state, now = dashboard
    state['conversations']['+1001:builder'] = [
        {'role': 'user', 'content': str(i), 'created': now - 250 + i} for i in range(250)]
    app = create_dashboard_app(MemoryStore(state), clock=lambda: now)
    path = conversation_path(conversation='+1001:builder')
    async with client(app) as http:
        recent = (await http.get(path)).json()
        older = (await http.get(path + '?offset=50&limit=50')).json()
        assert recent['total'] == 200
        assert [m['content'] for m in recent['messages']] == [str(i) for i in range(200, 250)]
        assert [m['content'] for m in older['messages']] == [str(i) for i in range(150, 200)]
        assert (await http.get(path + '?offset=300')).json()['messages'] == []
        assert (await http.get(path + '?limit=101')).status_code == 422
        assert (await http.get('/api/internal/users?offset=-1')).status_code == 422


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
async def test_all_mutations_are_rejected(dashboard, method):
    async with client(dashboard[0]) as http:
        response = await http.request(method, '/api/internal/overview')
        assert response.status_code == 405 and response.headers['allow'] == 'GET'


@pytest.mark.parametrize('peer,base_url,headers', [
    ('192.168.0.2', 'http://127.0.0.1:8002', {}),
    ('127.0.0.1', 'http://example.test:8002', {}),
    ('127.0.0.1', 'http://127.0.0.1:8002', {'Origin': 'https://evil.test'}),
    ('127.0.0.1', 'http://127.0.0.1:8002', {'X-Forwarded-For': '127.0.0.1'}),
    ('127.0.0.1', 'http://127.0.0.1:8002', {'Forwarded': 'for=127.0.0.1'}),
])
async def test_nonlocal_and_forwarded_requests_are_rejected(dashboard, peer, base_url, headers):
    async with client(dashboard[0], peer=peer, base_url=base_url) as http:
        response = await http.get('/api/internal/overview', headers=headers)
        assert response.status_code == 403 and response.headers['cache-control'] == 'no-store'
    dashboard[1].read.assert_not_awaited()


async def test_local_frontend_origins_and_empty_database(dashboard):
    app = create_dashboard_app(MemoryStore())
    async with client(app, base_url='http://localhost:8002') as http:
        response = await http.get('/api/internal/overview', headers={'Origin': 'http://localhost:5173'})
        assert response.status_code == 200 and response.json()['users'] == 0
        assert (await http.get('/api/internal/users')).json()['items'] == []


async def test_database_failure_is_sanitized_and_not_cached():
    store = MemoryStore()
    store.read = AsyncMock(side_effect=[DatabaseUnavailable('secret provider details'), (0, {})])
    async with client(create_dashboard_app(store)) as http:
        failed = await http.get('/api/internal/overview')
        assert failed.status_code == 503 and 'secret' not in failed.text
        assert (await http.get('/api/internal/overview')).status_code == 200


async def test_snapshot_cache_refreshes_after_ttl():
    now = [1800000000.0]
    store = MemoryStore()
    store.read = AsyncMock(wraps=store.read)
    async with client(create_dashboard_app(store, clock=lambda: now[0])) as http:
        await http.get('/api/internal/overview')
        now[0] += 3
        await http.get('/api/internal/overview')
    assert store.read.await_count == 2
