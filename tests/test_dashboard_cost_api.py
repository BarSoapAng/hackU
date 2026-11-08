import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.dashboard import create_dashboard_app
from app.dashboard_costs import CostConfigurationError
from app.database import MemoryStore
from test_dashboard import client, conversation_path, dashboard


def configured(dashboard, config=None):
    _, _, original, now = dashboard
    state = copy.deepcopy(original)
    state['jobs'] = {'j1': {
        'id': 'j1', 'kind': 'turn', 'user': '+1003', 'mode': 'customer', 'selected': 'p1',
        'created': now - 10, 'status': 'completed', 'attempts': 1, 'generations': 1,
        'requests': [{'id': 'r1', 'provider': 'openai', 'operation': 'answer', 'model': 'test-model',
                      'account_id': '+1001', 'project_id': 'p1', 'participant_id': '+1003',
                      'started': now - 8, 'finished': now - 7, 'duration_ms': 1000, 'status': 'received',
                      'input_tokens': 1_000_000, 'output_tokens': 0, 'cached_input_tokens': 0}],
    }}
    store = MemoryStore(state)
    return create_dashboard_app(store, clock=lambda: now, costs_config=config), store, state, now


def rates(price=1):
    return {'rates': [{'model': 'test-model', 'effective_at': '2020-01-01T00:00:00Z',
                       'input_per_million': price, 'output_per_million': 2, 'cached_input_per_million': .5}]}


async def test_default_cost_dates_are_inclusive_utc_and_read_only(dashboard):
    app, store, state, now = configured(dashboard, rates())
    today = datetime.fromtimestamp(now, timezone.utc).date()
    async with client(app) as http:
        response = await http.get('/api/internal/costs', params={'provider': 'openai'})
    assert response.status_code == 200
    body = response.json()
    assert body['period'] == {
        'start': datetime.combine(today - timedelta(days=6), datetime.min.time(), timezone.utc).timestamp(),
        'end': datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp(),
    }
    assert body['known_estimated_usd'] == 1
    assert body['billed_usd'] is None
    assert body['coverage']['partial']  # Earlier days predate the observed request ledger.
    assert (await store.read())[1] == state


@pytest.mark.parametrize('params,status', [
    ({'account_id': '+1001', 'project_id': 'p2'}, 404),
    ({'account_id': '+1001', 'project_id': 'deleted'}, 404),
    ({'account_id': 'unknown'}, 404),
    ({'project_id': 'p1'}, 422),
    ({'provider': 'unknown'}, 422),
    ({'start': 'invalid'}, 422),
    ({'start': '2026-02-02', 'end': '2026-02-01'}, 422),
    ({'start': '2024-01-01', 'end': '2026-01-01'}, 422),
    ({'start': '2099-01-01', 'end': '2099-01-01'}, 422),
])
async def test_cost_api_rejects_invalid_scope_period_and_provider(dashboard, params, status):
    app, _, _, _ = configured(dashboard, rates())
    async with client(app) as http:
        assert (await http.get('/api/internal/costs', params=params)).status_code == status


async def test_cost_api_filters_scope_and_redacts_labels(dashboard):
    _, _, state, now = configured(dashboard, rates())
    state['projects']['p1']['name'] = 'Project https://example.test/vault#never-expose'
    app = create_dashboard_app(MemoryStore(state), clock=lambda: now, costs_config=rates())
    async with client(app) as http:
        result = (await http.get('/api/internal/costs', params={
            'account_id': '+1001', 'project_id': 'p1', 'provider': 'openai',
        })).json()
        other = (await http.get('/api/internal/costs', params={'account_id': '+1002', 'provider': 'openai'})).json()
    assert result['known_estimated_usd'] == 1
    assert result['projects'][0]['label'] == 'Project [vault link omitted]'
    assert [row['id'] for row in result['accounts']] == ['+1001']
    assert other['known_estimated_usd'] == 0
    assert other['coverage']['request_count'] == 0
    assert not other['projects']


async def test_config_is_reloaded_and_activity_prices_the_same_request(dashboard):
    loader = Mock(side_effect=[rates(1), rates(2), rates(3)])
    app, _, _, _ = configured(dashboard, loader)
    async with client(app) as http:
        first = (await http.get('/api/internal/costs', params={'provider': 'openai'})).json()
        second = (await http.get('/api/internal/costs', params={'provider': 'openai'})).json()
        activity = (await http.get(conversation_path() + '/activity')).json()
    assert first['known_estimated_usd'] == 1
    assert second['known_estimated_usd'] == 2
    assert activity['items'][0]['requests'][0]['cost'] == {'estimated_usd': 3, 'basis': 'token_estimate'}
    assert loader.call_count == 3


@pytest.mark.parametrize('config', [
    {'currency': 'SECRET'},
    Mock(side_effect=CostConfigurationError('private path / SECRET')),
])
async def test_bad_cost_config_does_not_disable_transcripts_or_activity(dashboard, config):
    app, _, _, _ = configured(dashboard, config)
    async with client(app) as http:
        cost = await http.get('/api/internal/costs')
        assert cost.status_code == 503
        assert 'SECRET' not in cost.text
        assert (await http.get(conversation_path())).status_code == 200
        activity = await http.get(conversation_path() + '/activity')
    assert activity.status_code == 200
    assert activity.json()['items'][0]['requests'][0]['cost'] == {'estimated_usd': None, 'basis': 'configuration_error'}


async def test_missing_cost_configuration_stays_unknown(dashboard):
    app, _, _, _ = configured(dashboard)
    async with client(app) as http:
        report = (await http.get('/api/internal/costs')).json()
        activity = (await http.get(conversation_path() + '/activity')).json()
    assert report['estimated_usd'] is None
    assert report['coverage']['unpriced_requests'] == 1
    assert activity['items'][0]['requests'][0]['cost'] == {'estimated_usd': None, 'basis': 'not_configured'}
