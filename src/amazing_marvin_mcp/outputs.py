"""Shapes of what the tools return.

Declaring these as pydantic models gives every tool a published output schema.
The serializers keep the JSON the model sees compact: containers drop ``None``
fields, item summaries also drop ``False``/``0``/empty values, since "not
starred" or "no labels" carries no information worth tokens.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from .models import JsonObj

_EMPTY: tuple[object, ...] = (None, False, 0, "", [], {})


class Out(BaseModel):
    """Base for tool results: ``None`` fields are omitted from the output."""

    model_config = ConfigDict(extra="forbid")

    @model_serializer(mode="wrap")
    def _drop_none(self, handler: SerializerFunctionWrapHandler) -> JsonObj:
        return {k: v for k, v in handler(self).items() if v is not None}


class Compact(Out):
    """Like :class:`Out`, but also omits ``False``, ``0`` and empty values."""

    @model_serializer(mode="wrap")
    def _drop_empty(self, handler: SerializerFunctionWrapHandler) -> JsonObj:
        return {k: v for k, v in handler(self).items() if v not in _EMPTY}


# --- items ----------------------------------------------------------------------------
class ItemSummary(Compact):
    """One task or project, ids resolved to names. Omitted fields are unset."""

    id: str
    title: str
    type: Literal["task", "project", "category"] = "task"
    parent: Annotated[
        str, Field(description="path like 'Work > Client A > Website', or 'Inbox'")
    ]
    labels: list[str] = []
    do_date: Annotated[
        str | None, Field(description="Do date: the day it is scheduled for")
    ] = None
    due_date: Annotated[str | None, Field(description="Due date: hard deadline")] = None
    end_date: Annotated[
        str | None, Field(description="End date: self-imposed target")
    ] = None
    start_date: str | None = None
    planned_week: str | None = None
    planned_month: str | None = None
    estimate_min: Annotated[
        float | None, Field(description="Duration estimate in minutes")
    ] = None
    importance: Annotated[
        int, Field(description="3 = P1 red, 2 = P2 orange, 1 = P3 yellow")
    ] = 0
    frog: Annotated[
        int, Field(description="Eat the Frog: 1 frog, 2 baby, 3 monster")
    ] = 0
    backburner: Annotated[
        bool, Field(description="not active, for any of the reasons below")
    ] = False
    why_backburner: Annotated[
        list[str],
        Field(description="flag, label, start_date, dependency, parent, parent_done"),
    ] = []
    done: bool = False
    recurring: bool = False
    tracking: Annotated[bool, Field(description="time tracking running now")] = False
    subtasks: Annotated[str | None, Field(description="done/total, e.g. '1/3'")] = None
    note_preview: str | None = None


class SubtaskOut(Compact):
    """A checklist item of a task."""

    id: str
    title: str
    done: bool = False


class ItemDetail(ItemSummary):
    """Everything about one task or project, raw unmodelled fields included.

    Task-only: ``subtask_list``, ``depends_on``, ``minutes_tracked``,
    ``daily_section``, ``first_scheduled``, ``done_at``.
    Project/category-only: ``priority``, ``subprojects``, ``open_tasks``.
    """

    parent_id: str = ""
    note: str | None = None
    review_date: str | None = None
    daily_section: str | int | None = None
    first_scheduled: str | None = None
    subtask_list: list[SubtaskOut] = []
    depends_on: Annotated[list[str], Field(description="titles of blocking items")] = []
    minutes_tracked: float | None = None
    priority: Annotated[
        str | None, Field(description="low, mid or high (projects)")
    ] = None
    subprojects: list[ItemRef] = []
    open_tasks: list[ItemSummary] = []
    created_at: str | None = None
    updated_at: str | None = None
    done_at: str | None = None
    other_fields: Annotated[
        JsonObj, Field(description="fields not modelled by the server")
    ] = {}


class ItemRef(Compact):
    """Just enough to name and re-find an item."""

    id: str
    title: str
    type: Literal["task", "project", "category"] = "task"
    do_date: str | None = None


# --- collections ----------------------------------------------------------------------
class TaskList(Out):
    """A page of matching tasks."""

    count: Annotated[int, Field(description="matches before `limit` was applied")]
    tasks: list[ItemSummary]
    truncated: str | None = None


class SearchHit(ItemSummary):
    """A search result: the item, plus where the text matched."""

    matched_in: Annotated[
        list[str], Field(description="title, subtasks and/or note")
    ] = []
    snippet: Annotated[str | None, Field(description="the note around the match")] = (
        None
    )


class BriefHit(Compact):
    """A search result reduced to what identifies it."""

    id: str
    title: str
    type: Literal["task", "project", "category"] = "task"
    parent: str
    done: bool = False
    backburner: bool = False
    matched_in: list[str] = []


class SearchResult(Out):
    """A page of search results, best first."""

    count: Annotated[int, Field(description="all matches, before limit/offset")]
    include_done: bool
    include_backburner: bool
    done_hidden: Annotated[
        int, Field(description="matches left out because they are done")
    ] = 0
    backburner_hidden: Annotated[
        int, Field(description="matches left out because they are on the Backburner")
    ] = 0
    items: list[SearchHit | BriefHit]
    next_offset: Annotated[
        int | None, Field(description="pass as `offset` for the next page")
    ] = None


class DayView(Out):
    """What a given day looks like."""

    date: str
    scheduled: Annotated[list[ItemSummary], Field(description="do date is that day")]
    scheduled_earlier_not_done: list[ItemSummary]
    due_by_then: list[ItemSummary]


class Children(TaskList):
    """Direct contents of a project or category."""

    parent: str
    subprojects: list[ItemRef]


class TreeNode(Compact):
    """A category or project in the Master List tree."""

    id: str
    title: str
    type: Literal["project", "category"]
    tasks: Annotated[int, Field(description="open tasks directly inside")] = 0
    tasks_below: Annotated[
        int | None, Field(description="open tasks at any depth, when more than tasks")
    ] = None
    done_tasks_below: Annotated[
        int | None, Field(description="only with include_done")
    ] = None
    importance: int = 0
    do_date: str | None = None
    due_date: str | None = None
    done: bool = False
    backburner: bool = False
    more_categories: Annotated[
        int, Field(description="categories below, not listed at this depth")
    ] = 0
    more_projects: Annotated[
        int, Field(description="projects below, not listed at this depth")
    ] = 0
    children: list[TreeNode] = []


class LabelOut(Compact):
    """A label."""

    id: str
    title: str
    hidden: bool = False


class LabelGroupOut(BaseModel):
    """Labels of one group; ``group`` is ``null`` for ungrouped labels."""

    group: Annotated[str | None, Field(description="None = ungrouped labels")]
    exclusive: Annotated[
        bool, Field(description="a task may carry only one label of this group")
    ]
    labels: list[LabelOut]


class MirrorInfo(Out):
    """State of the local mirror."""

    documents: int
    age_seconds: int


class Structure(Out):
    """Categories and projects below ``root``, down to ``depth`` levels."""

    root: Annotated[str, Field(description="path of the root, or 'Master List'")]
    depth: int
    tasks: Annotated[int, Field(description="open tasks directly in the root")] = 0
    inbox_tasks: Annotated[
        int | None, Field(description="open Inbox tasks (top level only)")
    ] = None
    tree: list[TreeNode]
    done_projects_hidden: Annotated[
        int, Field(description="done projects left out; pass include_done to list them")
    ] = 0
    backburner_hidden: Annotated[
        int,
        Field(description="Backburner containers left out; pass include_backburner"),
    ] = 0


class Profile(Out):
    """Labels, the strategies in use and the state of the mirror."""

    labels: list[LabelGroupOut]
    backburner_labels: Annotated[
        list[str], Field(description="labels that put an item on the Backburner")
    ] = []
    enabled_strategies: Annotated[
        list[str], Field(description="names from Marvin's Strategies screen")
    ]
    mirror: MirrorInfo


# --- write results --------------------------------------------------------------------
class SyncResult(Out):
    """Outcome of a mirror refresh."""

    changed_documents: int
    documents: int


class Created(Out):
    """A newly created item."""

    created: Annotated[str, Field(description="id of the new item")]
    title: str
    parent: str | None = None
    group: str | None = None


class Updated(ItemSummary):
    """The item after an edit, plus which Marvin fields changed."""

    updated: Annotated[list[str], Field(description="Marvin fields that were changed")]


class Done(Out):
    """An item marked done."""

    done: Annotated[str, Field(description="id of the completed item")]
    title: str | None = None
