"""Local mirror of the Marvin database, and the queries that run against it.

The mirror is a plain ``{id: doc}`` dict kept in memory and persisted as one JSON
file, so cold starts only need an incremental ``_changes`` poll. Reads never
touch the network unless the mirror is older than ``max_age``; that keeps us far
under Marvin's 1440 requests/day budget even in a long triage session.
"""

from __future__ import annotations

import json
import secrets
import string
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .models import INBOX, ROOT, Category, JsonObj, Label, LabelGroup, Task

if TYPE_CHECKING:
    from pathlib import Path

    from .couch import CouchClient

LABELS_DOC = "strategySettings.labels"
LABEL_GROUPS_DOC = "strategySettings.labelSettings.groups"
LABELS_STRATEGY_DOC = "strategies.labels"
_ID_ALPHABET = string.ascii_letters + string.digits


def new_id() -> str:
    """Return a 13-char random id, the form Marvin's client uses."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(13))


# profile `strategies.<key>` -> name shown in Marvin's Strategies screen
STRATEGY_NAMES = {
    "labels": "Task Labels",
    "timeEstimates": "Duration Estimates",
    "priorities": "Importance Levels",
    "eatFrog": "Eat the Frog",
    "backburner": "Backburner",
    "dayPlanning": "Day Planning",
    "weekView": "Week Scheduler",
    "autoDueTasks": "Auto-schedule due Tasks",
    "MIP": "Most Important Projects",
    "reminders": "Reminders",
    "subtasks": "Subtasks",
    "notes": "Task and Project Notes",
    "energyLevel": "Energy",
    "mentalWeight": "Weight",
    "fireUrgency": "Urgency",
    "positiveEnergy": "Positive Feelings",
    "braindump": "Braindump",
    "calendar": "Calendar",
    "events": "Events",
    "timers": "Timers",
    "marvinPoints": "Marvin Kudos",
    "sfm": "Super Focus Mode",
    "projectShortcuts": "Project Shortcuts",
    "taskHints": "Task Hints",
    "categoryIndicators": "Category Context",
}


class NotFoundError(LookupError):
    """No item matches the given id or name."""


class AmbiguousError(LookupError):
    """More than one item matches the given name."""


@dataclass(slots=True, kw_only=True)
class TaskFilter:
    """Filters for :meth:`Mirror.search`. ``None`` means "don't filter on this"."""

    text: str | None = None
    parent_id: str | None = None
    include_descendants: bool = True
    label_ids: list[str] | None = None
    day_from: date | None = None
    day_to: date | None = None
    unscheduled: bool | None = None
    due_by: date | None = None
    has_due_date: bool | None = None
    end_by: date | None = None
    min_minutes: float | None = None
    max_minutes: float | None = None
    has_estimate: bool | None = None
    starred: bool | None = None
    frogged: bool | None = None
    backburner: bool | None = None
    done: bool | None = False

    def matches(self, task: Task) -> bool:  # noqa: C901, PLR0911, PLR0912
        """Whether ``task`` passes every set filter."""
        if self.done is not None and task.done != self.done:
            return False
        if self.text is not None:
            needle = self.text.casefold()
            haystack = f"{task.title}\n{task.note or ''}".casefold()
            if needle not in haystack:
                return False
        if self.label_ids and not set(self.label_ids) <= set(task.label_ids):
            return False
        day = task.scheduled_day
        if self.unscheduled is not None and (day is None) != self.unscheduled:
            return False
        if self.day_from is not None and (day is None or day < self.day_from):
            return False
        if self.day_to is not None and (day is None or day > self.day_to):
            return False
        due = task.due
        if self.has_due_date is not None and (due is not None) != self.has_due_date:
            return False
        if self.due_by is not None and (due is None or due > self.due_by):
            return False
        end = task.end
        if self.end_by is not None and (end is None or end > self.end_by):
            return False
        minutes = task.estimate_minutes
        if self.has_estimate is not None and (minutes is not None) != self.has_estimate:
            return False
        if self.min_minutes is not None and (
            minutes is None or minutes < self.min_minutes
        ):
            return False
        if self.max_minutes is not None and (
            minutes is None or minutes > self.max_minutes
        ):
            return False
        if self.starred is not None and bool(task.star_level) != self.starred:
            return False
        if self.frogged is not None and bool(task.frog_level) != self.frogged:
            return False
        return not (self.backburner is not None and task.backburner != self.backburner)


class Mirror:
    """In-memory copy of the user's documents with a JSON file behind it."""

    def __init__(
        self, couch: CouchClient | None, cache_file: Path, *, max_age: float = 60.0
    ) -> None:
        """Bind to ``couch`` (``None`` = offline) and the JSON file to persist into."""
        self._couch = couch
        self._cache_file = cache_file
        self._max_age = max_age
        self.docs: dict[str, JsonObj] = {}
        self.last_seq: str = "0"
        self.synced_at: float = 0.0
        self._loaded = False

    # --- sync -----------------------------------------------------------------------
    def load(self) -> bool:
        """Load the persisted mirror; returns whether a cache file existed."""
        self._loaded = True
        if not self._cache_file.exists():
            return False
        payload = json.loads(self._cache_file.read_text(encoding="utf-8"))
        self.docs = payload["docs"]
        self.last_seq = str(payload["last_seq"])
        self.synced_at = float(payload.get("synced_at", 0))
        return True

    def save(self) -> None:
        """Persist the mirror, owner-readable only (it holds all the user's tasks)."""
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_seq": self.last_seq,
            "synced_at": self.synced_at,
            "docs": self.docs,
        }
        tmp = self._cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self._cache_file)

    @property
    def age(self) -> float:
        """Seconds since the last successful poll."""
        return time.time() - self.synced_at

    async def refresh(self, *, force: bool = False) -> int:
        """Poll ``_changes`` if the mirror is stale (or ``force``). Returns #changes."""
        if not self._loaded:
            self.load()
        if self._couch is None or (not force and self.age < self._max_age):
            return 0
        changes = await self._couch.changes(since=self.last_seq)
        for doc in changes.docs:
            self.docs[str(doc["_id"])] = doc
        for doc_id in changes.deleted_ids:
            self.docs.pop(doc_id, None)
        self.last_seq = changes.last_seq
        self.synced_at = time.time()
        self.save()
        return len(changes.docs) + len(changes.deleted_ids)

    def apply(self, doc: JsonObj) -> None:
        """Merge a document returned by a write so reads see it immediately."""
        doc_id = doc.get("_id")
        if isinstance(doc_id, str):
            self.docs[doc_id] = doc

    # --- collections ----------------------------------------------------------------
    def _by_db(self, db: str) -> list[JsonObj]:
        return [doc for doc in self.docs.values() if doc.get("db") == db]

    def tasks(self, *, include_trashed: bool = False) -> list[Task]:
        """All tasks (trash excluded by default)."""
        tasks = (Task.model_validate(doc) for doc in self._by_db("Tasks"))
        return [t for t in tasks if include_trashed or not t.trashed]

    def categories(self, *, include_trashed: bool = False) -> list[Category]:
        """All projects and categories."""
        cats = (Category.model_validate(doc) for doc in self._by_db("Categories"))
        return [c for c in cats if include_trashed or not c.trashed]

    def _profile_val(self, doc_id: str) -> object:
        doc = self.docs.get(doc_id)
        return None if doc is None else doc.get("val")

    def labels(self) -> list[Label]:
        """All labels, from the profile."""
        return [
            Label.model_validate(raw) for raw in _values(self._profile_val(LABELS_DOC))
        ]

    def label_groups(self) -> list[LabelGroup]:
        """All label groups, from the profile."""
        raw_groups = _values(self._profile_val(LABEL_GROUPS_DOC))
        return sorted(
            (LabelGroup.model_validate(g) for g in raw_groups), key=lambda g: g.rank
        )

    def enabled_strategies(self) -> list[str]:
        """Marvin strategies switched on, by their UI names (``strategies.*`` docs)."""
        prefix = "strategies."
        keys = sorted(
            doc_id.removeprefix(prefix)
            for doc_id, doc in self.docs.items()
            if doc_id.startswith(prefix)
            and doc.get("db") == "ProfileItems"
            and doc.get("val")
        )
        return [STRATEGY_NAMES.get(k, k) for k in keys]

    # --- lookups --------------------------------------------------------------------
    def task(self, task_id: str) -> Task:
        """Task by id."""
        doc = self.docs.get(task_id)
        if doc is None or doc.get("db") != "Tasks":
            raise NotFoundError(f"no task with id {task_id!r}")
        return Task.model_validate(doc)

    def category(self, cat_id: str) -> Category:
        """Project/category by id."""
        doc = self.docs.get(cat_id)
        if doc is None or doc.get("db") != "Categories":
            raise NotFoundError(f"no project/category with id {cat_id!r}")
        return Category.model_validate(doc)

    def label(self, label_id: str) -> Label:
        """Label by id."""
        for label in self.labels():
            if label.id == label_id:
                return label
        raise NotFoundError(f"no label with id {label_id!r}")

    def path(self, parent_id: str) -> list[str]:
        """Titles from the top-level category down to ``parent_id`` (cycle-safe)."""
        titles: list[str] = []
        seen: set[str] = set()
        current = parent_id
        while current not in (ROOT, INBOX) and current not in seen:
            seen.add(current)
            doc = self.docs.get(current)
            if doc is None:
                titles.append(f"<missing {current}>")
                break
            titles.append(str(doc.get("title", "")))
            current = str(doc.get("parentId", ROOT))
        if not titles and parent_id == INBOX:
            return ["Inbox"]
        return list(reversed(titles))

    def descendants(self, parent_id: str) -> set[str]:
        """``parent_id`` plus every project/category nested under it."""
        children: dict[str, list[str]] = {}
        for cat in self.categories():
            children.setdefault(cat.parent_id, []).append(cat.id)
        result = {parent_id}
        stack = [parent_id]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in result:
                    result.add(child)
                    stack.append(child)
        return result

    def resolve_parent(self, name_or_id: str) -> str:
        """Turn an id, ``"inbox"``/``"root"`` or a unique title into a parent id."""
        lowered = name_or_id.strip().casefold()
        if lowered in ("inbox", INBOX):
            return INBOX
        if lowered == ROOT:
            return ROOT
        if name_or_id in self.docs and self.docs[name_or_id].get("db") == "Categories":
            return name_or_id
        matches = [c for c in self.categories() if c.title.casefold() == lowered]
        if not matches:
            matches = [c for c in self.categories() if lowered in c.title.casefold()]
        return _unique(matches, name_or_id, "project/category").id

    def resolve_label_group(self, name_or_id: str) -> LabelGroup:
        """Turn a label-group id or a unique title into a :class:`LabelGroup`."""
        groups = self.label_groups()
        for group in groups:
            if group.id == name_or_id:
                return group
        lowered = name_or_id.strip().casefold()
        matches = [g for g in groups if g.title.casefold() == lowered]
        return _unique(matches, name_or_id, "label group")

    def resolve_label(self, name_or_id: str) -> Label:
        """Turn a label id or a unique title into a :class:`Label`."""
        labels = self.labels()
        for label in labels:
            if label.id == name_or_id:
                return label
        lowered = name_or_id.strip().lstrip("@").casefold()
        matches = [lb for lb in labels if lb.title.casefold() == lowered]
        if not matches:
            matches = [lb for lb in labels if lowered in lb.title.casefold()]
        return _unique(matches, name_or_id, "label")

    # --- queries --------------------------------------------------------------------
    def search(self, flt: TaskFilter) -> list[Task]:
        """Tasks matching ``flt``, sorted by scheduled day then rank."""
        parents: set[str] | None = None
        if flt.parent_id is not None:
            parents = (
                self.descendants(flt.parent_id)
                if flt.include_descendants
                else {flt.parent_id}
            )
        found = [
            t
            for t in self.tasks()
            if (parents is None or t.parent_id in parents) and flt.matches(t)
        ]
        found.sort(key=lambda t: (t.scheduled_day or date.max, t.rank, t.title))
        return found


def _values(raw: object) -> list[object]:
    """Profile values are sometimes a list and sometimes an ``{id: item}`` dict."""
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, dict):
        return list(raw.values())
    return []


def _unique[T: (Category, Label, LabelGroup)](
    matches: list[T], query: str, kind: str
) -> T:
    if not matches:
        raise NotFoundError(f"no {kind} matching {query!r}")
    if len(matches) > 1:
        names = ", ".join(f"{m.title!r} ({m.id})" for m in matches)
        raise AmbiguousError(f"{kind} {query!r} is ambiguous: {names}")
    return matches[0]
