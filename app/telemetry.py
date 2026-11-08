"""Request metadata only; provider calls remain outside replayable transactions."""
import asyncio
from contextvars import ContextVar
import logging
import secrets
import time

from app.service import DomainError, check_job


request_context = ContextVar('provider_request_context', default=None)


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _usage(response, operation):
    usage = _get(response, 'usage') or _get(_get(response, 'meta', {}), 'usage', {})
    input_tokens = _count(_get(usage, 'input_tokens', _get(usage, 'prompt_tokens')))
    output_tokens = _count(_get(usage, 'output_tokens', _get(usage, 'completion_tokens')))
    if operation.endswith('_embedding') and input_tokens is not None:
        output_tokens = 0
    details = _get(usage, 'input_tokens_details') or _get(usage, 'prompt_tokens_details', {})
    cached = _count(_get(details, 'cached_tokens'))
    if cached is not None and (input_tokens is None or cached > input_tokens):
        cached = None
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cached_input_tokens': cached, 'total_tokens': _count(_get(usage, 'total_tokens'))}


async def tracked_call(call, provider, operation, model_name, **kwargs):
    """Observe one actual request, preserving the wrapped adapter's return value.

    A failed reservation prevents dispatch. A failed final metadata write never
    turns a received response into a retry: the durable pending row stays unknown.
    """
    context = request_context.get()
    if context is None:
        return await call(**kwargs)
    service, job_id, claim = context
    request_id, started = secrets.token_hex(16), service.clock()

    def reserve(state):
        job = check_job(state, job_id, claim, started)
        requests = job.setdefault('requests', [])
        # Five worker attempts; indexing batches contain at most 16 chunks.
        ceiling = ((service.limits.chunks_per_project + 15) // 16 * 5
                   if job['kind'] == 'index' else service.limits.generations_per_turn + 10)
        if len(requests) >= ceiling:
            raise DomainError('This job reached its request limit.', 429)
        project_id = (job.get('project') if job['kind'] == 'index' else
                      job.get('selected') if job.get('mode') == 'customer' else job.get('focus'))
        participant_id = job.get('user')
        account_id = (participant_id if job.get('mode') == 'builder' else
                      state['projects'].get(project_id, {}).get('owner'))
        conversation_id = (f'{participant_id}:builder' if job.get('mode') == 'builder' else
                           f'{participant_id}:project:{project_id}' if participant_id else None)
        requests.append({'id': request_id, 'sequence': len(requests) + 1,
            'provider': provider, 'operation': operation, 'model': model_name,
            'started': started, 'finished': None, 'duration_ms': None, 'status': 'pending',
            'account_id': account_id, 'project_id': project_id, 'participant_id': participant_id,
            'conversation_id': conversation_id, 'job_attempt': job.get('attempts', 0),
            'generation': job.get('generations', 0), 'input_tokens': None, 'output_tokens': None,
            'cached_input_tokens': None, 'total_tokens': None, 'provider_status': None,
            'error_type': None, 'http_status': None})

    await service.store.transaction(reserve)
    timer = time.monotonic()

    async def finish(values):
        finished, duration = service.clock(), round((time.monotonic() - timer) * 1000, 3)
        def update(state):
            # A late result records spend without restoring a superseded job or lease.
            for row in state.get('jobs', {}).get(job_id, {}).get('requests', []):
                if row['id'] == request_id and row['status'] == 'pending':
                    row.update(values, finished=finished, duration_ms=duration)
                    break
        try:
            await service.store.transaction(update)
        except Exception as exc:
            logging.getLogger(__name__).warning('request_metadata_unconfirmed request=%s type=%s',
                                               request_id, type(exc).__name__)

    try:
        response = await call(**kwargs)
    except (Exception, asyncio.CancelledError) as exc:
        status = _get(exc, 'status_code')
        status = status if type(status) is int and 100 <= status <= 599 else None
        await finish({'status': 'failed' if status is not None else 'unknown',
                      'http_status': status, 'error_type': type(exc).__name__})
        raise
    else:
        values = _usage(response, operation)
        status = _get(response, 'status')
        values.update(status='received', provider_status=status if status in
                      ('completed', 'incomplete', 'failed', 'cancelled', 'queued', 'in_progress') else None)
        await finish(values)
        return response


def recover_requests(state):
    """Called only once the previous worker has stopped; never infer zero spend."""
    for job in state.get('jobs', {}).values():
        for request in job.get('requests', []):
            if request['status'] == 'pending':
                request.update(status='unknown', error_type='Interrupted')
