"""Private, single-process recorded-file ASR service; importing starts nothing."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import io
import logging
import os
from pathlib import Path
import tempfile
import wave

from fastapi import FastAPI, HTTPException, Request


def create_app(infer, credential, *, lifespan=None, terminate=os._exit):
    if not credential:
        raise ValueError('TRANSCRIPTION_API_KEY is required')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    gate = asyncio.Lock()
    app.state.ready = False

    def authenticate(request):
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + credential).encode()):
            raise HTTPException(401, 'Unauthorized')

    @app.get('/readyz')
    async def ready(request: Request):
        authenticate(request)
        if not app.state.ready:
            raise HTTPException(503, 'Not ready')
        return {'ready': True, 'busy': gate.locked()}

    @app.post('/transcribe')
    async def transcribe(request: Request):
        authenticate(request)
        if not app.state.ready or gate.locked():
            raise HTTPException(503, 'Unavailable', headers={'Retry-After': '1'})
        if request.headers.get('content-type') != 'audio/wav':
            raise HTTPException(415, 'Send normalized WAV audio')
        async with gate:
            body = bytearray()
            try:
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 120 * 16000 * 2 + 4096:
                            raise HTTPException(413, 'Recording is too large')
                        body.extend(chunk)
            except TimeoutError:
                raise HTTPException(408, 'Upload timed out') from None
            try:
                with wave.open(io.BytesIO(body), 'rb') as audio:
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
                        raise ValueError()
                    frames = audio.getnframes()
                    if not 0 < frames <= 120 * 16000:
                        raise ValueError()
                    samples = audio.readframes(frames)
                    if len(samples) != frames * 2:
                        raise ValueError()
            except (ValueError, wave.Error, EOFError):
                raise HTTPException(422, 'Invalid normalized recording') from None
            if not any(samples):
                return {'text': '', 'language': 'en', 'duration_seconds': frames / 16000}
            path = None
            try:
                with tempfile.NamedTemporaryFile(prefix='hypha-asr-', suffix='.wav', delete=False) as file:
                    path = Path(file.name)
                    file.write(body)
                # CUDA inference cannot be cancelled safely in a Python thread.
                # Kill this separate ASR container on a hang; Compose restarts it.
                task = asyncio.create_task(asyncio.to_thread(infer, str(path)))
                try:
                    text = await asyncio.wait_for(asyncio.shield(task), 40)
                except (TimeoutError, asyncio.CancelledError):
                    app.state.ready = False
                    terminate(1)
                    raise
                if not isinstance(text, str) or len(text.encode()) > 60000:
                    raise ValueError()
                return {'text': text.strip(), 'language': 'en', 'duration_seconds': frames / 16000}
            except Exception:
                # Neither exception messages nor model output belongs in logs.
                raise HTTPException(503, 'Transcription unavailable') from None
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)

    return app


def production_app():
    model = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model
        for path in Path(tempfile.gettempdir()).glob('hypha-asr-*.wav'):
            if path.is_file():
                path.unlink()
        import torch
        from huggingface_hub import hf_hub_download
        from nemo.collections.asr.models import ASRModel
        from nemo.utils import logging as nemo_logging
        nemo_logging.set_verbosity(logging.CRITICAL)
        if not torch.cuda.is_available():
            raise RuntimeError('A CUDA GPU is required')
        checkpoint = hf_hub_download('nvidia/nemotron-speech-streaming-en-0.6b',
            filename='nemotron-speech-streaming-en-0.6b.nemo',
            revision='ebe59e5a817142986528bbbee5dba8db7b38ed50')
        # NeMo expands the multi-GB checkpoint through tempfile while loading.
        # Use model-cache disk for that work; recordings stay in /tmp tmpfs.
        with tempfile.TemporaryDirectory(prefix='restore-', dir='/cache') as scratch:
            previous = tempfile.tempdir
            try:
                tempfile.tempdir = scratch
                model = ASRModel.restore_from(checkpoint, map_location='cuda').eval()
            finally:
                tempfile.tempdir = previous
        app.state.ready = True
        yield
        app.state.ready = False

    def infer(path):
        import torch
        with torch.inference_mode():
            result = model.transcribe([path], batch_size=1, num_workers=0,
                                      return_hypotheses=False, verbose=False)[0]
        return result if isinstance(result, str) else result.text

    return create_app(infer, os.environ['TRANSCRIPTION_API_KEY'], lifespan=lifespan)
