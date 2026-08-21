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
    backburner: bool = False
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


class DueList(TaskList):
    """Tasks due on or before ``by``."""

    by: str


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
    open_tasks: int = 0
    do_date: str | None = None
    due_date: str | None = None
    done: bool = False
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
    """The user's Marvin layout: tree, labels, enabled strategies."""

    inbox_open_tasks: int
    tree: list[TreeNode]
    labels: list[LabelGroupOut]
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
