"""FastAPI entry points. Importing this module never starts services or contacts providers."""

import asyncio
import contextlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from app.database import DatabaseUnavailable
from app.photon import WebhookError, verify_event
from app.service import DomainError


class FileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    content: StrictStr


class FileUpdate(FileWrite):
    expected_version: StrictInt


class FileDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt


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

    app = FastAPI(title="Project vault", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
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
            raise DomainError("Invalid file request. Include a filename, text, and the current version for changes.", 422) from None

    @app.get("/healthz")
    async def health():
        await service.store.read()
        task = getattr(app.state, "worker_task", None)
        if run_worker and (task is None or task.done()):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.post("/webhooks/photon")
    async def webhook(request: Request):
        raw = await bounded_body(request, 1024 * 1024)
        try:
            inbound = verify_event(raw, request.headers, webhook_secret, service.clock(), project_id=photon_project_id)
        except WebhookError:
            return JSONResponse({"detail": "Invalid webhook."}, status_code=401)
        if inbound:
            await service.admit(inbound)
        return {"received": True}

    @app.get("/vault", response_class=HTMLResponse)
    async def vault():
        nonce = secrets.token_urlsafe(24)
        html = Path(__file__).with_name("static").joinpath("vault.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__NONCE__", nonce), headers={"Content-Security-Policy":
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})

    @app.get("/api/vault/files")
    async def files(request: Request):
        return {"files": await service.list_files(bearer(request))}

    @app.post("/api/vault/files", status_code=201)
    async def create_file(request: Request):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        body = await body_model(request, FileWrite)
        return public_file(await service.save_file(token, body.name, body.content))

    @app.post("/api/vault/files/upload", status_code=201)
    async def upload_file(request: Request, name: str):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        raw = await bounded_body(request, service.limits.file_bytes)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise DomainError("Import a UTF-8 .md file.", 400) from None
        return public_file(await service.save_file(token, name, content))

    @app.get("/api/vault/files/{file_id}")
    async def read_file(request: Request, file_id: str):
        return public_file(await service.read_file(bearer(request), file_id))

    @app.put("/api/vault/files/{file_id}")
    async def update_file(request: Request, file_id: str):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        body = await body_model(request, FileUpdate)
        return public_file(await service.save_file(token, body.name, body.content, file_id, body.expected_version))

    @app.delete("/api/vault/files/{file_id}", status_code=204)
    async def delete_file(request: Request, file_id: str):
        token = bearer(request)
        service.authorize(await service.snapshot(), token)
        body = await body_model(request, FileDelete)
        await service.delete_file(token, file_id, body.expected_version)
        return Response(status_code=204)

    return app


def production_app() -> FastAPI:
    """Use one process: uvicorn app.main:production_app --factory --workers 1."""
    import httpx

    from app.config import Limits, Settings
    from app.database import SpacetimeStore
    from app.models import ModelGateway
    from app.photon import PhotonClient
    from app.retrieval import HaystackEmbeddings
    from app.security import Keyring
    from app.service import Service
    from app.worker import Worker

    settings = Settings.from_env()
    store = SpacetimeStore(settings.database_url, settings.database_name, settings.database_token)
    models = ModelGateway.from_credentials(nemotron_base_url=settings.nemotron_base_url,
        nemotron_api_key=settings.nemotron_api_key, openai_api_key=settings.openai_api_key,
        builder_model=settings.nemotron_model, project_model=settings.project_model)
    embeddings = HaystackEmbeddings(settings.openai_api_key)
    http = httpx.AsyncClient()
    photon = PhotonClient(http, settings.photon_project_id, settings.photon_api_key)
    service = Service(store, Keyring.from_json(settings.encryption_keys, settings.active_key), settings.public_url, Limits.from_env())
    app = create_app(service, Worker(service, models, embeddings, photon), settings.photon_webhook_secret,
                     settings.photon_project_id, run_worker=True)

    async def close():
        await asyncio.gather(store.aclose(), models.close(), embeddings.close(), http.aclose())
    app.state.close = close
    return app
