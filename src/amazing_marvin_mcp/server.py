"""The MCP server: tools, resources and prompts over the mirror and the REST API.

Tools are shaped around what a person asks ("what's in my inbox", "what's due
this week", "rename X") rather than around Marvin's endpoints, and use the
names Marvin's UI uses (Do date, Due date, End date, Importance, Duration
estimate, Backburner...). Anything opinionated about *how* to triage or plan is
left to the user's workflow files.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server import MCPServer
from pydantic import Field, SecretStr

from . import __version__
from .api import MarvinAPI
from .couch import CouchClient
from .errors import MarvinError
from .mirror import (
    LABEL_GROUPS_DOC,
    LABELS_DOC,
    LABELS_STRATEGY_DOC,
    Mirror,
    TaskFilter,
    new_id,
)
from .models import INBOX, MS_PER_MINUTE, ROOT, Category, JsonObj, Task
from .outputs import (
    BriefHit,
    Children,
    Created,
    DayView,
    Done,
    ItemDetail,
    ItemRef,
    ItemSummary,
    LabelGroupOut,
    LabelOut,
    MirrorInfo,
    Profile,
    SearchHit,
    SearchResult,
    Structure,
    SubtaskOut,
    SyncResult,
    TaskList,
    TreeNode,
    Updated,
)
from .query import STARS_PRIORITY, Branch, Kind, Lens, field_help, parse_day
from .query import search as run_search
from .query import tree as build_tree
from .settings import Settings
from .workflow import Workflow

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
  tree with ids, a few levels at a time. The Master List is that whole tree.
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
  dreaded task, 1-3. Backburner = not now: parked by hand, by label, by a
  future start date, by an open dependency, or because its parent is.
  Lists show active items unless you ask for more.
- Labels are free-form and user-defined (`get_profile` lists them); their
  meaning is in the workflow section below, not in Marvin.

Reads come from a local mirror refreshed at most once a minute; call
`sync_marvin` if the user says they just changed something in Marvin.
Writes are rate-limited (about one per 3 seconds); for bulk edits go item by
item and tell the user it takes a moment rather than stopping.
Use `search` for any "which items…" question; use `get_task` before editing
so you see the current state. Never guess ids: resolve names via
`get_structure` or a search first.
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


def instructions_for(workflow: Workflow) -> str:
    """Server instructions: generic Marvin semantics + the user's own workflow."""
    return f"{GENERIC_INSTRUCTIONS}\n## This user's workflow\n{workflow.text()}"


# --- date helpers ---------------------------------------------------------------------
def tz_offset_minutes() -> int:
    """Local UTC offset in minutes, the sign Marvin expects (Pacific = -480)."""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return int(offset.total_seconds() // 60)


def _opt_day(value: str | None) -> str | None:
    return None if value is None else parse_day(value).isoformat()


def _iso(ms: float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes")


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)  # noqa: DTZ005


def _ms(minutes: float | None) -> int | None:
    return None if minutes is None else int(minutes * MS_PER_MINUTE)


def _compact(obj: JsonObj) -> JsonObj:
    return {k: v for k, v in obj.items() if v not in (None, False, [], "", {}, 0)}


# --- rendering ------------------------------------------------------------------------
def summary(lens: Lens, item: Task | Category) -> ItemSummary:
    """One-line-ish view of a task or project, with ids resolved to names."""
    note = (item.note or "").strip()
    subtasks = list(item.subtasks.values()) if isinstance(item, Task) else []
    reasons = [] if item.done else lens.backburner_reasons(item)
    return ItemSummary(
        id=item.id,
        title=item.title,
        type=lens.kind(item),
        parent=lens.path(item.parent_id),
        labels=lens.labels(item),
        do_date=item.day if item.day != INBOX else None,
        due_date=item.due_date,
        end_date=item.end_date,
        start_date=item.start_date,
        planned_week=item.planned_week,
        planned_month=item.planned_month,
        estimate_min=item.estimate_minutes,
        importance=lens.importance(item),
        frog=item.frog_level,
        backburner=bool(reasons),
        why_backburner=reasons,
        done=item.done,
        recurring=item.recurring,
        tracking=isinstance(item, Task) and item.is_tracking,
        subtasks=f"{sum(s.done for s in subtasks)}/{len(subtasks)}"
        if subtasks
        else None,
        note_preview=note[:NOTE_PREVIEW] + ("…" if len(note) > NOTE_PREVIEW else "")
        if note
        else None,
    )


def _other_fields(extra: JsonObj | None) -> JsonObj:
    return {
        k: v
        for k, v in (extra or {}).items()
        if k not in _NOISE and v not in (None, "", 0, False, [], {})
    }


def task_detail(lens: Lens, task: Task) -> ItemDetail:
    """Everything about a task, including raw fields we don't model."""
    # dict(model) keeps every field; model_dump() would drop the compacted ones.
    base = dict(summary(lens, task))
    base.pop("note_preview", None)
    docs = lens.mirror.docs
    return ItemDetail.model_validate(
        {
            **base,
            "parent_id": task.parent_id,
            "note": task.note,
            "review_date": task.review_date,
            "daily_section": task.daily_section,
            "first_scheduled": task.first_scheduled,
            "subtask_list": [
                SubtaskOut(id=s.id, title=s.title, done=s.done)
                for s in sorted(task.subtasks.values(), key=lambda s: s.rank)
            ],
            "depends_on": [
                str(docs.get(i, {}).get("title", i)) for i in task.depends_on
            ],
            "minutes_tracked": task.duration / MS_PER_MINUTE if task.duration else None,
            "created_at": _iso(task.created_at),
            "updated_at": _iso(task.updated_at),
            "done_at": _iso(task.done_at),
            "other_fields": _other_fields(task.model_extra),
        }
    )


def project_detail(lens: Lens, cat: Category) -> ItemDetail:
    """Everything about a project or category, including its open children."""
    mirror = lens.mirror
    kids = [c for c in lens.categories.values() if c.parent_id == cat.id and not c.done]
    tasks = mirror.search(TaskFilter(parent_id=cat.id, include_descendants=False))
    base = dict(summary(lens, cat))
    base.pop("note_preview", None)
    return ItemDetail.model_validate(
        {
            **base,
            "parent_id": cat.parent_id,
            "note": cat.note,
            "review_date": cat.review_date,
            "priority": cat.priority,
            "depends_on": [
                str(mirror.docs.get(i, {}).get("title", i)) for i in cat.depends_on
            ],
            "subprojects": [
                ItemRef(id=c.id, title=c.title, type=c.type)
                for c in sorted(kids, key=lambda c: c.rank)
            ],
            "open_tasks": [summary(lens, t) for t in tasks],
            "created_at": _iso(cat.created_at),
            "updated_at": _iso(cat.updated_at),
            "other_fields": _other_fields(cat.model_extra),
        }
    )


def _task_list(
    lens: Lens, tasks: list[Task], limit: int, *, include_backburner: bool
) -> TaskList:
    """Page ``tasks``, without those that are themselves on the Backburner.

    Only an item's own causes count here, not its containers': the caller asked
    for this container, so what is above it should not empty the list.
    """
    everything = len(tasks)
    if not include_backburner:
        tasks = [t for t in tasks if not lens.own_reasons(t)]
    truncated = (
        f"showing {limit} of {len(tasks)}; narrow the filter or raise limit"
        if len(tasks) > limit
        else None
    )
    return TaskList(
        count=len(tasks),
        tasks=[summary(lens, t) for t in tasks[:limit]],
        truncated=truncated,
        backburner_hidden=everything - len(tasks),
    )


def structure(
    lens: Lens,
    *,
    root: str = ROOT,
    depth: int = 2,
    include_done: bool = False,
    include_backburner: bool = False,
) -> Structure:
    """Categories and projects below ``root`` with open-task counts.

    Done projects and Backburner containers, and everything nested in them, are
    left out unless asked for: a long-lived account has far more of them than
    active ones.
    """
    found = build_tree(
        lens,
        root=root,
        depth=depth,
        include_done=include_done,
        include_backburner=include_backburner,
    )

    def node(branch: Branch) -> TreeNode:
        cat = branch.item
        project = cat.is_project
        return TreeNode(
            id=cat.id,
            title=cat.title,
            type=cat.type,
            tasks=branch.tasks,
            tasks_below=branch.tasks_below
            if branch.tasks_below != branch.tasks
            else None,
            done_tasks_below=branch.done_tasks_below if include_done else None,
            importance=lens.importance(cat),
            do_date=cat.day if project and cat.day != INBOX else None,
            due_date=cat.due_date if project else None,
            done=cat.done if project else False,
            backburner=bool(lens.backburner_reasons(cat)),
            more_categories=branch.more_categories,
            more_projects=branch.more_projects,
            children=[node(b) for b in branch.children],
        )

    top = root == ROOT
    return Structure(
        root="Master List" if top else lens.path(root),
        depth=depth,
        tasks=found.tasks,
        inbox_tasks=found.inbox_tasks if top else None,
        tree=[node(b) for b in found.branches],
        done_projects_hidden=found.done_projects_hidden,
        backburner_hidden=found.backburner_hidden,
    )


def profile(lens: Lens) -> Profile:
    """Labels by group, Backburner labels, strategies in use, mirror state."""
    mirror = lens.mirror
    groups = {g.id: g for g in mirror.label_groups()}
    by_group: dict[str | None, list[LabelOut]] = {}
    for label in mirror.labels():
        by_group.setdefault(label.group_id, []).append(
            LabelOut(id=label.id, title=label.title, hidden=label.is_hidden)
        )
    return Profile(
        labels=[
            LabelGroupOut(
                group=groups[gid].title if gid in groups else None,
                exclusive=groups[gid].is_exclusive if gid in groups else False,
                labels=items,
            )
            for gid, items in by_group.items()
        ],
        backburner_labels=sorted(
            lens.label_titles.get(i, i) for i in lens.backburner_label_ids
        ),
        enabled_strategies=mirror.enabled_strategies(),
        mirror=MirrorInfo(documents=len(mirror.docs), age_seconds=round(mirror.age)),
    )


SEARCH_DOC = f"""\
Find tasks and projects. Use `text`, `where`, or both.

`text`: words looked up in titles, notes and subtask titles, best match first
(a word in the title beats one in a note). Every word must appear, unless
match="any", which ranks by how many do: pass synonyms and translations then.

`where`: a filter expression over these fields:
{field_help()}.
Operators: == != < <= > >= in, and / or / not, parentheses.
Dates: today, tomorrow, yesterday, week_end, next_week_end, month_end,
date("YYYY-MM-DD"), days(n). Under a container: under("name or id").
Text ignores case; an unset field is None and never passes < or >.

Examples:
  importance == 3 and do_date == tomorrow
  "errand" in labels and estimate_min <= 30 and under("Home")
  due_date <= today + days(7) or importance >= 2
  not labels and estimate_min == None
  kind == "project" and has_dependency

Done and Backburner are two separate switches. By default neither done items
nor items on the Backburner are returned; include_done / include_backburner
add them. A `where` that tests done, or a Backburner cause (has_dependency,
start_date…), turns that switch on. The result counts what each left out.
Events and recurring-task templates are not searched.
"""


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
    workflow = Workflow(settings.workflow_file)

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
        instructions=instructions_for(workflow),
        version=__version__,
        lifespan=lifespan,
    )

    async def fresh() -> Mirror:
        await mirror.refresh()
        if not mirror.docs:
            raise MarvinError(
                "The mirror is empty. Configure MARVIN_SYNC_* credentials "
                "(Marvin → Strategies → API → settings) and run `amazing-marvin-mcp sync`."
            )
        return mirror

    # --- resources / prompts -----------------------------------------------------
    @mcp.resource("marvin://workflow", mime_type="text/markdown")
    def workflow_resource() -> str:
        """How this user works with Marvin (their own words), all sections."""
        return workflow.text()

    @mcp.resource("marvin://workflow/{section}", mime_type="text/markdown")
    def workflow_section(section: str) -> str:
        """One section of the user's workflow (a file in the workflow directory)."""
        return workflow.section(section)

    @mcp.resource("marvin://structure", mime_type="application/json")
    async def structure_resource() -> Structure:
        """Top two levels of the category/project tree, with open-task counts."""
        return structure(Lens(await fresh()))

    @mcp.prompt()
    def triage_inbox() -> str:
        """Go through the inbox item by item, applying the user's triage checklist."""
        return (
            "Let's triage my Marvin inbox. Call `list_inbox`, then take items one at "
            "a time: propose the changes my workflow calls for (title, labels, "
            "estimate, parent, do date / end date, deadline), ask me only when "
            "genuinely unclear, apply with `update_task`, then move to the next "
            f"item.\n\n{workflow.text(prefer='triage')}"
        )

    @mcp.prompt()
    def daily_review() -> str:
        """Show what the user checks every day."""
        return (
            "Give me my daily Marvin review: call `list_today`, and anything else my "
            "workflow says I check daily. Keep it scannable.\n\n"
            f"{workflow.text(prefer='daily')}"
        )

    # --- read tools ----------------------------------------------------------------
    @mcp.tool()
    async def sync_marvin() -> SyncResult:
        """Force-refresh the local mirror from Marvin (normally automatic)."""
        changed = await mirror.refresh(force=True)
        return SyncResult(changed_documents=changed, documents=len(mirror.docs))

    @mcp.tool()
    async def get_structure(
        root: Annotated[
            str, Field(description="category/project id or name; default: the top")
        ] = ROOT,
        depth: Annotated[int, Field(ge=1, le=8, description="levels to list")] = 2,
        include_done: Annotated[
            bool, Field(description="also list done projects and done-task counts")
        ] = False,
        include_backburner: Annotated[
            bool, Field(description="also list containers on the Backburner")
        ] = False,
    ) -> Structure:
        """Categories and projects below a root, with ids and open-task counts. No tasks.

        Call it once at the start of a session to map names to ids, then again
        with `root` set to go deeper: a node at the last level says how many
        categories and projects are below it.
        """
        m = await fresh()
        return structure(
            Lens(m),
            root=m.resolve_parent(root),
            depth=depth,
            include_done=include_done,
            include_backburner=include_backburner,
        )

    @mcp.tool()
    async def get_profile() -> Profile:
        """Labels by group (and which ones send items to the Backburner), strategies in use, mirror state."""
        return profile(Lens(await fresh()))

    @mcp.tool()
    async def list_inbox(
        limit: int = 50,
        include_backburner: Annotated[
            bool, Field(description="also tasks on the Backburner")
        ] = False,
    ) -> TaskList:
        """Open tasks in the Inbox (not filed in any category/project), oldest first."""
        m = await fresh()
        tasks = m.search(TaskFilter(parent_id=INBOX, include_descendants=False))
        tasks.sort(key=lambda t: t.created_at or 0)
        return _task_list(Lens(m), tasks, limit, include_backburner=include_backburner)

    @mcp.tool()
    async def list_today(
        day: str = "today",
        include_backburner: Annotated[
            bool, Field(description="also items on the Backburner")
        ] = True,
    ) -> DayView:
        """What's on for a day: do date that day, do date earlier but not done, due by then.

        Items on the Backburner are listed too, as a date on them is a decision
        to deal with them; include_backburner=false leaves them out.

        Args:
            day: YYYY-MM-DD or today/tomorrow/yesterday.
            include_backburner: false to leave Backburner items out.
        """
        m = await fresh()
        lens = Lens(m)
        d = parse_day(day)
        hidden = 0

        def rows(tasks: list[Task]) -> list[ItemSummary]:
            nonlocal hidden
            kept = [
                t for t in tasks if include_backburner or not lens.backburner_reasons(t)
            ]
            hidden += len(tasks) - len(kept)
            return [summary(lens, t) for t in kept]

        return DayView(
            date=d.isoformat(),
            scheduled=rows(m.search(TaskFilter(day_from=d, day_to=d))),
            scheduled_earlier_not_done=rows(
                m.search(TaskFilter(day_to=d - timedelta(days=1)))
            ),
            due_by_then=rows(m.search(TaskFilter(due_by=d))),
            backburner_hidden=hidden,
        )

    @mcp.tool(description=SEARCH_DOC)
    async def search(
        text: Annotated[
            str | None, Field(description='words to look for; "quote a phrase"')
        ] = None,
        where: Annotated[
            str | None, Field(description="filter expression, see the description")
        ] = None,
        kinds: Annotated[
            list[Kind] | None,
            Field(description="task, project, category; default task and project"),
        ] = None,
        include_done: Annotated[
            bool | None, Field(description="also done items; default: see above")
        ] = None,
        include_backburner: Annotated[
            bool | None,
            Field(description="also items on the Backburner; default: see above"),
        ] = None,
        match: Annotated[
            Literal["all", "any"], Field(description="how many `text` words must hit")
        ] = "all",
        sort: Annotated[
            str | None,
            Field(description="a field name, -name for descending; default: best"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 30,
        offset: Annotated[int, Field(ge=0)] = 0,
        brief: Annotated[
            bool, Field(description="only id, title, parent: for wide scans")
        ] = False,
    ) -> SearchResult:
        lens = Lens(await fresh())
        hits, scope = run_search(
            lens,
            text=text,
            where=where,
            kinds=set(kinds) if kinds else None,
            include_done=include_done,
            include_backburner=include_backburner,
            match=match,
            sort=sort,
        )
        page = hits[offset : offset + limit]
        items: list[SearchHit | BriefHit] = []
        for hit in page:
            row = summary(lens, hit.item)
            if brief:
                items.append(
                    BriefHit(
                        id=row.id,
                        title=row.title,
                        type=row.type,
                        parent=row.parent,
                        done=row.done,
                        backburner=row.backburner,
                        matched_in=hit.matched_in,
                    )
                )
            else:
                items.append(
                    SearchHit.model_validate(
                        {
                            **dict(row),
                            "matched_in": hit.matched_in,
                            "snippet": hit.snippet,
                        }
                    )
                )
        more = offset + limit < len(hits)
        return SearchResult(
            count=len(hits),
            include_done=scope.include_done,
            include_backburner=scope.include_backburner,
            done_hidden=scope.done_hidden,
            backburner_hidden=scope.backburner_hidden,
            items=items,
            next_offset=offset + limit if more else None,
        )

    @mcp.tool()
    async def get_task(
        task_id: Annotated[str, Field(description="task or project id")],
    ) -> ItemDetail:
        """Full details of one task (note, subtasks, dates, tracking) or project (its open children)."""
        m = await fresh()
        lens = Lens(m)
        doc = m.docs.get(task_id)
        if doc is not None and doc.get("db") == "Categories":
            return project_detail(lens, m.category(task_id))
        return task_detail(lens, m.task(task_id))

    @mcp.tool()
    async def list_children(
        parent: str,
        limit: int = 100,
        include_backburner: Annotated[
            bool, Field(description="also children on the Backburner")
        ] = False,
    ) -> Children:
        """Open tasks and sub-projects directly inside a project/category (by id or name).

        Leaves out children that are themselves on the Backburner (flag, label,
        start date, dependency, later step of a sequential project) unless
        include_backburner; the parent being on the Backburner hides nothing.
        """
        m = await fresh()
        lens = Lens(m)
        pid = m.resolve_parent(parent)
        every_sub = [c for c in m.categories() if c.parent_id == pid and not c.done]
        subs = [c for c in every_sub if include_backburner or not lens.own_reasons(c)]
        tasks = m.search(TaskFilter(parent_id=pid, include_descendants=False))
        page = _task_list(lens, tasks, limit, include_backburner=include_backburner)
        return Children(
            parent=" > ".join(m.path(pid)),
            subprojects=[
                ItemRef(id=c.id, title=c.title, type=c.type, do_date=c.day or None)
                for c in sorted(subs, key=lambda c: c.rank)
            ],
            count=page.count,
            tasks=page.tasks,
            truncated=page.truncated,
            backburner_hidden=page.backburner_hidden + len(every_sub) - len(subs),
        )

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
    ) -> Created:
        """Create a task. Names for parent/labels are resolved here; don't use #/@ shortcuts."""
        m = await fresh()
        parent_id = m.resolve_parent(parent)
        payload: JsonObj = _compact(
            {
                "title": title,
                "parentId": parent_id,
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
        new_id_ = result.get("_id")
        # addTask ignores start/end dates; set them with a follow-up edit.
        follow_up: JsonObj = _compact(
            {"endDate": _opt_day(end_date), "startDate": _opt_day(start_date)}
        )
        if follow_up and isinstance(new_id_, str):
            mirror.apply(await api.update_doc(new_id_, follow_up))
        return Created(
            created=str(new_id_), title=title, parent=" > ".join(m.path(parent_id))
        )

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
    ) -> Created:
        """Create a project (container for tasks). Convert an inbox item into one with this + update_task(parent=…)."""
        m = await fresh()
        parent_id = m.resolve_parent(parent)
        payload: JsonObj = _compact(
            {
                "title": title,
                "parentId": parent_id,
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
        return Created(
            created=str(result.get("_id")),
            title=title,
            parent=" > ".join(m.path(parent_id)),
        )

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
    ) -> Updated:
        """Edit a task or project: rename, move, relabel, (re)schedule, deadlines, estimate, note.

        Only the arguments you pass are changed. Requires the full-access token.
        """
        m = await fresh()
        current = m.docs.get(item_id)
        if current is None:
            raise MarvinError(f"no item with id {item_id!r}")
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
            if current.get("db") != "Categories":
                changes["isStarred"] = importance or False
            elif current.get("type") == "project":
                # Projects keep their stars in `priority`, not `isStarred`.
                changes["priority"] = STARS_PRIORITY.get(importance)
            else:
                raise MarvinError("a category has no importance; only its projects")
        if frog is not None:
            changes["isFrogged"] = frog or False
        if backburner is not None:
            changes["backburner"] = backburner
        for field in clear or []:
            key, empty = _CLEARABLE.get(field, (None, None))
            if key is None:
                raise MarvinError(
                    f"cannot clear {field!r}; one of {sorted(_CLEARABLE)}"
                )
            changes[key] = empty
        if not changes:
            raise MarvinError("nothing to change")
        result = await api.update_doc(item_id, changes)
        mirror.apply(result)
        item: Task | Category = (
            Task.model_validate(result)
            if result.get("db") == "Tasks"
            else Category.model_validate(result)
        )
        return Updated.model_validate(
            {"updated": sorted(changes), **dict(summary(Lens(m), item))}
        )

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
    ) -> ItemDetail:
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
                raise MarvinError(f"subtask {ref!r} not found or ambiguous")
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
            raise MarvinError("nothing to change")
        result = await api.update_doc(task_id, {"subtasks": subs})
        mirror.apply(result)
        return task_detail(Lens(m), Task.model_validate(result))

    @mcp.tool()
    async def create_category(
        title: str,
        parent: Annotated[
            str, Field(description="parent category id or name; 'root' = top level")
        ] = "root",
        color: Annotated[str | None, Field(description="#RRGGBB")] = None,
    ) -> Created:
        """Create a category (a folder in the Master List). Categories can only live under categories."""
        m = await fresh()
        parent_id = m.resolve_parent(parent)
        if parent_id != "root" and m.category(parent_id).is_project:
            raise MarvinError("categories can't be created inside a project")
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
        return Created(
            created=str(stored["_id"]),
            title=title,
            parent=" > ".join(m.path(parent_id)),
        )

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
    ) -> Created:
        """Create a label (optionally inside an existing or new label group).

        Requires the Task Labels strategy to be enabled in Marvin.
        """
        m = await fresh()
        strategy = m.docs.get(LABELS_STRATEGY_DOC)
        if not (strategy and strategy.get("val")):
            raise MarvinError(
                "The Task Labels strategy is off; enable it in Marvin (Strategies) first."
            )
        for existing in m.labels():
            if existing.title.casefold() == title.casefold():
                raise MarvinError(
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
        return Created(created=stored.id, title=title, group=new_group or group)

    @mcp.tool()
    async def mark_done(item_id: str) -> Done:
        """Mark a task or project done (handles recurring/echo tasks and stops tracking)."""
        result = await api.mark_done(item_id, tz_offset_minutes())
        _absorb(mirror, result)
        title = result.get("title")
        return Done(done=item_id, title=title if isinstance(title, str) else None)

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


def _absorb(mirror: Mirror, result: JsonObj) -> None:
    """Fold a write's response into the mirror, or force a re-poll if it's not a doc."""
    if isinstance(result.get("_id"), str) and result.get("db"):
        mirror.apply(result)
    else:
        mirror.synced_at = 0.0
