"""Client for Marvin's REST API (https://github.com/amazingmarvin/MarvinAPI/wiki/Marvin-API).

Only used for *writes*; reads come from the local mirror. Every call is
throttled to Marvin's requested rate (1 request / 3 s by default).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .models import JsonObj

BASE_URL = "https://serv.amazingmarvin.com/api"


class MarvinAPIError(RuntimeError):
    """Non-2xx response from the API."""

    def __init__(self, status: int, path: str, body: str) -> None:
        """Keep ``status`` for callers; the message carries a truncated body."""
        super().__init__(f"{status} from {path}: {body[:500]}")
        self.status = status


class MissingTokenError(RuntimeError):
    """The operation needs a token that was not configured."""


class MarvinAPI:
    """Throttled async REST client."""

    def __init__(
        self,
        *,
        api_token: str | None,
        full_access_token: str | None,
        min_interval: float = 3.0,
        client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        """Create a client; either token may be ``None`` to disable its endpoints."""
        self._api_token = api_token
        self._full_access_token = full_access_token
        self._min_interval = min_interval
        self._client = client or httpx.AsyncClient(timeout=60)
        self._base_url = base_url
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # --- plumbing -----------------------------------------------------------------
    def _headers(self, *, full: bool) -> dict[str, str]:
        if full:
            if self._full_access_token is None:
                raise MissingTokenError(
                    "MARVIN_FULL_ACCESS_TOKEN is required to edit existing documents"
                )
            return {"X-Full-Access-Token": self._full_access_token}
        if self._api_token is None:
            raise MissingTokenError("MARVIN_API_TOKEN is required for this operation")
        return {"X-API-Token": self._api_token}

    async def _request(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        json: JsonObj | None = None,
        params: dict[str, str] | None = None,
        full: bool = False,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        all_headers = self._headers(full=full) | (headers or {})
        async with self._lock:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}/{path}",
                    json=json,
                    params=params,
                    headers=all_headers,
                )
            finally:
                self._last_request = time.monotonic()
        if response.is_error:
            raise MarvinAPIError(response.status_code, path, response.text)
        return response

    # --- endpoints ----------------------------------------------------------------
    async def test(self) -> str:
        """``POST /api/test``: returns ``"OK"`` when the api token is valid."""
        return (await self._request("POST", "test")).text.strip().strip('"')

    async def me(self) -> JsonObj:
        """``GET /api/me``: account info."""
        return _as_obj((await self._request("GET", "me")).json())

    async def add_task(self, payload: JsonObj) -> JsonObj:
        """``POST /api/addTask`` with title autocompletion disabled.

        We resolve ``#project``/``@label`` ourselves, so ``X-Auto-Complete: false``
        keeps Marvin from reinterpreting titles that legitimately contain ``#``.
        """
        response = await self._request(
            "POST", "addTask", json=payload, headers={"X-Auto-Complete": "false"}
        )
        return _as_obj(response.json())

    async def add_project(self, payload: JsonObj) -> JsonObj:
        """``POST /api/addProject``."""
        response = await self._request(
            "POST", "addProject", json=payload, headers={"X-Auto-Complete": "false"}
        )
        return _as_obj(response.json())

    async def mark_done(self, item_id: str, tz_offset_minutes: int) -> JsonObj:
        """``POST /api/markDone``; ``tz_offset_minutes`` picks the completion date."""
        response = await self._request(
            "POST",
            "markDone",
            json={"itemId": item_id, "timeZoneOffset": tz_offset_minutes},
        )
        return _as_obj(response.json())

    async def get_doc(self, doc_id: str) -> JsonObj:
        """``GET /api/doc?id=`` (full access)."""
        response = await self._request("GET", "doc", params={"id": doc_id}, full=True)
        return _as_obj(response.json())

    async def update_doc(self, doc_id: str, changes: JsonObj) -> JsonObj:
        """``POST /api/doc/update`` (full access).

        Besides each field, Marvin wants ``fieldUpdates.<field>`` and ``updatedAt``
        stamped with the change time so its last-writer-wins merge resolves sync
        conflicts correctly and the UI shows the edit.
        """
        now = int(time.time() * 1000)
        setters: list[JsonObj] = []
        for key, val in changes.items():
            setters.append({"key": key, "val": val})
            setters.append({"key": f"fieldUpdates.{key}", "val": now})
        setters.append({"key": "updatedAt", "val": now})
        response = await self._request(
            "POST", "doc/update", json={"itemId": doc_id, "setters": setters}, full=True
        )
        return _as_obj(response.json())

    async def track(self, task_id: str, *, start: bool) -> JsonObj:
        """``POST /api/track`` to start or stop time tracking."""
        action = "START" if start else "STOP"
        response = await self._request(
            "POST", "track", json={"taskId": task_id, "action": action}
        )
        return _as_obj(response.json())


def _as_obj(value: object) -> JsonObj:
    if not isinstance(value, dict):
        raise MarvinAPIError(200, "?", f"expected a JSON object, got {value!r}")
    return {str(k): v for k, v in value.items()}
