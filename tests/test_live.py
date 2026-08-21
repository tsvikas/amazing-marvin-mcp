r"""End-to-end run of the tools, recorded against a throwaway Marvin account.

The HTTP traffic is stored as a VCR cassette (``tests/cassettes/``) by
pytest-recording, so by default this replays offline and doubles as a fixture
of what Marvin *really* returns (the ``_changes`` body is a full database).

To re-record (mutates the account: creates then deletes a few ``[live]`` items)::

    MARVIN_API_TOKEN=... MARVIN_FULL_ACCESS_TOKEN=... MARVIN_SYNC_*=... \
        uv run pytest tests/test_live.py --record-mode=rewrite

Secrets, the account host and e-mail are scrubbed before writing the cassette.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from mcp.types import CallToolResult
from pydantic import SecretStr

from marvin_mcp_server.server import build_state, create_server
from marvin_mcp_server.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mcp.server import MCPServer

    from marvin_mcp_server.server import State

pytestmark = pytest.mark.vcr

FAKE_HOST = "example.cloudant.com"
FAKE_DB = "userdb"
FAKE_EMAIL = "user@example.com"
PREFIX = "[live]"

Json = dict[str, Any]
type Call = Callable[..., Awaitable[Json]]


class _VcrRequest(Protocol):
    uri: str
    body: bytes | None
    headers: dict[str, Any]


def _settings(tmp_path: Path, *, recording: bool) -> Settings:
    """Real credentials when recording; stable placeholders when replaying."""
    workflow, cache = tmp_path / "workflow.md", tmp_path
    if recording:
        live = Settings(_env_file=None, workflow_file=workflow, cache_dir=cache)  # type: ignore[call-arg]
        if not (live.can_sync and live.can_edit):
            pytest.skip("recording needs MARVIN_* credentials in the environment")
        return live
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_token=SecretStr("api"),
        full_access_token=SecretStr("full"),
        sync_server=f"https://{FAKE_HOST}",
        sync_database=FAKE_DB,
        sync_user=SecretStr("placeholder-user"),
        sync_password=SecretStr("placeholder-password"),
        min_request_interval=0,
        mirror_max_age=10_000,
        workflow_file=workflow,
        cache_dir=cache,
    )


@pytest.fixture
def recording(pytestconfig: pytest.Config) -> bool:
    return pytestconfig.getoption("record_mode") not in (None, "none")


@pytest.fixture
def state(tmp_path: Path, recording: bool) -> State:  # noqa: FBT001
    return build_state(_settings(tmp_path, recording=recording))


@pytest.fixture
def vcr_config(state: State, recording: bool) -> Json:  # noqa: FBT001, C901
    """Scrub everything account-specific so the cassette is safe to commit."""
    if not recording:
        # vcr also runs `before_record_request` on requests it tries to match
        # during playback; with placeholder credentials there is nothing to hide.
        return {"match_on": ["method", "scheme", "host", "port", "path", "query"]}
    s = state.settings
    real_host = re.sub(r"^https?://", "", s.sync_server or FAKE_HOST).rstrip("/")
    real_db = s.sync_database or FAKE_DB
    secrets = [
        v.get_secret_value()
        for v in (s.api_token, s.full_access_token, s.sync_user, s.sync_password)
        if v is not None
    ]

    def scrub(text: str) -> str:
        text = text.replace(real_host, FAKE_HOST).replace(
            f"/{real_db}/", f"/{FAKE_DB}/"
        )
        for secret in secrets:
            text = text.replace(secret, "REDACTED")
        return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", FAKE_EMAIL, text)

    def scrub_headers(headers: dict[str, Any]) -> None:
        for key, value in list(headers.items()):
            if isinstance(value, str):
                headers[key] = scrub(value)
            elif isinstance(value, list):
                headers[key] = [scrub(v) if isinstance(v, str) else v for v in value]

    def before_request(request: _VcrRequest) -> _VcrRequest:
        request.uri = scrub(request.uri)
        scrub_headers(request.headers)
        if request.body:
            request.body = scrub(request.body.decode()).encode()
        return request

    def before_response(response: Json) -> Json:
        scrub_headers(response["headers"])
        body = response["body"].get("string")
        if isinstance(body, bytes):
            response["body"]["string"] = scrub(body.decode()).encode()
            # The stored body is plain and its length changed: drop framing
            # headers so the replaying client just reads it to EOF.
            for key in ("content-length", "Content-Length", "transfer-encoding"):
                response["headers"].pop(key, None)
        return response

    return {
        "filter_headers": [
            ("authorization", "REDACTED"),
            ("x-api-token", "REDACTED"),
            ("x-full-access-token", "REDACTED"),
            ("cookie", None),
            ("set-cookie", None),
        ],
        "before_record_request": before_request,
        "before_record_response": before_response,
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "decode_compressed_response": True,
    }


@pytest.fixture
def call(state: State) -> Call:
    server: MCPServer[None] = create_server(state.settings, state=state)

    async def _call(name: str, **args: object) -> Json:
        result = await server.call_tool(name, args)
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        return dict(result.structured_content)

    return _call


@pytest.mark.anyio
async def test_round_trip(call: Call, state: State) -> None:
    created: list[str] = []
    label_id = group_id = None
    try:
        synced = await call("sync_marvin")
        assert synced["documents"] > 0

        structure = await call("get_structure")
        assert isinstance(structure["tree"], list)
        assert "Task Labels" in structure["enabled_strategies"]

        cat = await call("create_category", title=f"{PREFIX} category")
        created.append(cat["created"])

        label = await call(
            "create_label",
            title=f"{PREFIX} label",
            new_group=f"{PREFIX} group",
            new_group_exclusive=True,
        )
        label_id = label["created"]
        group_id = state.mirror.resolve_label_group(f"{PREFIX} group").id

        task = await call(
            "create_task",
            title=f"{PREFIX} task",
            parent=f"{PREFIX} category",
            labels=[f"{PREFIX} label"],
            estimate_minutes=5,
            do_date="2030-01-01",
            end_date="2030-01-05",
            importance=2,
        )
        created.append(task["created"])

        found = await call("search_tasks", labels=[f"{PREFIX} label"], max_minutes=5)
        assert [t["id"] for t in found["tasks"]] == [task["created"]]
        assert found["tasks"][0]["end_date"] == "2030-01-05"
        assert found["tasks"][0]["importance"] == 2

        updated = await call(
            "update_task",
            item_id=task["created"],
            title=f"{PREFIX} task renamed",
            append_note="Research notes.",
            due_date="2030-01-03",
        )
        assert updated["due_date"] == "2030-01-03"

        await call("update_subtasks", task_id=task["created"], add=["one", "two"])
        with_subs = await call(
            "update_subtasks", task_id=task["created"], complete=["one"]
        )
        assert with_subs["subtasks"] == "1/2"

        detail = await call("get_task", task_id=task["created"])
        assert detail["title"] == f"{PREFIX} task renamed"
        assert "Research notes." in detail["note"]
        assert detail["labels"] == [f"{PREFIX} label"]

        project = await call("get_task", task_id=cat["created"])
        assert project["type"] == "category"
        assert [t["id"] for t in project["open_tasks"]] == [task["created"]]

        done = await call("mark_done", item_id=task["created"])
        assert done["done"] == task["created"]
    finally:
        # Leave the account as we found it (all of this is part of the cassette).
        for doc_id in reversed(created):
            await state.api.delete_doc(doc_id)
        if label_id is not None:
            raw_labels = cast(
                "list[Json]", state.mirror.docs["strategySettings.labels"]["val"]
            )
            labels = [lb for lb in raw_labels if lb["_id"] != label_id]
            await state.api.update_doc("strategySettings.labels", {"val": labels})
        if group_id is not None:
            raw_groups = cast(
                "Json",
                state.mirror.docs["strategySettings.labelSettings.groups"]["val"],
            )
            groups = dict(raw_groups)
            groups.pop(group_id, None)
            await state.api.update_doc(
                "strategySettings.labelSettings.groups", {"val": groups}
            )
        await state.api.aclose()
        if state.couch is not None:
            await state.couch.aclose()
