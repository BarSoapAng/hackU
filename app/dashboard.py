"""Local-only, read-only projections of the private application snapshot."""

import asyncio
import ipaddress
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.database import DatabaseUnavailable, SpacetimeStore
from app.dashboard_costs import CostConfigurationError, CostDataError, cost_report, estimate_request, load_config


def safe_text(value):
    """Capability links may appear in legacy transcripts; never make them usable."""
    return re.sub(r'https?://[^\s<>]*?/vault(?:[?#][^\s<>]*)?', '[vault link omitted]', str(value), flags=re.I)


def safe_number(value, *, integer=False):
    if type(value) in (int, float) and 0 <= value <= 2**53 - 1 and (not integer or type(value) is int):
        return value
    return None


def tool_payload(name, value, *, result=False):
    """Project known tool fields, never arbitrary nested trace/capability objects."""
    if not isinstance(value, dict):
        return None
    if result and isinstance(value.get('error'), str):
        return {'error': safe_text(value['error'])}
    if result and name == 'issue_vault_link':
        return {'vault_link_ready': bool(value.get('capability') or value.get('vault_link_ready'))}
    if result and name == 'update_instructions':
        return {'updated': value['updated']} if isinstance(value.get('updated'), bool) else {}
    if result and name == 'list_projects':
        return {'projects': [tool_payload('select_project', project, result=True)
                             for project in value.get('projects', []) if isinstance(project, dict)]}
    keys = ('id', 'name', 'code') if result and name in ('create_project', 'select_project') else {
        'create_project': ('name',), 'select_project': ('code',), 'update_instructions': ('instructions',),
    }.get(name, ())
    return {key: safe_text(value[key]) for key in keys if isinstance(value.get(key), str)}


class DashboardData:
    def __init__(self, state, now):
        self.state, self.now = state, now
        self.users = state.get('users', {})
        self.projects = {key: project for key, project in state.get('projects', {}).items()
                         if not project.get('deleted') and project.get('owner') in self.users}
        self.conversations = {}
        for key, history in state.get('conversations', {}).items():
            participant, separator, project = key.rpartition(':project:')
            if separator:
                if participant not in self.users or project not in self.projects:
                    continue
                kind = 'project'
            elif key.endswith(':builder') and key[:-8] in self.users:
                participant, project, kind = key[:-8], None, 'builder'
            else:
                continue
            messages = [dict(message) for message in history if message.get('created', 0) > now - 30 * 86400
                        and message.get('role') in ('user', 'assistant')][-200:]
            for index, message in enumerate(messages[:-1]):
                following = messages[index + 1]
                if (message['role'] == 'user' and following['role'] == 'assistant'
                        and message['created'] == following['created']):
                    message['job_id'] = following.get('outbox')
            if messages:
                self.conversations[key] = {'id': key, 'kind': kind, 'participant_id': participant,
                                           'project_id': project, 'messages': messages}
        for job in sorted(state.get('jobs', {}).values(), key=lambda item: (item.get('sequence', 0), item.get('created', 0))):
            key = self.job_conversation(job)
            if (not key or job.get('status') not in ('queued', 'running') or 'question' not in job
                    or job.get('created', 0) <= now - 30 * 86400):
                continue
            item = self.conversations.setdefault(key, {'id': key,
                'kind': 'builder' if job['mode'] == 'builder' else 'project',
                'participant_id': job['user'], 'project_id': None if job['mode'] == 'builder' else job['selected'],
                'messages': []})
            if not any(message.get('outbox', message.get('job_id')) == job['id'] for message in item['messages']):
                item['messages'].append({'role': 'user', 'content': job['question'],
                                         'created': job['created'], 'job_id': job['id']})
        for item in self.conversations.values():
            item['messages'] = item['messages'][-200:]

    def job_conversation(self, job):
        if job.get('kind') != 'turn' or job.get('user') not in self.users:
            return None
        if job.get('mode') == 'builder':
            return f'{job["user"]}:builder'
        if job.get('mode') == 'customer' and job.get('selected') in self.projects:
            return f'{job["user"]}:project:{job["selected"]}'
        return None

    def account(self, account_id):
        if account_id not in self.users:
            raise HTTPException(404, 'Account not found.')
        return self.users[account_id]

    def project(self, account_id, project_id):
        self.account(account_id)
        project = self.projects.get(project_id)
        if not project or project['owner'] != account_id:
            raise HTTPException(404, 'Project not found.')
        return project

    def conversation(self, account_id, conversation_id):
        self.account(account_id)
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            raise HTTPException(404, 'Conversation not found.')
        if conversation['kind'] == 'builder':
            if conversation['participant_id'] != account_id:
                raise HTTPException(404, 'Conversation not found.')
        else:
            self.project(account_id, conversation['project_id'])
        return conversation

    def project_conversations(self, project_id):
        return [conversation for conversation in self.conversations.values()
                if conversation['project_id'] == project_id]

    def conversation_summary(self, conversation):
        return {key: conversation[key] for key in ('id', 'participant_id')} | {
            'message_count': len(conversation['messages']),
            'last_active': max(message['created'] for message in conversation['messages']),
        }

    def project_summary(self, project_id):
        project = self.projects[project_id]
        conversations = self.project_conversations(project_id)
        return {'id': project_id, 'name': safe_text(project['name']), 'code': project['code'],
                'participant_count': len({item['participant_id'] for item in conversations}),
                'conversation_count': len(conversations),
                'last_active': max((self.conversation_summary(item)['last_active'] for item in conversations), default=None)}

    def user_summary(self, user_id):
        projects = {key for key, project in self.projects.items() if project['owner'] == user_id}
        conversations = [item for item in self.conversations.values()
                         if item['project_id'] in projects or item['id'] == f'{user_id}:builder']
        own_activity = [message['created'] for item in self.conversations.values()
                        if item['participant_id'] == user_id for message in item['messages']]
        return {'id': user_id, 'phone': self.users[user_id].get('phone', user_id),
                'project_count': len(projects), 'conversation_count': len(conversations),
                'last_active': max(own_activity, default=None)}

    def message(self, message):
        job_id = message.get('job_id') or message.get('outbox')
        box = self.state.get('outbox', {}).get(job_id, {})
        delivery = None
        if message['role'] == 'assistant':
            delivery = 'sent' if (message.get('part', 0) < box.get('delivered_parts', 0)
                                  or box.get('status') == 'sent') else box.get('status', 'unknown')
        return {'role': message['role'], 'content': safe_text(message.get('content', '')),
                'created': message['created'], 'job_id': job_id, 'delivery_status': delivery}

    def tools(self, job, trace_available):
        steps = {step['id']: dict(step) for step in job.get('steps', [])}
        order = []
        if trace_available:
            calls = {}
            for position, entry in enumerate(job.get('trace', []), start=1):
                if entry.get('role') == 'assistant':
                    for index, call in enumerate(entry.get('tool_calls', [])):
                        key = f'{position}:{index}'
                        step = next((step for step in steps.values() if step.get('call_key') == key), None)
                        if step is None:
                            function = call.get('function', {})
                            try:
                                arguments = json.loads(function.get('arguments', '{}'))
                            except (ValueError, TypeError):
                                arguments = None
                            step = {'id': f'{job["id"]}:call:{key}', 'name': function.get('name', 'Unknown tool'),
                                    'created': None, 'arguments': arguments}
                            steps[step['id']] = step
                        order.append(step['id'])
                        calls[call.get('id')] = step
                elif entry.get('role') == 'tool' and entry.get('tool_call_id') in calls:
                    try:
                        calls.pop(entry['tool_call_id'])['result'] = json.loads(entry.get('content', '{}'))
                    except (ValueError, TypeError):
                        pass
        items = []
        for step_id in dict.fromkeys([*order, *steps]):
            step = steps[step_id]
            result = step.get('result')
            status = ('failed' if isinstance(result, dict) and 'error' in result else 'completed') if 'result' in step else (
                'pending' if job.get('status') in ('queued', 'running') and job.get('deadline', 0) > self.now else 'unknown')
            items.append({'id': step['id'], 'name': step['name'], 'created': step.get('created'), 'status': status,
                          'arguments': tool_payload(step['name'], step.get('arguments')) if trace_available else None,
                          'result': tool_payload(step['name'], result, result=True) if trace_available else None})
        return items

    def request(self, request, job):
        result = {key: request.get(key) if isinstance(request.get(key), str) else None for key in (
            'id', 'provider', 'operation', 'model', 'error_type')}
        result.update({key: safe_number(request.get(key)) for key in ('started', 'finished', 'duration_ms')})
        result.update({key: safe_number(request.get(key), integer=True) for key in (
            'sequence', 'input_tokens', 'output_tokens', 'cached_input_tokens', 'total_tokens')})
        result['provider_status'] = request.get('provider_status') if request.get('provider_status') in (
            'completed', 'incomplete', 'failed', 'cancelled', 'queued', 'in_progress') else None
        status = request.get('http_status')
        result['http_status'] = status if type(status) is int and 100 <= status <= 599 else None
        result['status'] = request.get('status') if request.get('status') in ('pending', 'received', 'failed', 'unknown') else 'unknown'
        if result['status'] == 'pending' and (job.get('status') not in ('queued', 'running') or job.get('deadline', 0) <= self.now):
            result['status'] = 'unknown'
        return result

    def turn(self, job):
        trace_available = job['created'] > self.now - 7 * 86400 and ('steps' in job or 'trace' in job)
        requests = [self.request(request, job) for request in job.get('requests', [])
                    if isinstance(request, dict) and (safe_number(request.get('started')) is None
                        or request['started'] > self.now - 30 * 86400)]
        return {'id': job['id'], 'created': job['created'], 'status': job['status'],
                'attempts': job.get('attempts', 0), 'generations': job.get('generations', 0),
                'trace_available': trace_available, 'tools': self.tools(job, trace_available),
                'requests': requests, 'usage_recorded': bool(requests)}


def create_dashboard_app(store, *, clock=time.time, cache_seconds=2, costs_config=None):
    """Separate app factory: no messaging workers, credentials, or write routes."""
    cached = None
    cached_at = 0
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            close = getattr(store, 'aclose', None)
            if close:
                await close()

    app = FastAPI(title='Hypha internal dashboard', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def local_read_only(request: Request, call_next):
        try:
            host = urlsplit('http://' + request.headers.get('host', ''))
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
            local = local and host.hostname in ('127.0.0.1', 'localhost', '::1') and host.port is not None
            local = local and not host.username and not host.password and not host.path and not host.query and not host.fragment
        except ValueError:
            local = False
        origin = request.headers.get('origin')
        if origin and origin not in ('http://127.0.0.1:5173', 'http://localhost:5173',
                                      str(request.base_url).rstrip('/')):
            local = False
        # Refuse forwarded requests; the local UI proxy preserves Host and adds no forwarding headers.
        if any(name in request.headers for name in ('forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto')):
            local = False
        if not local:
            response = JSONResponse({'detail': 'This dashboard is available locally only.'}, status_code=403)
        elif request.method != 'GET':
            response = JSONResponse({'detail': 'This dashboard is read only.'}, status_code=405,
                                    headers={'Allow': 'GET'})
        else:
            response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'})
        return response

    @app.exception_handler(DatabaseUnavailable)
    async def unavailable(request, error):
        return JSONResponse({'detail': 'Storage is temporarily unavailable. Try refreshing.'}, status_code=503)

    async def data():
        nonlocal cached, cached_at
        async with lock:
            now = clock()
            if cached is None or now - cached_at >= cache_seconds:
                _, state = await store.read()
                cached, cached_at = DashboardData(state, now), now
            return cached

    def pricing():
        return costs_config() if callable(costs_config) else costs_config

    @app.get('/api/internal/costs')
    async def costs(start: date | None = None, end: date | None = None,
                    account_id: str | None = Query(None, max_length=256),
                    project_id: str | None = Query(None, max_length=256),
                    provider: Literal['openai', 'nvidia', 'spacetimedb'] | None = None):
        current = await data()
        if account_id is not None:
            current.account(account_id)
        if project_id is not None:
            if account_id is None:
                raise HTTPException(422, 'Select an account before filtering by project.')
            current.project(account_id, project_id)
        today = datetime.fromtimestamp(current.now, timezone.utc).date()
        end = end or today
        start = start or end - timedelta(days=6)
        if start > end or (end - start).days >= 366 or end > today:
            raise HTTPException(422, 'Choose a date range of up to 366 days ending today or earlier (UTC).')
        lower = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()
        try:
            result = cost_report(current.state, pricing(), lower, upper, account_id=account_id,
                                 project_id=project_id, provider=provider, now=current.now)
        except CostConfigurationError:
            raise HTTPException(503, 'Cost configuration is unavailable. Check the local costs file.') from None
        except CostDataError:
            raise HTTPException(503, 'Recorded cost data is unavailable. Try refreshing.') from None
        for row in [*result['accounts'], *result['projects']]:
            row['label'] = safe_text(row['label'])
        return result

    @app.get('/api/internal/overview')
    async def overview():
        current = await data()
        return {'users': len(current.users), 'owners': len({p['owner'] for p in current.projects.values()}),
                'projects': len(current.projects), 'conversations': len(current.conversations), 'updated_at': current.now}

    @app.get('/api/internal/users')
    async def users(q: str = Query('', max_length=120), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        current = await data()
        matches = [key for key, user in current.users.items() if q.casefold() in str(user.get('phone', key)).casefold()]
        matches.sort()
        return {'items': [current.user_summary(key) for key in matches[offset:offset + limit]],
                'total': len(matches), 'offset': offset, 'limit': limit}

    @app.get('/api/internal/accounts/{account_id}')
    async def account(account_id: str):
        current = await data()
        current.account(account_id)
        builder = current.conversations.get(f'{account_id}:builder')
        return {'user': current.user_summary(account_id),
                'projects': [current.project_summary(key) for key, project in current.projects.items()
                             if project['owner'] == account_id],
                'builder_conversation': current.conversation_summary(builder) if builder else None}

    @app.get('/api/internal/accounts/{account_id}/projects/{project_id}')
    async def project(account_id: str, project_id: str):
        current = await data()
        current.project(account_id, project_id)
        conversations = sorted(current.project_conversations(project_id),
                               key=lambda item: current.conversation_summary(item)['last_active'], reverse=True)
        return {'project': current.project_summary(project_id), 'participants': [
            {'id': item['participant_id'], 'phone': current.users[item['participant_id']].get('phone', item['participant_id']),
             'conversation_id': item['id'], 'message_count': len(item['messages']),
             'last_active': current.conversation_summary(item)['last_active']} for item in conversations]}

    @app.get('/api/internal/accounts/{account_id}/conversations/{conversation_id}')
    async def conversation(account_id: str, conversation_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        current = await data()
        item = current.conversation(account_id, conversation_id)
        end = max(0, len(item['messages']) - offset)
        messages = item['messages'][max(0, end - limit):end]
        return {key: item[key] for key in ('id', 'kind', 'project_id', 'participant_id')} | {
            'messages': [current.message(message) for message in messages], 'total': len(item['messages']),
            'offset': offset, 'limit': limit, 'coverage': {'message_days': 30, 'message_limit': 200, 'trace_days': 7}}

    @app.get('/api/internal/accounts/{account_id}/conversations/{conversation_id}/activity')
    async def activity(account_id: str, conversation_id: str, offset: int = Query(0, ge=0),
                       limit: int = Query(25, ge=1, le=100), turn_id: str | None = Query(None, max_length=256)):
        current = await data()
        current.conversation(account_id, conversation_id)
        jobs = [job for job in current.state.get('jobs', {}).values()
                if current.job_conversation(job) == conversation_id and job.get('created', 0) > current.now - 30 * 86400
                and (turn_id is None or job['id'] == turn_id)]
        jobs.sort(key=lambda job: (job['created'], job['id']), reverse=True)
        items = [current.turn(job) for job in jobs[offset:offset + limit]]
        try:
            config = pricing()
        except CostConfigurationError:
            config = None
            configuration_error = True
        else:
            configuration_error = False
        for turn in items:
            for request in turn['requests']:
                try:
                    if configuration_error:
                        raise CostConfigurationError()
                    request['cost'] = estimate_request(request, config)
                except (CostConfigurationError, CostDataError):
                    request['cost'] = {'estimated_usd': None, 'basis': 'configuration_error'}
        return {'items': items,
                'total': len(jobs), 'offset': offset, 'limit': limit, 'coverage': {'trace_days': 7, 'usage_days': 30}}

    return app


def local_app():
    return create_dashboard_app(SpacetimeStore(os.environ['DATABASE_URL'], os.environ['DATABASE_NAME'],
                                              os.environ['DATABASE_TOKEN']),
                                costs_config=lambda: load_config(os.environ.get('DASHBOARD_COSTS_FILE', '.local/dashboard-costs.json')))
