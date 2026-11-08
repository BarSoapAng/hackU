"""Photon beta HTTP adapter. No automatic retries of possibly accepted writes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx


class WebhookError(ValueError):
    pass


class PhotonError(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(code)  # Provider bodies can contain personal data or vault links.


@dataclass(frozen=True)
class Inbound:
    event_id: str
    phone: str
    destination: str
    text: str
    occurred_at: float
    space_id: str
    agent_id: str
    message_id: str


@dataclass(frozen=True)
class Assignment:
    assignment_id: str
    phone: str
    destination: str
    created_at: float


@dataclass(frozen=True)
class SendResult:
    status: str
    message_id: str | None = None


def normalize_phone(value: str) -> str:
    """Accept international phone numbers only; never guess a country code."""
    if not isinstance(value, str) or not re.fullmatch(r"\+[0-9 ()-]+", value):
        raise ValueError("Use an international phone number with country code.")
    normalized = re.sub(r"[ ()-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", normalized):
        raise ValueError("Use an international phone number with country code.")
    return normalized


def verify_signature(raw: bytes, headers: Mapping[str, str], secret: str, now: float) -> None:
    """Photon's Standard Webhooks signature, checked before JSON parsing."""
    values = {key.lower(): value for key, value in headers.items()}
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (ValueError, binascii.Error):
        raise WebhookError("Webhook signing configuration is invalid.") from None
    if not key:
        raise WebhookError("Webhook signing configuration is missing.")
    webhook_id = values.get("webhook-id", "")
    timestamp = values.get("webhook-timestamp", "")
    signatures = values.get("webhook-signature", "").split()
    if (not webhook_id or len(webhook_id) > 255 or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or not signatures):
        raise WebhookError("Missing or malformed webhook headers.")
    if abs(now - int(timestamp)) > 300:
        raise WebhookError("Webhook timestamp is outside the replay window.")
    signed = f"{webhook_id}.{timestamp}.".encode() + raw
    expected = b"v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    if not any(hmac.compare_digest(signature.encode(), expected) for signature in signatures):
        raise WebhookError("Invalid webhook signature.")


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires a timezone.")
    return parsed.timestamp()


def verify_event(
    raw: bytes, headers: Mapping[str, str], secret: str, now: float | None = None,
    *, project_id: str, api_version: str = "2026-07-01",
) -> Inbound | None:
    """Return supported direct inbound text, or None for verified unsupported events.

    Registration and assignment eligibility remain the transaction layer's job.
    Do not use a delivery timestamp as evidence of post-assignment receipt.
    """
    if len(raw) > 1024 * 1024:
        raise WebhookError("Webhook body is too large.")
    verify_signature(raw, headers, secret, time.time() if now is None else now)
    try:
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("apiVersion") != api_version:
            raise WebhookError("Unsupported webhook API version.")
        if event.get("type") != "message.received" or event.get("source") != "imessage.photon":
            return None
        data = event["data"]
        message = data["object"]
        if data["projectId"] != project_id or message["projectId"] != project_id:
            raise WebhookError("Webhook project mismatch.")
        if message["platform"] != "imessage" or message["status"] != "received":
            return None
        sender, receiver, space = message["sentBy"], message.get("receivedBy"), message["space"]
        if (not receiver or sender.get("kind") != "human" or receiver.get("kind") != "agent"
                or space.get("kind") != "direct"):
            return None
        if any(item.get("platform") != "imessage" or item.get("projectId") != project_id
               for item in (sender, receiver, space)):
            raise WebhookError("Webhook routing identity mismatch.")
        if (len(space["participantIds"]) != 2
                or set(space["participantIds"]) != {sender["id"], receiver["id"]}):
            return None
        content = message["content"]
        if content.get("type") != "text":
            return None  # Attachments are a later, explicitly gated extension.
        text = content["text"]
        if not isinstance(text, str) or not text.strip():
            return None
        if not all(isinstance(item, str) and item for item in
                   (event["id"], message["id"], space["id"], receiver["id"])):
            raise WebhookError("Missing webhook identifiers.")
        try:
            phone, destination = normalize_phone(sender["handle"]), normalize_phone(receiver["handle"])
        except ValueError:
            return None  # Email iMessage identities are unsupported by V1.
        return Inbound(event["id"], phone, destination, text,
                       _timestamp(message["timestamps"]["receivedAt"]),
                       space["id"], receiver["id"], message["id"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, WebhookError):
            raise
        raise WebhookError("Malformed webhook event.") from None


def plain_text(text: str) -> str:
    """Remove presentation markup while preserving literal code, URLs and citations."""
    text = text.replace('\x00', '')
    literals = []

    def keep(value):
        literals.append(value)
        return f'\x00{len(literals) - 1}\x00'

    text = re.sub(r'(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*\1[ \t]*(?=\n|$)',
                  lambda m: keep(m[2].rstrip('\n')), text)
    text = re.sub(r'(`+)([^\n]*?)\1', lambda m: keep(m[2]), text)
    definition = r'(?m)^[ \t]*\[([^\]\n]+)\]:[ \t]+(\S+)(?:[ \t]+[^\n]*)?$'
    references = {label.casefold(): url.strip('<>') for label, url in re.findall(definition, text)}
    text = re.sub(definition, '', text)
    text = re.sub(r'!?\[([^\]\n]*)\]\(<?((?:[^\s()<>]|\([^\s()]*\))+)>?(?:\s+"[^"]*")?\)',
                  lambda m: f'{m[1]} ({keep(m[2])})', text)
    text = re.sub(r'!?\[([^\]\n]+)\](?:\[([^\]\n]*)\])?',
                  lambda m: f'{m[1]} ({keep(references[(m[2] or m[1]).casefold()])})'
                  if (m[2] or m[1]).casefold() in references else m[0], text)
    text = re.sub(r'<(https?://[^\s<>]+)>', lambda m: keep(m[1]), text)
    text = re.sub(r'https?://[^\s<>\x00]*[^\s<>\x00*_~.,;:!?)\]}"]', lambda m: keep(m[0]), text)
    text = re.sub(r'(?m)^[ \t]*#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$', r'\1', text)
    text = re.sub(r'(?m)^[ \t]*(?:>[ \t]*)+', '', text)
    text = re.sub(r'(?m)^[ \t]*(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|={3,})$', '', text)
    text = re.sub(r'(?m)^([ \t]*)(?:[-+*]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?', r'\1• ', text)
    while (unmarked := re.sub(r'(?<!\w)(\*\*\*|___|\*\*|__|~~|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)', r'\2', text)) != text:
        text = unmarked
    text = re.sub(r'(?m)^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$', '', text)
    text = re.sub(r'(?m)^[ \t]*\|(.+)\|[ \t]*$', lambda m: ' — '.join(c.strip() for c in m[1].split('|')), text)
    return re.sub(r'\x00(\d+)\x00', lambda m: literals[int(m[1])], text).strip()


class PhotonClient:
    def __init__(self, client: httpx.AsyncClient, project_id: str, api_key: str,
                 base_url: str = "https://api.photon.codes"):
        self.client = client
        self.project_id = project_id
        self.base = f"{base_url.rstrip('/')}/v1/projects/{quote(project_id, safe='')}"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        # Single process/operator enrollment; provider remains the capacity authority.
        self._enrollment_gate = asyncio.Lock()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return await self.client.request(method, self.base + path, headers=headers,
                                         timeout=10, follow_redirects=False, **kwargs)

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not response.is_success:
            # Deliberately omit arbitrary provider error strings and response bodies.
            raise PhotonError(f"PHOTON_HTTP_{response.status_code}", response.status_code)

    @staticmethod
    def _result(response: httpx.Response) -> SendResult:
        try:
            data = response.json()
            status, message_id = data["status"], data["id"]
            if status not in {"dispatched", "accepted", "delivered", "read", "failed"} or not isinstance(message_id, str):
                return SendResult("unknown")
            return SendResult(status, message_id)
        except (ValueError, KeyError, TypeError):
            return SendResult("unknown")

    async def send(self, space_id: str, agent_id: str, text: str, idempotency_key: str) -> SendResult:
        text = plain_text(text)
        if not idempotency_key or not text or not space_id or not agent_id:
            raise ValueError("A send requires its persisted identity, destination and body.")
        try:
            response = await self._request("POST", "/messages", headers={"Idempotency-Key": idempotency_key},
                                          json={"spaceId": space_id, "senderId": agent_id,
                                                "content": {"type": "text", "format": "plain", "text": text}})
        except httpx.TransportError:
            return SendResult("unknown")
        if response.status_code >= 500 or response.status_code in {408, 409}:
            return SendResult("unknown")
        self._check(response)
        return self._result(response)

    async def reconcile(self, message_id: str | None) -> SendResult:
        """Read only. Unknown IDs cannot be recovered safely by searching message text."""
        if not message_id:
            return SendResult("unknown")
        try:
            response = await self._request("GET", f"/messages/{quote(message_id, safe='')}")
        except httpx.TransportError:
            return SendResult("unknown", message_id)
        if response.status_code == 404 or response.status_code >= 500:
            return SendResult("unknown", message_id)
        self._check(response)
        result = self._result(response)
        if result.message_id != message_id:
            return SendResult("unknown", message_id)
        return result

    async def react(self, space_id: str, agent_id: str, message_id: str,
                    emoji: str, action: str, idempotency_key: str) -> SendResult:
        if (not all((space_id, agent_id, message_id, idempotency_key))
                or emoji not in ('👀', '👍', '❤️') or action not in ('add', 'remove')):
            raise ValueError('A reaction requires its persisted target, identity and supported content.')
        try:
            response = await self._request('POST', '/messages',
                headers={'Idempotency-Key': idempotency_key},
                json={'spaceId': space_id, 'senderId': agent_id, 'parentMessageId': message_id,
                      'content': {'type': 'reaction', 'reaction': emoji, 'action': action}})
        except httpx.TransportError:
            return SendResult('unknown')
        if response.status_code >= 500 or response.status_code in (408, 409):
            return SendResult('unknown')
        self._check(response)
        return self._result(response)

    async def mark_read(self, message_id: str) -> SendResult:
        if not message_id:
            raise ValueError("A read receipt requires the incoming message ID.")
        try:
            response = await self._request("POST", f"/messages/{quote(message_id, safe='')}/read",
                                          headers={"Idempotency-Key": f"read:{message_id}"})
        except httpx.TransportError:
            return SendResult("unknown")
        if response.status_code >= 500 or response.status_code in {408, 409}:
            return SendResult("unknown")
        self._check(response)
        return self._result(response)

    async def _assignment_rows(self) -> list[dict]:
        rows, page, seen = [], None, set()
        for _ in range(100):
            params = {"pageSize": 100}
            if page:
                params["pageToken"] = page
            response = await self._request("GET", "/platforms/imessage/assignments", params=params)
            self._check(response)
            try:
                data = response.json()
                batch = data["assignments"]
                if not isinstance(batch, list):
                    raise ValueError()
                rows.extend(item for item in batch if not item.get("releasedAt"))
                page = data.get("nextPageToken")
                if not page:
                    return rows
                if page in seen:
                    break
                seen.add(page)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise PhotonError("PHOTON_INVALID_ASSIGNMENTS") from None
        raise PhotonError("PHOTON_ASSIGNMENT_PAGINATION_LIMIT")

    def _assignment(self, row: dict) -> Assignment:
        if row.get("projectId") != self.project_id or row.get("platform") != "imessage":
            raise PhotonError("PHOTON_ASSIGNMENT_PROJECT_MISMATCH")
        return Assignment(row["assignmentId"], normalize_phone(row["handle"]),
                          normalize_phone(row["assignedPhoneNumber"]), _timestamp(row["createdAt"]))

    def _existing(self, rows: list[dict], phone: str) -> Assignment | None:
        for row in rows:
            try:
                if normalize_phone(row.get("handle")) == phone:
                    return self._assignment(row)
            except ValueError:
                continue
        return None

    async def list_assignments(self) -> list[Assignment]:
        assignments = []
        for row in await self._assignment_rows():
            try:
                assignments.append(self._assignment(row))
            except ValueError:
                continue  # Email assignments consume seats but are outside V1 application users.
        return assignments

    async def enroll(self, phone: str, idempotency_key: str, capacity: int = 25) -> Assignment:
        """Explicit operator or share-link opt-in. Never called from webhook ingestion."""
        phone = normalize_phone(phone)
        if not idempotency_key:
            raise ValueError("Enrollment requires a stable operation idempotency key.")
        async with self._enrollment_gate:
            rows = await self._assignment_rows()
            existing = self._existing(rows, phone)
            if existing:
                return existing
            if len(rows) >= capacity:
                raise PhotonError("PHOTON_ASSIGNMENT_CAPACITY")
            try:
                response = await self._request("POST", "/platforms/imessage/assignments",
                                               headers={"Idempotency-Key": idempotency_key}, json={"handle": phone})
                if response.status_code < 500 and response.status_code not in {408, 409}:
                    self._check(response)
                    created = self._assignment(response.json())
                    if created.phone == phone:
                        return created
            except (httpx.TransportError, ValueError, KeyError, TypeError):
                pass
            # An ambiguous write may have consumed the seat. Reconcile before any retry.
            existing = self._existing(await self._assignment_rows(), phone)
            if existing:
                return existing
            raise PhotonError("PHOTON_ASSIGNMENT_UNKNOWN")
