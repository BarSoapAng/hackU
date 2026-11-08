"""Explicitly requested live gates only. Never collected by the test suite."""
import argparse
import asyncio
import os


async def database():
    import httpx
    from app.database import SpacetimeStore
    store = SpacetimeStore(os.environ['DATABASE_URL'], os.environ['DATABASE_NAME'], os.environ['DATABASE_TOKEN'])
    try:
        revision, state = await store.read()
        if not await store.compare_and_swap(revision, state):
            raise RuntimeError('Concurrent database change; retry with the backend stopped')
        changed, persisted = await store.read()
        if changed != revision + 1 or persisted != state:
            raise RuntimeError('Authenticated read/write round trip failed')
        unrelated = os.environ['DATABASE_UNRELATED_TOKEN']
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            base = f'{os.environ["DATABASE_URL"].rstrip("/")}/v1/database/{os.environ["DATABASE_NAME"]}/call'
            for token in ('', unrelated):
                headers = {'Authorization': f'Bearer {token}'} if token else {}
                for method, arguments in [('read_state', []), ('commit_state', [changed, __import__('json').dumps(state)])]:
                    response = await client.post(f'{base}/{method}', params={'confirmed': 'true'}, headers=headers, json=arguments)
                    if response.status_code not in (401, 403) and not (response.status_code == 530 and 'backend identity required' in response.text):
                        raise RuntimeError('Database did not prove rejection of an unauthorized identity')
        print('PASS authenticated database read/write; anonymous and unrelated reads/writes rejected')
    finally:
        await store.aclose()


async def models():
    from app.models import ModelGateway
    from app.retrieval import NemotronEmbeddings
    gateway = ModelGateway.from_credentials(nemotron_base_url=os.environ['NEMOTRON_BASE_URL'],
        nemotron_api_key=os.environ['NEMOTRON_API_KEY'], openai_api_key=os.environ['OPENAI_API_KEY'])
    embeddings = NemotronEmbeddings(os.environ['EMBEDDING_API_KEY'],
        base_url=os.environ.get('EMBEDDING_BASE_URL', 'http://127.0.0.1:8004/v1'))
    try:
        result = await gateway.builder([{'role': 'user', 'content': 'Call create_project to create a project named Demo. Do not answer without the tool call.'}],
            [{'type': 'function', 'function': {'name': 'create_project', 'description': 'Create a project',
             'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name'], 'additionalProperties': False}}}])
        if not any(c['name'] == 'create_project' and c['arguments'] == {'name': 'Demo'} for c in result['tool_calls']):
            raise RuntimeError('Nemotron did not produce the required valid tool call')
        chunks = await embeddings.embed_chunks([{'filename': 'demo.md', 'heading': 'Hours', 'text': 'The library opens at 9 AM.', 'index': 0}])
        query = await embeddings.embed_query('When does the library open?')
        if len(query) != len(chunks[0]['embedding']):
            raise RuntimeError('Embedding dimensions differ')
        answer = await gateway.answer('Be concise.', [], chunks, 'When does the library open?')
        if not answer:
            raise RuntimeError('Project model returned no answer')
        print('PASS Nemotron tool call, requested OpenAI model response at low reasoning, Nemotron document/query embeddings')
        print('This transport check does not establish real-answer grounding or two-person Photon delivery.')
    finally:
        await gateway.close()
        await embeddings.close()


async def embeddings():
    from app.retrieval import NemotronEmbeddings, cosine_retrieve
    embedder = NemotronEmbeddings(os.environ['EMBEDDING_API_KEY'],
        base_url=os.environ.get('EMBEDDING_BASE_URL', 'http://127.0.0.1:8004/v1'))
    try:
        chunks = await embedder.embed_chunks([
            {'filename': 'hours.md', 'heading': 'Hours', 'text': 'The library opens at nine AM and closes at five PM.', 'index': 0},
            {'filename': 'refunds.md', 'heading': 'Refunds', 'text': 'Refunds are available within thirty days with a receipt.', 'index': 1}])
        for question, expected in [('When does the library open?', 'hours.md'),
                                   ('How long do I have to return a purchase for a refund?', 'refunds.md')]:
            query = await embedder.embed_query(question)
            selected = cosine_retrieve(query, chunks, model=embedder.model,
                dimensions=embedder.dimensions, preprocessing_version=embedder.preprocessing_version)
            if not selected or selected[0]['filename'] != expected:
                raise RuntimeError('Synthetic retrieval ranking failed')
        print('PASS Nemotron passage/query embeddings and two synthetic retrieval rankings')
    finally:
        await embedder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gate', choices=['database', 'models', 'embeddings'])
    parser.add_argument('--live', action='store_true', help='Required explicit opt-in to real provider calls')
    args = parser.parse_args()
    if not args.live:
        parser.error('Live checks require --live and an explicitly requested verification step')
    os.environ['HAYSTACK_TELEMETRY_ENABLED'] = 'false'
    try:
        asyncio.run({'database': database, 'models': models, 'embeddings': embeddings}[args.gate]())
    except Exception as error:
        # Do not echo exceptions from providers, which can contain credentials/body text.
        parser.exit(1, f'Live gate failed ({type(error).__name__}); inspect configuration and provider status securely.\n')


if __name__ == '__main__':
    main()
