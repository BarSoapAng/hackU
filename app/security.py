import base64
import hashlib
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Keyring:
    """Keys live in deployment secrets, never in application state."""

    def __init__(self, keys: dict[str, bytes], active: str):
        if active not in keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError('Each delivery encryption key must contain 32 bytes')
        self.keys = keys
        self.active = active

    @classmethod
    def from_json(cls, value: str, active: str):
        return cls({name: base64.urlsafe_b64decode(key) for name, key in json.loads(value).items()}, active)

    def encrypt(self, value: dict, context: str) -> dict:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.keys[self.active]).encrypt(nonce, json.dumps(value).encode(), context.encode())
        return {'key': self.active, 'data': base64.urlsafe_b64encode(nonce + ciphertext).decode()}

    def decrypt(self, value: dict, context: str) -> dict:
        raw = base64.urlsafe_b64decode(value['data'])
        return json.loads(AESGCM(self.keys[value['key']]).decrypt(raw[:12], raw[12:], context.encode()))
