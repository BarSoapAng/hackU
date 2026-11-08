"""FastAPI entry points. Importing this module never starts services or contacts providers."""

import asyncio
import contextlib
import base64
import html
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from cryptography.exceptions import InvalidTag
import httpx

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from app.database import DatabaseUnavailable
from app.photon import PhotonError, WebhookError, normalize_phone, verify_event
from app.security import token_hash
from app.service import DomainError, by_code, consume


class FileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    content: StrictStr


class FileUpdate(FileWrite):
    expected_version: StrictInt


class FileDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt


class AgentConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: StrictStr = Field(min_length=1, max_length=32)


async def bounded_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise DomainError("Request is too large.", 413)
        body.extend(chunk)
    return bytes(body)


def bearer(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512 or any(c.isspace() for c in token):
        raise DomainError("Open the editing link sent in iMessage.", 401)
    return token


def public_file(file: dict) -> dict:
    return {key: file[key] for key in ("id", "name", "version", "status", "content")}


def create_app(service, worker, webhook_secret: str, photon_project_id: str, *, run_worker: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        task = None
        try:
            if run_worker:
                assignments = await worker.photon.list_assignments()
                await service.sync_assignments([{"id": a.assignment_id, "phone": a.phone,
                                                 "destination": a.destination, "created": a.created_at}
                                                for a in assignments])
                await worker.recover()
                task = asyncio.create_task(worker.run())
                app.state.worker_task = task
            yield
        finally:
            try:
                if task:
                    worker.stopping.set()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            finally:
                close = getattr(app.state, "close", None)
                if close:
                    await close()

    app = FastAPI(title="Hypha", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.exception_handler(DatabaseUnavailable)
    async def database_error(request, error):
        return JSONResponse({"detail": "Storage is temporarily unavailable. Please try again."}, status_code=503)

    async def body_model(request, model):
        try:
            # JSON escaping may multiply UTF-8 size; the service enforces actual text quotas.
            return model.model_validate_json(await bounded_body(request, service.limits.file_bytes * 6 + 4096))
        except ValidationError:
            raise DomainError("Invalid note request. Include a note name, text, and the current version for changes.", 422) from None

    @app.get("/healthz")
    async def health():
        await service.store.read()
        task = getattr(app.state, "worker_task", None)
        if run_worker and (task is None or task.done()):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    async def mark_read(inbound):
        try:
            if await service.claim_read_receipt(inbound.event_id):
                result = await worker.photon.mark_read(inbound.message_id)
                if result.status in ("unknown", "failed"):
                    logging.getLogger(__name__).warning("read_receipt_unconfirmed event=%s status=%s", inbound.event_id, result.status)
        except (DatabaseUnavailable, PhotonError) as error:
            logging.getLogger(__name__).warning("read_receipt_failed event=%s type=%s", inbound.event_id, type(error).__name__)

    @app.post("/webhooks/photon")
    async def webhook(request: Request, background_tasks: BackgroundTasks):
        raw = await bounded_body(request, 1024 * 1024)
        try:
            inbound = verify_event(raw, request.headers, webhook_secret, service.clock(), project_id=photon_project_id)
        except WebhookError:
            return JSONResponse({"detail": "Invalid webhook."}, status_code=401)
        if inbound:
            receipt = await service.admit(inbound)
            if receipt['status'] == 'accepted':
                background_tasks.add_task(mark_read, inbound)
        return {"received": True}

    @app.get("/a/{code}", response_class=HTMLResponse)
    async def shared_agent(code: str, request: Request):
        state = await service.snapshot()
        project = by_code(state, code)
        if not project:
            raise DomainError("That Sprout is no longer available.", 404)
        destination = ''
        cookie = request.cookies.get('acorn_connection', '')
        if cookie and len(cookie) < 1024:
            try:
                remembered = service.keys.decrypt(json.loads(base64.urlsafe_b64decode(cookie)), 'share-connection')
                if remembered['expires'] > service.clock():
                    destination = state['assignments'].get(remembered['phone'], {}).get('destination', '')
            except (ValueError, KeyError, TypeError, InvalidTag):
                pass
        nonce = secrets.token_urlsafe(24)
        page = Path(__file__).with_name('static').joinpath('share.html').read_text(encoding='utf-8')
        values = {'__NONCE__': nonce, '__NAME__': project['name'], '__CODE__': project['code'], '__DESTINATION__': destination}
        page = re.sub(r'__(?:NONCE|NAME|CODE|DESTINATION)__', lambda match: html.escape(values[match[0]], quote=True), page)
        return HTMLResponse(page, headers={'Content-Security-Policy':
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}' https://db.onlinewebfonts.com https://fonts.googleapis.com; "
            "font-src https://db.onlinewebfonts.com https://fonts.gstatic.com; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})

    @app.post('/api/sprouts/{code}/connect')
    async def connect_agent(code: str, request: Request):
        if request.headers.get('origin', service.public_url) != service.public_url:
            raise DomainError('Open the Sprout’s share link to connect.', 403)
        project = by_code(await service.snapshot(), code)
        if not project:
            raise DomainError('That Sprout is no longer available.', 404)
        try:
            body = AgentConnection.model_validate_json(await bounded_body(request, 1024))
            phone = normalize_phone(body.phone)
        except (ValueError, ValidationError):
            raise DomainError('Enter your iMessage number with its country code, such as +1 204 555 0123.') from None
        now = service.clock()
        def reserve(state):
            consume(state, f'join-minute:{token_hash(request.client.host)}:{int(now // 60)}', 5)
            consume(state, f'join-day:{token_hash(phone)}:{int(now // 86400)}', 10)
        await service.store.transaction(reserve)
        try:
            async with service.gates['assignments']:
                assignment = await worker.photon.enroll(phone, 'join:' + token_hash(phone))
                def remember(state):
                    state['assignments'][phone] = {'id': assignment.assignment_id, 'phone': assignment.phone,
                        'destination': assignment.destination, 'created': assignment.created_at}
                await service.store.transaction(remember)
        except (PhotonError, httpx.HTTPError) as error:
            if isinstance(error, PhotonError) and error.code == 'PHOTON_ASSIGNMENT_CAPACITY':
                raise DomainError('This demo’s texting spots are full. Ask the person who shared the Sprout to make room.', 503) from None
            raise DomainError('We couldn’t connect your number just yet. Please try again shortly.', 503) from None
        response = JSONResponse({'destination': assignment.destination, 'message': '/use ' + project['code']})
        sealed = service.keys.encrypt({'phone': phone, 'expires': now + 30 * 86400}, 'share-connection')
        response.set_cookie('acorn_connection', base64.urlsafe_b64encode(json.dumps(sealed).encode()).decode(),
                            max_age=30 * 86400, secure=True, httponly=True, samesite='lax')
        return response

    @app.get("/notes", response_class=HTMLResponse)
    async def notes():
        nonce = secrets.token_urlsafe(24)
        html = Path(__file__).with_name("static").joinpath("vault.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__NONCE__", nonce), headers={"Content-Security-Policy":
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})

    @app.get("/api/notes")
    async def files(request: Request):
        return {"notes": await service.list_files(bearer(request))}

    @app.post("/api/notes", status_code=201)
    async def create_file(request: Request):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        body = await body_model(request, FileWrite)
        return public_file(await service.save_file(token, body.name, body.content))

    @app.post("/api/notes/upload", status_code=201)
    async def upload_file(request: Request, name: str):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        raw = await bounded_body(request, service.limits.file_bytes)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise DomainError("Import a UTF-8 .md note.", 400) from None
        return public_file(await service.save_file(token, name, content))

    @app.get("/api/notes/{file_id}")
    async def read_file(request: Request, file_id: str):
        return public_file(await service.read_file(bearer(request), file_id))

    @app.put("/api/notes/{file_id}")
    async def update_file(request: Request, file_id: str):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        body = await body_model(request, FileUpdate)
        return public_file(await service.save_file(token, body.name, body.content, file_id, body.expected_version))

    @app.delete("/api/notes/{file_id}", status_code=204)
    async def delete_file(request: Request, file_id: str):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        body = await body_model(request, FileDelete)
        await service.delete_file(token, file_id, body.expected_version)
        return Response(status_code=204)

    return app


def production_app() -> FastAPI:
    """Use one process: uvicorn app.main:production_app --factory --workers 1."""
    import os
    import httpx

    os.environ.setdefault('HAYSTACK_TELEMETRY_ENABLED', 'false')

    from app.config import Limits, Settings
    from app.database import SpacetimeStore
    from app.models import ModelGateway
    from app.photon import PhotonClient
    from app.retrieval import HaystackEmbeddings
    from app.security import Keyring
    from app.service import Service
    from app.worker import Worker
    from app.voice import Transcriber

    settings = Settings.from_env()
    store = SpacetimeStore(settings.database_url, settings.database_name, settings.database_token)
    models = ModelGateway.from_credentials(nemotron_base_url=settings.nemotron_base_url,
        nemotron_api_key=settings.nemotron_api_key, openai_api_key=settings.openai_api_key,
        builder_model=settings.nemotron_model, project_model=settings.project_model)
    embeddings = HaystackEmbeddings(settings.openai_api_key)
    http = httpx.AsyncClient()
    photon = PhotonClient(http, settings.photon_project_id, settings.photon_api_key)
    service = Service(store, Keyring.from_json(settings.encryption_keys, settings.active_key), settings.public_url, Limits.from_env())
    transcriber = Transcriber(http, settings.transcription_url, settings.transcription_api_key) if settings.voice_enabled else None
    app = create_app(service, Worker(service, models, embeddings, photon, transcriber=transcriber), settings.photon_webhook_secret,
                     settings.photon_project_id, run_worker=True)

    async def close():
        await asyncio.gather(store.aclose(), models.close(), embeddings.close(), http.aclose())
    app.state.close = close
    return app
