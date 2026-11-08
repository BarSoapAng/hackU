"""Private Nemotron text embeddings; importing does not load models or start services."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: Literal['nvidia/Nemotron-3-Embed-1B-BF16']
    input: list[StrictStr] = Field(min_length=1, max_length=32)
    input_type: Literal['query', 'passage']
    dimensions: int = Field(default=1536, ge=128, le=2048, strict=True)
    encoding_format: Literal['float'] = 'float'
    modality: Literal['text'] = 'text'
    truncate: Literal['NONE'] = 'NONE'


def create_app(infer, credential, *, lifespan=None, terminate=os._exit):
    if not credential:
        raise ValueError('EMBEDDING_API_KEY is required')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    gate = asyncio.Lock()
    app.state.ready = lifespan is None

    def authenticate(request):
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + credential).encode()):
            raise HTTPException(401, 'Unauthorized')

    @app.get('/readyz')
    async def ready(request: Request):
        authenticate(request)
        if not app.state.ready:
            raise HTTPException(503, 'Not ready')
        return {'ready': True, 'busy': gate.locked()}

    @app.post('/v1/embeddings')
    async def embed(request: Request):
        authenticate(request)
        if not app.state.ready or gate.locked():
            raise HTTPException(503, 'Embedding service is busy', headers={'Retry-After': '1'})
        async with gate:
            raw = bytearray()
            try:
                async with asyncio.timeout(10):
                    async for part in request.stream():
                        if len(raw) + len(part) > 32 * 8000 * 6 + 4096:
                            raise HTTPException(413, 'Request is too large')
                        raw.extend(part)
            except TimeoutError:
                raise HTTPException(408, 'Upload timed out') from None
            try:
                body = EmbeddingRequest.model_validate_json(bytes(raw))
            except ValidationError:
                raise HTTPException(422, 'Invalid embedding request') from None
            if any(not text.strip() or len(text.encode('utf-8')) > 8000 for text in body.input):
                raise HTTPException(422, 'Inputs require 1-8,000 UTF-8 bytes')
            # Keep the lock until inference really finishes, even if the client disconnects.
            task = asyncio.create_task(asyncio.to_thread(infer, body))
            try:
                async with asyncio.timeout(40):
                    vectors, tokens = await asyncio.shield(task)
            except TimeoutError:
                app.state.ready = False
                terminate(1)  # CUDA work cannot be cancelled safely; Compose restarts the process.
                raise HTTPException(503, 'Embedding inference timed out') from None
            except asyncio.CancelledError:
                app.state.ready = False
                terminate(1)
                raise
            except Exception:
                raise HTTPException(503, 'Embedding inference failed') from None
            return {'object': 'list', 'model': body.model,
                    'data': [{'object': 'embedding', 'index': i, 'embedding': vector}
                             for i, vector in enumerate(vectors)],
                    'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}

    return app


def production_app():
    from sentence_transformers import SentenceTransformer
    import torch
    import torch.nn.functional as F

    model = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model
        torch.set_num_threads(4)
        model = SentenceTransformer('nvidia/Nemotron-3-Embed-1B-BF16',
            revision='c0c9fea93ea424587517f2c59e20db9f1d6bf615', device='cuda',
            model_kwargs={'dtype': torch.bfloat16, 'attn_implementation': 'sdpa'})
        model.max_seq_length = 8192
        app.state.ready = True
        yield
        app.state.ready = False

    def infer(body):
        prompt = 'query: ' if body.input_type == 'query' else 'passage: '
        tokens = model.tokenizer([prompt + text for text in body.input], truncation=False)['input_ids']
        if any(len(row) > model.max_seq_length for row in tokens):
            raise ValueError('Input exceeds model context')
        with torch.inference_mode():
            vectors = model.encode(body.input, prompt=prompt, batch_size=1,
                                   convert_to_tensor=True, show_progress_bar=False)
            vectors = F.normalize(vectors[:, :body.dimensions].float(), p=2, dim=1)
        return vectors.tolist(), sum(map(len, tokens))

    return create_app(infer, os.environ['EMBEDDING_API_KEY'], lifespan=lifespan)
