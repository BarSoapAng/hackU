"""Transactional application operations. Callbacks are pure and may be replayed by CAS."""
import asyncio
import copy
import re
import secrets
import time
from collections import defaultdict

from app.config import Limits
from app.security import token_hash


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def initialize(state):
    for name in ('users', 'assignments', 'receipts', 'jobs', 'projects', 'files', 'chunks',
                 'capabilities', 'delivery_material', 'outbox', 'conversations', 'usage'):
        state.setdefault(name, {})


def redact_links(text):
    return re.sub(r'https?://[^\s<>]*?/vault#[^\s<>]+', '[vault link omitted]', text)


def available(state, project_id):
    project = state['projects'].get(project_id)
    if not project or project.get('deleted'):
        raise DomainError('Project is unavailable.', 404)
    return project


def owned(state, user_id, project_id):
    project = available(state, project_id)
    if project['owner'] != user_id:
        raise DomainError('Select a project you own.', 403)
    return project


def by_code(state, code):
    return next((p for p in state['projects'].values() if p['code'] == code.upper() and not p.get('deleted')), None)


def consume(state, key, limit, amount=1):
    current = state['usage'].get(key, 0)
    if current + amount > limit:
        raise DomainError('The usage limit has been reached. Please try again later.', 429)
    state['usage'][key] = current + amount


def revoke_capability(state, cap):
    cap['revoked'] = True
    state['delivery_material'].pop(cap['id'], None)


def cancel_outbox(state, outbox):
    if outbox['status'] in ('pending', 'submitting', 'unknown'):
        outbox['status'] = 'unknown' if outbox['status'] in ('submitting', 'unknown') else 'cancelled'
        outbox.pop('payload', None)
        for cap_id in outbox.get('capabilities', []):
            revoke_capability(state, state['capabilities'][cap_id])


def cancel_job(state, job, status='superseded'):
    job['status'] = status
    for cap in state['capabilities'].values():
        if cap['job'] == job['id'] and not cap.get('delivered'):
            revoke_capability(state, cap)
    if job['id'] in state['outbox']:
        cancel_outbox(state, state['outbox'][job['id']])


def route(state, user, *, mode=None, focus=None, selected=None, origin=None):
    updated = {key: value for key, value in {'mode': mode, 'focus': focus, 'selected': selected}.items() if value is not None}
    if all(user.get(key) == value for key, value in updated.items()):
        return
    user.update(updated)
    user['revision'] += 1
    for job in state['jobs'].values():
        if job.get('user') == user['id'] and job['id'] != origin and job.get('revision') != user['revision']:
            if job['status'] in ('queued', 'running'):
                cancel_job(state, job)
    for outbox in state['outbox'].values():
        if outbox['user'] == user['id'] and outbox['revision'] != user['revision'] and outbox['id'] != origin:
            cancel_outbox(state, outbox)
    if origin:
        job = state['jobs'][origin]
        job.update(revision=user['revision'], focus=user['focus'], selected=user['selected'], mode=user['mode'])


def check_job(state, job_id, claim, now):
    job = state['jobs'].get(job_id)
    if not job or job['status'] != 'running' or job.get('claim') != claim or job.get('lease', 0) <= now:
        raise DomainError('This work lease is no longer current.', 409)
    if job['deadline'] <= now:
        raise DomainError('This turn has expired.', 409)
    if job.get('user'):
        user = state['users'][job['user']]
        if job['revision'] != user['revision']:
            raise DomainError('This turn was superseded by navigation.', 409)
    if job.get('project'):
        available(state, job['project'])
    return job


def source_versions_current(state, versions):
    return all(fid in state['files'] and state['files'][fid]['version'] == version
               and state['files'][fid]['status'] == 'ready' for fid, version in versions.items())


class Service:
    def __init__(self, store, keyring, public_url, limits=None, clock=time.time):
        self.store, self.keys = store, keyring
        self.public_url = public_url.rstrip('/')
        self.limits, self.clock = limits or Limits(), clock
        # One process only: all navigation and provider submission share these gates.
        self.gates = defaultdict(asyncio.Lock)

    async def snapshot(self):
        _, state = await self.store.read()
        initialize(state)
        return state

    async def sync_assignments(self, assignments):
        def update(state):
            initialize(state)
            state['assignments'] = {a['phone']: a for a in assignments}
        await self.store.transaction(update)

    async def admit(self, event):
        """The transaction is the per-user sequencer; no model work runs here."""
        now = self.clock()
        async with self.gates[event.phone]:
            snapshot = await self.snapshot()
            user = snapshot['users'].get(event.phone)
            prepared = self._prepare_step('issue_vault_link', event.event_id, f'{event.event_id}:0', user['focus'], now) if user and event.text.strip().lower() == '/vault' else None
            def accept(state):
                initialize(state)
                if event.event_id in state['receipts']:
                    return copy.deepcopy(state['receipts'][event.event_id])
                receipt = {'id': event.event_id, 'created': now, 'status': 'rejected'}
                state['receipts'][event.event_id] = receipt
                if event.occurred_at <= now - self.limits.dedup_days * 86400 or event.occurred_at > now + 300:
                    receipt['reason'] = 'This event is outside the supported retention window.'
                    return receipt
                assignment = state['assignments'].get(event.phone)
                if not assignment or assignment['destination'] != event.destination or event.occurred_at <= assignment['created']:
                    receipt['reason'] = 'An active preassignment and a new inbound message are required.'
                    return receipt
                user = state['users'].setdefault(event.phone, {'id': event.phone, 'phone': event.phone,
                    'mode': 'builder', 'focus': None, 'selected': None, 'revision': 0, 'sequence': 0})
                pending = [j for j in state['jobs'].values() if j['status'] in ('queued', 'running') and j['kind'] == 'turn']
                text = redact_links(event.text.strip())
                deterministic = text.startswith('/') or bool(re.search(r'\b(reset|clear|erase|forget|delete)\b.*\b(history|conversation|chat|memory)\b', text, re.I))
                if not deterministic and state['usage'].get(f'generation-day:{int(now // 86400)}', 0) >= self.limits.generations_per_day:
                    receipt['reason'] = 'The generation limit has been reached. Please try again tomorrow.'
                    self._notice(state, user, event, receipt['reason'], now)
                    return receipt
                # Navigation must remain available when model queues are full.
                if not deterministic and (len(pending) >= self.limits.pending_total or sum(j['user'] == user['id'] for j in pending) >= self.limits.pending_per_user):
                    receipt['reason'] = 'Temporary capacity limit. Please try again later.'
                    self._notice(state, user, event, receipt['reason'], now)
                    return receipt
                keys = [(f'turn-minute:{event.phone}:{int(now // 60)}', self.limits.turns_per_minute),
                        (f'turn-day:{event.phone}:{int(now // 86400)}', self.limits.turns_per_day)]
                if any(state['usage'].get(key, 0) >= limit for key, limit in keys):
                    receipt['reason'] = 'The usage limit has been reached. Please try again later.'
                    self._notice(state, user, event, receipt['reason'], now)
                    return receipt
                for key, limit in keys:
                    consume(state, key, limit)
                user['sequence'] += 1
                receipt.update(status='accepted', sequence=user['sequence'], operation=event.event_id)
                job = {'id': event.event_id, 'kind': 'turn', 'user': user['id'], 'sequence': user['sequence'],
                    'created': now, 'deadline': now + self.limits.turn_seconds, 'status': 'queued',
                    'mode': user['mode'], 'focus': user['focus'], 'selected': user['selected'],
                    'revision': user['revision'], 'question': text, 'attempts': 0, 'generations': 0,
                    'steps': [], 'model_replies': [], 'space_id': event.space_id, 'agent_id': event.agent_id,
                    'destination': event.destination, 'assignment': assignment['id'], 'received_at': event.occurred_at}
                state['jobs'][job['id']] = job
                if deterministic:
                    job.update(status='running', claim='command', lease=now + self.limits.lease_seconds)
                    answer = self._command(state, user, job, now, prepared)
                    self._complete(state, job, answer, now)
                return copy.deepcopy(receipt)
            return await self.store.transaction(accept)

    def _notice(self, state, user, event, text, now):
        key = f'notice:{user["id"]}:{int(now // 60)}'
        if state['usage'].get(key):
            return
        state['usage'][key] = 1
        job = {'id': event.event_id, 'user': user['id'], 'revision': user['revision'], 'mode': user['mode'],
            'focus': user['focus'], 'selected': user['selected'], 'question': '', 'space_id': event.space_id,
            'agent_id': event.agent_id, 'destination': event.destination, 'received_at': event.occurred_at, 'created': now}
        self._outbox(state, job, text, [], {}, now)

    def _command(self, state, user, job, now, prepared=None):
        parts = job['question'].split()
        command = parts[0].lower() if parts else ''
        try:
            if command == '/builder' and len(parts) == 1:
                route(state, user, mode='builder', origin=job['id'])
                return 'Builder mode. Your editing focus is restored.'
            if command in ('/edit', '/use') and len(parts) == 2:
                project = by_code(state, parts[1])
                if not project:
                    raise DomainError('Unknown project code.')
                if command == '/edit':
                    owned(state, user['id'], project['id'])
                    route(state, user, mode='builder', focus=project['id'], origin=job['id'])
                else:
                    route(state, user, mode='customer', selected=project['id'], origin=job['id'])
                return f'Selected {project["name"]} ({project["code"]}).'
            if command == '/projects' and len(parts) == 1:
                projects = [p for p in state['projects'].values() if p['owner'] == user['id'] and not p.get('deleted')]
                return '\n'.join(f'{p["name"]}: {p["code"]}' for p in projects) or 'You do not own any projects yet. Ask me to create one.'
            if command == '/vault' and len(parts) == 1:
                owned(state, user['id'], user['focus'])
                step = self._intent(state, job, 'issue_vault_link', {}, now)
                self._execute(state, job, step, now, prepared)
                return 'Your editing link is valid for one hour.'
            if command == '/help' and len(parts) == 1:
                return '/builder — builder mode\n/projects — your projects\n/edit CODE — edit an owned project\n/use CODE — ask a project agent\n/vault — one-hour editor link\n/help — commands\nHistory reset is unsupported.'
            if not job['question'].startswith('/') or command in ('/reset', '/clear'):
                return 'History reset is unsupported. Your conversation has not changed.'
            return 'Invalid command. Use /help to see available commands.'
        except DomainError as exc:
            return str(exc)

    def _intent(self, state, job, name, arguments, now):
        if len(job['steps']) >= self.limits.tools_per_turn:
            raise DomainError('This turn reached its tool limit. Please send another message.')
        schemas = {'create_project': {'name': str}, 'list_projects': {}, 'select_project': {'code': str},
                   'update_instructions': {'instructions': str}, 'issue_vault_link': {}}
        if name not in schemas or set(arguments) != set(schemas[name]) or any(type(arguments[k]) is not t for k, t in schemas.get(name, {}).items()):
            raise DomainError('Unsupported tool or invalid arguments.')
        if any(not v.strip() or len(v.encode()) > (4096 if k == 'instructions' else 120) for k, v in arguments.items()):
            raise DomainError('Tool arguments are empty or too long.')
        step = {'id': f'{job["id"]}:{len(job["steps"])}', 'name': name, 'arguments': arguments,
                'target': job['focus'], 'expected_revision': job['revision'], 'created': now}
        job['steps'].append(step)
        return step

    async def record_intent(self, job_id, claim, name, arguments):
        now = self.clock()
        def record(state):
            job = check_job(state, job_id, claim, now)
            return copy.deepcopy(self._intent(state, job, name, arguments, now))
        return await self.store.transaction(record)

    def _prepare_step(self, name, job_id, step_id, project_id, now):
        if name == 'create_project':
            return {'id': secrets.token_hex(16), 'code': secrets.token_hex(4).upper()}
        if name == 'issue_vault_link':
            cap_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
            return {'id': cap_id, 'hash': token_hash(token), 'expires': now + self.limits.link_seconds,
                    'ciphertext': self.keys.encrypt({'token': token}, f'cap:{cap_id}:{project_id}:{step_id}')}
        return None

    def _execute(self, state, job, step, now, prepared=None):
        if 'result' in step:
            return step['result']
        user = state['users'][job['user']]
        if user['revision'] != step['expected_revision'] or job['revision'] != user['revision']:
            raise DomainError('This turn was superseded.', 409)
        name, args = step['name'], step['arguments']
        if name == 'create_project':
            active = [p for p in state['projects'].values() if not p.get('deleted')]
            if len(active) >= self.limits.projects_total or sum(p['owner'] == user['id'] for p in active) >= self.limits.projects_per_user:
                raise DomainError('Project limit reached.', 429)
            project_id, code = prepared['id'], prepared['code']
            if project_id in state['projects'] or any(p['code'] == code for p in state['projects'].values()):
                raise DomainError('Project code allocation conflicted. Please try again.', 409)
            project = {'id': project_id, 'owner': user['id'], 'name': args['name'].strip(), 'code': code,
                'instructions': 'Give concise answers grounded in current sources, with filename and heading citations.', 'created': now}
            state['projects'][project_id] = project
            route(state, user, mode='builder', focus=project_id, origin=job['id'])
            result = {'id': project_id, 'name': project['name'], 'code': code}
        elif name == 'list_projects':
            result = {'projects': [{'name': p['name'], 'code': p['code']} for p in state['projects'].values() if p['owner'] == user['id'] and not p.get('deleted')]}
        elif name == 'select_project':
            project = by_code(state, args['code'])
            if not project:
                raise DomainError('Unknown project code.')
            owned(state, user['id'], project['id'])
            route(state, user, mode='builder', focus=project['id'], origin=job['id'])
            result = {'id': project['id'], 'name': project['name'], 'code': project['code']}
        elif name == 'update_instructions':
            project = owned(state, user['id'], step['target'])
            project['instructions'] = args['instructions']
            result = {'updated': True}
        else:
            project = owned(state, user['id'], step['target'])
            if not prepared:
                raise DomainError('Select a project you own before requesting a vault link.')
            cap_id = prepared['id']
            cap = {'id': cap_id, 'project': project['id'], 'owner': user['id'], 'job': job['id'],
                   'step': step['id'], 'hash': prepared['hash'], 'expires': prepared['expires'],
                   'revoked': False, 'delivered': False}
            state['capabilities'][cap_id] = cap
            state['delivery_material'][cap_id] = prepared['ciphertext']
            result = {'capability': cap_id, 'expires': cap['expires']}
        step.update(result=result, resulting_revision=job['revision'], continuation={k: job[k] for k in ('mode', 'focus', 'selected', 'revision')})
        return result

    async def execute_step(self, job_id, claim, step_id):
        now = self.clock()
        async with self.gates[(await self.snapshot())['jobs'][job_id]['user']]:
            snapshot = await self.snapshot()
            job = check_job(snapshot, job_id, claim, now)
            step = next(s for s in job['steps'] if s['id'] == step_id)
            prepared = self._prepare_step(step['name'], job_id, step_id, step['target'], now) if 'result' not in step else None
            def execute(state):
                job = check_job(state, job_id, claim, now)
                step = next(s for s in job['steps'] if s['id'] == step_id)
                return copy.deepcopy(self._execute(state, job, step, now, prepared))
            return await self.store.transaction(execute)

    def _outbox(self, state, job, text, caps, versions, now):
        outbox = {'id': job['id'], 'user': job['user'], 'revision': job['revision'], 'created': now,
                  'status': 'pending', 'key': job['id'], 'space_id': job['space_id'], 'agent_id': job['agent_id'],
                  'destination': job['destination'], 'project': job['selected'] if job['mode'] == 'customer' else job['focus'],
                  'capabilities': caps, 'versions': versions, 'attempts': 0,
                  'assignment': job.get('assignment'), 'received_at': job.get('received_at', job['created'])}
        outbox['payload'] = self.keys.encrypt({'text': text}, f'outbox:{job["id"]}')
        state['outbox'][job['id']] = outbox

    def _complete(self, state, job, text, now, versions=None):
        text = redact_links(text)
        for step in job.get('steps', []):
            if step['name'] == 'create_project' and 'result' in step and step['result'].get('code') not in text:
                text += f'\nProject code: {step["result"]["code"]}'
        versions = versions or {}
        if not source_versions_current(state, versions):
            raise DomainError('Sources are updating. Please ask again shortly.', 409)
        if job.get('status') == 'running':
            for step in job.get('steps', []):
                cap_id = step.get('result', {}).get('capability')
                if cap_id and (cap_id not in state['capabilities'] or state['capabilities'][cap_id]['revoked']):
                    raise DomainError('The editing link is no longer available. Request /vault again.', 409)
        caps, links = [], []
        for cap in state['capabilities'].values():
            if cap['job'] == job['id'] and not cap['revoked']:
                if cap['expires'] <= now:
                    raise DomainError('The editing link expired. Request /vault again.', 409)
                material = self.keys.decrypt(state['delivery_material'][cap['id']], f'cap:{cap["id"]}:{cap["project"]}:{cap["step"]}')
                links.append(f'{self.public_url}/vault#{material["token"]}')
                caps.append(cap['id'])
        self._outbox(state, job, '\n'.join([text, *links]), caps, versions, now)
        for cap_id in caps:
            state['delivery_material'].pop(cap_id, None)
            state['capabilities'][cap_id]['outbox'] = job['id']
        conversation = f'{job["user"]}:builder' if job['mode'] == 'builder' else f'{job["user"]}:project:{job["selected"]}'
        history = state['conversations'].setdefault(conversation, [])
        history.extend([{'role': 'user', 'content': job['question'], 'created': now},
                        {'role': 'assistant', 'content': text + ('\n[vault link issued]' if caps else ''), 'created': now, 'outbox': job['id']}])
        state['conversations'][conversation] = [m for m in history if m['created'] > now - 30 * 86400][-200:]
        job.update(status='completed', completed=now)

    async def complete(self, job_id, claim, text, versions=None):
        now = self.clock()
        def complete(state):
            job = check_job(state, job_id, claim, now)
            if job['mode'] == 'customer':
                available(state, job['selected'])
            return self._complete(state, job, text, now, versions)
        return await self.store.transaction(complete)

    def history(self, state, job):
        conversation = f'{job["user"]}:builder' if job['mode'] == 'builder' else f'{job["user"]}:project:{job["selected"]}'
        return [{'role': m['role'], 'content': m['content']} for m in state['conversations'].get(conversation, [])
                if m['created'] > self.clock() - 30 * 86400 and (m['role'] != 'assistant' or state['outbox'].get(m.get('outbox'), {}).get('status') == 'sent')][-40:]

    def authorize(self, state, token):
        hashed = token_hash(token)
        cap = next((c for c in state['capabilities'].values() if secrets.compare_digest(c['hash'], hashed)), None)
        if not cap or cap['revoked'] or cap['expires'] <= self.clock():
            raise DomainError('This editing link is invalid or expired. Request /vault in iMessage.', 401)
        available(state, cap['project'])
        return cap

    async def list_files(self, token):
        state = await self.snapshot()
        cap = self.authorize(state, token)
        return [{k: f[k] for k in ('id', 'name', 'version', 'status')} for f in state['files'].values() if f['project'] == cap['project']]

    async def read_file(self, token, file_id):
        state = await self.snapshot()
        cap = self.authorize(state, token)
        file = state['files'].get(file_id)
        if not file or file['project'] != cap['project']:
            raise DomainError('File not found.', 404)
        return file

    async def save_file(self, token, name, content, file_id=None, expected_version=None):
        from app.retrieval import split_markdown
        if not name or len(name) > 120 or not name.lower().endswith('.md') or any(c in name for c in '/\\\x00\r\n'):
            raise DomainError('Choose a simple .md filename.')
        if '\x00' in content or len(content.encode('utf-8')) > self.limits.file_bytes:
            raise DomainError('Markdown must be UTF-8 text within the file size limit.', 413)
        try:
            chunks = split_markdown(name, content)
        except ValueError as exc:
            raise DomainError(str(exc)) from None
        now = self.clock()
        new_id = file_id or secrets.token_hex(16)
        def save(state):
            cap = self.authorize(state, token)
            project = cap['project']
            files = [f for f in state['files'].values() if f['project'] == project]
            current = state['files'].get(file_id)
            if file_id and (not current or current['project'] != project):
                raise DomainError('File not found.', 404)
            if current and current['version'] != expected_version:
                raise DomainError('This file changed. Reload before saving.', 409)
            if any(f['name'].casefold() == name.casefold() and f['id'] != file_id for f in files):
                raise DomainError('A file with this name exists. Open it to explicitly replace its contents.', 409)
            others = [f for f in files if f['id'] != file_id]
            if len(others) + 1 > self.limits.files_per_project or sum(f['bytes'] for f in others) + len(content.encode()) > self.limits.project_bytes or sum(f['chunk_count'] for f in others) + len(chunks) > self.limits.chunks_per_project:
                raise DomainError('Project vault quota exceeded.', 413)
            version = current['version'] + 1 if current else 1
            file = {'id': new_id, 'project': project, 'name': name, 'content': content, 'version': version,
                    'status': 'pending', 'bytes': len(content.encode()), 'chunk_count': len(chunks)}
            state['files'][new_id] = file
            state['chunks'].pop(new_id, None)
            for old in state['jobs'].values():
                if old['kind'] == 'index' and old['file'] == new_id and old['status'] in ('queued', 'running'):
                    old['status'] = 'superseded'
            state['jobs'][f'index:{new_id}:{version}'] = {'id': f'index:{new_id}:{version}', 'kind': 'index', 'file': new_id,
                'project': project, 'version': version, 'created': now, 'deadline': now + 7 * 86400,
                'status': 'queued', 'attempts': 0}
            return copy.deepcopy(file)
        return await self.store.transaction(save)

    async def delete_file(self, token, file_id, expected_version):
        def delete(state):
            cap = self.authorize(state, token)
            file = state['files'].get(file_id)
            if not file or file['project'] != cap['project']:
                raise DomainError('File not found.', 404)
            if file['version'] != expected_version:
                raise DomainError('This file changed. Reload before deleting.', 409)
            del state['files'][file_id]
            state['chunks'].pop(file_id, None)
            for job in state['jobs'].values():
                if job['kind'] == 'index' and job['file'] == file_id and job['status'] in ('queued', 'running'):
                    job['status'] = 'superseded'
        await self.store.transaction(delete)
