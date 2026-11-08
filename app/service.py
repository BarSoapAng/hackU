"""Transactional application operations. Callbacks are pure and may be replayed by CAS."""
import asyncio
import copy
import re
import secrets
import time
from collections import defaultdict
from urllib.parse import quote

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
    return re.sub(r'https?://[^\s<>]*?/(?:notes|vault)#[^\s<>]+', '[editing link omitted]', text)


def is_greeting(text):
    return bool(re.fullmatch(r'h+i+|he+y+|hello+|hi+ there|hey there', text.casefold().strip(' .!?')))


def chat_command(text):
    if text.startswith('/'):
        return text
    query = text.casefold().strip(' .!?')
    if is_greeting(text) or query in (
            'help', 'what can you do', 'how does this work', 'what are you here for'):
        return '/help'
    if query in ('commands', 'slash commands', 'slash command') or re.fullmatch(
            r'(?:what|show|list|tell|explain|help).*\bslash commands?\b.*', query):
        return '/commands'
    if query in ('notes', 'my notes', 'open my notes', 'edit my notes', 'notes link',
                 'send me my notes link', 'give me my notes link', 'open my sprout'):
        return '/notes'
    if query in ('share', 'share sprout', 'share my sprout', 'share this sprout', 'share link',
                 'how do i share my sprout', 'how can i share my sprout'):
        return '/share'
    return None


def deterministic_input(text):
    return bool(chat_command(text)) or bool(re.search(
        r'\b(reset|clear|erase|forget|delete)\b.*\b(history|conversation|chat|memory)\b', text, re.I))


def available(state, project_id):
    project = state['projects'].get(project_id)
    if not project or project.get('deleted'):
        raise DomainError('Sprout is unavailable.', 404)
    return project


def owned(state, user_id, project_id):
    project = available(state, project_id)
    if project['owner'] != user_id:
        raise DomainError('Select a Sprout you own.', 403)
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


def split_messages(text):
    parts = [part.strip() for part in text.split('<next_message>') if part.strip()]
    # Keep excess content in the final bubble rather than dropping it.
    return parts[:2] + ['\n\n'.join(parts[2:])] if len(parts) > 3 else (parts or [''])


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
        if outbox.get('reaction', {}).get('action') == 'remove':
            continue  # Cleanup targets the original message even after navigation.
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
        assignment = state['assignments'].get(job['user'])
        if not assignment or assignment['id'] != job.get('assignment') or assignment['destination'] != job['destination'] or assignment['created'] >= job['received_at']:
            raise DomainError('This assignment is no longer current.', 409)
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
            prepared = self._prepare_step('issue_vault_link', event.event_id, f'{event.event_id}:0', user['focus'], now) if user and (chat_command(event.text.strip()) or '').lower() == '/notes' else None
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
                deterministic = not event.attachment_id and deterministic_input(text)
                waiting_for_voice = any(j['user'] == user['id'] and j.get('audio') for j in pending)
                navigation = text.split(maxsplit=1)[0].lower() in ('/use', '/edit') if text else False
                queued = not deterministic or (waiting_for_voice and not navigation)
                if not deterministic and state['usage'].get(f'generation-day:{int(now // 86400)}', 0) >= self.limits.generations_per_day:
                    receipt['reason'] = 'The generation limit has been reached. Please try again tomorrow.'
                    self._notice(state, user, event, receipt['reason'], now)
                    return receipt
                # Navigation must remain available when model queues are full.
                if queued and (len(pending) >= self.limits.pending_total or sum(j['user'] == user['id'] for j in pending) >= self.limits.pending_per_user):
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
                    'destination': event.destination, 'assignment': assignment['id'], 'received_at': event.occurred_at,
                    'message_id': event.message_id}
                state['jobs'][job['id']] = job
                if event.attachment_id:
                    job['audio'] = {'attachment_id': event.attachment_id, 'status': 'pending', 'attempts': 0}
                # Explicit navigation still cancels obsolete work immediately. Other
                # commands wait behind a voice note just like ordinary text.
                if not queued:
                    job.update(status='running', claim='command', lease=now + self.limits.lease_seconds)
                    answer = self._command(state, user, job, now, prepared)
                    self._complete(state, job, answer, now)
                return copy.deepcopy(receipt)
            return await self.store.transaction(accept)

    async def handle_command(self, job):
        """Handle a queued text/transcript in its original turn, including notes-editing links."""
        async with self.gates[job['user']]:
            now = self.clock()
            current = check_job(await self.snapshot(), job['id'], job['claim'], now)
            if not deterministic_input(current['question']):
                return False
            prepared = self._prepare_step('issue_vault_link', job['id'], f'{job["id"]}:0', current['focus'], now) if (chat_command(current['question']) or '').lower() == '/notes' else None
            def complete(state):
                current = check_job(state, job['id'], job['claim'], now)
                answer = self._command(state, state['users'][current['user']], current, now, prepared)
                self._complete(state, current, answer, now)
            await self.store.transaction(complete)
            return True

    async def claim_read_receipt(self, event_id):
        def claim(state):
            receipt = state['receipts'].get(event_id)
            if not receipt or receipt['status'] != 'accepted' or receipt.get('read_receipt_attempted'):
                return False
            receipt['read_receipt_attempted'] = True
            return True
        return await self.store.transaction(claim)

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
        parts = (chat_command(job['question']) or job['question']).split()
        command = parts[0].lower() if parts else ''
        try:
            if command == '/edit' and len(parts) == 1:
                route(state, user, mode='builder', origin=job['id'])
                project = state['projects'].get(user['focus'])
                return f'Let’s tend to {project["name"]}! What would you like to change?' if project else 'Let’s grow a new Sprout! What should it help people with?'
            if command in ('/edit', '/use') and len(parts) == 2:
                project = by_code(state, parts[1].rstrip('.,!'))
                if not project:
                    raise DomainError('Unknown Sprout code.')
                if command == '/edit':
                    owned(state, user['id'], project['id'])
                    route(state, user, mode='builder', focus=project['id'], origin=job['id'])
                else:
                    route(state, user, mode='customer', selected=project['id'], origin=job['id'])
                return (f'Let’s tend to {project["name"]}! Tell me what to change, or ask to edit its notes.' if command == '/edit'
                        else f'You’re chatting with {project["name"]} now! What would you like to know?')
            if command == '/sprouts' and len(parts) == 1:
                projects = [p for p in state['projects'].values() if p['owner'] == user['id'] and not p.get('deleted')]
                return '\n'.join(f'{p["name"]}: {p["code"]}' for p in projects) or 'No Sprouts in your little grove yet. Tell me what you’d like to grow!'
            if command == '/notes' and len(parts) == 1:
                owned(state, user['id'], user['focus'])
                step = self._intent(state, job, 'issue_vault_link', {}, now)
                self._execute(state, job, step, now, prepared)
                return 'Your Sprout is ready for tending! Add or edit its notes with this private link, which lasts one hour.'
            if command == '/share' and len(parts) in (1, 2):
                project = by_code(state, parts[1].rstrip('.,!')) if len(parts) == 2 else available(
                    state, user['selected'] if user['mode'] == 'customer' else user['focus'])
                if not project:
                    raise DomainError('I couldn’t find that Sprout. Check the code and try again.')
                return f'Send this link to a friend to chat with {project["name"]}:\n{self.share_url(project)}'
            if command == '/help' and len(parts) == 1:
                if user['mode'] == 'customer' and is_greeting(job['question']):
                    project = available(state, user['selected'])
                    return (f'Hi, I’m {project["name"]}! Nice to meet you.<next_message>'
                            'I’m here to help with the notes my owner shares with me. What are you curious about?<next_message>'
                            'Say “help” if you want to learn about growing or sharing Sprouts of your own.')
                return ('Hey, I’m Hypha. I help you grow iMessage Sprouts from your notes.<next_message>'
                        'Here’s what we can do together:\n'
                        'Grow a Sprout and give it a personality\n'
                        'Add or update its notes\n'
                        'Ask your Sprout questions about those notes\n'
                        'Share your Sprout with a friend<next_message>'
                        'You can send me a voice note, too.\n'
                        'What would you like to make? You can also ask me about slash commands for shortcuts.')
            if (command == '/commands' and len(parts) == 1) or (command == '/help' and parts[1:] == ['commands']):
                return ('Here are your shortcuts:\n\n'
                        '/help — what I can help with\n'
                        '/commands — these shortcuts\n'
                        '/sprouts — your Sprouts and their codes\n'
                        '/edit — grow a Sprout or return to editing\n'
                        '/edit CODE — edit one of your Sprouts\n'
                        '/use CODE — chat with a Sprout\n'
                        '/notes — add or edit a Sprout’s notes\n'
                        '/share — get a link to share this Sprout\n'
                        '/share CODE — get a link for a specific Sprout')
            if not job['question'].startswith('/') or command in ('/reset', '/clear'):
                return 'History reset is unsupported. Your conversation has not changed.'
            return 'I don’t know that shortcut. Ask me about slash commands to see the list.'
        except DomainError as exc:
            return str(exc)

    def share_url(self, project):
        return f'{self.public_url}/a/{quote(project["code"], safe="")}'

    def _intent(self, state, job, name, arguments, now):
        if len(job['steps']) >= self.limits.tools_per_turn:
            raise DomainError('This turn reached its tool limit. Please send another message.')
        schemas = {'create_project': {'name': str}, 'list_projects': {}, 'select_project': {'code': str},
                   'update_instructions': {'instructions': str}, 'set_reactions': {'enabled': bool}, 'issue_vault_link': {}}
        if name not in schemas or set(arguments) != set(schemas[name]) or any(type(arguments[k]) is not t for k, t in schemas.get(name, {}).items()):
            raise DomainError('Unsupported tool or invalid arguments.')
        if any(not v.strip() or len(v.encode()) > (4096 if k == 'instructions' else 120) for k, v in arguments.items() if isinstance(v, str)):
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
                raise DomainError('Sprout limit reached.', 429)
            project_id, code = prepared['id'], prepared['code']
            if project_id in state['projects'] or any(p['code'] == code for p in state['projects'].values()):
                raise DomainError('Sprout code allocation conflicted. Please try again.', 409)
            project = {'id': project_id, 'owner': user['id'], 'name': args['name'].strip(), 'code': code,
                'instructions': ('Be a friendly, curious little guide to the notes your owner shares. Talk like a helpful person, '
                    'with a little warmth and occasional light humor, not a documentation page. Keep answers short and useful. '
                    'If you don’t know something from your notes, say so kindly instead of guessing. '
                    'Support factual answers with note name and heading citations.'), 'created': now, 'reactions_enabled': False}
            state['projects'][project_id] = project
            route(state, user, mode='builder', focus=project_id, origin=job['id'])
            result = {'id': project_id, 'name': project['name'], 'code': code}
        elif name == 'list_projects':
            result = {'projects': [{'name': p['name'], 'code': p['code']} for p in state['projects'].values() if p['owner'] == user['id'] and not p.get('deleted')]}
        elif name == 'select_project':
            project = by_code(state, args['code'])
            if not project:
                raise DomainError('Unknown Sprout code.')
            owned(state, user['id'], project['id'])
            route(state, user, mode='builder', focus=project['id'], origin=job['id'])
            result = {'id': project['id'], 'name': project['name'], 'code': project['code']}
        elif name == 'set_reactions':
            project = owned(state, user['id'], step['target'])
            project['reactions_enabled'] = args['enabled']
            result = {'reactions_enabled': args['enabled']}
        elif name == 'update_instructions':
            project = owned(state, user['id'], step['target'])
            project['instructions'] = args['instructions']
            result = {'updated': True}
        else:
            project = owned(state, user['id'], step['target'])
            if not prepared:
                raise DomainError('Select a Sprout you own before requesting an editing link.')
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
        messages = split_messages(text)
        outbox = {'id': job['id'], 'user': job['user'], 'revision': job['revision'], 'created': now,
                  'status': 'pending', 'key': job['id'], 'space_id': job['space_id'], 'agent_id': job['agent_id'],
                  'destination': job['destination'], 'project': job['selected'] if job['mode'] == 'customer' else job['focus'],
                  'capabilities': caps, 'versions': versions, 'attempts': 0,
                  'parts': len(messages), 'delivered_parts': 0,
                  'assignment': job.get('assignment'), 'received_at': job.get('received_at', job['created'])}
        outbox['payload'] = self.keys.encrypt({'text': messages[0], 'remaining': messages[1:]}, f'outbox:{job["id"]}')
        state['outbox'][job['id']] = outbox

    def _complete(self, state, job, text, now, versions=None):
        text = redact_links(text)
        for step in job.get('steps', []):
            if step['name'] == 'create_project' and 'result' in step:
                text += f'\nSprout code: {step["result"]["code"]}\nShare your Sprout:\n{self.share_url(step["result"])}'
            if step['name'] == 'issue_vault_link' and 'capability' in step.get('result', {}):
                text = text.replace(step['result']['capability'], '')
        text = '<next_message>'.join(split_messages(text))
        versions = versions or {}
        if not source_versions_current(state, versions):
            raise DomainError('Sources are updating. Please ask again shortly.', 409)
        if job.get('status') == 'running':
            for step in job.get('steps', []):
                cap_id = step.get('result', {}).get('capability')
                if cap_id and (cap_id not in state['capabilities'] or state['capabilities'][cap_id]['revoked']):
                    raise DomainError('The editing link is no longer available. Request /notes again.', 409)
        caps, links = [], []
        for cap in state['capabilities'].values():
            if cap['job'] == job['id'] and not cap['revoked']:
                if cap['expires'] <= now:
                    raise DomainError('The editing link expired. Request /notes again.', 409)
                material = self.keys.decrypt(state['delivery_material'][cap['id']], f'cap:{cap["id"]}:{cap["project"]}:{cap["step"]}')
                links.append(f'Edit your Sprout’s notes:\n{self.public_url}/notes#{material["token"]}')
                caps.append(cap['id'])
        self._outbox(state, job, '\n'.join([text, *links]), caps, versions, now)
        for cap_id in caps:
            state['delivery_material'].pop(cap_id, None)
            state['capabilities'][cap_id]['outbox'] = job['id']
        conversation = f'{job["user"]}:builder' if job['mode'] == 'builder' else f'{job["user"]}:project:{job["selected"]}'
        history = state['conversations'].setdefault(conversation, [])
        if job['question']:
            history.append({'role': 'user', 'content': job['question'], 'created': now})
        messages = split_messages(text)
        history.extend({'role': 'assistant', 'content': part + ('\n[notes editing link issued]' if caps and index == len(messages) - 1 else ''),
                        'created': now, 'outbox': job['id'], 'part': index} for index, part in enumerate(messages))
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
                if m['created'] > self.clock() - 30 * 86400 and (m['role'] != 'assistant' or (
                    (state['outbox'].get(m.get('outbox'), {}).get('status') == 'sent'
                     or m.get('part', 0) < state['outbox'].get(m.get('outbox'), {}).get('delivered_parts', 0))
                    and state['jobs'].get(m.get('outbox'), {}).get('status') != 'failed'
                    and not ('vault' in m['content'].lower() and re.search(r'\b[0-9a-f]{32}\b', m['content']))
                    # Exclude legacy model echoes too, without deleting the conversation audit.
                    and m['content'].strip() not in (
                        'This turn reached its model limit. Please send another message.',
                        'This turn reached its tool limit. Please send another message.')))][-40:]

    def authorize(self, state, token):
        hashed = token_hash(token)
        cap = next((c for c in state['capabilities'].values() if secrets.compare_digest(c['hash'], hashed)), None)
        if not cap or cap['revoked'] or cap['expires'] <= self.clock():
            raise DomainError('This editing link is invalid or expired. Request /notes in iMessage.', 401)
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
            raise DomainError('Note not found.', 404)
        return file

    async def save_file(self, token, name, content, file_id=None, expected_version=None):
        from app.retrieval import split_markdown
        if not name or len(name) > 120 or not name.lower().endswith('.md') or any(c in name for c in '/\\\x00\r\n'):
            raise DomainError('Choose a simple .md note name.')
        if '\x00' in content or len(content.encode('utf-8')) > self.limits.file_bytes:
            raise DomainError('A note must be UTF-8 Markdown within the size limit.', 413)
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
                raise DomainError('Note not found.', 404)
            if current and current['version'] != expected_version:
                raise DomainError('This note changed. Reload before saving.', 409)
            if any(f['name'].casefold() == name.casefold() and f['id'] != file_id for f in files):
                raise DomainError('A note with this name exists. Open it to explicitly replace its contents.', 409)
            others = [f for f in files if f['id'] != file_id]
            if len(others) + 1 > self.limits.files_per_project or sum(f['bytes'] for f in others) + len(content.encode()) > self.limits.project_bytes or sum(f['chunk_count'] for f in others) + len(chunks) > self.limits.chunks_per_project:
                raise DomainError('Sprout notes limit exceeded.', 413)
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
                raise DomainError('Note not found.', 404)
            if file['version'] != expected_version:
                raise DomainError('This note changed. Reload before deleting.', 409)
            del state['files'][file_id]
            state['chunks'].pop(file_id, None)
            for job in state['jobs'].values():
                if job['kind'] == 'index' and job['file'] == file_id and job['status'] in ('queued', 'running'):
                    job['status'] = 'superseded'
        await self.store.transaction(delete)
