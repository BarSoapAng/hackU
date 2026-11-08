import asyncio
import re

import pytest

from app.photon import plain_text
from test_service import create_project, environment, inbound


def reply(env, state, operation):
    return env.service.keys.decrypt(state['outbox'][operation]['payload'], 'outbox:' + operation)['text']


@pytest.mark.parametrize('message', ['hi', 'hiii!', 'hello', 'help', '/help', 'what can you do?'])
def test_greetings_and_help_explain_capabilities_without_dumping_commands(message):
    async def scenario():
        env = await environment()
        await env.service.admit(inbound(env, 'welcome', message))
        state = await env.service.snapshot()
        body = reply(env, state, 'welcome')
        assert 'Acorn' in body and 'Create an agent' in body and 'Share it with a friend' in body
        assert 'ask me about slash commands' in body
        assert not re.search(r'/[a-z]+', body)
        assert state['jobs']['welcome']['status'] == 'completed'
        env.models.builder.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('message', ['/commands', '/help commands', 'what slash commands can I use?', 'slash commands'])
def test_command_list_is_explicit_and_has_one_command_per_line(message):
    async def scenario():
        env = await environment()
        await env.service.admit(inbound(env, 'commands', message))
        body = reply(env, await env.service.snapshot(), 'commands')
        assert '/builder' not in body
        commands = [line for line in body.splitlines() if line.startswith('/')]
        assert len(commands) == 9
        assert any(line.startswith('/edit CODE ') for line in commands)
        assert not re.search(r'(?m)^[•*\-]|`', body)
    asyncio.run(scenario())


def test_edit_switches_modes_and_builder_is_no_longer_a_command():
    async def scenario():
        env = await environment()
        job, project, _ = await create_project(env)
        await env.service.complete(job['id'], job['claim'], 'Created')
        await env.service.admit(inbound(env, 'use', '/use ' + project['code'] + '.'))
        await env.service.admit(inbound(env, 'agent-hello', 'hiii'))
        greeting = reply(env, await env.service.snapshot(), 'agent-hello')
        assert f'I’m {project["name"]}' in greeting and 'I’m Acorn' not in greeting
        await env.service.admit(inbound(env, 'old-command', '/builder'))
        state = await env.service.snapshot()
        assert state['users'][job['user']]['mode'] == 'customer'
        assert 'don’t know that shortcut' in reply(env, state, 'old-command')
        await env.service.admit(inbound(env, 'edit', '/edit ' + project['code']))
        user = (await env.service.snapshot())['users'][job['user']]
        assert user['mode'] == 'builder' and user['focus'] == project['id']
    asyncio.run(scenario())


def test_natural_vault_request_issues_usable_private_url_and_share_stays_public():
    async def scenario():
        env = await environment()
        job, project, _ = await create_project(env)
        await env.service.complete(job['id'], job['claim'], 'Created')
        created = reply(env, await env.service.snapshot(), job['id'])
        assert f'https://vault.example/a/{project["code"]}' in created
        await env.service.admit(inbound(env, 'vault', 'my vault'))
        state = await env.service.snapshot()
        body = plain_text(reply(env, state, 'vault'))
        url = next(line for line in body.splitlines() if line.startswith('https://'))
        assert url.startswith('https://vault.example/vault#')
        token = url.split('#', 1)[1]
        cap = env.service.authorize(state, token)
        assert cap['project'] == project['id'] and cap['id'] not in body
        assert 'Open your vault:' in body
        await env.service.admit(inbound(env, 'share', 'share my agent'))
        shared = reply(env, await env.service.snapshot(), 'share')
        assert f'https://vault.example/a/{project["code"]}' in shared
        assert token not in shared and '/vault#' not in shared
        env.models.builder.assert_not_called()
    asyncio.run(scenario())
