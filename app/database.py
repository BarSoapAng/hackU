"""Authenticated SpacetimeDB snapshot transactions and an isolated test store.

Callbacks must be synchronous and free of side effects: CAS conflicts rerun them.
Provider calls and randomness belong outside these callbacks. An uncertain commit
raises DatabaseUnavailable; recover through the operation IDs in durable state.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import quote

import httpx


T = TypeVar('T')


class DatabaseUnavailable(RuntimeError):
    pass


class StateStore:
    async def read(self) -> tuple[int, dict[str, Any]]:
        raise NotImplementedError

    async def compare_and_swap(self, expected: int, state: dict[str, Any]) -> bool:
        raise NotImplementedError

    async def transaction(self, fn: Callable[[dict[str, Any]], T]) -> T:
        for attempt in range(8):
            revision, state = await self.read()
            result = fn(state)
            if await self.compare_and_swap(revision, state):
                return copy.deepcopy(result)
            await asyncio.sleep(min(0.005 * 2**attempt, 0.1))
        raise DatabaseUnavailable('Database is busy; retry the operation later.')


class MemoryStore(StateStore):
    """Test double only. Production startup must explicitly use SpacetimeStore."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self._state = copy.deepcopy(state) if state is not None else {}
        self._revision = 0
        self._lock = asyncio.Lock()

    async def read(self) -> tuple[int, dict[str, Any]]:
        async with self._lock:
            return self._revision, copy.deepcopy(self._state)

    async def compare_and_swap(self, expected: int, state: dict[str, Any]) -> bool:
        async with self._lock:
            if expected != self._revision:
                return False
            self._state = copy.deepcopy(state)
            self._revision += 1
            return True


class SpacetimeStore(StateStore):
    def __init__(
        self,
        base_url: str,
        database: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token.strip() or not database.strip():
            raise ValueError('Database name and explicit runtime token are required.')
        url = httpx.URL(base_url)
        if url.scheme != 'https' and not (
            url.scheme == 'http' and url.host in {'localhost', '127.0.0.1', '::1'}
        ):
            raise ValueError('SpacetimeDB requires HTTPS outside loopback.')
        if url.username or url.password or url.query or url.fragment:
            raise ValueError('SpacetimeDB base URL must not contain credentials or query parameters.')
        self._url = f'{base_url.rstrip("/")}/v1/database/{quote(database, safe="")}/call'
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, arguments: list[Any]) -> httpx.Response:
        try:
            response = await self._client.post(
                f'{self._url}/{method}',
                params={'confirmed': 'true'},
                headers={'Authorization': f'Bearer {self._token}'},
                json=arguments,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            # Never include provider bodies, snapshots, or credentials in errors.
            raise DatabaseUnavailable('Database request failed; commit outcome may be unknown.') from exc
        if response.status_code == 530 and response.text.strip() == 'state revision conflict':
            return response
        if not response.is_success:
            raise DatabaseUnavailable(f'Database request rejected (HTTP {response.status_code}).')
        return response

    async def read(self) -> tuple[int, dict[str, Any]]:
        response = await self._call('read_state', [])
        try:
            # A procedure returning t.string() is a JSON string containing JSON.
            snapshot = json.loads(response.json())
            revision = int(snapshot['revision'])
            state = snapshot['state']
            if revision < 0 or not isinstance(state, dict):
                raise ValueError('invalid snapshot')
            return revision, state
        except (ValueError, TypeError, KeyError) as exc:
            raise DatabaseUnavailable('Database returned an invalid snapshot.') from exc

    async def compare_and_swap(self, expected: int, state: dict[str, Any]) -> bool:
        value = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        response = await self._call('commit_state', [expected, value])
        return response.is_success
