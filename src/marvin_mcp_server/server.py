"""The MCP server: tools, resources and prompts over the mirror and the REST API.

Tools are shaped around what a person asks ("what's in my inbox", "what's due
this week", "rename X") rather than around Marvin's endpoints, and use the
names Marvin's UI uses (Do date, Due date, End date, Importance, Duration
estimate, Backburner...). Anything opinionated about *how* to triage or plan is
left to the user's workflow file.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from mcp.server import MCPServer
from pydantic import Field, SecretStr

from . import __version__
from .api import MarvinAPI
from .couch import CouchClient
from .mirror import (
    LABEL_GROUPS_DOC,
    LABELS_DOC,
    LABELS_STRATEGY_DOC,
    Mirror,
    TaskFilter,
    new_id,
)
from .models import INBOX, MS_PER_MINUTE, Category, JsonObj, Task
from .settings import Settings
from .workflow import load_workflow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

MAX_RESULTS = 200
NOTE_PREVIEW = 120
# Fields of a raw document that carry no meaning for the assistant.
_NOISE = frozenset({"_rev", "db", "fieldUpdates", "rank", "masterRank", "_id"})

GENERIC_INSTRUCTIONS = """\
You are connected to the user's Amazing Marvin task manager.

Marvin vocabulary (the tools use the same names as Marvin's UI):
- Inbox = items not yet filed anywhere (parent "unassigned"). Everything else
  lives in a tree of categories (folders, arbitrarily nested) and projects
  (nested anywhere; hold tasks and sub-projects). `get_structure` shows the
  tree with ids. The Master List is that whole tree.
- Dates are four different things:
  * Do date (`do_date`) = the day the user plans to *do* it; "scheduled".
  * Due date (`due_date`) = a hard, external deadline. Rare and serious.
  * End date (`end_date`) = a self-imposed target, "artificial deadline";
    Planning Ahead (planned week / planned month) is the softer version.
  * Start date (`start_date`) = hidden on the backburner until that day.
  Don't translate one into another: "deadline" usually means due date; "I
  want this done by" usually means end date or planned week.
- Duration estimate = `estimate_min` (minutes). Importance = stars:
  3 = P1 (red), 2 = P2 (orange), 1 = P3 (yellow). Frog = Eat-the-Frog
  dreaded task, 1-3. Backburner = deliberately parked / not now.
- Labels are free-form and user-defined; their meaning is in the workflow
  section below, not in Marvin. Label groups can be exclusive (one per group).

Reads come from a local mirror refreshed at most once a minute; call
`sync_marvin` if the user says they just changed something in Marvin.
Writes are rate-limited (about one per 3 seconds); for bulk edits go item by
item and tell the user it takes a moment rather than stopping.
Prefer `search_tasks` for any filtering question; use `get_task` before editing
so you see the current state. Never guess ids: resolve names via
`get_structure` or a search first.
"""

NO_WORKFLOW = """\
## This user's workflow
No workflow file found. Run `marvin-mcp-server init-workflow` to create one; until
then, ask the user how they use labels, scheduling and deadlines before making
assumptions.
"""


@dataclass(slots=True)
class State:
    """Shared clients; one per server process."""

    settings: Settings
    mirror: Mirror
    api: MarvinAPI
    couch: CouchClient | None


def build_state(settings: Settings) -> State:
    """Construct clients from settings (no network until first use)."""
    couch = None
    if settings.can_sync:
        assert settings.sync_server and settings.sync_database  # noqa: S101
        assert settings.sync_user and settings.sync_password  # noqa: S101
        couch = CouchClient(
            settings.sync_server,
            settings.sync_database,
            settings.sync_user.get_secret_value(),
            settings.sync_password.get_secret_value(),
        )
    mirror = Mirror(
        couch, settings.cache_dir / "mirror.json", max_age=settings.mirror_max_age
    )
    api = MarvinAPI(
        api_token=_secret(settings.api_token),
        full_access_token=_secret(settings.full_access_token),
        min_interval=settings.min_request_interval,
    )
    return State(settings=settings, mirror=mirror, api=api, couch=couch)


def _secret(value: SecretStr | None) -> str | None:
    return None if value is None else value.get_secret_value()


def instructions_for(settings: Settings) -> str:
    """Server instructions: generic Marvin semantics + the user's own workflow."""
    workflow = load_workflow(settings.workflow_file)
    user_part = (
        NO_WORKFLOW if workflow is None else f"## This user's workflow\n{workflow}"
    )
    return f"{GENERIC_INSTRUCTIONS}\n{user_part}"


# --- date helpers ---------------------------------------------------------------------
def today() -> date:
    """Local date (the server runs on the user's machine)."""
    return date.today()  # noqa: DTZ011


def tz_offset_minutes() -> int:
    """Local UTC offset in minutes, the sign Marvin expects (Pacific = -480)."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


def parse_day(value: str, *, base: date | None = None) -> date:  # noqa: PLR0911
    """Accept ``YYYY-MM-DD`` or a few relative words."""
    base = base or today()
    word = value.strip().casefold()
    match word:
        case "today":
            return base
        case "tomorrow":
            return base + timedelta(days=1)
        case "yesterday":
            return base - timedelta(days=1)
        case "week" | "this-week" | "this week" | "end-of-week":
            return base + timedelta(days=6 - base.weekday())
        case "next-week" | "next week":
            return base + timedelta(days=13 - base.weekday())
        case "month" | "this-month" | "this month":
            first_next = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
            return first_next - timedelta(days=1)
        case _:
            return date.fromisoformat(word)


def _opt_day(value: str | None) -> str | None:
    return None if value is None else parse_day(value).isoformat()


# --- rendering ------------------------------------------------------------------------
def _compact(obj: JsonObj) -> JsonObj:
    return {k: v for k, v in obj.items() if v not in (None, False, [], "", {}, 0)}


def task_summary(mirror: Mirror, task: Task) -> JsonObj:
    """One-line-ish view of a task, with ids resolved to names."""
    labels = {lb.id: lb.title for lb in mirror.labels()}
    note = (task.note or "").strip()
    subtasks = list(task.subtasks.values())
    return _compact(
        {
            "id": task.id,
            "title": task.title,
            "parent": " > ".join(mirror.path(task.parent_id)),
            "labels": [labels.get(i, i) for i in task.label_ids],
            "do_date": task.day if task.day != INBOX else None,
            "due_date": task.due_date,
            "end_date": task.end_date,
            "start_date": task.start_date,
            "planned_week": task.planned_week,
            "planned_month": task.planned_month,
            "estimate_min": task.estimate_minutes,
            "importance": task.star_level,
            "frog": task.frog_level,
            "backburner": task.backburner,
            "done": task.done,
            "recurring": task.recurring,
            "tracking": task.is_tracking,
            "subtasks": f"{sum(s.done for s in subtasks)}/{len(subtasks)}"
            if subtasks
            else None,
            "note_preview": note[:NOTE_PREVIEW]
            + ("…" if len(note) > NOTE_PREVIEW else ""),
        }
    )


def task_detail(mirror: Mirror, task: Task) -> JsonObj:
    """Everything about a task, including raw fields we don't model."""
    detail = task_summary(mirror, task)
    detail.pop("note_preview", None)
    extra = task.model_extra or {}
    detail.update(
        _compact(
            {
                "parent_id": task.parent_id,
                "note": task.note,
                "review_date": task.review_date,
                "daily_section": task.daily_section,
                "first_scheduled": task.first_scheduled,
                "subtask_list": [
                    {"id": s.id, "title": s.title, "done": s.done}
                    for s in sorted(task.subtasks.values(), key=lambda s: s.rank)
                ],
                "depends_on": [
                    mirror.docs.get(i, {}).get("title", i) for i in task.depends_on
                ],
                "minutes_tracked": task.duration / MS_PER_MINUTE
                if task.duration
                else None,
                "created_at": _iso(task.created_at),
                "updated_at": _iso(task.updated_at),
                "done_at": _iso(task.done_at),
                "other_fields": {
                    k: v
                    for k, v in extra.items()
                    if k not in _NOISE and v not in (None, "", 0, False, [], {})
                },
            }
        )
    )
    return detail


def project_detail(mirror: Mirror, cat: Category) -> JsonObj:
    """Everything about a project or category, including its open children."""
    labels = {lb.id: lb.title for lb in mirror.labels()}
    kids = [c for c in mirror.categories() if c.parent_id == cat.id and not c.done]
    tasks = mirror.search(TaskFilter(parent_id=cat.id, include_descendants=False))
    extra = cat.model_extra or {}
    return _compact(
        {
            "id": cat.id,
            "title": cat.title,
            "type": cat.type,
            "parent": " > ".join(mirror.path(cat.parent_id)),
            "parent_id": cat.parent_id,
            "labels": [labels.get(i, i) for i in cat.label_ids],
            "do_date": cat.day if cat.day != INBOX else None,
            "due_date": cat.due_date,
            "end_date": cat.end_date,
            "start_date": cat.start_date,
            "planned_week": cat.planned_week,
            "planned_month": cat.planned_month,
            "review_date": cat.review_date,
            "priority": cat.priority,
            "estimate_min": cat.estimate_minutes,
            "frog": cat.frog_level,
            "backburner": cat.backburner,
            "done": cat.done,
            "note": cat.note,
            "subprojects": [
                _compact({"id": c.id, "title": c.title, "type": c.type})
                for c in sorted(kids, key=lambda c: c.rank)
            ],
            "open_tasks": [task_summary(mirror, t) for t in tasks],
            "created_at": _iso(cat.created_at),
            "updated_at": _iso(cat.updated_at),
            "other_fields": {
                k: v
                for k, v in extra.items()
                if k not in _NOISE and v not in (None, "", 0, False, [], {})
            },
        }
    )


def _iso(ms: float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes")


def _summaries(mirror: Mirror, tasks: list[Task], limit: int) -> JsonObj:
    shown = tasks[:limit]
    out: JsonObj = {
        "count": len(tasks),
        "tasks": [task_summary(mirror, t) for t in shown],
    }
    if len(tasks) > limit:
        out["truncated"] = (
            f"showing {limit} of {len(tasks)}; narrow the filter or raise limit"
        )
    return out


def structure(mirror: Mirror) -> JsonObj:
    """Category/project tree with open-task counts, labels by group, strategies."""
    open_counts: dict[str, int] = {}
    for task in mirror.tasks():
        if not task.done:
            open_counts[task.parent_id] = open_counts.get(task.parent_id, 0) + 1
    cats = mirror.categories()
    children: dict[str, list[Category]] = {}
    for cat in cats:
        children.setdefault(cat.parent_id, []).append(cat)

    def node(cat: Category) -> JsonObj:
        kids = sorted(children.get(cat.id, []), key=lambda c: c.rank)
        return _compact(
            {
                "id": cat.id,
                "title": cat.title,
                "type": cat.type,
                "open_tasks": open_counts.get(cat.id, 0),
                "do_date": cat.day if cat.is_project else None,
                "due_date": cat.due_date if cat.is_project else None,
                "done": cat.done if cat.is_project else None,
                "children": [node(c) for c in kids],
            }
        )

    groups = {g.id: g for g in mirror.label_groups()}
    by_group: dict[str | None, list[JsonObj]] = {}
    for label in mirror.labels():
        by_group.setdefault(label.group_id, []).append(
            _compact({"id": label.id, "title": label.title, "hidden": label.is_hidden})
        )
    labels = [
        {
            "group": groups[gid].title if gid in groups else None,
            "exclusive": groups[gid].is_exclusive if gid in groups else False,
            "labels": items,
        }
        for gid, items in by_group.items()
    ]
    roots = sorted(children.get("root", []), key=lambda c: c.rank)
    return {
        "inbox_open_tasks": open_counts.get(INBOX, 0),
        "tree": [node(c) for c in roots],
        "labels": labels,
        "enabled_strategies": mirror.enabled_strategies(),
        "mirror": {"documents": len(mirror.docs), "age_seconds": round(mirror.age)},
    }


# --- the server -----------------------------------------------------------------------
def create_server(
    settings: Settings | None = None, *, state: State | None = None
) -> MCPServer[None]:
    """Build a configured :class:`MCPServer` (does not start it).

    ``state`` lets tests inject a pre-filled mirror and a mocked API client.
    """
    settings = settings or Settings()
    state = state or build_state(settings)
    mirror, api = state.mirror, state.api

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await api.aclose()
            if state.couch is not None:
                await state.couch.aclose()

    mcp = MCPServer(
        "marvin",
        instructions=instructions_for(settings),
        version=__version__,
        lifespan=lifespan,
    )

    async def fresh() -> Mirror:
        await mirror.refresh()
        if not mirror.docs:
            raise RuntimeError(
                "The mirror is empty. Configure MARVIN_SYNC_* credentials "
                "(Marvin → Strategies → API → settings) and run `marvin-mcp-server sync`."
            )
        return mirror

    # --- resources / prompts -----------------------------------------------------
    @mcp.resource("marvin://workflow", mime_type="text/markdown")
    def workflow_resource() -> str:
        """How this user works with Marvin (their own words)."""
        return load_workflow(settings.workflow_file) or NO_WORKFLOW

    @mcp.resource("marvin://structure", mime_type="application/json")
    async def structure_resource() -> JsonObj:
        """Category/project tree, labels, and enabled strategies."""
        return structure(await fresh())

    @mcp.prompt()
    def triage_inbox() -> str:
        """Go through the inbox item by item, applying the user's triage checklist."""
        return (
            "Let's triage my Marvin inbox. Call `list_inbox`, then take items one at "
            "a time: propose the changes my workflow calls for (title, labels, "
            "estimate, parent, do date / end date, deadline), ask me only when "
            "genuinely unclear, apply with `update_task`, then move to the next "
            f"item.\n\n{workflow_resource()}"
        )

    @mcp.prompt()
    def daily_review() -> str:
        """Show what the user checks every day."""
        return (
            "Give me my daily Marvin review: call `list_today`, and anything else my "
            f"workflow says I check daily. Keep it scannable.\n\n{workflow_resource()}"
        )

    # --- read tools ----------------------------------------------------------------
    @mcp.tool()
    async def sync_marvin() -> JsonObj:
        """Force-refresh the local mirror from Marvin (normally automatic)."""
        changed = await mirror.refresh(force=True)
        return {"changed_documents": changed, "documents": len(mirror.docs)}

    @mcp.tool()
    async def get_structure() -> JsonObj:
        """Category/project tree with ids and open-task counts, labels by group, strategies in use.

        Call this once at the start of a session; it is how you map names to ids.
        """
        return structure(await fresh())

    @mcp.tool()
    async def list_inbox(limit: int = 50) -> JsonObj:
        """Open tasks in the Inbox (not filed in any category/project), oldest first."""
        m = await fresh()
        tasks = m.search(TaskFilter(parent_id=INBOX, include_descendants=False))
        tasks.sort(key=lambda t: t.created_at or 0)
        return _summaries(m, tasks, limit)

    @mcp.tool()
    async def list_today(day: str = "today") -> JsonObj:
        """What's on for a day: do date that day, do date earlier but not done, due by then.

        Args:
            day: YYYY-MM-DD or today/tomorrow/yesterday.
        """
        m = await fresh()
        d = parse_day(day)
        scheduled = m.search(TaskFilter(day_from=d, day_to=d))
        overdue = m.search(TaskFilter(day_to=d - timedelta(days=1)))
        due = m.search(TaskFilter(due_by=d))
        return {
            "date": d.isoformat(),
            "scheduled": [task_summary(m, t) for t in scheduled],
            "scheduled_earlier_not_done": [task_summary(m, t) for t in overdue],
            "due_by_then": [task_summary(m, t) for t in due],
        }

    @mcp.tool()
    async def list_due(by: str = "week", limit: int = 100) -> JsonObj:
        """Open tasks with a Due date on or before a day (default: end of this week).

        Args:
            by: YYYY-MM-DD, today, tomorrow, week, next-week, month.
            limit: Maximum number of tasks to return.
        """
        m = await fresh()
        d = parse_day(by)
        tasks = m.search(TaskFilter(due_by=d))
        tasks.sort(key=lambda t: (t.due or date.max, t.title))
        return {"by": d.isoformat()} | _summaries(m, tasks, limit)

    @mcp.tool()
    async def search_tasks(
        text: Annotated[
            str | None, Field(description="substring of title or note")
        ] = None,
        parent: Annotated[
            str | None, Field(description="project/category id, name, or 'inbox'")
        ] = None,
        include_subprojects: bool = True,
        labels: Annotated[
            list[str] | None, Field(description="label names or ids; all must match")
        ] = None,
        scheduled_from: Annotated[
            str | None, Field(description="do date >= (YYYY-MM-DD or today…)")
        ] = None,
        scheduled_to: Annotated[
            str | None, Field(description="do date <= (YYYY-MM-DD or today…)")
        ] = None,
        unscheduled: Annotated[
            bool | None, Field(description="True = no do date")
        ] = None,
        due_by: Annotated[
            str | None, Field(description="due date <= (YYYY-MM-DD, week, month…)")
        ] = None,
        has_due_date: bool | None = None,
        end_by: Annotated[
            str | None, Field(description="end date <= (YYYY-MM-DD, week, month…)")
        ] = None,
        min_minutes: Annotated[
            float | None, Field(description="duration estimate >=")
        ] = None,
        max_minutes: Annotated[
            float | None, Field(description="duration estimate <=")
        ] = None,
        has_estimate: bool | None = None,
        important: Annotated[
            bool | None, Field(description="has any importance star")
        ] = None,
        frogged: bool | None = None,
        backburner: bool | None = None,
        done: Annotated[
            bool | None, Field(description="False (default) = open only")
        ] = False,
        limit: int = 50,
    ) -> JsonObj:
        """Find tasks by any combination of filters (all ANDead). Unset filters are ignored.

        This is the tool for "what can I do in 5 minutes", "everything tagged X",
        "what's scheduled this week in project Y", etc.
        """
        m = await fresh()
        flt = TaskFilter(
            text=text,
            parent_id=m.resolve_parent(parent) if parent else None,
            include_descendants=include_subprojects,
            label_ids=[m.resolve_label(lb).id for lb in labels] if labels else None,
            day_from=parse_day(scheduled_from) if scheduled_from else None,
            day_to=parse_day(scheduled_to) if scheduled_to else None,
            unscheduled=unscheduled,
            due_by=parse_day(due_by) if due_by else None,
            has_due_date=has_due_date,
            end_by=parse_day(end_by) if end_by else None,
            min_minutes=min_minutes,
            max_minutes=max_minutes,
            has_estimate=has_estimate,
            starred=important,
            frogged=frogged,
            backburner=backburner,
            done=done,
        )
        return _summaries(m, m.search(flt), min(limit, MAX_RESULTS))

    @mcp.tool()
    async def get_task(
        task_id: Annotated[str, Field(description="task or project id")],
    ) -> JsonObj:
        """Full details of one task (note, subtasks, dates, tracking) or project (its open children)."""
        m = await fresh()
        doc = m.docs.get(task_id)
        if doc is not None and doc.get("db") == "Categories":
            return project_detail(m, m.category(task_id))
        return task_detail(m, m.task(task_id))

    @mcp.tool()
    async def list_children(parent: str, limit: int = 100) -> JsonObj:
        """Open tasks and sub-projects directly inside a project/category (by id or name)."""
        m = await fresh()
        pid = m.resolve_parent(parent)
        subs = [c for c in m.categories() if c.parent_id == pid and not c.done]
        tasks = m.search(TaskFilter(parent_id=pid, include_descendants=False))
        return {
            "parent": " > ".join(m.path(pid)),
            "subprojects": [
                _compact(
                    {"id": c.id, "title": c.title, "type": c.type, "do_date": c.day}
                )
                for c in sorted(subs, key=lambda c: c.rank)
            ],
        } | _summaries(m, tasks, limit)

    # --- write tools ---------------------------------------------------------------
    @mcp.tool()
    async def create_task(
        title: str,
        parent: Annotated[
            str, Field(description="project/category id or name")
        ] = "inbox",
        labels: list[str] | None = None,
        do_date: Annotated[
            str | None, Field(description="day to do it: YYYY-MM-DD or today…")
        ] = None,
        due_date: Annotated[str | None, Field(description="hard deadline")] = None,
        end_date: Annotated[
            str | None, Field(description="self-imposed target (needs full access)")
        ] = None,
        start_date: Annotated[
            str | None, Field(description="hidden until then (needs full access)")
        ] = None,
        estimate_minutes: float | None = None,
        note: str | None = None,
        importance: Annotated[
            int, Field(ge=0, le=3, description="3=P1 red, 2=P2, 1=P3/star")
        ] = 0,
        frog: Annotated[int, Field(ge=0, le=3)] = 0,
        planned_week: Annotated[
            str | None, Field(description="Monday, YYYY-MM-DD")
        ] = None,
        planned_month: Annotated[str | None, Field(description="YYYY-MM")] = None,
        backburner: bool = False,
    ) -> JsonObj:
        """Create a task. Names for parent/labels are resolved here; don't use #/@ shortcuts."""
        m = await fresh()
        payload: JsonObj = _compact(
            {
                "title": title,
                "parentId": m.resolve_parent(parent),
                "labelIds": [m.resolve_label(lb).id for lb in labels]
                if labels
                else None,
                "day": _opt_day(do_date),
                "dueDate": _opt_day(due_date),
                "timeEstimate": _ms(estimate_minutes),
                "note": note,
                "isStarred": importance or None,
                "isFrogged": frog or None,
                "plannedWeek": planned_week,
                "plannedMonth": planned_month,
                "backburner": backburner,
            }
        )
        payload["done"] = False
        payload["timeZoneOffset"] = tz_offset_minutes()
        result = await api.add_task(payload)
        _absorb(mirror, result)
        new_id = result.get("_id")
        # addTask ignores start/end dates; set them with a follow-up edit.
        follow_up: JsonObj = _compact(
            {"endDate": _opt_day(end_date), "startDate": _opt_day(start_date)}
        )
        if follow_up and isinstance(new_id, str):
            mirror.apply(await api.update_doc(new_id, follow_up))
        return {"created": new_id, "title": title}

    @mcp.tool()
    async def create_project(
        title: str,
        parent: Annotated[
            str, Field(description="category/project id or name")
        ] = "inbox",
        labels: list[str] | None = None,
        do_date: str | None = None,
        due_date: str | None = None,
        note: str | None = None,
        priority: Annotated[str | None, Field(description="low, mid, high")] = None,
    ) -> JsonObj:
        """Create a project (container for tasks). Convert an inbox item into one with this + update_task(parent=…)."""
        m = await fresh()
        payload: JsonObj = _compact(
            {
                "title": title,
                "parentId": m.resolve_parent(parent),
                "labelIds": [m.resolve_label(lb).id for lb in labels]
                if labels
                else None,
                "day": _opt_day(do_date),
                "dueDate": _opt_day(due_date),
                "note": note,
                "priority": priority,
            }
        )
        payload["done"] = False
        payload["timeZoneOffset"] = tz_offset_minutes()
        result = await api.add_project(payload)
        _absorb(mirror, result)
        return {"created": result.get("_id"), "title": title}

    @mcp.tool()
    async def update_task(
        item_id: Annotated[str, Field(description="task or project id")],
        title: str | None = None,
        parent: Annotated[
            str | None, Field(description="move to project/category/inbox")
        ] = None,
        labels: Annotated[
            list[str] | None, Field(description="replace all labels")
        ] = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        do_date: Annotated[
            str | None, Field(description="day to do it: YYYY-MM-DD, today…")
        ] = None,
        due_date: Annotated[str | None, Field(description="hard deadline")] = None,
        end_date: Annotated[
            str | None, Field(description="self-imposed target")
        ] = None,
        start_date: Annotated[
            str | None, Field(description="hidden until then")
        ] = None,
        estimate_minutes: float | None = None,
        note: Annotated[
            str | None, Field(description="replace the note (markdown)")
        ] = None,
        append_note: Annotated[
            str | None, Field(description="add to the end of the note")
        ] = None,
        importance: Annotated[
            int | None,
            Field(ge=0, le=3, description="3=P1 red, 2=P2, 1=P3/star, 0=none"),
        ] = None,
        frog: Annotated[int | None, Field(ge=0, le=3)] = None,
        backburner: bool | None = None,
        planned_week: Annotated[
            str | None, Field(description="Monday, YYYY-MM-DD")
        ] = None,
        planned_month: Annotated[str | None, Field(description="YYYY-MM")] = None,
        review_date: str | None = None,
        clear: Annotated[
            list[str] | None,
            Field(
                description="fields to unset: do_date, due_date, end_date, start_date, "
                "estimate, note, planned_week, planned_month, review_date"
            ),
        ] = None,
    ) -> JsonObj:
        """Edit a task or project: rename, move, relabel, (re)schedule, deadlines, estimate, note.

        Only the arguments you pass are changed. Requires the full-access token.
        """
        m = await fresh()
        current = m.docs.get(item_id)
        if current is None:
            raise LookupError(f"no item with id {item_id!r}")
        changes: JsonObj = _compact(
            {
                "title": title,
                "parentId": m.resolve_parent(parent) if parent is not None else None,
                "day": _opt_day(do_date),
                "dueDate": _opt_day(due_date),
                "endDate": _opt_day(end_date),
                "startDate": _opt_day(start_date),
                "timeEstimate": _ms(estimate_minutes),
                "note": note,
                "plannedWeek": planned_week,
                "plannedMonth": planned_month,
                "reviewDate": review_date,
            }
        )
        if labels is not None or add_labels or remove_labels:
            raw_ids = current.get("labelIds")
            ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
            if labels is not None:
                ids = [m.resolve_label(lb).id for lb in labels]
            for lb in add_labels or []:
                if (lid := m.resolve_label(lb).id) not in ids:
                    ids.append(lid)
            for lb in remove_labels or []:
                with contextlib.suppress(ValueError):
                    ids.remove(m.resolve_label(lb).id)
            changes["labelIds"] = ids
        if append_note is not None:
            old = current.get("note")
            old_text = old.rstrip() if isinstance(old, str) and old.strip() else ""
            changes["note"] = (
                f"{old_text}\n\n{append_note}" if old_text else append_note
            )
        if importance is not None:
            changes["isStarred"] = importance or False
        if frog is not None:
            changes["isFrogged"] = frog or False
        if backburner is not None:
            changes["backburner"] = backburner
        for field in clear or []:
            key, empty = _CLEARABLE.get(field, (None, None))
            if key is None:
                raise ValueError(f"cannot clear {field!r}; one of {sorted(_CLEARABLE)}")
            changes[key] = empty
        if not changes:
            raise ValueError("nothing to change")
        result = await api.update_doc(item_id, changes)
        mirror.apply(result)
        if result.get("db") == "Tasks":
            return {"updated": sorted(changes)} | task_summary(
                m, Task.model_validate(result)
            )
        return {"updated": sorted(changes), "id": item_id, "title": result.get("title")}

    @mcp.tool()
    async def update_subtasks(
        task_id: str,
        add: Annotated[
            list[str] | None, Field(description="new subtask titles, appended")
        ] = None,
        complete: Annotated[
            list[str] | None, Field(description="subtask ids or titles to mark done")
        ] = None,
        reopen: Annotated[
            list[str] | None,
            Field(description="subtask ids or titles to mark not done"),
        ] = None,
        remove: Annotated[
            list[str] | None, Field(description="subtask ids or titles to delete")
        ] = None,
        rename: Annotated[
            dict[str, str] | None, Field(description="{id or title: new title}")
        ] = None,
    ) -> JsonObj:
        """Add, complete, reopen, rename or remove subtasks of a task. Requires full access."""
        m = await fresh()
        task = m.task(task_id)
        subs: dict[str, JsonObj] = {
            sid: s.model_dump(by_alias=True, exclude_none=True)
            for sid, s in task.subtasks.items()
        }

        def find(ref: str) -> str:
            if ref in subs:
                return ref
            hits = [
                sid
                for sid, s in subs.items()
                if str(s.get("title", "")).casefold() == ref.casefold()
            ]
            if len(hits) != 1:
                raise LookupError(f"subtask {ref!r} not found or ambiguous")
            return hits[0]

        next_rank = (
            max((float(str(s.get("rank", 0))) for s in subs.values()), default=-1) + 1
        )
        for title in add or []:
            sid = new_id()
            subs[sid] = {"_id": sid, "title": title, "done": False, "rank": next_rank}
            next_rank += 1
        for ref in complete or []:
            subs[find(ref)]["done"] = True
        for ref in reopen or []:
            subs[find(ref)]["done"] = False
        for ref, title in (rename or {}).items():
            subs[find(ref)]["title"] = title
        for ref in remove or []:
            del subs[find(ref)]
        if not any((add, complete, reopen, remove, rename)):
            raise ValueError("nothing to change")
        result = await api.update_doc(task_id, {"subtasks": subs})
        mirror.apply(result)
        return task_detail(m, Task.model_validate(result))

    @mcp.tool()
    async def create_category(
        title: str,
        parent: Annotated[
            str, Field(description="parent category id or name; 'root' = top level")
        ] = "root",
        color: Annotated[str | None, Field(description="#RRGGBB")] = None,
    ) -> JsonObj:
        """Create a category (a folder in the Master List). Categories can only live under categories."""
        m = await fresh()
        parent_id = m.resolve_parent(parent)
        if parent_id != "root" and m.category(parent_id).is_project:
            raise ValueError("categories can't be created inside a project")
        siblings = [c.rank for c in m.categories() if c.parent_id == parent_id]
        now = _now_ms()
        doc: JsonObj = _compact(
            {
                "_id": new_id(),
                "db": "Categories",
                "type": "category",
                "title": title,
                "parentId": parent_id,
                "rank": (max(siblings) + 1) if siblings else 0,
                "color": color,
                "createdAt": now,
                "updatedAt": now,
            }
        )
        stored = doc | await api.create_doc(doc)
        mirror.apply(stored)
        return {
            "created": stored["_id"],
            "title": title,
            "parent": " > ".join(m.path(parent_id)),
        }

    @mcp.tool()
    async def create_label(
        title: str,
        group: Annotated[
            str | None, Field(description="existing label group (name or id)")
        ] = None,
        new_group: Annotated[
            str | None, Field(description="create this group and put the label in it")
        ] = None,
        new_group_exclusive: Annotated[
            bool, Field(description="only one label of the new group per task")
        ] = False,
        color: Annotated[str | None, Field(description="#RRGGBB")] = None,
    ) -> JsonObj:
        """Create a label (optionally inside an existing or new label group).

        Requires the Task Labels strategy to be enabled in Marvin.
        """
        m = await fresh()
        strategy = m.docs.get(LABELS_STRATEGY_DOC)
        if not (strategy and strategy.get("val")):
            raise RuntimeError(
                "The Task Labels strategy is off; enable it in Marvin (Strategies) first."
            )
        for existing in m.labels():
            if existing.title.casefold() == title.casefold():
                raise ValueError(
                    f"label {existing.title!r} already exists ({existing.id})"
                )
        now = _now_ms()
        group_id: str | None = None
        if new_group is not None:
            groups_doc = m.docs.get(LABEL_GROUPS_DOC)
            raw_groups = groups_doc.get("val") if groups_doc else None
            groups = dict(raw_groups) if isinstance(raw_groups, dict) else {}
            group_id = new_id()
            groups[group_id] = {
                "_id": group_id,
                "title": new_group,
                "rank": len(groups) + 1,
                "createdAt": now,
                "isExclusive": new_group_exclusive,
            }
            mirror.apply(await _write_profile(api, m, LABEL_GROUPS_DOC, groups, now))
        elif group is not None:
            group_id = m.resolve_label_group(group).id
        labels_doc = m.docs.get(LABELS_DOC)
        raw_labels = labels_doc.get("val") if labels_doc else None
        labels = list(raw_labels) if isinstance(raw_labels, list) else []
        label: JsonObj = _compact(
            {
                "_id": new_id(),
                "title": title,
                "color": color,
                "createdAt": now,
                "groupId": group_id,
            }
        )
        labels.append(label)
        mirror.apply(await _write_profile(api, m, LABELS_DOC, labels, now))
        stored = m.resolve_label(title)  # id as Marvin stored it
        return {"created": stored.id, "title": title, "group": new_group or group}

    @mcp.tool()
    async def mark_done(item_id: str) -> JsonObj:
        """Mark a task or project done (handles recurring/echo tasks and stops tracking)."""
        result = await api.mark_done(item_id, tz_offset_minutes())
        _absorb(mirror, result)
        return {"done": item_id, "title": result.get("title")}

    return mcp


# field name the model uses -> (Marvin key, value that means "unset")
_CLEARABLE: dict[str, tuple[str, object]] = {
    "do_date": ("day", INBOX),
    "due_date": ("dueDate", None),
    "end_date": ("endDate", None),
    "start_date": ("startDate", None),
    "estimate": ("timeEstimate", None),
    "note": ("note", ""),
    "planned_week": ("plannedWeek", None),
    "planned_month": ("plannedMonth", None),
    "review_date": ("reviewDate", None),
}


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)  # noqa: DTZ005


async def _write_profile(
    api: MarvinAPI, mirror: Mirror, doc_id: str, value: object, now: int
) -> JsonObj:
    """Set ``val`` of a ProfileItems doc, creating the doc if Marvin never wrote it."""
    if doc_id in mirror.docs:
        return await api.update_doc(doc_id, {"val": value})
    doc: JsonObj = {
        "_id": doc_id,
        "db": "ProfileItems",
        "val": value,
        "createdAt": now,
        "updatedAt": now,
    }
    return doc | await api.create_doc(doc)


def _ms(minutes: float | None) -> int | None:
    return None if minutes is None else int(minutes * MS_PER_MINUTE)


def _absorb(mirror: Mirror, result: JsonObj) -> None:
    """Fold a write's response into the mirror, or force a re-poll if it's not a doc."""
    if isinstance(result.get("_id"), str) and result.get("db"):
        mirror.apply(result)
    else:
        mirror.synced_at = 0.0
