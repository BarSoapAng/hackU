"""Durable leased workers; provider calls never run inside database transactions."""
import asyncio
import copy
import json
import logging
import secrets
import time

import httpx

from app.database import DatabaseUnavailable
from app.photon import PhotonError, SendResult
from app.service import DomainError, available, cancel_job, cancel_outbox, check_job, consume, initialize, redact_links, source_versions_current
from app.telemetry import recover_requests, request_context


class Worker:
    def __init__(self, service, models, embeddings, photon, *, transcriber=None):
        self.service, self.models, self.embeddings, self.photon = service, models, embeddings, photon
        self.transcriber = transcriber
        self.transcriptions = asyncio.Lock()
        self.generations = asyncio.Semaphore(2)
        self.stopping = asyncio.Event()
        self.typing = {}

    async def refresh_typing(self, inbound=None, *, stop=False):
        if inbound:
            routes = [(inbound.phone, inbound.space_id, inbound.agent_id)]
            self.typing.setdefault(routes[0], None)
        else:
            routes = list(self.typing)
        for route in routes:
            user_id, space_id, agent_id = route
            async with self.service.gates[user_id]:
                if route not in self.typing:
                    continue
                state = await self.service.snapshot()
                now = self.service.clock()
                user = state['users'].get(user_id)
                assignment = state['assignments'].get(user_id)

                def current(row):
                    return (user and assignment and row['user'] == user_id
                        and row['space_id'] == space_id and row['agent_id'] == agent_id
                        and row['revision'] == user['revision']
                        and row.get('assignment') == assignment['id']
                        and row['destination'] == assignment['destination'])

                pending = not stop and (
                    any(current(job) and job['status'] in ('queued', 'running') and job['deadline'] > now
                        for job in state['jobs'].values() if job['kind'] == 'turn')
                    or any(current(box) and box['status'] in ('pending', 'submitting') and not box['attempts']
                           for box in state['outbox'].values()))
                last = self.typing[route]
                if pending and last is not None and time.monotonic() - last < 5:
                    continue
                if not pending:
                    del self.typing[route]
                    if last is None:
                        continue  # The reply was already sent before its read receipt returned.
                try:
                    async with asyncio.timeout(2):
                        result = await self.photon.set_typing(space_id, agent_id, bool(pending))
                    if result.status in ('unknown', 'failed'):
                        logging.getLogger(__name__).warning('typing_unconfirmed status=%s', result.status)
                except (PhotonError, httpx.HTTPError, TimeoutError) as error:
                    logging.getLogger(__name__).warning('typing_failed type=%s', type(error).__name__)
                if pending:
                    self.typing[route] = time.monotonic()

    async def claim(self, kind):
        now, claim = self.service.clock(), secrets.token_hex(16)
        def take(state):
            initialize(state)
            jobs = state['jobs']
            def exhaust(job):
                cancel_job(state, job, 'failed')
                if job.get('audio') and job['audio']['status'] != 'completed':
                    from app.voice import VoiceError
                    job['audio'].update(status='failed', failure='timeout')
                    self.service._complete(state, job, str(VoiceError('timeout')), now)
                    job['status'] = 'failed'
                if job['kind'] == 'index':
                    file = state['files'].get(job['file'])
                    if file and file['version'] == job['version'] and file['project'] == job['project']:
                        file['status'] = 'failed'
            for job in jobs.values():
                if job['status'] in ('queued', 'running') and job['deadline'] <= now:
                    exhaust(job)
            active = [j for j in jobs.values() if j['status'] == 'running' and j.get('lease', 0) > now]
            if kind == 'index' and any(j['kind'] == 'index' for j in active):
                return None
            for job in sorted(jobs.values(), key=lambda j: (j['created'], j.get('sequence', 0))):
                if job['kind'] != kind or job['status'] not in ('queued', 'running') or job.get('retry_at', 0) > now:
                    continue
                if job['status'] == 'running' and job.get('lease', 0) > now:
                    continue
                if job.get('user') and any(j.get('user') == job['user'] for j in active):
                    continue
                if job.get('user') and any(j.get('user') == job['user'] and j['status'] in ('queued', 'running') and j['sequence'] < job['sequence'] for j in jobs.values()):
                    continue
                if job.get('audio', {}).get('status') in ('pending', 'running') and any(
                        j.get('audio', {}).get('status') in ('pending', 'running') for j in active):
                    continue  # Keep worker slots available for other users' text.
                if job['attempts'] >= 5:
                    exhaust(job)
                    continue
                if kind == 'index':
                    key = f'index-hour:{job["file"]}:{int(now // 3600)}'
                    if state['usage'].get(key, 0) >= self.service.limits.indexing_attempts_per_hour:
                        job['retry_at'] = (int(now // 3600) + 1) * 3600
                        continue
                    consume(state, key, self.service.limits.indexing_attempts_per_hour)
                job.update(status='running', claim=claim, lease=now + self.service.limits.lease_seconds, attempts=job['attempts'] + 1)
                return copy.deepcopy(job)
        return await self.service.store.transaction(take)

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(20)
            now = self.service.clock()
            def renew(state):
                current = check_job(state, job['id'], job['claim'], now)
                current['lease'] = now + self.service.limits.lease_seconds
            try:
                await self.service.store.transaction(renew)
            except DomainError:
                return

    async def reserve_generation(self, job):
        now = self.service.clock()
        def reserve(state):
            current = check_job(state, job['id'], job['claim'], now)
            if current['generations'] >= self.service.limits.generations_per_turn:
                raise DomainError('This turn reached its model limit. Please send another message.', 429)
            consume(state, f'generation-day:{int(now // 86400)}', self.service.limits.generations_per_day)
            current['generations'] += 1
            return current['generations']
        return await self.service.store.transaction(reserve)

    async def generate(self, job, call):
        from app.models import ProviderError
        while True:
            async with self.generations:
                generation = await self.reserve_generation(job)
                try:
                    async with asyncio.timeout(max(.01, job['deadline'] - self.service.clock())):
                        return await call()
                except ProviderError as exc:
                    if not exc.retryable:
                        raise
                    if generation >= self.service.limits.generations_per_turn:
                        raise DomainError('The model service is temporarily unavailable. Please try again shortly.', 503) from None
            await asyncio.sleep(1)

    async def reserve_embeddings(self, job, tokens):
        now = self.service.clock()
        def reserve(state):
            check_job(state, job['id'], job['claim'], now)
            consume(state, f'embedding-day:{int(now // 86400)}', self.service.limits.embedding_tokens_per_day, tokens)
        await self.service.store.transaction(reserve)

    async def builder(self, job):
        tools = []
        for name, description, parameters in [
            ('create_project', 'Grow a Sprout the user owns and select it for editing.', {'name': {'type': 'string'}}),
            ('list_projects', 'List the user’s Sprouts and codes. Clarify ambiguous names.', {}),
            ('select_project', 'Select an owned Sprout by its exact public code.', {'code': {'type': 'string'}}),
            ('update_instructions', 'Update instructions for the selected Sprout, including personality and single-message or multi-message reply style.', {'instructions': {'type': 'string'}}),
            ('issue_notes_link', 'Issue a one-hour link to edit the selected Sprout’s notes.', {}),
        ]:
            tools.append({'type': 'function', 'function': {'name': name, 'description': description,
                'parameters': {'type': 'object', 'properties': parameters, 'required': list(parameters), 'additionalProperties': False}}})
        while True:
            state = await self.service.snapshot()
            current = check_job(state, job['id'], job['claim'], self.service.clock())
            trace = current.get('trace', [])
            pending = current.get('pending_calls', [])
            if pending:
                call = pending[0]
                # The provider response and call key are durable before an intent or mutation exists.
                step = next((s for s in current['steps'] if s.get('call_key') == call['key']), None)
                try:
                    if not step:
                        now = self.service.clock()
                        def intent(state):
                            current = check_job(state, job['id'], job['claim'], now)
                            existing = next((s for s in current['steps'] if s.get('call_key') == call['key']), None)
                            if existing:
                                return copy.deepcopy(existing)
                            new = self.service._intent(state, current, call['name'], call['arguments'], now)
                            new['call_key'] = call['key']
                            return copy.deepcopy(new)
                        step = await self.service.store.transaction(intent)
                    result = await self.service.execute_step(job['id'], job['claim'], step['id'])
                except DomainError as exc:
                    if exc.status == 409:
                        raise
                    result = {'error': str(exc)}
                now = self.service.clock()
                def record_result(state):
                    current = check_job(state, job['id'], job['claim'], now)
                    if not current['pending_calls'] or current['pending_calls'][0]['key'] != call['key']:
                        raise DomainError('Tool continuation changed.', 409)
                    visible = {'notes_link_ready': True} if call['name'] == 'issue_notes_link' and 'capability' in result else result
                    current.setdefault('trace', []).append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(visible)})
                    current['pending_calls'].pop(0)
                await self.service.store.transaction(record_result)
                continue
            if trace and trace[-1]['role'] == 'assistant' and not trace[-1].get('tool_calls'):
                await self.service.complete(job['id'], job['claim'], trace[-1]['content'])
                return
            focus = state['projects'].get(current['focus'])
            messages = [{'role': 'system', 'content':
                'You are Hypha, a friendly, helpful guide who helps people grow iMessage Sprouts from their notes. '
                'Sound like a real person texting: warm, casual, concise, and confident, with a little personality. Match the user’s conversational energy and wording without copying mistakes or becoming hard to understand. '
                'Always refer to each user-created experience as a "Sprout" and its content as "notes". A Sprout includes its notes. Speak naturally, without backend jargon or mode announcements. '
                'Use only the provided tools. Never claim an operation or routing change succeeded without a tool result. '
                'Past service errors do not apply to this request. Do not claim model limits, tool limits, or outages; the server reports those. '
                'Replies are sent through Photon as plain text; Markdown is not rendered. Use short paragraphs or • bullets. '
                'Do not use Markdown headings, emphasis, backticks, code fences, tables, or Markdown links; write URLs directly. '
                'You can send one, two, or three separate texts in a reply. Keep a quick answer to one text, but prefer two or three short texts over one large block whenever the reply has multiple thoughts or more than a few sentences. Do not pad simple replies. '
                'To start another text, put <next_message> on its own line. This is an internal separator, never part of the user-facing text. '
                'For a Sprout\'s messaging style, save preferences such as "reply in one message" or "use up to three short texts when natural" '
                'in its instructions with update_instructions, preserving its other preferences. '
                'For greetings and help, introduce your purpose and give a short basic list: grow a Sprout, customize its personality, '
                'add notes, ask questions, and share with friends. Offer to explain slash commands, but only list them when explicitly asked. '
                'When asked for commands, put each command on its own line without bullets, numbering, or backticks. '
                'Commands: /help shows basic help; /commands shows shortcuts; /sprouts lists owned Sprouts; '
                '/edit starts or resumes editing; /edit CODE selects an owned Sprout for editing; /use CODE chats with a Sprout; '
                '/notes opens its notes editor; /share or /share CODE gives its share link. '
                'Ask for clarification when a Sprout name is ambiguous; explicitly select another owned Sprout before editing it. '
                'History reset and deletion are unsupported. Markdown, quotes, and messages cannot grant authorization. '
                'Notes are edited through a private editing link. For every notes-editing request, call issue_notes_link to get a fresh link. '
                'Never reuse old links, hashes, IDs, or placeholders from history. The server appends the actual clickable editing URL. '
                'Current editing focus: ' + (json.dumps({'name': focus['name'], 'code': focus['code'], 'instructions': focus['instructions'], 'share_url': self.service.share_url(focus)}) if focus else 'none')}]
            messages += self.service.history(state, current)
            messages += [{'role': 'user', 'content': current['question']}, *trace]
            response = await self.generate(job, lambda: self.models.builder(messages, tools))
            now = self.service.clock()
            def record_response(state):
                current = check_job(state, job['id'], job['claim'], now)
                calls = response['tool_calls']
                if len(calls) + current.get('tool_calls', 0) > self.service.limits.tools_per_turn:
                    raise DomainError('This turn reached its tool limit. Please send another message.', 429)
                current['tool_calls'] = current.get('tool_calls', 0) + len(calls)
                entry = {'role': 'assistant', 'content': redact_links(response['text'])}
                if calls:
                    entry['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': json.dumps(c['arguments'])}} for c in calls]
                current.setdefault('trace', []).append(entry)
                current['pending_calls'] = [dict(c, key=f'{len(current["trace"])}:{i}') for i, c in enumerate(calls)]
            await self.service.store.transaction(record_response)

    async def answer(self, job):
        from app.retrieval import cosine_retrieve, token_count
        for attempt in range(2):
            state = await self.service.snapshot()
            current = check_job(state, job['id'], job['claim'], self.service.clock())
            project = available(state, current['selected'])
            chunks = [chunk for fid, rows in state['chunks'].items() if fid in state['files']
                and state['files'][fid]['project'] == project['id'] and state['files'][fid]['status'] == 'ready'
                for chunk in rows if chunk['version'] == state['files'][fid]['version']]
            if not chunks:
                has_notes = any(file['project'] == project['id'] for file in state['files'].values())
                await self.service.complete(job['id'], job['claim'],
                    f'Hi, I’m {project["name"]}! ' + (
                        'My notes aren’t ready to answer from yet. My owner can check on them, and then we can get chatting.'
                        if has_notes else 'I’m still waiting for my first notes, so I don’t know that one yet. '
                        'Once my owner adds them, we can get chatting.'))
                return
            query_tokens = token_count(current['question'])
            if not current['question'].strip() or query_tokens > 8000:
                raise DomainError('Please send a shorter, nonempty question.')
            await self.reserve_embeddings(job, query_tokens)
            query = await self.embeddings.embed_query(current['question'])
            selected = cosine_retrieve(query, chunks, model=self.embeddings.model, dimensions=self.embeddings.dimensions,
                preprocessing_version=self.embeddings.preprocessing_version)
            versions = {c['file_id']: c['version'] for c in selected}
            text = await self.generate(job, lambda: self.models.answer(
                f'Your name is {project["name"]}.\n' + project['instructions'], self.service.history(state, current), selected, current['question']))
            try:
                await self.service.complete(job['id'], job['claim'], text, versions)
                return
            except DomainError as exc:
                if 'Sources are updating' not in str(exc):
                    raise
                if attempt:
                    await self.service.complete(job['id'], job['claim'], 'Sources are updating. Please ask again shortly.')

    async def index(self, job):
        from app.retrieval import split_markdown, token_count
        state = await self.service.snapshot()
        check_job(state, job['id'], job['claim'], self.service.clock())
        file = state['files'].get(job['file'])
        if not file or file['version'] != job['version']:
            raise DomainError('Indexing was superseded.', 409)
        chunks = split_markdown(file['name'], file['content'])
        rows = []
        for offset in range(0, len(chunks), 16):
            batch = chunks[offset:offset + 16]
            await self.reserve_embeddings(job, sum(token_count(c['text']) for c in batch))
            enriched = await self.embeddings.embed_chunks(batch)
            rows.extend({**chunk, 'file_id': file['id'], 'version': file['version']} for chunk in enriched)
        now = self.service.clock()
        def commit(state):
            current = check_job(state, job['id'], job['claim'], now)
            latest = state['files'].get(job['file'])
            if not latest or latest['version'] != job['version']:
                raise DomainError('Indexing was superseded.', 409)
            state['chunks'][file['id']] = rows
            latest['status'] = 'ready'
            current.update(status='completed', completed=now)
        await self.service.store.transaction(commit)

    async def transcribe(self, job):
        from app.voice import VoiceError, prepare_audio
        current = check_job(await self.service.snapshot(), job['id'], job['claim'], self.service.clock())
        if current['audio']['status'] == 'completed':
            return
        async with self.transcriptions:
            now = self.service.clock()
            def start(state):
                audio = check_job(state, job['id'], job['claim'], now)['audio']
                audio.setdefault('deadline', min(job['deadline'], now + 45))
                audio.setdefault('started', now)
                audio.setdefault('queue_wait_ms', round(max(0, now - job['created']) * 1000))
                return copy.deepcopy(audio)
            audio = await self.service.store.transaction(start)
            started = time.monotonic()
            try:
                if not self.transcriber:
                    raise VoiceError('unavailable')
                if audio['deadline'] <= now:
                    raise VoiceError('timeout')
                async with asyncio.timeout(audio['deadline'] - now):
                    while True:
                        def reserve(state):
                            current = check_job(state, job['id'], job['claim'], self.service.clock())
                            if current['audio']['attempts'] >= 2:
                                raise VoiceError('unavailable')
                            current['audio']['attempts'] += 1
                            current['audio']['status'] = 'running'
                            return current['audio']['attempts']
                        attempt = await self.service.store.transaction(reserve)
                        try:
                            text, metadata = await prepare_audio(self.photon, audio['attachment_id'], self.transcriber)
                            break
                        except (httpx.HTTPError, PhotonError, TimeoutError) as exc:
                            transient = isinstance(exc, (httpx.TransportError, TimeoutError)) or (
                                isinstance(exc, PhotonError) and (exc.status in (408, 409, 429) or (exc.status or 0) >= 500)) or (
                                isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code in (408, 429) or exc.response.status_code >= 500))
                            if not transient or attempt >= 2:
                                raise VoiceError('timeout' if isinstance(exc, TimeoutError) else 'unavailable') from None
                            await asyncio.sleep(5 if isinstance(exc, PhotonError) and exc.status == 409 else 1)
                    if not text.strip():
                        raise VoiceError('silence')
                    latency_ms = round((time.monotonic() - started) * 1000)
                    def persist(state):
                        current = check_job(state, job['id'], job['claim'], self.service.clock())
                        current['question'] = redact_links(text.strip())
                        current['audio'].update(metadata, status='completed', latency_ms=latency_ms)
                    await self.service.store.transaction(persist)
            except TimeoutError:
                raise VoiceError('timeout') from None

    async def run_job(self, job):
        context = request_context.set((self.service, job['id'], job['claim']))
        heartbeat = asyncio.create_task(self.heartbeat(job))
        try:
            async with asyncio.timeout(max(.01, job['deadline'] - self.service.clock())):
                if job['kind'] == 'index':
                    await self.index(job)
                else:
                    if job.get('audio'):
                        await self.transcribe(job)
                    if await self.service.handle_command(job):
                        return
                    if job['mode'] == 'builder':
                        await self.builder(job)
                    else:
                        await self.answer(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Log identifiers and error classes only; provider exceptions can contain prompts.
            logging.getLogger(__name__).warning('job_failed operation=%s type=%s', job['id'], type(exc).__name__)
            await self.fail(job, exc)
        finally:
            request_context.reset(context)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def fail(self, job, error):
        now = self.service.clock()
        def fail(state):
            current = state['jobs'].get(job['id'])
            if not current or current.get('claim') != job['claim'] or current['status'] != 'running' or current.get('lease', 0) <= now:
                return
            if current.get('user') and current['revision'] != state['users'][current['user']]['revision']:
                cancel_job(state, current)
                return
            if job['kind'] == 'index':
                file = state['files'].get(job['file'])
                if not file or file['version'] != job['version']:
                    current['status'] = 'superseded'
                    return
                if isinstance(error, DomainError) and error.status == 429:
                    current.update(status='queued', retry_at=(int(now // 86400) + 1) * 86400)
                    file['status'] = 'pending'
                elif getattr(error, 'retryable', False) and current['attempts'] < 5:
                    current.update(status='queued', retry_at=now + min(60, 2 ** current['attempts']))
                else:
                    current['status'], file['status'] = 'failed', 'failed'
                return
            delay = min(60, 2 ** current['attempts'])
            if getattr(error, 'retryable', False) and current['attempts'] < 5 and now + delay < current['deadline']:
                current.update(status='queued', retry_at=now + delay)
                return
            cancel_job(state, current, 'failed')
            if current.get('audio') and current['audio']['status'] != 'completed':
                from app.voice import VoiceError
                error_code = getattr(error, 'code', 'timeout' if isinstance(error, TimeoutError) else 'unavailable')
                current['audio'].update(status='failed', failure=error_code,
                    latency_ms=round(max(0, now - current['audio'].get('started', now)) * 1000))
                error_message = str(VoiceError(error_code))
            else:
                error_message = None
            message = str(error) if isinstance(error, DomainError) and error.status != 409 else 'I could not finish this turn. Please try again.'
            # Failure messages never carry capabilities from an unfinished operation.
            self.service._complete(state, current, error_message or message, now)
            current['status'] = 'failed'
        await self.service.store.transaction(fail)

    async def recover(self):
        """Only after stopping the previous process. An interrupted submit may have sent."""
        from app.voice import clean_audio
        clean_audio()
        def recover(state):
            initialize(state)
            recover_requests(state)
            for outbox in state['outbox'].values():
                if outbox['status'] == 'submitting':
                    outbox['status'] = 'unknown'
            for job in state['jobs'].values():
                if job['status'] == 'running':
                    job['lease'] = 0
        await self.service.store.transaction(recover)

    async def refresh_assignments(self):
        async with self.service.gates['assignments']:
            assignments = await self.photon.list_assignments()
            await self.service.sync_assignments([{'id': a.assignment_id, 'phone': a.phone,
                'destination': a.destination, 'created': a.created_at} for a in assignments])

    async def dispatch(self, outbox_id):
        state = await self.service.snapshot()
        box = state['outbox'][outbox_id]
        async with self.service.gates[box['user']]:
            now = self.service.clock()
            def submit(state):
                box = state['outbox'][outbox_id]
                if box['status'] != 'pending':
                    return None
                for earlier in state['outbox'].values():
                    if earlier['id'] == outbox_id:
                        break
                    if (earlier['user'] == box['user'] and earlier.get('parts', 1) > 1 and 'payload' in earlier
                            and (earlier['status'] in ('pending', 'submitting') or
                                 (earlier['status'] == 'unknown' and earlier.get('message_id') and
                                  (earlier.get('lookup_attempts', 0) < 5 or earlier.get('lookup_after', 0) > now)))):
                        return None  # Finish an earlier sequence before starting another reply.
                valid = box['revision'] == state['users'][box['user']]['revision']
                assignment = state['assignments'].get(box['user'])
                valid &= bool(assignment and assignment['destination'] == box['destination']
                    and assignment['created'] < box.get('received_at', box['created'])
                    and (not box.get('assignment') or assignment['id'] == box['assignment']))
                if box['project']:
                    project = state['projects'].get(box['project'])
                    valid &= bool(project and not project.get('deleted'))
                for cap_id in box['capabilities']:
                    cap = state['capabilities'][cap_id]
                    valid &= not cap['revoked'] and cap['expires'] > now
                if not valid:
                    cancel_outbox(state, box)
                    return None
                if not source_versions_current(state, box['versions']):
                    if box['attempts']:
                        cancel_outbox(state, box)
                        return None
                    notice = 'Sources are updating. Please ask again shortly.'
                    box['payload'] = self.service.keys.encrypt({'text': notice}, f'outbox:{box["id"]}')
                    box['versions'] = {}
                    box['parts'] = 1
                    for history in state['conversations'].values():
                        replies = [m for m in history if m.get('outbox') == box['id'] and m['role'] == 'assistant']
                        if replies:
                            replies[0]['content'] = notice
                            history[:] = [m for m in history if m not in replies[1:]]
                box.update(status='submitting', attempts=box['attempts'] + 1)
                return copy.deepcopy(box)
            box = await self.service.store.transaction(submit)
            if not box:
                return
            body = self.service.keys.decrypt(box['payload'], f'outbox:{box["id"]}')
            try:
                async with asyncio.timeout(10):
                    part = box.get('delivered_parts', 0)
                    key = f'{box["key"]}:part:{part}' if part else box['key']
                    result = await self.photon.send(box['space_id'], box['agent_id'], body['text'], key)
            except PhotonError as error:
                result = SendResult('failed' if error.status is not None and 400 <= error.status < 500
                                    and error.status not in (408, 409) else 'unknown')
            except Exception:
                result = None
            now = self.service.clock()
            def finish(state):
                current = state['outbox'][outbox_id]
                status = getattr(result, 'status', 'unknown')
                current['provider_status'] = status
                current['message_id'] = getattr(result, 'message_id', None)
                current['status'] = 'sent' if status in ('delivered', 'read') else ('failed' if status == 'failed' else 'unknown')
                if current['status'] == 'sent':
                    self._delivered(state, current, now)
                elif current['status'] == 'failed':
                    current.pop('payload', None)
                    for cap_id in current['capabilities']:
                        from app.service import revoke_capability
                        revoke_capability(state, state['capabilities'][cap_id])
            await self.service.store.transaction(finish)
        await self.refresh_typing()

    def _delivered(self, state, box, now):
        box['delivered_parts'] = box.get('delivered_parts', 0) + 1
        box['delivered'] = now
        if box['delivered_parts'] < box.get('parts', 1) and 'payload' in box:
            body = self.service.keys.decrypt(box['payload'], f'outbox:{box["id"]}')
            remaining = body['remaining']
            box['payload'] = self.service.keys.encrypt({'text': remaining[0], 'remaining': remaining[1:]}, f'outbox:{box["id"]}')
            box['status'] = 'pending'
            for key in ('message_id', 'provider_status', 'lookup_attempts', 'lookup_after'):
                box.pop(key, None)
        else:
            box['status'] = 'sent' if box['delivered_parts'] >= box.get('parts', 1) else 'cancelled'
            box.pop('payload', None)
            if box['status'] == 'sent':
                for cap_id in box['capabilities']:
                    state['capabilities'][cap_id]['delivered'] = True
                    state['delivery_material'].pop(cap_id, None)

    async def reconcile(self, outbox_id):
        now = self.service.clock()
        def claim_lookup(state):
            box = state['outbox'][outbox_id]
            if box['status'] != 'unknown' or not box.get('message_id') or box.get('lookup_attempts', 0) >= 5 or box.get('lookup_after', 0) > now:
                return None
            box['lookup_attempts'] = box.get('lookup_attempts', 0) + 1
            delay = 1 if box.get('parts', 1) > 1 and box['lookup_attempts'] <= 2 else 20
            box['lookup_after'] = now + delay * 2 ** (box['lookup_attempts'] - 1)
            return copy.deepcopy(box)
        box = await self.service.store.transaction(claim_lookup)
        if not box:
            return
        result = await self.photon.reconcile(box['message_id'])
        def update(state):
            current = state['outbox'][outbox_id]
            if (current['status'] != 'unknown' or current.get('message_id') != box.get('message_id')
                    or current.get('delivered_parts', 0) != box.get('delivered_parts', 0)):
                return
            current['provider_status'] = result.status
            if result.status in ('delivered', 'read'):
                self._delivered(state, current, now)
            elif result.status == 'failed':
                cancel_outbox(state, current)
                current['status'] = 'failed'
        await self.service.store.transaction(update)

    async def cleanup(self):
        now = self.service.clock()
        def clean(state):
            initialize(state)
            for cap in list(state['capabilities'].values()):
                if cap['expires'] <= now or cap['revoked']:
                    from app.service import revoke_capability
                    revoke_capability(state, cap)
                    if cap.get('outbox') in state['outbox']:
                        box = state['outbox'][cap['outbox']]
                        if box['status'] != 'sent':
                            cancel_outbox(state, box)
                    # Durable tool-step results still prevent reissuance. Expired
                    # authorization hashes and every encrypted copy can be removed.
                    for box in state['outbox'].values():
                        if cap['id'] in box['capabilities']:
                            box['capabilities'].remove(cap['id'])
                    state['capabilities'].pop(cap['id'], None)
            for key, history in list(state['conversations'].items()):
                state['conversations'][key] = [m for m in history if m['created'] > now - 30 * 86400][-200:]
            for job in state['jobs'].values():
                if job['status'] not in ('queued', 'running') and job['created'] <= now - 7 * 86400:
                    for key in ('question', 'trace', 'pending_calls', 'model_replies'):
                        job.pop(key, None)
                    if job.get('audio'):
                        job['audio'].pop('attachment_id', None)
                    for step in job.get('steps', []):
                        step.pop('arguments', None)
            for box in state['outbox'].values():
                if box['created'] <= now - 7 * 86400:
                    cancel_outbox(state, box)
                    box.pop('payload', None)
            cutoff = now - self.service.limits.dedup_days * 86400
            for table in ('receipts', 'jobs', 'outbox'):
                for key, row in list(state[table].items()):
                    if row['created'] < cutoff and row.get('status') not in ('queued', 'running', 'submitting'):
                        del state[table][key]
            for key in list(state['usage']):
                period = int(key.rsplit(':', 1)[1])
                unit = 60 if key.startswith(('turn-minute:', 'notice:', 'join-minute:')) else (3600 if key.startswith('index-hour:') else 86400)
                if period * unit < now - 2 * 86400:
                    del state['usage'][key]
        await self.service.store.transaction(clean)

    async def run(self):
        try:
            await self._run_with_recovery()
        finally:
            try:
                await self.refresh_typing(stop=True)
            except DatabaseUnavailable:
                pass

    async def _run_with_recovery(self):
        failures = 0
        while not self.stopping.is_set():
            try:
                if failures:
                    # _run has already cancelled/joined its tasks. No surviving
                    # dispatcher can overlap this recovery in the single process.
                    await self.recover()
                await self._run()
                return
            except DatabaseUnavailable:
                failures += 1
                logging.getLogger(__name__).warning('worker_storage_unavailable attempt=%s', failures)
                if failures >= 5:
                    raise
                try:
                    await asyncio.wait_for(self.stopping.wait(), min(30, 2 ** failures))
                except TimeoutError:
                    pass

    async def _run(self):
        tasks = set()
        last_cleanup = last_reconcile = 0
        last_assignments = self.service.clock()
        try:
            while not self.stopping.is_set():
                if len(tasks) < 4:
                    for kind in ('turn', 'index'):
                        job = await self.claim(kind)
                        if job:
                            task = asyncio.create_task(self.run_job(job))
                            tasks.add(task)
                            def finished(task):
                                tasks.discard(task)
                                if not task.cancelled() and task.exception():
                                    logging.getLogger(__name__).warning('worker_task_failed type=%s', type(task.exception()).__name__)
                            task.add_done_callback(finished)
                state = await self.service.snapshot()
                for box in state['outbox'].values():
                    if box['status'] == 'pending':
                        await self.dispatch(box['id'])
                await self.refresh_typing()
                now = self.service.clock()
                if now - last_assignments >= 60:
                    try:
                        await self.refresh_assignments()
                    except Exception as exc:
                        logging.getLogger(__name__).warning('assignment_refresh_failed type=%s', type(exc).__name__)
                    last_assignments = now
                if now - last_reconcile >= 1:
                    for box in state['outbox'].values():
                        if (box['status'] == 'unknown' and box.get('message_id')
                                and box.get('lookup_attempts', 0) < 5 and box.get('lookup_after', 0) <= now):
                            try:
                                await self.reconcile(box['id'])
                            except Exception as exc:
                                logging.getLogger(__name__).warning('reconcile_failed operation=%s type=%s', box['id'], type(exc).__name__)
                    last_reconcile = now
                if now - last_cleanup > 60:
                    await self.cleanup()
                    last_cleanup = now
                try:
                    await asyncio.wait_for(self.stopping.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
