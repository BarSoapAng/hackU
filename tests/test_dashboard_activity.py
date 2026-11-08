import copy
import json
from urllib.parse import quote

import httpx
import pytest

from app.dashboard import create_dashboard_app
from app.database import MemoryStore


@pytest.fixture
def state():
    now = 1800000000.0
    return {
        'users': {key: {'id': key, 'phone': key} for key in ('owner', 'other', 'participant')},
        'projects': {'p1': {'id': 'p1', 'owner': 'owner', 'name': 'First', 'code': 'FIRST'},
                     'p2': {'id': 'p2', 'owner': 'other', 'name': 'Second', 'code': 'SECOND'}},
        'conversations': {}, 'jobs': {}, 'outbox': {},
    }, now


def turn(now, **changes):
    return {'id': 'j1', 'kind': 'turn', 'user': 'participant', 'mode': 'customer', 'selected': 'p1',
            'status': 'running', 'created': now - 10, 'deadline': now + 30, 'question': 'A first question',
            'steps': [], 'attempts': 1, 'generations': 1, **changes}


def request(now, **changes):
    return {'id': 'r1', 'provider': 'openai', 'operation': 'answer', 'model': 'project-model',
            'started': now - 8, 'finished': now - 7, 'duration_ms': 1000, 'status': 'received',
            'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 50, 'total_tokens': 120,
            'error_type': None, **changes}


def client(state, now):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_dashboard_app(MemoryStore(state), clock=lambda: now)),
                             base_url='http://127.0.0.1:8002')


def path(account='owner', conversation='participant:project:p1'):
    return f'/api/internal/accounts/{quote(account, safe="")}/conversations/{quote(conversation, safe="")}'


async def test_first_pending_turn_is_visible_before_reply_and_scoped(state):
    data, now = state
    data['jobs'] = {'j1': turn(now), 'j2': turn(now, id='j2', user='other', mode='builder', selected=None)}
    before = copy.deepcopy(data)
    async with client(data, now) as http:
        overview = (await http.get('/api/internal/overview')).json()
        assert overview['conversations'] == 2
        project = (await http.get('/api/internal/accounts/owner/projects/p1')).json()
        assert project['participants'][0]['message_count'] == 1
        transcript = (await http.get(path())).json()
        assert transcript['messages'] == [{'role': 'user', 'content': 'A first question',
            'created': now - 10, 'job_id': 'j1', 'delivery_status': None}]
        activity = (await http.get(path() + '/activity')).json()
        assert activity['items'][0]['id'] == 'j1'
        assert activity['items'][0]['usage_recorded'] is False
        assert activity['items'][0]['requests'] == []
        assert (await http.get(path(account='other') + '/activity')).status_code == 404
        assert (await http.get(path(conversation='other:builder') + '/activity')).status_code == 404
        own_builder = (await http.get(path(account='other', conversation='other:builder') + '/activity')).json()
        assert own_builder['items'][0]['id'] == 'j2'
    assert data == before


async def test_completed_transcript_is_not_duplicated_and_links_messages_to_turn(state):
    data, now = state
    data['jobs']['j1'] = turn(now)
    data['conversations']['participant:project:p1'] = [
        {'role': 'user', 'content': 'A first question', 'created': now - 5},
        {'role': 'assistant', 'content': 'An answer', 'created': now - 5, 'outbox': 'j1'},
    ]
    async with client(data, now) as http:
        result = (await http.get(path())).json()
    assert result['total'] == 2
    assert [message['job_id'] for message in result['messages']] == ['j1', 'j1']


async def test_pending_questions_follow_completed_transcript_in_sequence_order(state):
    data, now = state
    data['conversations']['participant:project:p1'] = [
        {'role': 'user', 'content': 'First question', 'created': now - 1},
        {'role': 'assistant', 'content': 'First answer', 'created': now - 1, 'outbox': 'finished'},
    ]
    data['jobs'] = {
        'j2': turn(now, id='j2', sequence=3, question='Third question', created=now - 3),
        'j1': turn(now, id='j1', sequence=2, question='Second question', created=now - 4),
    }
    async with client(data, now) as http:
        result = (await http.get(path())).json()
    assert [message['content'] for message in result['messages']] == [
        'First question', 'First answer', 'Second question', 'Third question']


async def test_expired_completed_history_is_not_reconstructed_from_jobs(state):
    data, now = state
    data['jobs']['j1'] = turn(now, status='completed')
    data['conversations']['participant:project:p1'] = [
        {'role': 'user', 'content': 'Expired', 'created': now - 31 * 86400}]
    async with client(data, now) as http:
        assert (await http.get('/api/internal/overview')).json()['conversations'] == 0
        assert (await http.get(path())).status_code == 404
        assert (await http.get(path() + '/activity')).status_code == 404


async def test_tools_are_allowlisted_and_capabilities_reduced_to_readiness(state):
    data, now = state
    data['jobs']['j1'] = turn(now, steps=[
        {'id': 's1', 'name': 'issue_vault_link', 'created': now - 9,
         'arguments': {'api_key': 'hidden-key'}, 'result': {'capability': 'hidden-capability',
             'hash': 'hidden-hash', 'ciphertext': {'nested': 'hidden-ciphertext'}, 'expires': now + 10}},
        {'id': 's2', 'name': 'update_instructions', 'created': now - 8,
         'arguments': {'instructions': 'Read https://example.test/notes#hidden-url', 'credential': {'token': 'hidden-token'}},
         'result': {'updated': True, 'capability': 'hidden-capability'}},
        {'id': 's3', 'name': 'list_projects', 'created': now - 7, 'arguments': {},
         'result': {'projects': [{'id': 'p1', 'name': 'First', 'code': 'FIRST',
                                 'credentials': {'bearer': 'hidden-bearer'}}], 'token': 'hidden-token'}},
        {'id': 's4', 'name': 'unknown_tool', 'created': now - 6,
         'arguments': {'nested': {'secret': 'hidden-secret'}}, 'result': {'nested': {'hash': 'hidden-hash'}}},
    ])
    async with client(data, now) as http:
        response = await http.get(path() + '/activity')
        tools = response.json()['items'][0]['tools']
    assert 'hidden-' not in response.text
    assert tools[0]['result'] == {'notes_link_ready': True}
    assert tools[0]['arguments'] == {}
    assert tools[1]['arguments'] == {'instructions': 'Read [editing link omitted]'}
    assert tools[2]['result'] == {'projects': [{'id': 'p1', 'name': 'First', 'code': 'FIRST'}]}
    assert tools[3]['arguments'] == {} and tools[3]['result'] == {}


async def test_invalid_tool_calls_and_failures_are_included_from_trace(state):
    data, now = state
    data['jobs']['j1'] = turn(now, trace=[
        {'role': 'assistant', 'content': 'private raw intermediate prompt', 'tool_calls': [
            {'id': 'call1', 'function': {'name': 'select_project', 'arguments': json.dumps({'code': 'MISSING', 'token': 'secret'})}}]},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': json.dumps({'error': 'Unknown project code.', 'hash': 'secret'})},
    ])
    async with client(data, now) as http:
        response = await http.get(path() + '/activity')
        tools = response.json()['items'][0]['tools']
    assert len(tools) == 1 and tools[0]['status'] == 'failed'
    assert tools[0]['arguments'] == {'code': 'MISSING'}
    assert tools[0]['result'] == {'error': 'Unknown project code.'}
    assert 'secret' not in response.text and 'private raw intermediate prompt' not in response.text


async def test_trace_and_step_for_same_call_are_one_tool(state):
    data, now = state
    data['jobs']['j1'] = turn(now, steps=[{'id': 's1', 'name': 'select_project', 'created': now - 7,
        'arguments': {'code': 'MISSING'}, 'call_key': '1:0'}], trace=[
        {'role': 'assistant', 'tool_calls': [{'id': 'call1', 'function': {'name': 'select_project', 'arguments': '{"code":"MISSING"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': '{"error":"Unknown project code."}'},
    ])
    async with client(data, now) as http:
        tools = (await http.get(path() + '/activity')).json()['items'][0]['tools']
    assert len(tools) == 1 and tools[0]['id'] == 's1' and tools[0]['status'] == 'failed'


async def test_invalid_and_executed_tools_keep_trace_order(state):
    data, now = state
    data['jobs']['j1'] = turn(now, steps=[{'id': 's1', 'name': 'list_projects', 'created': now - 7,
        'arguments': {}, 'result': {'projects': []}, 'call_key': '1:1'}], trace=[
        {'role': 'assistant', 'tool_calls': [
            {'id': 'invalid', 'function': {'name': 'select_project', 'arguments': '{}'}},
            {'id': 'valid', 'function': {'name': 'list_projects', 'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'invalid', 'content': '{"error":"Unsupported arguments."}'},
        {'role': 'tool', 'tool_call_id': 'valid', 'content': '{"projects":[]}'},
    ])
    async with client(data, now) as http:
        tools = (await http.get(path() + '/activity')).json()['items'][0]['tools']
    assert [tool['name'] for tool in tools] == ['select_project', 'list_projects']
    assert [tool['status'] for tool in tools] == ['failed', 'completed']


async def test_trace_details_expire_even_before_cleanup_but_usage_remains(state):
    data, now = state
    data['conversations']['participant:project:p1'] = [{'role': 'user', 'content': 'History', 'created': now - 9 * 86400}]
    data['jobs']['j1'] = turn(now, created=now - 9 * 86400, status='completed', requests=[request(now)], steps=[
        {'id': 's1', 'name': 'create_project', 'created': now - 9 * 86400,
         'arguments': {'name': 'Old name'}, 'result': {'id': 'p1', 'name': 'Old name', 'code': 'FIRST'}}])
    async with client(data, now) as http:
        item = (await http.get(path() + '/activity')).json()['items'][0]
    assert item['trace_available'] is False
    assert item['tools'][0]['arguments'] is None and item['tools'][0]['result'] is None
    assert item['tools'][0]['status'] == 'completed'
    assert item['requests'][0]['input_tokens'] == 100 and item['usage_recorded'] is True


@pytest.mark.parametrize('status,deadline,expected', [
    ('running', 1800000030.0, 'pending'), ('running', 1799999999.0, 'unknown'),
    ('completed', 1800000030.0, 'unknown'), ('failed', 1800000030.0, 'unknown'),
])
async def test_pending_usage_is_unknown_after_interruption_or_deadline(state, status, deadline, expected):
    data, now = state
    data['conversations']['participant:project:p1'] = [{'role': 'user', 'content': 'History', 'created': now - 10}]
    data['jobs']['j1'] = turn(now, status=status, deadline=deadline, requests=[request(now,
        status='pending', input_tokens=None, output_tokens=None, cached_input_tokens=None, total_tokens=None,
        finished=None, duration_ms=None, ciphertext={'token': 'hidden'})])
    async with client(data, now) as http:
        response = await http.get(path() + '/activity')
        record = response.json()['items'][0]['requests'][0]
    assert record['status'] == expected and record['input_tokens'] is None and record['duration_ms'] is None
    assert 'hidden' not in response.text and 'ciphertext' not in response.text


async def test_activity_is_paginated_newest_first_without_other_threads(state):
    data, now = state
    data['jobs'] = {f'j{i}': turn(now, id=f'j{i}', created=now - i) for i in range(30)}
    data['jobs']['other'] = turn(now, id='other', user='other', mode='builder', created=now)
    data['jobs']['other-project'] = turn(now, id='other-project', selected='p2', created=now)
    data['jobs']['index'] = {'id': 'index', 'kind': 'index', 'project': 'p1', 'created': now, 'status': 'running'}
    async with client(data, now) as http:
        first = (await http.get(path() + '/activity')).json()
        second = (await http.get(path() + '/activity?offset=25')).json()
        invalid = await http.get(path() + '/activity?limit=101')
    assert first['total'] == 30 and len(first['items']) == 25
    assert [item['id'] for item in first['items']] == [f'j{i}' for i in range(25)]
    assert [item['id'] for item in second['items']] == [f'j{i}' for i in range(25, 30)]
    assert invalid.status_code == 422


async def test_expired_jobs_and_requests_are_excluded(state):
    data, now = state
    data['jobs'] = {'j1': turn(now, requests=[request(now, started=now - 31 * 86400)]),
                    'expired': turn(now, id='expired', created=now - 31 * 86400)}
    async with client(data, now) as http:
        result = (await http.get(path() + '/activity')).json()
    assert result['total'] == 1
    assert result['items'][0]['requests'] == [] and result['items'][0]['usage_recorded'] is False


async def test_request_sequence_and_safe_provider_status_are_projected(state):
    data, now = state
    data['jobs']['j1'] = turn(now, requests=[request(now, sequence=2, provider_status='incomplete', http_status=429)])
    async with client(data, now) as http:
        record = (await http.get(path() + '/activity')).json()['items'][0]['requests'][0]
    assert record['sequence'] == 2 and record['provider_status'] == 'incomplete' and record['http_status'] == 429


async def test_turn_filter_selects_older_turn_before_paging_and_never_crosses_scope(state):
    data, now = state
    data['jobs'] = {f'j{i}': turn(now, id=f'j{i}', created=now - i) for i in range(30)}
    data['jobs']['foreign'] = turn(now, id='foreign', user='other', mode='builder')
    async with client(data, now) as http:
        selected = (await http.get(path() + '/activity?turn_id=j29')).json()
        assert selected['total'] == 1 and [item['id'] for item in selected['items']] == ['j29']
        for turn_id in ('foreign', 'missing'):
            hidden = (await http.get(path() + '/activity', params={'turn_id': turn_id})).json()
            assert hidden['total'] == 0 and hidden['items'] == []
        assert (await http.get(path() + '/activity', params={'turn_id': 'x' * 257})).status_code == 422


@pytest.mark.parametrize('invalid', [-1, float('nan'), float('inf'), True, '5', 1.5, 2**54])
async def test_invalid_usage_numbers_become_unknown_without_serialization_errors(state, invalid):
    data, now = state
    data['jobs']['j1'] = turn(now, requests=[request(now, input_tokens=invalid, output_tokens=invalid,
        cached_input_tokens=invalid, total_tokens=invalid, sequence=invalid, started=float('inf'),
        finished=float('nan'), duration_ms=-1, http_status=999, provider_status={'token': 'hidden'})])
    async with client(data, now) as http:
        response = await http.get(path() + '/activity')
    assert response.status_code == 200
    record = response.json()['items'][0]['requests'][0]
    assert all(record[key] is None for key in ('input_tokens', 'output_tokens', 'cached_input_tokens',
        'total_tokens', 'sequence', 'started', 'finished', 'duration_ms', 'http_status', 'provider_status'))
    assert 'hidden' not in response.text
