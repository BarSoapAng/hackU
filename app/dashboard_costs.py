"""Configured cost estimates over recorded usage; never queries billing providers."""

import json
import math
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path


class CostConfigurationError(ValueError):
    pass


class CostDataError(ValueError):
    pass


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise CostConfigurationError('Cost configuration is invalid.') from None


def _decimal(value):
    if type(value) not in (int, float):
        raise CostConfigurationError('Cost configuration is invalid.')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise CostConfigurationError('Cost configuration is invalid.')
    return result


def _config(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'currency', 'rates', 'infrastructure', 'reported_bills'}:
        raise CostConfigurationError('Cost configuration is invalid.')
    if value.get('currency', 'USD') != 'USD':
        raise CostConfigurationError('Cost configuration is invalid.')
    result = {'rates': [], 'infrastructure': [], 'reported_bills': []}
    for group in result:
        if not isinstance(value.get(group, []), list):
            raise CostConfigurationError('Cost configuration is invalid.')
        seen = set()
        for row in value.get(group, []):
            if not isinstance(row, dict):
                raise CostConfigurationError('Cost configuration is invalid.')
            if group == 'rates':
                if (set(row) - {'model', 'effective_at', 'input_per_million', 'output_per_million', 'cached_input_per_million'}
                        or not isinstance(row.get('model'), str) or not row['model'].strip()):
                    raise CostConfigurationError('Cost configuration is invalid.')
                parsed = {'model': row['model'], 'effective_at': _timestamp(row.get('effective_at'))}
                parsed.update({key: _decimal(row[key]) if row.get(key) is not None else None
                               for key in ('input_per_million', 'output_per_million', 'cached_input_per_million')})
                key = (parsed['model'], parsed['effective_at'])
            else:
                if (set(row) - {'provider', 'start', 'end', 'amount_usd', 'hourly_rate_usd'}
                        or row.get('provider') not in ('nvidia', 'spacetimedb', 'openai')
                        or (group == 'infrastructure' and row['provider'] == 'openai')):
                    raise CostConfigurationError('Cost configuration is invalid.')
                parsed = {'provider': row['provider'], 'start': _timestamp(row.get('start')), 'end': _timestamp(row.get('end'))}
                if parsed['end'] <= parsed['start'] or ('amount_usd' in row) == ('hourly_rate_usd' in row):
                    raise CostConfigurationError('Cost configuration is invalid.')
                if 'hourly_rate_usd' in row:
                    if group != 'infrastructure' or row['provider'] != 'nvidia':
                        raise CostConfigurationError('Cost configuration is invalid.')
                    parsed['amount_usd'] = _decimal(row['hourly_rate_usd']) * Decimal(str(parsed['end'] - parsed['start'])) / 3600
                else:
                    parsed['amount_usd'] = _decimal(row['amount_usd'])
                key = (parsed['provider'], parsed['start'], parsed['end'])
            if key in seen:
                raise CostConfigurationError('Cost configuration is invalid.')
            seen.add(key)
            result[group].append(parsed)
    for provider in ('nvidia', 'spacetimedb'):
        rows = sorted((row for row in result['infrastructure'] if row['provider'] == provider), key=lambda row: row['start'])
        if any(first['end'] > second['start'] for first, second in zip(rows, rows[1:])):
            raise CostConfigurationError('Cost configuration is invalid.')
    return result


def load_config(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise CostConfigurationError('Cost configuration is unavailable.') from None
    _config(value)
    return value


def _number(value, *, integer=False):
    return value if type(value) in (int, float) and 0 <= value <= 2**53 - 1 and (not integer or type(value) is int) else None


def _money(value):
    result = float(value)
    if not math.isfinite(result):
        raise CostDataError('Cost records cannot be calculated.')
    return result


def _estimate(request, config):
    if request.get('provider') == 'nvidia':
        return None, 'shared_gpu'
    rates = [rate for rate in config['rates'] if rate['model'] == request.get('model')
             and _number(request.get('started')) is not None and rate['effective_at'] <= request['started']]
    if request.get('provider') != 'openai' or not rates:
        return None, 'not_configured'
    rate = max(rates, key=lambda item: item['effective_at'])
    input_tokens, output_tokens, cached = (_number(request.get(key), integer=True) for key in
                                           ('input_tokens', 'output_tokens', 'cached_input_tokens'))
    if input_tokens is None or output_tokens is None or (cached is not None and cached > input_tokens):
        return None, 'usage_unknown'
    embedding = request.get('operation') in ('query_embedding', 'document_embedding')
    if output_tokens and rate['output_per_million'] is None:
        return None, 'not_configured'
    if embedding or input_tokens == 0 or rate['cached_input_per_million'] == rate['input_per_million']:
        if input_tokens and rate['input_per_million'] is None:
            return None, 'not_configured'
        input_cost = input_tokens * (rate['input_per_million'] or Decimal(0))
    elif cached is None:
        return None, 'usage_unknown'
    elif (cached and rate['cached_input_per_million'] is None
          or input_tokens > cached and rate['input_per_million'] is None):
        return None, 'not_configured'
    else:
        input_cost = (input_tokens - cached) * (rate['input_per_million'] or Decimal(0)) + cached * (rate['cached_input_per_million'] or Decimal(0))
    return (input_cost + output_tokens * (rate['output_per_million'] or Decimal(0))) / 1000000, 'token_estimate'


def estimate_request(request, config):
    amount, basis = _estimate(request, _config(config))
    return {'estimated_usd': _money(amount) if amount is not None else None, 'basis': basis}


def _records(state):
    records = {}
    for job in state.get('jobs', {}).values():
        for request in job.get('requests', []):
            if not isinstance(request, dict) or not isinstance(request.get('id'), str) or not request['id']:
                raise CostDataError('Usage records are inconsistent.')
            previous = records.get(request['id'])
            if previous and previous[0] != request:
                raise CostDataError('Usage records are inconsistent.')
            records.setdefault(request['id'], (request, job))
    return list(records.values())


def _storage(state):
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    weights = defaultdict(int)
    projects = state.get('projects', {})
    for project_id, project in projects.items():
        weights[project_id] += size(project)
    for file in state.get('files', {}).values():
        if file.get('project') in projects:
            weights[file['project']] += size(file)
    for file_id, chunks in state.get('chunks', {}).items():
        project_id = state.get('files', {}).get(file_id, {}).get('project')
        if project_id in projects:
            weights[project_id] += size(chunks)
    for key, history in state.get('conversations', {}).items():
        _, separator, project_id = key.rpartition(':project:')
        if separator and project_id in projects:
            weights[project_id] += size(history)
    return size(state), dict(weights)


def cost_report(state, config, start, end, account_id=None, project_id=None, provider=None, *, now=None):
    """Amounts are estimates; configured invoice totals remain a separate measure."""
    config = _config(config)
    now = time.time() if now is None else now
    if _number(start) is None or _number(end) is None or end <= start or provider not in (None, 'openai', 'nvidia', 'spacetimedb'):
        raise CostDataError('Cost period or provider is invalid.')
    selected = [provider] if provider else ['openai', 'nvidia', 'spacetimedb']
    projects = state.get('projects', {})
    records = _records(state)
    earliest = min((request['started'] for request, _ in records if _number(request.get('started')) is not None), default=None)
    history_partial = start < now - 30 * 86400 or earliest is None or start < earliest
    amounts = {name: defaultdict(Decimal) for name in selected}
    counts = {name: defaultdict(int) for name in selected}
    unpriced = {name: defaultdict(int) for name in selected}
    unmetered = {name: defaultdict(int) for name in selected}
    overhead = {name: Decimal(0) for name in selected}
    missing = {name: False for name in selected}

    def scope(request):
        project = request.get('project_id')
        return (request.get('account_id') or projects.get(project, {}).get('owner'), project)

    def matches(key):
        return (account_id is None or key[0] == account_id) and (project_id is None or key[1] == project_id)

    def add(name, key, amount):
        if key[0] is None:
            overhead[name] += amount
        else:
            amounts[name][key] += amount

    def active_window(request, lower, upper):
        started, finished, elapsed = (_number(request.get(key)) for key in ('started', 'finished', 'duration_ms'))
        if started is None or finished is None or elapsed is None or finished < started:
            return None
        if finished == started and elapsed:
            return None
        # The reservation starts before the provider timer. Anchor its measured
        # duration at completion, bounded by the recorded wall-clock interval.
        begin = max(lower, started, finished - elapsed / 1000)
        return begin, max(begin, min(upper, finished))

    for request, job in records:
        name = request.get('provider')
        if name not in selected:
            continue
        started = _number(request.get('started'))
        active = started is not None and start <= started < end
        if name == 'nvidia':
            window = active_window(request, start, end)
            finished = _number(request.get('finished'))
            active = active or window is not None and window[1] > window[0] or (
                started is not None and started < end and (finished is None or finished > start))
        if not active:
            continue
        key = scope(request)
        counts[name][key] += 1
        if name == 'openai':
            amount, _ = _estimate(request, config)
            if amount is None:
                unpriced[name][key] += 1
            else:
                add(name, key, amount)
        elif active_window(request, start, end) is None:
            unpriced[name][key] += 1

    for job in state.get('jobs', {}).values():
        if not start <= job.get('created', 0) < end:
            continue
        model_requests = {request.get('id') for request in job.get('requests', []) if request.get('operation') in ('builder', 'answer')}
        absent = (job.get('kind') == 'turn' and job.get('generations', 0) > len(model_requests)
                  or job.get('kind') == 'index' and job.get('attempts', 0) > 0
                  and not any(request.get('operation') == 'document_embedding' for request in job.get('requests', [])))
        name = 'nvidia' if job.get('mode') == 'builder' else 'openai'
        if absent and name in selected:
            project = job.get('project') if job.get('kind') == 'index' else job.get('focus') if name == 'nvidia' else job.get('selected')
            account = job.get('user') if name == 'nvidia' else projects.get(project, {}).get('owner')
            unmetered[name][(account, project)] += 1

    snapshot_bytes, project_bytes = _storage(state)
    for name in selected:
        if name == 'openai':
            missing[name] = not config['rates']
            continue
        intervals = sorted((row for row in config['infrastructure'] if row['provider'] == name
                            and row['start'] < end and row['end'] > start), key=lambda row: row['start'])
        covered_until = start
        for interval in intervals:
            lower, upper = max(start, interval['start']), min(end, interval['end'])
            if lower > covered_until:
                missing[name] = True
            covered_until = upper
            amount = interval['amount_usd'] * Decimal(str(upper - lower)) / Decimal(str(interval['end'] - interval['start']))
            allocated = Decimal(0)
            if name == 'nvidia':
                events = defaultdict(list)
                for request, _ in records:
                    if request.get('provider') != 'nvidia':
                        continue
                    window = active_window(request, lower, upper)
                    if window is not None and window[1] > window[0]:
                        events[window[0]].append((True, request['id'], scope(request)))
                        events[window[1]].append((False, request['id'], scope(request)))
                active, previous = {}, lower
                for timestamp in sorted(events):
                    if active:
                        segment = amount * Decimal(str(timestamp - previous)) / Decimal(str(upper - lower))
                        share = segment / len(active)
                        for key in active.values():
                            add(name, key, share)
                            allocated += share
                    for begins, request_id, key in events[timestamp]:
                        if begins:
                            active[request_id] = key
                        else:
                            active.pop(request_id, None)
                    previous = timestamp
            else:
                for project, weight in project_bytes.items():
                    share = amount * weight / max(snapshot_bytes, sum(project_bytes.values()))
                    add(name, (projects[project].get('owner'), project), share)
                    allocated += share
            overhead[name] += amount - allocated
        missing[name] = missing[name] or covered_until < end

    deployment = account_id is None and project_id is None

    def subtotal(values, predicate):
        return sum((value for key, value in values.items() if predicate(key)), Decimal(0))

    def complete(name, predicate, whole_deployment=False):
        if missing[name]:
            return False
        if name == 'spacetimedb' or name == 'nvidia' and whole_deployment:
            return True
        return not (history_partial or subtotal(unpriced[name], predicate) or subtotal(unmetered[name], predicate))

    provider_rows = []
    for name in selected:
        known = subtotal(amounts[name], matches) + (overhead[name] if deployment else 0)
        bill = next((row['amount_usd'] for row in config['reported_bills']
                     if row['provider'] == name and row['start'] == start and row['end'] == end), None) if deployment else None
        provider_rows.append({'provider': name, 'estimated_usd': _money(known) if complete(name, matches, deployment) else None,
            'known_estimated_usd': _money(known), 'billed_usd': _money(bill) if bill is not None else None,
            'request_count': int(subtotal(counts[name], matches)), 'unpriced_requests': int(subtotal(unpriced[name], matches)),
            'unmetered_jobs': int(subtotal(unmetered[name], matches)), 'configuration_missing': missing[name]})

    def allocation_row(identifier, predicate, label):
        values = {name: subtotal(amounts[name], predicate) for name in selected}
        known = sum(values.values(), Decimal(0))
        return {'id': identifier, 'label': label, 'estimated_usd': _money(known) if all(complete(name, predicate) for name in selected) else None,
                'known_estimated_usd': _money(known), 'providers': {name: _money(values.get(name, Decimal(0))) for name in ('openai', 'nvidia', 'spacetimedb')},
                'request_count': sum(int(subtotal(counts[name], predicate)) for name in selected),
                'unpriced_requests': sum(int(subtotal(unpriced[name], predicate)) for name in selected)}

    keys = {key for name in selected for table in (amounts[name], counts[name], unmetered[name]) for key in table if matches(key)}
    if account_id is not None:
        keys.add((account_id, project_id))
    accounts = [allocation_row(account, lambda key, account=account: matches(key) and key[0] == account,
                              state.get('users', {}).get(account, {}).get('phone', account))
                for account in sorted({key[0] for key in keys if key[0] is not None})]
    project_rows = []
    for project in sorted({key[1] for key in keys if key[1] is not None}):
        row = allocation_row(project, lambda key, project=project: matches(key) and key[1] == project,
                             projects.get(project, {}).get('name', project))
        row.update(account_id=projects.get(project, {}).get('owner'), deleted=bool(projects.get(project, {}).get('deleted')))
        project_rows.append(row)
    known_total = sum(row['known_estimated_usd'] for row in provider_rows)
    return {'currency': 'USD', 'period': {'start': start, 'end': end},
            'scope': {'account_id': account_id, 'project_id': project_id, 'provider': provider},
            'estimated_usd': known_total if all(row['estimated_usd'] is not None for row in provider_rows) else None,
            'known_estimated_usd': known_total,
            'billed_usd': sum(row['billed_usd'] for row in provider_rows) if all(row['billed_usd'] is not None for row in provider_rows) else None,
            'known_billed_usd': sum(row['billed_usd'] or 0 for row in provider_rows),
            'providers': provider_rows, 'accounts': accounts, 'projects': project_rows,
            'shared_overhead_usd': _money(sum(overhead.values(), Decimal(0))) if deployment else 0,
            'unassigned_project_usd': _money(sum((subtotal(amounts[name], lambda key: matches(key) and key[1] is None) for name in selected), Decimal(0))),
            'coverage': {'request_count': sum(row['request_count'] for row in provider_rows),
                         'unpriced_requests': sum(row['unpriced_requests'] for row in provider_rows),
                         'unmetered_jobs': sum(row['unmetered_jobs'] for row in provider_rows),
                         'usage_days': 30, 'history_partial': history_partial, 'earliest_request_at': earliest,
                         'partial': history_partial or any(row['estimated_usd'] is None or row['unpriced_requests'] or row['unmetered_jobs'] for row in provider_rows)},
            'allocation': {'gpu_method': 'concurrent_request_time_with_idle_shared', 'database_method': 'current_logical_project_bytes',
                           'snapshot_bytes': snapshot_bytes,
                           'project_bytes': {project: weight for project, weight in project_bytes.items()
                                             if matches((projects[project].get('owner'), project))}}}
