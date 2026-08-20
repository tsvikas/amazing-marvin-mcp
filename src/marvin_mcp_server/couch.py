"""Read-only client for Marvin's CouchDB (Cloudant) sync database.

Marvin's own apps replicate from this database with PouchDB, so the standard
``_changes`` feed is what we use too: ``since=0`` gives the full database, a
later ``since=<last_seq>`` gives only what changed. One request either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .models import JsonObj


@dataclass(slots=True)
class Changes:
    """Result of one ``_changes`` poll."""

    last_seq: str
    docs: list[JsonObj] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)


class CouchError(RuntimeError):
    """Non-2xx response from the database."""


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
        response = await self._client.get(
            f"{self.base_url}/_changes",
            params={"since": since, "include_docs": "true", "style": "main_only"},
            auth=self._auth,
        )
        if response.is_error:
            raise CouchError(f"{response.status_code} from _changes: {response.text}")
        payload = response.json()
        result = Changes(last_seq=str(payload["last_seq"]))
        for row in payload["results"]:
            if row.get("deleted"):
                result.deleted_ids.append(str(row["id"]))
            elif (doc := row.get("doc")) is not None:
                result.docs.append(doc)
        return result
