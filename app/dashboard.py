"""Local-only, read-only projections of the private application snapshot."""

import asyncio
import ipaddress
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.database import DatabaseUnavailable, SpacetimeStore


def safe_text(value):
    """Capability links may appear in legacy transcripts; never make them usable."""
    return re.sub(r'https?://[^\s<>]*?/vault(?:[?#][^\s<>]*)?', '[vault link omitted]', str(value), flags=re.I)


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
            messages = [message for message in history if message.get('created', 0) > now - 30 * 86400
                        and message.get('role') in ('user', 'assistant')][-200:]
            if messages:
                self.conversations[key] = {'id': key, 'kind': kind, 'participant_id': participant,
                                           'project_id': project, 'messages': messages}

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
        job_id = message.get('outbox')
        box = self.state.get('outbox', {}).get(job_id, {})
        delivery = None
        if message['role'] == 'assistant':
            delivery = 'sent' if (message.get('part', 0) < box.get('delivered_parts', 0)
                                  or box.get('status') == 'sent') else box.get('status', 'unknown')
        return {'role': message['role'], 'content': safe_text(message.get('content', '')),
                'created': message['created'], 'job_id': job_id, 'delivery_status': delivery}


def create_dashboard_app(store, *, clock=time.time, cache_seconds=2):
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

    app = FastAPI(title='Acorn internal dashboard', docs_url=None, redoc_url=None,
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

    return app


def local_app():
    return create_dashboard_app(SpacetimeStore(os.environ['DATABASE_URL'], os.environ['DATABASE_NAME'],
                                              os.environ['DATABASE_TOKEN']))
