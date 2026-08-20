import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from marvin_mcp_server.couch import CouchClient
from marvin_mcp_server.mirror import AmbiguousError, Mirror, NotFoundError, TaskFilter
from marvin_mcp_server.models import INBOX, Task


def titles(tasks: list[Task]) -> set[str]:
    return {t.title for t in tasks}


def test_collections_exclude_trash_and_keep_done(mirror: Mirror) -> None:
    assert "Trashed" not in titles(mirror.tasks())
    assert "Already done" in titles(mirror.tasks())
    assert {c.title for c in mirror.categories()} == {
        "Work",
        "Home",
        "Client A",
        "Website",
        "Old",
    }


def test_labels_and_groups_from_profile(mirror: Mirror) -> None:
    assert {lb.title for lb in mirror.labels()} == {
        "car",
        "high-energy",
        "low-energy",
        "short-win",
    }
    groups = mirror.label_groups()
    assert [g.title for g in groups] == ["Energy", "Context"]
    assert groups[0].is_exclusive


def test_enabled_strategies_use_ui_names(mirror: Mirror) -> None:
    assert mirror.enabled_strategies() == ["orbit", "Duration Estimates"]


def test_path_and_descendants(mirror: Mirror) -> None:
    assert mirror.path("website") == ["Work", "Client A", "Website"]
    assert mirror.path(INBOX) == ["Inbox"]
    assert mirror.path("root") == []
    assert mirror.descendants("work") == {"work", "client-a", "website", "oldproj"}


def test_path_is_cycle_safe(mirror: Mirror) -> None:
    mirror.docs["work"]["parentId"] = "website"
    assert len(mirror.path("website")) == 3


def test_resolve_parent(mirror: Mirror) -> None:
    assert mirror.resolve_parent("inbox") == INBOX
    assert mirror.resolve_parent("Inbox") == INBOX
    assert mirror.resolve_parent("website") == "website"
    assert mirror.resolve_parent("WEBSITE") == "website"
    assert mirror.resolve_parent("client") == "client-a"
    with pytest.raises(NotFoundError):
        mirror.resolve_parent("nope")
    mirror.docs["dup"] = {
        "_id": "dup",
        "db": "Categories",
        "type": "category",
        "title": "client a",
    }
    with pytest.raises(AmbiguousError):
        mirror.resolve_parent("Client A")


def test_resolve_label(mirror: Mirror) -> None:
    assert mirror.resolve_label("@car").id == "lb-car"
    assert mirror.resolve_label("lb-hi").title == "high-energy"
    with pytest.raises(AmbiguousError):
        mirror.resolve_label("energy")


def test_search_default_excludes_done(mirror: Mirror) -> None:
    assert "Already done" not in titles(mirror.search(TaskFilter()))
    assert "Already done" in titles(mirror.search(TaskFilter(done=True)))
    assert "Already done" in titles(mirror.search(TaskFilter(done=None)))


def test_search_inbox(mirror: Mirror) -> None:
    found = mirror.search(TaskFilter(parent_id=INBOX, include_descendants=False))
    assert titles(found) == {"Call dentist", "Think about vacation"}


def test_search_parent_with_descendants(mirror: Mirror) -> None:
    assert titles(mirror.search(TaskFilter(parent_id="work"))) == {
        "Fix header CSS",
        "Deploy site",
        "Someday maybe",
    }
    assert titles(
        mirror.search(TaskFilter(parent_id="work", include_descendants=False))
    ) == {"Someday maybe"}


def test_search_labels_all_must_match(mirror: Mirror) -> None:
    assert titles(mirror.search(TaskFilter(label_ids=["lb-quick", "lb-lo"]))) == {
        "Deploy site"
    }
    assert titles(mirror.search(TaskFilter(label_ids=["lb-quick", "lb-hi"]))) == set()


def test_search_schedule_and_due(mirror: Mirror) -> None:
    d = date(2026, 8, 21)
    assert titles(mirror.search(TaskFilter(day_from=d, day_to=d))) == {"Fix header CSS"}
    assert titles(mirror.search(TaskFilter(day_to=date(2026, 8, 20)))) == {
        "Deploy site"
    }
    assert titles(mirror.search(TaskFilter(unscheduled=True, parent_id="home"))) == {
        "Buy wiper fluid",
        "Taxes",
    }
    assert titles(mirror.search(TaskFilter(due_by=date(2026, 8, 25)))) == {
        "Fix header CSS"
    }
    assert titles(mirror.search(TaskFilter(has_due_date=True))) == {
        "Fix header CSS",
        "Taxes",
    }
    assert titles(mirror.search(TaskFilter(end_by=date(2026, 8, 25)))) == {
        "Fix header CSS"
    }


def test_search_estimates_flags_text(mirror: Mirror) -> None:
    assert titles(mirror.search(TaskFilter(max_minutes=10))) == {
        "Deploy site",
        "Buy wiper fluid",
    }
    assert titles(mirror.search(TaskFilter(min_minutes=10, has_estimate=True))) == {
        "Fix header CSS",
        "Buy wiper fluid",
    }
    assert titles(mirror.search(TaskFilter(has_estimate=False, parent_id="home"))) == {
        "Taxes"
    }
    assert mirror.task("t-due").estimate_minutes is None  # 0 means "no estimate"
    assert titles(mirror.search(TaskFilter(starred=True))) == {"Fix header CSS"}
    assert titles(mirror.search(TaskFilter(frogged=True))) == {"Someday maybe"}
    assert titles(mirror.search(TaskFilter(backburner=False, parent_id="work"))) == {
        "Fix header CSS",
        "Deploy site",
    }
    assert titles(mirror.search(TaskFilter(text="bug report"))) == {"Fix header CSS"}


def test_search_sorted_by_day_then_rank(mirror: Mirror) -> None:
    found = mirror.search(TaskFilter(parent_id="website"))
    assert [t.title for t in found] == ["Deploy site", "Fix header CSS"]


def test_empty_and_null_fields_use_defaults(mirror: Mirror) -> None:
    task = mirror.task("t-web1")
    assert task.times == []
    assert task.planned_week is None
    assert task.daily_section == 0
    assert task.end == date(2026, 8, 25)


def test_task_lookup(mirror: Mirror) -> None:
    task = mirror.task("t-web2")
    assert task.estimate_minutes == 5
    assert [s.title for s in task.subtasks.values()] == ["build", "push"]
    with pytest.raises(NotFoundError):
        mirror.task("website")  # a project, not a task


def test_save_and_load_roundtrip(mirror: Mirror, tmp_path: Path) -> None:
    mirror.last_seq = "42-abc"
    mirror.save()
    path = tmp_path / "mirror.json"
    assert path.stat().st_mode & 0o777 == 0o600
    fresh = Mirror(None, path)
    assert fresh.load()
    assert fresh.last_seq == "42-abc"
    assert len(fresh.docs) == len(mirror.docs)


def _couch(handler: httpx.MockTransport) -> CouchClient:
    return CouchClient(
        "db.example.com",
        "u123",
        "user",
        "pw",
        client=httpx.AsyncClient(transport=handler),
    )


@pytest.mark.anyio
async def test_refresh_applies_changes_and_deletes(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        assert request.url.path == "/u123/_changes"
        assert request.headers["authorization"].startswith("Basic ")
        since = request.url.params["since"]
        if since == "0":
            return httpx.Response(
                200,
                json={
                    "last_seq": "2-a",
                    "results": [
                        {
                            "id": "a",
                            "seq": "1-a",
                            "doc": {"_id": "a", "db": "Tasks", "title": "A"},
                        },
                        {
                            "id": "b",
                            "seq": "2-a",
                            "doc": {"_id": "b", "db": "Tasks", "title": "B"},
                        },
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "last_seq": "3-a",
                "results": [
                    {"id": "a", "seq": "3-a", "deleted": True, "changes": []},
                    {
                        "id": "b",
                        "seq": "3-a",
                        "doc": {"_id": "b", "db": "Tasks", "title": "B2"},
                    },
                ],
            },
        )

    mirror = Mirror(
        _couch(httpx.MockTransport(handler)), tmp_path / "m.json", max_age=3600
    )
    assert await mirror.refresh() == 2
    assert {t.title for t in mirror.tasks()} == {"A", "B"}
    # Fresh enough: no request.
    assert await mirror.refresh() == 0
    assert len(seen) == 1
    assert await mirror.refresh(force=True) == 2
    assert seen[-1]["since"] == "2-a"
    assert {t.title for t in mirror.tasks()} == {"B2"}
    assert json.loads((tmp_path / "m.json").read_text())["last_seq"] == "3-a"


@pytest.mark.anyio
async def test_refresh_without_couch_is_noop(tmp_path: Path) -> None:
    mirror = Mirror(None, tmp_path / "m.json")
    assert await mirror.refresh(force=True) == 0
