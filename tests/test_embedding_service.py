import asyncio
import time
from unittest.mock import Mock

import httpx
import pytest

from deploy.embeddings.server import create_app


@pytest.fixture
def inference():
    return Mock(side_effect=lambda body: ([[1.0] * body.dimensions for _ in body.input], 8))


@pytest.fixture
async def client(inference):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(inference, 'private')),
                                base_url='http://embedding.test') as client:
        yield client


def payload(**changes):
    return {'model': 'nvidia/Nemotron-3-Embed-1B-BF16', 'input': ['Open at nine'],
            'input_type': 'passage', 'dimensions': 1536, **changes}


async def test_private_embedding_contract_and_readiness(client, inference):
    assert (await client.get('/readyz')).status_code == 401
    assert (await client.post('/v1/embeddings', json=payload())).status_code == 401
    inference.assert_not_called()
    client.headers['Authorization'] = 'Bearer private'
    assert (await client.get('/readyz')).json() == {'ready': True, 'busy': False}
    for mode in ('query', 'passage'):
        response = await client.post('/v1/embeddings', json=payload(input_type=mode))
        assert response.status_code == 200
        assert response.json()['data'][0]['embedding'] == [1.0] * 1536
        assert response.json()['usage'] == {'prompt_tokens': 8, 'total_tokens': 8}
        assert inference.call_args.args[0].input_type == mode


@pytest.mark.parametrize('changes', [{'model': 'other'}, {'input': []}, {'input': [' ']},
    {'input': ['a' * 8001]}, {'input': ['a'] * 33}, {'input_type': 'other'},
    {'dimensions': True}, {'dimensions': 2049}, {'truncate': 'END'}])
async def test_invalid_requests_never_reach_model(client, inference, changes):
    response = await client.post('/v1/embeddings', headers={'Authorization': 'Bearer private'}, json=payload(**changes))
    assert response.status_code == 422
    inference.assert_not_called()


async def test_errors_do_not_echo_note_text_or_model_details(client, inference):
    inference.side_effect = RuntimeError('private note and model details')
    response = await client.post('/v1/embeddings', headers={'Authorization': 'Bearer private'}, json=payload())
    assert response.status_code == 503
    assert response.json() == {'detail': 'Embedding inference failed'}


async def test_hung_inference_marks_unready_and_restarts_only_embedding_process(monkeypatch):
    timeout = asyncio.timeout
    monkeypatch.setattr('deploy.embeddings.server.asyncio.timeout', lambda seconds: timeout(.01 if seconds == 40 else seconds))
    terminate = Mock()
    app = create_app(lambda body: (time.sleep(.05), 0), 'private', terminate=terminate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://embedding.test',
                                headers={'Authorization': 'Bearer private'}) as client:
        response = await client.post('/v1/embeddings', json=payload())
        assert response.status_code == 503
        terminate.assert_called_once_with(1)
        assert (await client.get('/readyz')).status_code == 503
        await asyncio.sleep(.06)
