"""Tool-level tests: call tools through the MCPServer with a mocked REST API."""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, GetPromptResult, InputRequiredResult
from pydantic import SecretStr

from amazing_marvin_mcp.api import BASE_URL, MarvinAPI
from amazing_marvin_mcp.mirror import Mirror
from amazing_marvin_mcp.server import State, create_server, parse_day
from amazing_marvin_mcp.settings import Settings

Json = dict[str, Any]


class FakeMarvin:
    """Records REST calls and answers like Marvin would."""

    def __init__(self, docs: dict[str, Json] | None = None) -> None:
        self.requests: list[tuple[str, Json]] = []
        self.docs = docs or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, body))
        if request.url.path == "/api/doc/update":
            doc = self.docs.get(body["itemId"]) or {
                "_id": body["itemId"],
                "db": "Tasks",
                "title": "T",
                "parentId": "home",
            }
            for setter in body["setters"]:
                doc[setter["key"]] = setter["val"]
            return httpx.Response(200, json=doc)
        if request.url.path in ("/api/addTask", "/api/addProject"):
            return httpx.Response(
                200, json={"_id": "created-id", "db": "Tasks", **body}
            )
        if request.url.path == "/api/doc/create":
            return httpx.Response(200, json={**body, "_rev": "1-new"})
        if request.url.path == "/api/markDone":
            return httpx.Response(
                200, json={"_id": body["itemId"], "db": "Tasks", "title": "T"}
            )
        return httpx.Response(404)


@pytest.fixture
def fake(mirror: Mirror) -> FakeMarvin:
    return FakeMarvin(mirror.docs)


@pytest.fixture
def server(
    mirror: Mirror, fake: FakeMarvin, tmp_path: Path
) -> Iterator[MCPServer[None]]:
    settings = Settings(
        api_token=SecretStr("a"),
        full_access_token=SecretStr("f"),
        workflow_file=tmp_path / "workflow.md",
        cache_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]
    )
    api = MarvinAPI(api_token="a", full_access_token="f", min_interval=0)
    state = State(settings=settings, mirror=mirror, api=api, couch=None)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.route().mock(side_effect=fake)
        yield create_server(settings, state=state)


async def call(server: MCPServer[None], name: str, **args: object) -> Json:
    result = await server.call_tool(name, args)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return dict(result.structured_content)


async def call_error(server: MCPServer[None], name: str, **args: object) -> str:
    with pytest.raises(ToolError) as exc_info:
        await server.call_tool(name, args)
    return str(exc_info.value)


def test_parse_day() -> None:
    base = date(2026, 8, 21)  # a Friday
    assert parse_day("today", base=base) == base
    assert parse_day("tomorrow", base=base) == date(2026, 8, 22)
    assert parse_day("week", base=base) == date(2026, 8, 23)
    assert parse_day("next-week", base=base) == date(2026, 8, 30)
    assert parse_day("month", base=base) == date(2026, 8, 31)
    assert parse_day("2026-01-02", base=base) == date(2026, 1, 2)


@pytest.mark.anyio
async def test_instructions_mention_missing_workflow(server: MCPServer[None]) -> None:
    assert "No workflow file found" in (server.instructions or "")


@pytest.mark.anyio
async def test_tools_listed(server: MCPServer[None]) -> None:
    names = {t.name for t in await server.list_tools()}
    assert {
        "list_inbox",
        "search_tasks",
        "get_task",
        "update_task",
        "create_task",
    } <= names


@pytest.mark.anyio
async def test_list_inbox_oldest_first(server: MCPServer[None]) -> None:
    out = await call(server, "list_inbox")
    assert [t["title"] for t in out["tasks"]] == [
        "Think about vacation",
        "Call dentist",
    ]
    assert out["tasks"][0]["parent"] == "Inbox"


@pytest.mark.anyio
async def test_structure(server: MCPServer[None]) -> None:
    out = await call(server, "get_structure")
    assert out["inbox_open_tasks"] == 2
    work = next(n for n in out["tree"] if n["title"] == "Work")
    website = work["children"][0]["children"][0]
    assert website["title"] == "Website"
    assert website["open_tasks"] == 2
    groups = {g["group"]: [lb["title"] for lb in g["labels"]] for g in out["labels"]}
    assert groups["Energy"] == ["high-energy", "low-energy"]
    assert groups[None] == ["short-win"]
    assert out["enabled_strategies"] == ["orbit", "Duration Estimates"]


@pytest.mark.anyio
async def test_search_resolves_names(server: MCPServer[None]) -> None:
    out = await call(server, "search_tasks", parent="Client A", labels=["short-win"])
    assert [t["title"] for t in out["tasks"]] == ["Deploy site"]
    assert out["tasks"][0]["labels"] == ["short-win", "low-energy"]
    assert out["tasks"][0]["subtasks"] == "1/2"
    out = await call(server, "search_tasks", max_minutes=10, labels=["@car"])
    assert [t["title"] for t in out["tasks"]] == ["Buy wiper fluid"]


@pytest.mark.anyio
async def test_search_bad_label_is_error(server: MCPServer[None]) -> None:
    assert "no label matching" in await call_error(
        server, "search_tasks", labels=["nope"]
    )


@pytest.mark.anyio
async def test_list_today(server: MCPServer[None]) -> None:
    out = await call(server, "list_today", day="2026-08-21")
    assert [t["title"] for t in out["scheduled"]] == ["Fix header CSS"]
    assert [t["title"] for t in out["scheduled_earlier_not_done"]] == ["Deploy site"]
    assert out["due_by_then"] == []


@pytest.mark.anyio
async def test_list_due(server: MCPServer[None]) -> None:
    out = await call(server, "list_due", by="2026-08-31")
    assert [t["title"] for t in out["tasks"]] == ["Fix header CSS", "Taxes"]


@pytest.mark.anyio
async def test_get_task_detail(server: MCPServer[None]) -> None:
    out = await call(server, "get_task", task_id="t-web1")
    assert out["note"] == "See the bug report.\nSecond line."
    assert out["parent"] == "Work > Client A > Website"
    assert out["importance"] == 2
    assert out["end_date"] == "2026-08-25"
    assert out["parent_id"] == "website"


@pytest.mark.anyio
async def test_list_children(server: MCPServer[None]) -> None:
    out = await call(server, "list_children", parent="work")
    assert [c["title"] for c in out["subprojects"]] == [
        "Client A"
    ]  # done project hidden
    assert [t["title"] for t in out["tasks"]] == ["Someday maybe"]


@pytest.mark.anyio
async def test_create_task_payload(server: MCPServer[None], fake: FakeMarvin) -> None:
    out = await call(
        server,
        "create_task",
        title="Rotate tires #not-a-project",
        parent="Home",
        labels=["car"],
        do_date="2026-09-01",
        estimate_minutes=30,
        importance=1,
        end_date="2026-09-05",
    )
    assert out["created"] == "created-id"
    path, body = fake.requests[0]
    assert path == "/api/addTask"
    assert body["parentId"] == "home"
    assert body["labelIds"] == ["lb-car"]
    assert body["timeEstimate"] == 30 * 60_000
    assert body["day"] == "2026-09-01"
    assert body["isStarred"] == 1
    assert body["done"] is False
    assert "timeZoneOffset" in body
    # end date is not consumed by addTask, so it goes through doc/update
    path2, body2 = fake.requests[1]
    assert path2 == "/api/doc/update"
    assert {s["key"]: s["val"] for s in body2["setters"]}["endDate"] == "2026-09-05"


@pytest.mark.anyio
async def test_update_task_labels_and_clear(
    server: MCPServer[None], fake: FakeMarvin, mirror: Mirror
) -> None:
    out = await call(
        server,
        "update_task",
        item_id="t-web2",
        title="Deploy the site",
        add_labels=["car"],
        remove_labels=["low-energy"],
        parent="inbox",
        clear=["do_date", "estimate"],
    )
    _, body = fake.requests[0]
    changes = {s["key"]: s["val"] for s in body["setters"]}
    assert changes["title"] == "Deploy the site"
    assert changes["labelIds"] == ["lb-quick", "lb-car"]
    assert changes["parentId"] == "unassigned"
    assert changes["day"] == "unassigned"
    assert changes["timeEstimate"] is None
    assert "fieldUpdates.title" in changes
    assert set(out["updated"]) == {
        "title",
        "labelIds",
        "parentId",
        "day",
        "timeEstimate",
    }
    # The mirror saw the write without a re-sync.
    assert mirror.docs["t-web2"]["title"] == "Deploy the site"


@pytest.mark.anyio
async def test_update_task_append_note(
    server: MCPServer[None], fake: FakeMarvin
) -> None:
    await call(server, "update_task", item_id="t-web1", append_note="Research: done.")
    _, body = fake.requests[0]
    changes = {s["key"]: s["val"] for s in body["setters"]}
    assert changes["note"] == "See the bug report.\nSecond line.\n\nResearch: done."


@pytest.mark.anyio
async def test_update_task_nothing(server: MCPServer[None]) -> None:
    assert "nothing to change" in await call_error(
        server, "update_task", item_id="t-web1"
    )
    assert "cannot clear" in await call_error(
        server, "update_task", item_id="t-web1", clear=["x"]
    )


@pytest.mark.anyio
async def test_mark_done(server: MCPServer[None], fake: FakeMarvin) -> None:
    out = await call(server, "mark_done", item_id="t-web1")
    assert out["done"] == "t-web1"
    assert fake.requests[0][0] == "/api/markDone"


@pytest.mark.anyio
async def test_prompts_and_resources(server: MCPServer[None], tmp_path: Path) -> None:
    (tmp_path / "workflow.md").write_text("# mine\nshort-win means < 10m")
    prompt = await server.get_prompt("triage_inbox", {})
    assert isinstance(prompt, GetPromptResult)
    assert "short-win means" in str(prompt.messages[0].content)
    res = await server.read_resource("marvin://workflow")
    assert not isinstance(res, InputRequiredResult)
    assert "short-win" in str(next(iter(res)).content)


@pytest.mark.anyio
async def test_create_category(
    server: MCPServer[None], fake: FakeMarvin, mirror: Mirror
) -> None:
    out = await call(server, "create_category", title="Admin", parent="Work")
    path, body = fake.requests[0]
    assert path == "/api/doc/create"
    assert body["db"] == "Categories"
    assert body["type"] == "category"
    assert body["parentId"] == "work"
    assert body["rank"] == 1  # after "Client A" (rank 0)
    assert len(body["_id"]) == 13
    assert out["parent"] == "Work"
    assert mirror.resolve_parent("Admin") == body["_id"]
    assert "inside a project" in await call_error(
        server, "create_category", title="x", parent="Website"
    )


@pytest.mark.anyio
async def test_create_label_needs_strategy(server: MCPServer[None]) -> None:
    assert "Task Labels strategy is off" in await call_error(
        server, "create_label", title="waiting"
    )


@pytest.mark.anyio
async def test_create_label_in_existing_and_new_group(
    server: MCPServer[None], fake: FakeMarvin, mirror: Mirror
) -> None:
    mirror.docs["strategies.labels"]["val"] = True
    await call(
        server, "create_label", title="waiting", group="Context", color="#123456"
    )
    path, body = fake.requests[0]
    assert path == "/api/doc/update"
    assert body["itemId"] == "strategySettings.labels"
    new_list = next(s["val"] for s in body["setters"] if s["key"] == "val")
    assert [lb["title"] for lb in new_list] == [
        "car",
        "high-energy",
        "low-energy",
        "short-win",
        "waiting",
    ]
    assert new_list[-1]["groupId"] == "g-ctx"
    assert new_list[-1]["color"] == "#123456"
    assert mirror.resolve_label("waiting").group_id == "g-ctx"

    out = await call(
        server,
        "create_label",
        title="office",
        new_group="Location",
        new_group_exclusive=True,
    )
    _, groups_body = fake.requests[1]
    assert groups_body["itemId"] == "strategySettings.labelSettings.groups"
    groups = next(s["val"] for s in groups_body["setters"] if s["key"] == "val")
    new_group = next(g for g in groups.values() if g["title"] == "Location")
    assert new_group["isExclusive"] is True
    assert new_group["rank"] == 3
    assert out["group"] == "Location"
    assert mirror.resolve_label("office").group_id == new_group["_id"]
    assert "already exists" in await call_error(server, "create_label", title="Office")


@pytest.mark.anyio
async def test_get_task_on_project(server: MCPServer[None]) -> None:
    out = await call(server, "get_task", task_id="website")
    assert out["type"] == "project"
    assert out["parent"] == "Work > Client A"
    assert [t["title"] for t in out["open_tasks"]] == ["Deploy site", "Fix header CSS"]


@pytest.mark.anyio
async def test_update_subtasks(server: MCPServer[None], fake: FakeMarvin) -> None:
    out = await call(
        server,
        "update_subtasks",
        task_id="t-web2",
        add=["verify"],
        complete=["push"],
        rename={"s1": "build it"},
    )
    _, body = fake.requests[0]
    subs = next(s["val"] for s in body["setters"] if s["key"] == "subtasks")
    by_title = {s["title"]: s for s in subs.values()}
    assert by_title["push"]["done"] is True
    assert by_title["verify"]["done"] is False
    assert by_title["verify"]["rank"] == 3
    assert "build it" in by_title
    assert out["subtasks"] == "2/3"
    await call(server, "update_subtasks", task_id="t-web2", remove=["verify"])
    assert "not found" in await call_error(
        server, "update_subtasks", task_id="t-web2", remove=["nope"]
    )
