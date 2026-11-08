import copy
import json
from datetime import datetime, timezone

import pytest

from app.dashboard_costs import CostConfigurationError, CostDataError, cost_report, estimate_request, load_config


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def rate(start=0, **changes):
    return {'model': 'model', 'effective_at': iso(start), 'input_per_million': 2,
            'output_per_million': 8, 'cached_input_per_million': 0.5, **changes}


def request(start=1800000000.0, **changes):
    return {'id': 'r1', 'provider': 'openai', 'operation': 'answer', 'model': 'model',
            'started': start, 'finished': start + 10, 'duration_ms': 10000, 'status': 'received',
            'account_id': 'owner', 'project_id': 'p1', 'input_tokens': 1000000,
            'output_tokens': 100000, 'cached_input_tokens': 200000, **changes}


def state(*requests):
    return {'users': {'owner': {'phone': '+1001'}, 'other': {'phone': '+1002'}},
            'projects': {'p1': {'owner': 'owner', 'name': 'First'}, 'p2': {'owner': 'other', 'name': 'Second'}},
            'jobs': {'j1': {'id': 'j1', 'kind': 'turn', 'mode': 'customer', 'user': 'participant',
                            'selected': 'p1', 'created': 1800000000.0, 'generations': len(requests), 'requests': list(requests)}}}


def infrastructure(provider, start=1800000000.0, end=1800000100.0, **changes):
    return {'provider': provider, 'start': iso(start), 'end': iso(end), 'amount_usd': 100, **changes}


def report(data, config, **changes):
    return cost_report(data, config, 1800000000.0, 1800000100.0, now=1800000100.0, **changes)


def test_dated_rates_and_cached_subset_are_not_double_counted():
    config = {'rates': [rate(), rate(1800000050.0, input_per_million=4)]}
    before = estimate_request(request(), config)
    after = estimate_request(request(1800000060.0), config)
    assert before == {'estimated_usd': pytest.approx(2.5), 'basis': 'token_estimate'}
    assert after['estimated_usd'] == pytest.approx(4.1)
    assert estimate_request(request(1800000049.0), {'rates': [rate(1800000050.0)]})['basis'] == 'not_configured'


def test_missing_cache_usage_or_rate_is_unknown_and_equal_rates_do_not_need_cache_count():
    assert estimate_request(request(cached_input_tokens=None), {'rates': [rate()]})['basis'] == 'usage_unknown'
    assert estimate_request(request(), {'rates': [rate(cached_input_per_million=None)]})['basis'] == 'not_configured'
    equal = estimate_request(request(cached_input_tokens=None), {'rates': [rate(cached_input_per_million=2)]})
    assert equal['estimated_usd'] == pytest.approx(2.8)
    all_cached = estimate_request(request(cached_input_tokens=1000000), {'rates': [rate(input_per_million=None)]})
    assert all_cached['estimated_usd'] == pytest.approx(1.3)


def test_embeddings_price_known_input_without_assuming_cache_count():
    result = estimate_request(request(operation='document_embedding', output_tokens=0, cached_input_tokens=None),
                              {'rates': [rate(output_per_million=None, cached_input_per_million=None)]})
    assert result == {'estimated_usd': 2, 'basis': 'token_estimate'}
    assert estimate_request(request(provider='nvidia'), {'rates': [rate()]}) == {'estimated_usd': None, 'basis': 'shared_gpu'}


def test_unknown_and_invalid_usage_never_becomes_zero():
    for value in (None, -1, float('nan'), float('inf'), True, '100', 1.5):
        estimate = estimate_request(request(input_tokens=value), {'rates': [rate()]})
        assert estimate['estimated_usd'] is None and estimate['basis'] == 'usage_unknown'
    assert estimate_request(request(cached_input_tokens=1000001), {'rates': [rate()]})['basis'] == 'usage_unknown'
    unknown = report(state(request(input_tokens=None)), {'rates': [rate()]}, provider='openai')
    assert unknown['estimated_usd'] is None and unknown['coverage']['unpriced_requests'] == 1


def test_empty_configuration_returns_unknown_totals_with_zero_known_subtotal():
    result = report(state(), {})
    assert result['estimated_usd'] is None and result['known_estimated_usd'] == 0
    assert all(row['estimated_usd'] is None for row in result['providers'])
    assert all(row['configuration_missing'] for row in result['providers'])
    assert result['coverage']['partial'] and result['coverage']['earliest_request_at'] is None


def test_retry_attempts_are_charged_separately_and_request_ids_deduplicate_globally():
    first, retry = request(status='failed'), request(1800000001.0, id='r2', status='received')
    data = state(first, retry)
    data['jobs']['duplicate'] = {'id': 'duplicate', 'requests': [copy.deepcopy(first)]}
    before = copy.deepcopy(data)
    result = report(data, {'rates': [rate()]}, provider='openai')
    assert result['estimated_usd'] == pytest.approx(5)
    assert result['coverage']['request_count'] == 2
    assert result['coverage']['unmetered_jobs'] == 0
    assert data == before
    data['jobs']['duplicate']['requests'][0]['output_tokens'] = 1
    with pytest.raises(CostDataError, match='inconsistent'):
        report(data, {'rates': [rate()]})


def test_period_uses_request_start_and_excludes_end_boundary():
    data = state(request(1799999999.0, id='before'), request(), request(1800000100.0, id='after'))
    # Each request is fully attributed to its start, rather than prorating tokens.
    result = report(data, {'rates': [rate()]}, provider='openai')
    assert result['known_estimated_usd'] == pytest.approx(2.5)
    assert result['coverage']['request_count'] == 1


def test_usage_predating_metering_or_retention_is_marked_partial():
    result = report(state(request(1800000010.0)), {'rates': [rate()]}, provider='openai')
    assert result['estimated_usd'] is None and result['coverage']['history_partial'] is True
    result = cost_report(state(request()), {'rates': [rate()]}, 1800000000.0, 1800000100.0,
                         now=1800000000.0 + 31 * 86400, provider='openai')
    assert result['coverage']['history_partial'] and result['estimated_usd'] is None


def test_missing_old_generations_and_index_calls_are_unmetered():
    data = state(request())
    data['jobs']['j1']['generations'] = 2
    data['jobs']['index'] = {'kind': 'index', 'project': 'p1', 'created': 1800000001.0, 'attempts': 1}
    result = report(data, {'rates': [rate()]}, provider='openai')
    assert result['estimated_usd'] is None and result['known_estimated_usd'] == pytest.approx(2.5)
    assert result['coverage']['unmetered_jobs'] == 2


def test_gpu_idle_and_concurrent_allocations_conserve_cost():
    idle = report(state(request(provider='nvidia', duration_ms=20000, finished=1800000020.0)),
                  {'infrastructure': [infrastructure('nvidia')]}, provider='nvidia')
    assert idle['estimated_usd'] == 100
    assert idle['accounts'][0]['known_estimated_usd'] == pytest.approx(20)
    assert idle['shared_overhead_usd'] == pytest.approx(80)
    assert sum(row['known_estimated_usd'] for row in idle['accounts']) + idle['shared_overhead_usd'] == pytest.approx(100)
    data = state(request(provider='nvidia', finished=1800000100.0, duration_ms=100000),
                 request(id='r2', provider='nvidia', account_id='other', project_id='p2', finished=1800000100.0, duration_ms=100000))
    concurrent = report(data, {'infrastructure': [infrastructure('nvidia')]}, provider='nvidia')
    assert concurrent['shared_overhead_usd'] == pytest.approx(0)
    assert [row['known_estimated_usd'] for row in concurrent['accounts']] == pytest.approx([50, 50])


def test_gpu_concurrent_requests_preserve_union_idle_time_and_scoped_totals():
    data = state(request(provider='nvidia', finished=1800000020.0, duration_ms=20000),
                 request(id='r2', provider='nvidia', account_id='other', project_id='p2',
                         finished=1800000020.0, duration_ms=20000))
    config = {'infrastructure': [infrastructure('nvidia')]}
    result = report(data, config, provider='nvidia')
    assert result['estimated_usd'] == 100
    assert result['shared_overhead_usd'] == pytest.approx(80)
    assert [row['known_estimated_usd'] for row in result['accounts']] == pytest.approx([10, 10])
    scoped = [report(data, config, provider='nvidia', account_id=owner)['known_estimated_usd'] for owner in ('owner', 'other')]
    assert sum(scoped) + result['shared_overhead_usd'] == pytest.approx(result['known_estimated_usd'])
    assert result['allocation']['gpu_method'] == 'concurrent_request_time_with_idle_shared'


def test_gpu_partial_overlap_splits_only_simultaneous_segments():
    data = state(request(provider='nvidia', finished=1800000040.0, duration_ms=40000),
                 request(1800000020.0, id='r2', provider='nvidia', account_id='other', project_id='p2',
                         finished=1800000060.0, duration_ms=40000))
    result = report(data, {'infrastructure': [infrastructure('nvidia')]}, provider='nvidia')
    assert [row['known_estimated_usd'] for row in result['accounts']] == pytest.approx([30, 30])
    assert result['shared_overhead_usd'] == pytest.approx(40)
    assert sum(row['known_estimated_usd'] for row in result['projects']) + result['shared_overhead_usd'] == pytest.approx(100)


def test_gpu_duration_excludes_reservation_time_and_is_bounded_by_wall_interval():
    data = state(request(provider='nvidia', finished=1800000040.0, duration_ms=20000),
                 request(1800000020.0, id='r2', provider='nvidia', account_id='other', project_id='p2',
                         finished=1800000040.0, duration_ms=30000))
    result = report(data, {'infrastructure': [infrastructure('nvidia')]}, provider='nvidia')
    # Both measured windows cover seconds20–40; the first20seconds were reservation time.
    assert [row['known_estimated_usd'] for row in result['accounts']] == pytest.approx([10, 10])
    assert result['shared_overhead_usd'] == pytest.approx(80)


def test_gpu_interval_clipping_hourly_input_and_missing_duration():
    interval = infrastructure('nvidia', start=1799999950.0, end=1800000150.0)
    del interval['amount_usd']
    interval['hourly_rate_usd'] = 3600
    data = state(request(1799999990.0, provider='nvidia', finished=1800000010.0, duration_ms=20000))
    clipped = report(data, {'infrastructure': [interval]}, provider='nvidia')
    assert clipped['estimated_usd'] == 100 and clipped['accounts'][0]['known_estimated_usd'] == pytest.approx(10)
    data['jobs']['j1']['requests'][0]['duration_ms'] = None
    unknown = report(data, {'infrastructure': [interval]}, provider='nvidia', account_id='owner')
    assert unknown['estimated_usd'] is None


def test_missing_infrastructure_intervals_leave_partial_known_subtotal():
    result = report(state(request(provider='nvidia')), {'infrastructure': [infrastructure('nvidia', end=1800000050.0)]}, provider='nvidia')
    assert result['estimated_usd'] is None and result['known_estimated_usd'] == 100
    assert result['providers'][0]['configuration_missing'] is True


def test_database_current_byte_allocation_keeps_system_data_shared():
    data = state()
    data['files'] = {'f1': {'project': 'p1', 'content': 'a' * 1000}, 'f2': {'project': 'p2', 'content': 'b' * 100}}
    data['chunks'] = {'f1': [{'embedding': [0.1] * 100}]}
    data['conversations'] = {'participant:project:p1': [{'content': 'hello'}], 'owner:builder': [{'content': 'system shared'}]}
    config = {'infrastructure': [infrastructure('spacetimedb')]}
    result = report(data, config, provider='spacetimedb')
    assert result['estimated_usd'] == 100
    assert result['allocation']['project_bytes']['p1'] > result['allocation']['project_bytes']['p2']
    assert sum(result['allocation']['project_bytes'].values()) < result['allocation']['snapshot_bytes']
    assert sum(row['known_estimated_usd'] for row in result['projects']) + result['shared_overhead_usd'] == pytest.approx(100)
    scoped = report(data, config, provider='spacetimedb', account_id='owner', project_id='p1')
    assert scoped['shared_overhead_usd'] == 0
    assert [row['id'] for row in scoped['projects']] == ['p1']
    assert list(scoped['allocation']['project_bytes']) == ['p1']
    assert scoped['allocation']['snapshot_bytes'] == result['allocation']['snapshot_bytes']
    assert scoped['known_estimated_usd'] < result['known_estimated_usd']


def test_scopes_use_recorded_account_and_project_and_retained_deleted_labels():
    data = state(request(), request(id='r2', account_id='other', project_id='p2'))
    data['projects']['p2']['deleted'] = True
    config = {'rates': [rate()]}
    all_usage = report(data, config, provider='openai')
    selected = report(data, config, provider='openai', account_id='owner', project_id='p1')
    assert all_usage['known_estimated_usd'] == pytest.approx(5)
    assert selected['known_estimated_usd'] == pytest.approx(2.5)
    assert selected['accounts'][0]['label'] == '+1001' and selected['projects'][0]['label'] == 'First'
    assert next(row for row in all_usage['projects'] if row['id'] == 'p2')['deleted'] is True
    assert all(row['id'] != 'p2' for row in selected['projects'])


def test_account_level_usage_reconciles_without_fabricating_a_project():
    data = state(request(project_id=None))
    result = report(data, {'rates': [rate()]}, provider='openai')
    assert result['projects'] == [] and result['accounts'][0]['known_estimated_usd'] == pytest.approx(2.5)
    assert result['unassigned_project_usd'] == pytest.approx(2.5)
    assert sum(row['known_estimated_usd'] for row in result['projects']) + result['unassigned_project_usd'] + result['shared_overhead_usd'] == pytest.approx(result['known_estimated_usd'])


def test_bills_are_separate_and_require_exact_period_and_deployment_scope():
    bill = infrastructure('openai', amount_usd=20)
    config = {'rates': [rate()], 'reported_bills': [bill]}
    result = report(state(request()), config, provider='openai')
    assert result['estimated_usd'] == pytest.approx(2.5) and result['billed_usd'] == 20
    assert report(state(request()), config, provider='openai', account_id='owner')['billed_usd'] is None
    clipped = cost_report(state(request()), config, 1800000001.0, 1800000100.0, provider='openai', now=1800000100.0)
    assert clipped['billed_usd'] is None and clipped['known_billed_usd'] == 0
    combined = report(state(request()), config)
    assert combined['billed_usd'] is None and combined['known_billed_usd'] == 20


@pytest.mark.parametrize('config', [
    {'currency': 'EUR'}, {'rates': None}, {'rates': [rate(input_per_million=-1)]},
    {'rates': [rate(output_per_million=float('inf'))]}, {'rates': [rate(cached_input_per_million=float('nan'))]},
    {'rates': [rate(input_per_million=True)]}, {'rates': [rate(), rate()]},
    {'rates': [rate(effective_at='2026-11-08')]}, {'rates': [rate(effective_at='2026-11-08T00:00:00+01:00')]},
    {'infrastructure': [infrastructure('nvidia', hourly_rate_usd=1)]},
    {'infrastructure': [infrastructure('nvidia'), infrastructure('nvidia', start=1800000050.0)]},
    {'infrastructure': [infrastructure('openai')]}, {'reported_bills': [infrastructure('openai'), infrastructure('openai')]},
])
def test_invalid_configuration_is_rejected_without_exposing_values(config):
    with pytest.raises(CostConfigurationError, match='Cost configuration is invalid'):
        estimate_request(request(), config)


def test_missing_config_file_defaults_empty_and_invalid_file_is_sanitized(tmp_path):
    file = tmp_path / 'costs.json'
    assert load_config(file) == {}
    file.write_text('private malformed config', encoding='utf-8')
    with pytest.raises(CostConfigurationError, match='Cost configuration is unavailable'):
        load_config(file)
    file.write_text(json.dumps({'currency': 'USD', 'rates': [], 'infrastructure': [], 'reported_bills': []}), encoding='utf-8')
    assert load_config(file)['rates'] == []
