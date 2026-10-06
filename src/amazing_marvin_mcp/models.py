"""Pydantic views of Marvin documents.

Marvin's wiki documents the fields (see ``Marvin-Data-Types`` in
https://github.com/amazingmarvin/MarvinAPI/wiki) but is not up to date, so every
model uses ``extra="allow"``: unknown fields ride along in ``model_extra`` instead
of breaking parsing, and are preserved when a document is echoed back.

Conventions worth remembering (all from the wiki):

* ``parentId == "unassigned"`` is the Inbox, ``"root"`` is the top level.
  Some capture paths write ``"#Inbox"`` instead; the app shows those in the Inbox
  and rewrites them when it next opens it, so they are read as the Inbox too.
* UI names for the dates: ``day`` = **Do date** (the day you plan to do it,
  "scheduled"); ``dueDate`` = **Due date** (hard, external deadline);
  ``endDate`` = **End date** (self-imposed target, also set by Planning Ahead);
  ``startDate`` = **Start date** (backburner until then).
  ``day == "unassigned"``/``None``/``""`` means unscheduled.
* ``timeEstimate`` is in milliseconds.
* ``isStarred`` = **Importance**: 3 = P1 (red), 2 = P2 (orange), 1 = P3 (yellow),
  ``True`` = plain star in standard mode. ``isFrogged`` = **Eat the Frog**:
  1 = frog, 2 = baby frog, 3 = monster frog.
* Projects and categories share ``db == "Categories"``; ``type`` tells them apart.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

INBOX = "unassigned"
INBOX_ALIASES = frozenset({"#Inbox"})
ROOT = "root"
MS_PER_MINUTE = 60_000

JsonObj = dict[str, object]


class _Doc(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _drop_empty(cls, data: object) -> object:
        """Marvin writes ``null``/``""`` for unset fields; let the defaults apply."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None and v != ""}
        return data

    id: str = Field(alias="_id")
    rev: str | None = Field(default=None, alias="_rev")
    title: str = ""
    created_at: float | None = Field(default=None, alias="createdAt")
    updated_at: float | None = Field(default=None, alias="updatedAt")
    deleted_at: float | None = Field(default=None, alias="deletedAt")

    @property
    def trashed(self) -> bool:
        """Whether the item is in Marvin's (client-side) trash."""
        return self.deleted_at is not None


class Subtask(BaseModel):
    """Checklist item nested inside ``Task.subtasks``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="_id")
    title: str = ""
    done: bool = False
    rank: float = 0
    time_estimate: float | None = Field(default=None, alias="timeEstimate")


class _Item(_Doc):
    """Fields shared by tasks and projects (both are schedulable 'items')."""

    parent_id: str = Field(default=INBOX, alias="parentId")
    label_ids: list[str] = Field(default_factory=list, alias="labelIds")
    day: str | None = None
    due_date: str | None = Field(default=None, alias="dueDate")
    start_date: str | None = Field(default=None, alias="startDate")
    end_date: str | None = Field(default=None, alias="endDate")
    planned_week: str | None = Field(default=None, alias="plannedWeek")
    planned_month: str | None = Field(default=None, alias="plannedMonth")
    review_date: str | None = Field(default=None, alias="reviewDate")
    time_estimate: float | None = Field(default=None, alias="timeEstimate")
    note: str | None = None
    done: bool = False
    is_frogged: bool | int = Field(default=False, alias="isFrogged")
    backburner: bool = False
    rank: float = 0
    recurring: bool = False

    @field_validator("parent_id", mode="before")
    @classmethod
    def _inbox_alias(cls, value: object) -> object:
        return INBOX if value in INBOX_ALIASES else value

    @property
    def in_inbox(self) -> bool:
        """Whether the item sits in the Inbox."""
        return self.parent_id == INBOX

    @property
    def scheduled_day(self) -> date | None:
        """``day`` as a date, or ``None`` when unscheduled."""
        return _parse_date(self.day)

    @property
    def due(self) -> date | None:
        """``dueDate`` (UI: Due date, a hard deadline) as a date."""
        return _parse_date(self.due_date)

    @property
    def end(self) -> date | None:
        """``endDate`` (UI: End date, a self-imposed target) as a date."""
        return _parse_date(self.end_date)

    @property
    def start(self) -> date | None:
        """``startDate`` (UI: Start date, hidden on the backburner until then)."""
        return _parse_date(self.start_date)

    @property
    def estimate_minutes(self) -> float | None:
        """Duration estimate in minutes; Marvin stores "no estimate" as ``0``."""
        if not self.time_estimate:
            return None
        return self.time_estimate / MS_PER_MINUTE

    @property
    def frog_level(self) -> int:
        """0 = not a frog, 1 = normal, 2 = baby, 3 = monster."""
        return int(self.is_frogged)


class Task(_Item):
    """A task (``db == "Tasks"``)."""

    db: Literal["Tasks"] = "Tasks"
    is_starred: bool | int = Field(default=False, alias="isStarred")
    is_pinned: bool = Field(default=False, alias="isPinned")
    first_scheduled: str | None = Field(default=None, alias="firstScheduled")
    done_at: float | None = Field(default=None, alias="doneAt")
    subtasks: dict[str, Subtask] = Field(default_factory=dict)
    depends_on: dict[str, bool] = Field(default_factory=dict, alias="dependsOn")
    daily_section: str | int | None = Field(default=None, alias="dailySection")
    duration: float | None = None
    times: list[float] = Field(default_factory=list)

    @property
    def star_level(self) -> int:
        """0 = none, 1 = yellow, 2 = orange, 3 = red."""
        return int(self.is_starred)

    @property
    def is_tracking(self) -> bool:
        """Odd number of ``times`` entries means the clock is running."""
        return len(self.times) % 2 == 1


class Category(_Item):
    """A project or category (``db == "Categories"``)."""

    db: Literal["Categories"] = "Categories"
    type: Literal["project", "category"] = "category"
    priority: Literal["low", "mid", "high"] | None = None
    color: str | None = None
    icon: str | None = None
    done_date: str | None = Field(default=None, alias="doneDate")

    @property
    def is_project(self) -> bool:
        """Projects can be scheduled, have due dates and be done; categories cannot."""
        return self.type == "project"


class Label(BaseModel):
    """Label, from profile doc ``strategySettings.labels``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="_id")
    title: str = ""
    group_id: str | None = Field(default=None, alias="groupId")
    color: str | None = None
    icon: str | None = None
    is_hidden: bool = Field(default=False, alias="isHidden")
    is_action: bool = Field(default=False, alias="isAction")


class LabelGroup(BaseModel):
    """Label group, from profile doc ``strategySettings.labelSettings.groups``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(alias="_id")
    title: str = ""
    rank: float = 0
    is_exclusive: bool = Field(default=False, alias="isExclusive")


def _parse_date(value: str | None) -> date | None:
    if not value or value == INBOX:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
