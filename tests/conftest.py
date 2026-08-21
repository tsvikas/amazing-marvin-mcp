"""Shared fixtures: a small but structurally realistic Marvin database."""

from pathlib import Path

import pytest

from amazing_marvin_mcp.mirror import Mirror
from amazing_marvin_mcp.models import JsonObj

MIN = 60_000


def doc(db: str, _id: str, **fields: object) -> JsonObj:
    return {"_id": _id, "_rev": "1-x", "db": db, **fields}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def docs() -> list[JsonObj]:
    """Work > Client A > Website (project) ; Home ; labels in two groups."""
    return [
        doc(
            "Categories", "work", type="category", title="Work", parentId="root", rank=1
        ),
        doc(
            "Categories", "home", type="category", title="Home", parentId="root", rank=2
        ),
        doc(
            "Categories", "client-a", type="category", title="Client A", parentId="work"
        ),
        doc(
            "Categories",
            "website",
            type="project",
            title="Website",
            parentId="client-a",
        ),
        doc(
            "Categories",
            "oldproj",
            type="project",
            title="Old",
            parentId="work",
            done=True,
        ),
        doc(
            "ProfileItems",
            "strategySettings.labels",
            val=[
                {"_id": "lb-car", "title": "car", "groupId": "g-ctx"},
                {"_id": "lb-hi", "title": "high-energy", "groupId": "g-energy"},
                {"_id": "lb-lo", "title": "low-energy", "groupId": "g-energy"},
                {"_id": "lb-quick", "title": "short-win"},
            ],
        ),
        doc(
            "ProfileItems",
            "strategySettings.labelSettings.groups",
            val={
                "g-ctx": {"_id": "g-ctx", "title": "Context", "rank": 2},
                "g-energy": {
                    "_id": "g-energy",
                    "title": "Energy",
                    "rank": 1,
                    "isExclusive": True,
                },
            },
        ),
        doc("ProfileItems", "strategies.timeEstimates", val=True),
        doc("ProfileItems", "strategies.labels", val=False),
        doc("ProfileItems", "strategies.orbit", val=True),
        doc(
            "Tasks",
            "t-inbox1",
            title="Call dentist",
            parentId="unassigned",
            createdAt=2,
        ),
        doc(
            "Tasks",
            "t-inbox2",
            title="Think about vacation",
            parentId="unassigned",
            createdAt=1,
        ),
        doc(
            "Tasks",
            "t-web1",
            title="Fix header CSS",
            parentId="website",
            day="2026-08-21",
            dueDate="2026-08-22",
            timeEstimate=15 * MIN,
            labelIds=["lb-hi"],
            isStarred=2,
            rank=2,
            note="See the bug report.\nSecond line.",
            endDate="2026-08-25",
            times=None,
            dailySection=0,
            plannedWeek="",
        ),
        doc(
            "Tasks",
            "t-web2",
            title="Deploy site",
            parentId="website",
            day="2026-08-20",
            timeEstimate=5 * MIN,
            labelIds=["lb-quick", "lb-lo"],
            rank=1,
            subtasks={
                "s1": {"_id": "s1", "title": "build", "done": True, "rank": 1},
                "s2": {"_id": "s2", "title": "push", "done": False, "rank": 2},
            },
        ),
        doc(
            "Tasks",
            "t-car",
            title="Buy wiper fluid",
            parentId="home",
            labelIds=["lb-car"],
            timeEstimate=10 * MIN,
        ),
        doc(
            "Tasks",
            "t-done",
            title="Already done",
            parentId="home",
            done=True,
            day="2026-08-19",
        ),
        doc("Tasks", "t-trash", title="Trashed", parentId="home", deletedAt=123),
        doc(
            "Tasks",
            "t-burner",
            title="Someday maybe",
            parentId="work",
            backburner=True,
            isFrogged=3,
        ),
        doc(
            "Tasks",
            "t-due",
            title="Taxes",
            parentId="home",
            dueDate="2026-08-30",
            timeEstimate=0,
        ),
    ]


@pytest.fixture
def mirror(docs: list[JsonObj], tmp_path: Path) -> Mirror:
    m = Mirror(None, tmp_path / "mirror.json")
    m.docs = {str(d["_id"]): d for d in docs}
    m.synced_at = 1e12  # far future: never considered stale
    return m
