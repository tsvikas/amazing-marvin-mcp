"""Read-only client for Marvin's CouchDB (Cloudant) sync database.

Marvin's own apps replicate from this database with PouchDB, so the standard
``_changes`` feed is what we use too: ``since=0`` gives the full database, a
later ``since=<last_seq>`` gives only what changed. One request either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from .api import transient_retry
from .errors import MarvinError

if TYPE_CHECKING:
    from .models import JsonObj


@dataclass(slots=True)
class Changes:
    """Result of one ``_changes`` poll."""

    last_seq: str
    docs: list[JsonObj] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)


class CouchError(MarvinError):
    """Non-2xx response from the database."""

    def __init__(self, status: int, body: str) -> None:
        """Keep ``status`` so transient failures (429/5xx) can be retried."""
        super().__init__(f"{status} from _changes: {body[:500]}")
        self.status = status


class CouchClient:
    """Minimal async CouchDB client bound to one database."""

    def __init__(
        self,
        server: str,
        database: str,
        user: str,
        password: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind to ``database`` on ``server`` with basic-auth credentials."""
        if "://" not in server:
            server = f"https://{server}"
        self.base_url = f"{server.rstrip('/')}/{database}"
        self._client = client or httpx.AsyncClient(timeout=120)
        self._auth = httpx.BasicAuth(user, password)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def changes(self, since: str | int = 0) -> Changes:
        """Fetch documents changed since ``since`` (``0`` for everything)."""

        @transient_retry
        async def send() -> httpx.Response:
            response = await self._client.get(
                f"{self.base_url}/_changes",
                params={"since": since, "include_docs": "true", "style": "main_only"},
                auth=self._auth,
            )
            if response.is_error:
                raise CouchError(response.status_code, response.text)
            return response

        try:
            response = await send()
        except httpx.TransportError as exc:
            raise MarvinError(f"cannot reach {self.base_url}: {exc}") from exc
        payload = response.json()
        result = Changes(last_seq=str(payload["last_seq"]))
        for row in payload["results"]:
            if row.get("deleted"):
                result.deleted_ids.append(str(row["id"]))
            elif (doc := row.get("doc")) is not None:
                result.docs.append(doc)
        return result
