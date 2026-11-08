"""Mocked first journey, using real routing, worker, retrieval, and model adapters."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Limits
from app.database import MemoryStore
from app.models import ModelGateway
from app.photon import Inbound, SendResult
from app.security import Keyring
from app.service import Service
from app.worker import Worker


class Embeddings:
    model = "text-embedding-3-small"
    dimensions = 2
    preprocessing_version = "markdown-v1"

    async def embed_chunks(self, chunks):
        return [dict(c, embedding=[1.0, 0.0], embedding_model=self.model, dimensions=self.dimensions,
                     preprocessing_version=self.preprocessing_version) for c in chunks]

    async def embed_query(self, text):
        return [1.0, 0.0]


async def setup(limits=None):
    service = Service(MemoryStore(), Keyring({"v1": b"x" * 32}, "v1"), "https://example.test", limits, clock=lambda: 1000)
    await service.sync_assignments([
        {"id": "assignment-owner", "phone": "+15550000001", "destination": "+15551111111", "created": 900},
        {"id": "assignment-guest", "phone": "+15550000002", "destination": "+15552222222", "created": 900},
    ])
    nemotron = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    openai = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value={"status": "completed", "output": [
        {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": "We open at nine. [hours.md > Opening]"}]}]})))
    photon = SimpleNamespace(send=AsyncMock(return_value=SendResult("delivered", "photon-message")), reconcile=AsyncMock())
    return service, Worker(service, ModelGateway(nemotron, openai), Embeddings(), photon), nemotron, openai, photon


def event(key, text, guest=False):
    return Inbound(key, "+15550000002" if guest else "+15550000001",
        "+15552222222" if guest else "+15551111111", text, 999,
        "space-guest" if guest else "space-owner", "agent-guest" if guest else "agent-owner", "msg-" + key)


def tool(name, arguments, call_id):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}


async def create_project(service, worker, nemotron):
    nemotron.chat.completions.create.side_effect = [tool("create_project", {"name": "Bakery"}, "create"),
        tool("issue_vault_link", {}, "vault"),
        {"choices": [{"finish_reason": "stop", "message": {"content": "Your Bakery project is ready."}}]}]
    await service.admit(event("owner-create", "Create Bakery and give me its vault link"))
    await worker.run_job(await worker.claim("turn"))
    state = await service.snapshot()
    assert state["jobs"]["owner-create"]["status"] == "completed"
    assert len(state["projects"]) == 1
    project = next(iter(state["projects"].values()))
    body = service.keys.decrypt(state["outbox"]["owner-create"]["payload"], "outbox:owner-create")["text"]
    token = body.split("/vault#")[1]
    return project, token


async def ready_project(service, worker, nemotron):
    project, token = await create_project(service, worker, nemotron)
    file = await service.save_file(token, "hours.md", "# Opening\nWe open at nine every morning.")
    await worker.run_job(await worker.claim("index"))
    state = await service.snapshot()
    assert state["files"][file["id"]]["status"] == "ready"
    chunk = state["chunks"][file["id"]][0]
    assert chunk["embedding"] == [1.0, 0.0]
    assert chunk["embedding_model"] == "text-embedding-3-small"
    return project, token, file


def test_full_two_user_journey_preserves_isolation_and_private_link_material():
    async def exercise():
        service, worker, nemotron, openai, photon = await setup()
        project, token, file = await ready_project(service, worker, nemotron)
        state = await service.snapshot()
        assert token not in json.dumps(state)
        assert state["delivery_material"] == {}
        assert state["users"]["+15550000001"]["focus"] == project["id"]
        assert state["jobs"]["owner-create"]["revision"] == state["users"]["+15550000001"]["revision"]
        await worker.dispatch("owner-create")
        assert "/vault#" + token in photon.send.call_args.args[2]
        assert "payload" not in (await service.snapshot())["outbox"]["owner-create"]

        await service.admit(event("guest-use", "/use " + project["code"], guest=True))
        await worker.dispatch("guest-use")
        nemotron.chat.completions.create.reset_mock()
        nemotron.chat.completions.create.side_effect = RuntimeError("Nemotron offline")
        await service.admit(event("guest-question", "When do you open?", guest=True))
        await worker.run_job(await worker.claim("turn"))
        state = await service.snapshot()
        assert state["jobs"]["guest-question"]["status"] == "completed"
        assert state["outbox"]["guest-question"]["versions"] == {file["id"]: 1}
        await worker.dispatch("guest-question")
        assert photon.send.call_args.args[0:2] == ("space-guest", "agent-guest")
        assert "[hours.md > Opening]" in photon.send.call_args.args[2]
        nemotron.chat.completions.create.assert_not_called()
        request = openai.responses.create.call_args.kwargs
        assert request["model"] == "gpt-5.6-luna" and request["reasoning"] == {"effort": "low"}
        assert "tools" not in request
        assert token not in json.dumps(request)
        assert "Create Bakery" not in json.dumps(request)
        assert state["users"]["+15550000002"]["focus"] is None
        assert state["users"]["+15550000001"]["selected"] is None
    asyncio.run(exercise())


def test_builder_generation_budget_stops_before_fifth_provider_call():
    async def exercise():
        service, worker, nemotron, openai, photon = await setup(Limits(generations_per_turn=2))
        class Temporary(Exception):
            status_code = 503
        nemotron.chat.completions.create.side_effect = Temporary("sensitive provider response")
        await service.admit(event("budget", "Please help"))
        await worker.run_job(await worker.claim("turn"))
        state = await service.snapshot()
        assert nemotron.chat.completions.create.await_count == 2
        assert state["jobs"]["budget"]["generations"] == 2
        assert state["jobs"]["budget"]["status"] == "failed"
        assert "sensitive" not in json.dumps(state)
        assert not state["projects"]
    asyncio.run(exercise())


@pytest.mark.parametrize("result", [
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "private refusal"}]}]},
    {"status": "completed", "output": []},
])
def test_project_refusal_incomplete_and_empty_take_failure_path(result):
    async def exercise():
        service, worker, nemotron, openai, photon = await setup()
        project, _, _ = await ready_project(service, worker, nemotron)
        await service.admit(event("guest-use", "/use " + project["code"], guest=True))
        openai.responses.create.return_value = result
        await service.admit(event("bad-answer", "When?", guest=True))
        await worker.run_job(await worker.claim("turn"))
        state = await service.snapshot()
        assert state["jobs"]["bad-answer"]["status"] == "failed"
        payload = service.keys.decrypt(state["outbox"]["bad-answer"]["payload"], "outbox:bad-answer")["text"]
        assert payload == "I could not finish this turn. Please try again."
        assert "private refusal" not in json.dumps(state)
        assert openai.responses.create.await_count == 1
    asyncio.run(exercise())


def test_source_change_during_answer_regenerates_once_using_current_version():
    async def exercise():
        service, worker, nemotron, openai, photon = await setup()
        project, token, file = await ready_project(service, worker, nemotron)
        await service.admit(event("guest-use", "/use " + project["code"], guest=True))
        calls = 0
        async def generate(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await service.save_file(token, "hours.md", "# Opening\nNow open at ten.", file["id"], 1)
                await worker.run_job(await worker.claim("index"))
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": ("nine" if calls == 1 else "ten") + " [hours.md > Opening]"}]}]}
        openai.responses.create.side_effect = generate
        await service.admit(event("changing-answer", "When?", guest=True))
        await worker.run_job(await worker.claim("turn"))
        state = await service.snapshot()
        assert calls == 2
        assert state["outbox"]["changing-answer"]["versions"] == {file["id"]: 2}
        payload = service.keys.decrypt(state["outbox"]["changing-answer"]["payload"], "outbox:changing-answer")["text"]
        assert payload.startswith("ten")
        assert state["jobs"]["changing-answer"]["generations"] == 2
    asyncio.run(exercise())


def test_indexing_budget_exhaustion_keeps_file_pending_and_intact():
    async def exercise():
        service, worker, nemotron, openai, photon = await setup(Limits(embedding_tokens_per_day=1))
        _, token = await create_project(service, worker, nemotron)
        file = await service.save_file(token, "hours.md", "# Opening\nWe open at nine every morning.")
        await worker.run_job(await worker.claim("index"))
        state = await service.snapshot()
        assert state["files"][file["id"]]["content"] == file["content"]
        assert state["files"][file["id"]]["status"] == "pending"
        assert file["id"] not in state["chunks"]
        assert state["jobs"]["index:" + file["id"] + ":1"]["retry_at"] > 1000
    asyncio.run(exercise())
