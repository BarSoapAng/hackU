import asyncio
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

import httpx

from app.config import Limits
from app.database import DatabaseUnavailable, MemoryStore
from app.main import create_app
from app.photon import Assignment, PhotonError, SendResult
from app.security import Keyring, token_hash
from app.service import Service, initialize


class TestApi(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [1767225600.0]
        state = {}
        initialize(state)
        state["projects"] = {
            "p1": {"id": "p1", "name": "First", "code": "FIRST", "owner": "+14155550123", "instructions": "Answer from sources."},
            "p2": {"id": "p2", "name": "Second", "code": "SECOND", "owner": "+14155550124", "instructions": "Other project."},
        }
        for project, token in [("p1", "token-one"), ("p2", "token-two")]:
            state["capabilities"][project] = {"id": project, "hash": token_hash(token), "project": project,
                "expires": self.now[0] + 3600, "revoked": False, "job": "seed"}
        state["assignments"]["+14155550123"] = {"id": "assignment-1", "phone": "+14155550123",
            "destination": "+16283166196", "created": self.now[0] - 10}
        self.service = Service(MemoryStore(state), Keyring({"test": b"x" * 32}, "test"),
                               "https://example.test", Limits(), lambda: self.now[0])
        self.secret = "whsec_" + base64.b64encode(b"s" * 32).decode()
        self.photon = SimpleNamespace(mark_read=AsyncMock(return_value=SendResult("dispatched", "read-action-1")))
        self.app = create_app(self.service, SimpleNamespace(photon=self.photon), self.secret, "project")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://example.test")
        self.auth = {"Authorization": "Bearer token-one"}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def create(self, name="guide.md", content="# Guide\nA source answer.", auth=None):
        return await self.client.post("/api/vault/files", headers=self.auth if auth is None else auth,
                                      json={"name": name, "content": content})

    async def test_vault_lifecycle_atomic_index_job_and_conflicting_saves(self):
        response = await self.create()
        assert response.status_code == 201
        file = response.json()
        assert file["status"] == "pending" and file["version"] == 1
        assert "project" not in file
        state = await self.service.snapshot()
        assert f'index:{file["id"]}:1' in state["jobs"]
        read = await self.client.get("/api/vault/files/" + file["id"], headers=self.auth)
        assert read.json()["content"].startswith("# Guide")
        updated = await self.client.put("/api/vault/files/" + file["id"], headers=self.auth,
            json={"name": "guide.md", "content": "A new answer.", "expected_version": 1})
        assert updated.status_code == 200 and updated.json()["version"] == 2
        conflict = await self.client.put("/api/vault/files/" + file["id"], headers=self.auth,
            json={"name": "guide.md", "content": "Stale write.", "expected_version": 1})
        assert conflict.status_code == 409
        assert (await self.service.snapshot())["files"][file["id"]]["content"] == "A new answer."
        stale_delete = await self.client.request("DELETE", "/api/vault/files/" + file["id"], headers=self.auth,
                                                json={"expected_version": 1})
        assert stale_delete.status_code == 409
        deleted = await self.client.request("DELETE", "/api/vault/files/" + file["id"], headers=self.auth,
                                           json={"expected_version": 2})
        assert deleted.status_code == 204
        assert (await self.client.get("/api/vault/files", headers=self.auth)).json() == {"files": []}
        assert (await self.service.snapshot())["jobs"][f'index:{file["id"]}:2']["status"] == "superseded"

    async def test_capability_scope_and_header_only_authentication(self):
        other = await self.create(auth={"Authorization": "Bearer token-two"})
        assert other.status_code == 201
        path = "/api/vault/files/" + other.json()["id"]
        assert (await self.client.get(path, headers=self.auth)).status_code == 404
        assert (await self.client.put(path, headers=self.auth,
            json={"name": "guide.md", "content": "hijack", "expected_version": 1})).status_code == 404
        assert (await self.client.get("/api/vault/files?token=token-one")).status_code == 401
        assert (await self.client.get("/api/vault/files", headers={"Authorization": "Bearer invalid"})).status_code == 401

    async def test_expired_or_revoked_token_cannot_read_or_save(self):
        self.now[0] += 3600
        assert (await self.create()).status_code == 401
        assert (await self.client.get("/api/vault/files", headers=self.auth)).status_code == 401
        self.now[0] -= 3600
        await self.service.store.transaction(lambda state: state["capabilities"]["p1"].update(revoked=True))
        assert (await self.create()).status_code == 401

    async def test_deleted_project_denies_old_capability(self):
        await self.service.store.transaction(lambda state: state["projects"]["p1"].update(deleted=True))
        assert (await self.create()).status_code == 404

    async def test_upload_utf8_limit_filename_and_duplicate_rules(self):
        response = await self.client.post("/api/vault/files/upload", params={"name": "source.md"},
                                          headers=self.auth, content="# Café\nText".encode())
        assert response.status_code == 201 and response.json()["content"] == "# Café\nText"
        duplicate = await self.client.post("/api/vault/files/upload", params={"name": "source.md"},
                                           headers=self.auth, content=b"replacement")
        assert duplicate.status_code == 409
        invalid = await self.client.post("/api/vault/files/upload", params={"name": "binary.md"},
                                         headers=self.auth, content=b"\xff\xfe")
        assert invalid.status_code == 400
        oversized = await self.client.post("/api/vault/files/upload", params={"name": "large.md"},
                                           headers=self.auth, content=b"a" * (self.service.limits.file_bytes + 1))
        assert oversized.status_code == 413
        assert (await self.create("../evil.md")).status_code == 400
        assert (await self.create("image.png")).status_code == 400

    async def test_version_required_and_strict_and_management_endpoints_absent(self):
        file = (await self.create()).json()
        for body in [{"name": "guide.md", "content": "new"},
                     {"name": "guide.md", "content": "new", "expected_version": True},
                     {"name": "guide.md", "content": "new", "expected_version": 1, "project": "p2"}]:
            assert (await self.client.put("/api/vault/files/" + file["id"], headers=self.auth, json=body)).status_code == 422
        for path in ["/api/vault/settings", "/api/vault/chat", "/api/vault/reset", "/api/vault/files/" + file["id"] + "/reindex"]:
            assert (await self.client.post(path, headers=self.auth)).status_code == 404

    async def test_editor_security_headers_and_no_token_storage(self):
        response = await self.client.get("/vault")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert "__NONCE__" not in response.text
        assert "history.replaceState" in response.text and "location.hash" in response.text
        assert "localStorage" not in response.text and "sessionStorage" not in response.text
        assert ".innerHTML" not in response.text

    def delivery(self, *, event_id="event-1", text="hello", kind="human", received="2026-01-01T00:00:00Z"):
        def user(identifier, kind, phone):
            return {"id": identifier, "kind": kind, "handle": phone, "projectId": "project", "platform": "imessage"}
        body = {"id": event_id, "type": "message.received", "apiVersion": "2026-07-01", "source": "imessage.photon",
            "data": {"projectId": "project", "object": {"id": "message-1", "projectId": "project", "platform": "imessage",
                "status": "received", "sentBy": user("person", kind, "+14155550123"),
                "receivedBy": user("agent", "agent", "+16283166196"),
                "space": {"id": "space", "kind": "direct", "platform": "imessage", "projectId": "project",
                          "participantIds": ["person", "agent"]}, "content": {"type": "text", "text": text},
                "timestamps": {"receivedAt": received}}}}
        raw = json.dumps(body).encode()
        timestamp = str(int(self.now[0]))
        signature = base64.b64encode(hmac.new(b"s" * 32, b"delivery." + timestamp.encode() + b"." + raw, hashlib.sha256).digest()).decode()
        return raw, {"webhook-id": "delivery", "webhook-timestamp": timestamp, "webhook-signature": "v1," + signature}

    async def test_verified_webhook_registers_atomically_and_deduplicates_before_ack(self):
        raw, headers = self.delivery()
        for _ in range(2):
            assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        state = await self.service.snapshot()
        assert len(state["users"]) == 1
        assert len(state["jobs"]) == 1 and state["jobs"]["event-1"]["status"] == "queued"
        assert state["receipts"]["event-1"]["status"] == "accepted"
        assert state["receipts"]["event-1"]["read_receipt_attempted"]
        self.photon.mark_read.assert_awaited_once_with("message-1")
        assert state["users"]["+14155550123"]["sequence"] == 1

    async def test_concurrent_redeliveries_submit_only_one_read_receipt(self):
        raw, headers = self.delivery()
        responses = await asyncio.gather(*[
            self.client.post("/webhooks/photon", content=raw, headers=headers) for _ in range(4)])
        assert all(response.status_code == 200 for response in responses)
        self.photon.mark_read.assert_awaited_once_with("message-1")

    async def test_read_receipt_failure_preserves_reply_and_does_not_retry(self):
        self.photon.mark_read.side_effect = PhotonError("PHOTON_HTTP_403", 403)
        raw, headers = self.delivery(text="/help")
        for _ in range(2):
            assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        state = await self.service.snapshot()
        assert state["jobs"]["event-1"]["status"] == "completed"
        assert state["outbox"]["event-1"]["status"] == "pending"
        self.photon.mark_read.assert_awaited_once_with("message-1")

    async def test_unconfirmed_read_receipt_does_not_fail_the_turn(self):
        self.photon.mark_read.return_value = SendResult("unknown")
        raw, headers = self.delivery()
        assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        assert (await self.service.snapshot())["jobs"]["event-1"]["status"] == "queued"

    async def test_read_receipt_storage_failure_preserves_accepted_turn(self):
        self.service.claim_read_receipt = AsyncMock(side_effect=DatabaseUnavailable("temporary"))
        raw, headers = self.delivery()
        assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        state = await self.service.snapshot()
        assert state["jobs"]["event-1"]["status"] == "queued"
        assert state["receipts"]["event-1"]["status"] == "accepted"
        self.photon.mark_read.assert_not_awaited()

    async def test_invalid_signature_unsupported_and_preassignment_messages_create_no_user(self):
        raw, headers = self.delivery()
        assert (await self.client.post("/webhooks/photon", content=raw + b" ", headers=headers)).status_code == 401
        raw, headers = self.delivery(kind="agent")
        assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        raw, headers = self.delivery(received="2025-12-31T23:59:00Z")
        assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        state = await self.service.snapshot()
        assert not state["users"] and not state["jobs"]
        assert state["receipts"]["event-1"]["status"] == "rejected"
        self.photon.mark_read.assert_not_awaited()

    async def test_commands_do_not_wait_for_worker(self):
        raw, headers = self.delivery(text="/help")
        assert (await self.client.post("/webhooks/photon", content=raw, headers=headers)).status_code == 200
        state = await self.service.snapshot()
        assert state["jobs"]["event-1"]["status"] == "completed"
        assert state["outbox"]["event-1"]["status"] == "pending"
        self.photon.mark_read.assert_awaited_once_with("message-1")

    async def test_health_and_inprocess_lifespan_use_only_injected_doubles(self):
        assert (await self.client.get("/healthz")).json() == {"status": "ok"}
        calls = []
        async def list_assignments():
            calls.append("list")
            return [Assignment("new-assignment", "+14155550124", "+16283166222", self.now[0] - 5)]
        async def recover():
            calls.append("recover")
        async def run():
            calls.append("run")
            await asyncio.Event().wait()
        worker = SimpleNamespace(photon=SimpleNamespace(list_assignments=list_assignments), recover=recover,
                                 run=run, stopping=asyncio.Event())
        app = create_app(self.service, worker, self.secret, "project", run_worker=True)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            state = await self.service.snapshot()
            assert state["assignments"]["+14155550124"]["destination"] == "+16283166222"
            assert calls == ["list", "recover", "run"]
        assert worker.stopping.is_set() and app.state.worker_task.cancelled()
