import asyncio
from pathlib import Path
import threading
from unittest.mock import Mock

import httpx
import pytest

from deploy.speech.server import create_app
from test_voice import recording


@pytest.mark.asyncio
async def test_private_auth_readiness_and_normalized_audio(tmp_path, monkeypatch):
    monkeypatch.setattr('tempfile.tempdir', str(tmp_path))
    def infer(path):
        assert Path(path).read_bytes() == recording()
        return 'A spoken request'
    infer = Mock(side_effect=infer)
    app = create_app(infer, 'private-key')
    headers = {'Authorization': 'Bearer private-key', 'Content-Type': 'audio/wav'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://speech') as client:
        assert (await client.get('/readyz')).status_code == 401
        assert (await client.post('/transcribe', content=recording())).status_code == 401
        assert (await client.get('/readyz', headers=headers)).status_code == 503
        app.state.ready = True
        assert (await client.get('/readyz', headers=headers)).json() == {'ready': True, 'busy': False}
        response = await client.post('/transcribe', headers=headers, content=recording())
        assert response.json() == {'text': 'A spoken request', 'language': 'en', 'duration_seconds': 1}
        for data, status in [(b'bad', 422), (recording(channels=2), 422), (recording(rate=8000), 422),
                             (recording(121), 413), (recording()[:-8], 422)]:
            assert (await client.post('/transcribe', headers=headers, content=data)).status_code == status
        assert (await client.post('/transcribe', headers=headers, content=recording(silence=True))).json()['text'] == ''
    assert infer.call_count == 1 and list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_only_one_recording_runs_and_errors_do_not_expose_content(tmp_path, monkeypatch):
    monkeypatch.setattr('tempfile.tempdir', str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    def infer(path):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('Private transcript or audio')
    app = create_app(infer, 'private-key')
    app.state.ready = True
    headers = {'Authorization': 'Bearer private-key', 'Content-Type': 'audio/wav'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://speech') as client:
        task = asyncio.create_task(client.post('/transcribe', headers=headers, content=recording()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert (await client.get('/readyz', headers=headers)).json()['busy']
            assert (await client.post('/transcribe', headers=headers, content=recording())).status_code == 503
        finally:
            release.set()
        response = await task
        assert response.status_code == 503 and 'Private' not in response.text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_hung_inference_terminates_asr_service(monkeypatch):
    # Exercise the hard timeout without a GPU, service process, or actual exit.
    terminate = Mock()
    app = create_app(lambda path: 'Hello', 'private-key', terminate=terminate)
    app.state.ready = True
    async def timeout(task, seconds):
        assert seconds == 40
        await task
        raise TimeoutError()
    monkeypatch.setattr('deploy.speech.server.asyncio.wait_for', timeout)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://speech') as client:
        result = await client.post('/transcribe', headers={'Authorization': 'Bearer private-key', 'Content-Type': 'audio/wav'}, content=recording())
        assert result.status_code == 503
    terminate.assert_called_once_with(1)
    assert not app.state.ready
