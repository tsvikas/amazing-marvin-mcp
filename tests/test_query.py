"""The normalised view (`Lens`), the `where` language, text ranking and the tree."""

from datetime import date
from pathlib import Path

import pytest

from amazing_marvin_mcp.errors import MarvinError
from amazing_marvin_mcp.mirror import Mirror
from amazing_marvin_mcp.models import JsonObj
from amazing_marvin_mcp.query import FIELDS, Hit, Lens, Scope, search, tree, words_of
from amazing_marvin_mcp.server import SEARCH_DOC


def doc(db: str, _id: str, **fields: object) -> JsonObj:
    return {"_id": _id, "_rev": "1-x", "db": db, **fields}


TODAY = date(2026, 8, 21)  # a Friday; the fixture's "Fix header CSS" is due tomorrow


@pytest.fixture
def docs(docs: list[JsonObj]) -> list[JsonObj]:
    """Extend the shared database with each way an item leaves the active list."""
    return [
        *docs,
        doc(
            "ProfileItems",
            "strategySettings.backburner.backburnerLabels",
            val=["lb-lo"],
        ),
        doc(
            "Categories",
            "someday",
            type="project",
            title="Someday project",
            parentId="home",
            backburner=True,
            priority="high",
        ),
        doc("Tasks", "t-in-someday", title="Learn the cello", parentId="someday"),
        doc("Tasks", "t-in-old", title="Leftover", parentId="oldproj"),
        doc(
            "Tasks",
            "t-later",
            title="Renew passport",
            parentId="home",
            startDate="2026-09-01",
        ),
        doc(
            "Tasks",
            "t-blocked",
            title="Send invoice",
            parentId="work",
            dependsOn={"t-web1": True, "t-done": True, "gone": True},
        ),
        doc("Tasks", "t-call", title="להתקשר לאודי", parentId="home"),
        doc(
            "Tasks",
            "t-audio",
            title="להוריד אודיובוק",
            parentId="home",
            note="A long note. Somewhere in the middle it mentions the dentist again.",
        ),
    ]


@pytest.fixture
def lens(mirror: Mirror) -> Lens:
    return Lens(mirror, day=TODAY)


def ids(found: tuple[list[Hit], Scope]) -> list[str]:
    return [h.item.id for h in found[0]]


# --- state ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("task_id", "reasons"),
    [
        ("t-car", []),
        ("t-burner", ["flag"]),
        ("t-web2", ["label"]),
        ("t-later", ["start_date"]),
        ("t-blocked", ["dependency"]),
        ("t-in-someday", ["parent"]),
        ("t-in-old", ["parent_done"]),
    ],
)
def test_backburner_reasons(lens: Lens, task_id: str, reasons: list[str]) -> None:
    task = lens.mirror.task(task_id)
    assert lens.backburner_reasons(task) == reasons


def test_sequential_project_keeps_only_its_first_step(mirror: Mirror) -> None:
    mirror.docs["website"]["projectType"] = "sequential"
    mirror.docs["t-web1"]["masterRank"] = 2
    mirror.docs["t-web2"].update(masterRank=1, labelIds=[])
    first, second = mirror.task("t-web2"), mirror.task("t-web1")
    assert Lens(mirror, day=TODAY).backburner_reasons(second) == []  # strategy is off
    mirror.docs["strategies.sequentialProjects"] = {"db": "ProfileItems", "val": True}
    lens = Lens(mirror, day=TODAY)
    assert lens.backburner_reasons(first) == []
    assert lens.backburner_reasons(second) == ["sequential"]
    mirror.docs["t-web2"]["done"] = True
    assert Lens(mirror, day=TODAY).backburner_reasons(second) == []


def test_done_dependencies_do_not_block(mirror: Mirror) -> None:
    mirror.docs["t-web1"]["done"] = True
    assert not Lens(mirror, day=TODAY).has_dependency(mirror.task("t-blocked"))


def test_importance_reads_project_priority(lens: Lens) -> None:
    assert lens.importance(lens.categories["someday"]) == 3
    assert lens.importance(lens.mirror.task("t-web1")) == 2
    assert ids(search(lens, where="importance == 3", include_backburner=True)) == [
        "someday"
    ]


def test_search_defaults_to_active_tasks_and_projects(lens: Lens) -> None:
    found, scope = search(lens)
    assert (scope.include_done, scope.include_backburner) == (False, False)
    assert (scope.done_hidden, scope.backburner_hidden) == (2, 7)
    assert {h.item.id for h in found} == {
        "t-inbox1",
        "t-inbox2",
        "t-web1",
        "t-car",
        "t-due",
        "t-call",
        "t-audio",
        "website",
    }


@pytest.mark.parametrize(
    ("where", "done", "backburner"),
    [
        ("importance > 0", False, False),
        ("has_dependency", False, True),
        ("start_date > today", False, True),
        ("done", True, False),
        ("done and backburner", True, True),
    ],
)
def test_each_switch_turns_on_when_the_filter_asks(
    lens: Lens, where: str, *, done: bool, backburner: bool
) -> None:
    scope = search(lens, where=where)[1]
    assert (scope.include_done, scope.include_backburner) == (done, backburner)


def test_a_filter_is_not_silently_empty(lens: Lens) -> None:
    assert ids(search(lens, where="has_dependency")) == ["t-blocked"]
    assert ids(search(lens, where="done")) == ["t-done", "oldproj"]
    assert ids(search(lens, where='"flag" in why_backburner')) == [
        "t-burner",
        "someday",
    ]


def test_done_and_backburner_are_independent(lens: Lens) -> None:
    lens.mirror.docs["t-burner"]["done"] = True
    lens = Lens(lens.mirror, day=TODAY)
    assert "t-burner" not in ids(search(lens, include_done=True))
    assert "t-burner" not in ids(search(lens, include_backburner=True))
    assert ids(search(lens, where="done and backburner")) == ["t-burner"]
    found = search(lens, where="done", include_backburner=False)
    assert "t-burner" not in ids(found)
    assert found[1].backburner_hidden == 1


# --- where ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("importance == 2 and do_date == today", ["t-web1"]),
        ('do_date == "2026-08-21"', ["t-web1"]),
        ("due_date == tomorrow", ["t-web1"]),
        ("due_date <= today + days(9)", ["t-web1", "t-due"]),
        ('"CAR" in labels and estimate_min <= 10', ["t-car"]),
        ("estimate_min < 30", ["t-web1", "t-car"]),
        ('under("Client A")', ["t-web1", "website"]),
        ('under("Work") and kind == "project"', ["website"]),
        ('"dentist" in title or "dentist" in note', ["t-inbox1", "t-audio"]),
        ("due_date != None and not labels", ["t-due"]),
        ('raw.endDate == "2026-08-25"', ["t-web1"]),
        ('"push" in subtasks', []),
        ("len(labels) == 1 and kind == 'task'", ["t-web1", "t-car"]),
    ],
)
def test_where(lens: Lens, where: str, expected: list[str]) -> None:
    assert sorted(ids(search(lens, where=where))) == sorted(expected)


def test_where_combines_with_scope_and_kinds(lens: Lens) -> None:
    found = search(lens, where='"push" in subtasks', include_backburner=True)
    assert ids(found) == ["t-web2"]
    found = search(lens, where='under("root")', kinds={"category"}, include_done=True)
    assert set(ids(found)) == {"work", "home", "client-a"}


@pytest.mark.parametrize(
    ("where", "message"),
    [
        ('labels == "car"', '"x" in labels'),
        ('"nope" in labels', "no label 'nope'. Labels: car, high-energy"),
        ("bogus > 1", "unknown name(s) ['bogus']"),
        ("importance >", "cannot read"),
        ("title < 3", "cannot compare"),
        ('do_date == "soon"', "is not a date"),
        ('under("Nowhere")', "no project/category matching"),
        ('"x".__class__', "cannot evaluate"),
        ("__import__('os')", "unknown name(s)"),
        ("9 ** 9 ** 9 ** 9 > 1", "cannot evaluate"),
    ],
)
def test_where_errors_explain_themselves(lens: Lens, where: str, message: str) -> None:
    with pytest.raises(MarvinError) as exc_info:
        search(lens, where=where)
    assert message in str(exc_info.value)


def test_unknown_name_error_lists_the_fields(lens: Lens) -> None:
    with pytest.raises(MarvinError) as exc_info:
        search(lens, where="priority == 3")
    assert all(name in str(exc_info.value) for name in FIELDS)


def test_sort_keeps_unset_values_last(lens: Lens) -> None:
    by_due = ids(search(lens, sort="due_date", kinds={"task"}))
    assert by_due[:2] == ["t-web1", "t-due"]
    assert ids(search(lens, sort="-due_date", kinds={"task"}))[:2] == [
        "t-due",
        "t-web1",
    ]
    with pytest.raises(MarvinError, match="cannot sort by 'nope'"):
        search(lens, sort="nope")


# --- text -----------------------------------------------------------------------------
def test_words_of_keeps_quoted_phrases() -> None:
    assert words_of('Header "bug report" ') == ["header", "bug report"]
    assert words_of('unbalanced "quote') == ["unbalanced", '"quote']


def test_text_ranks_whole_words_and_hebrew_prefixes_first(lens: Lens) -> None:
    found, _ = search(lens, text="אודי")
    assert [h.item.id for h in found] == ["t-call", "t-audio"]
    assert found[0].score > found[1].score


def test_text_reports_where_it_matched(lens: Lens) -> None:
    (hit,) = search(lens, text="dentist", where='kind == "task" and note != None')[0]
    assert hit.matched_in == ["note"]
    assert hit.snippet is not None
    assert "mentions the dentist again" in hit.snippet
    assert hit.snippet.startswith("…")
    (hit,) = search(lens, text="push", include_backburner=True)[0]
    assert (hit.item.id, hit.matched_in) == ("t-web2", ["subtasks"])


def test_text_finds_projects_by_title(lens: Lens) -> None:
    assert ids(search(lens, text="website")) == ["website"]
    assert ids(search(lens, text="someday", include_backburner=True)) == [
        "t-burner",
        "someday",
    ]


def test_text_all_words_or_any_ranked_by_count(lens: Lens) -> None:
    assert ids(search(lens, text="fix header")) == ["t-web1"]
    assert ids(search(lens, text="fix dentist")) == []
    found = ids(search(lens, text="call dentist vacation", match="any"))
    assert found[0] == "t-inbox1"  # two of the three words
    assert set(found) == {"t-inbox1", "t-inbox2", "t-audio"}


# --- tree -----------------------------------------------------------------------------
def test_tree_counts_follow_what_is_shown(lens: Lens) -> None:
    found = tree(lens, depth=1)
    work, home = found.branches
    assert (work.item.id, work.tasks, work.tasks_below) == ("work", 0, 1)
    assert (work.more_categories, work.more_projects, work.children) == (1, 1, [])
    assert (home.item.id, home.tasks, home.tasks_below) == ("home", 4, 4)
    assert (found.inbox_tasks, found.done_projects_hidden) == (2, 1)
    assert found.backburner_hidden == 1


def test_tree_depth_and_root(lens: Lens) -> None:
    (work, _home) = tree(lens, depth=3).branches
    (client,) = work.children
    (website,) = client.children
    assert (website.item.id, website.tasks) == ("website", 1)
    assert (work.more_categories, work.more_projects) == (0, 0)
    found = tree(lens, root="client-a", depth=1)
    assert [b.item.id for b in found.branches] == ["website"]
    assert found.done_projects_hidden == 0


def test_tree_flags_include_the_hidden(lens: Lens) -> None:
    (work, home) = tree(
        lens, depth=2, include_done=True, include_backburner=True
    ).branches
    assert {b.item.id for b in work.children} == {"client-a", "oldproj"}
    assert {b.item.id for b in home.children} == {"someday"}
    assert work.tasks_below == 5  # incl. Backburner tasks and the done project's
    assert home.done_tasks_below == 1


# --- the description the model reads --------------------------------------------------
def test_search_description_fits_and_lists_every_field() -> None:
    assert len(SEARCH_DOC) <= 2048  # Claude Code truncates tool descriptions there
    assert all(name in SEARCH_DOC for name in FIELDS)


def test_mirror_fixture_is_isolated(tmp_path: Path, mirror: Mirror) -> None:
    assert not (tmp_path / "mirror.json").exists()
    assert "t-call" in mirror.docs
