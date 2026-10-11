"""Read-only queries over the mirror: a normalised view of items, and ``search``.

Marvin stores the same idea in different ways (importance is ``isStarred`` on a
task and ``priority`` on a project; an item is on the Backburner by flag, label,
start date, dependency or because its parent is). :class:`Lens` hides that
behind the names Marvin's UI uses, and :func:`search` filters and ranks items
through it: ``text`` for "what relates to these words", ``where`` for a filter
expression over the fields in :data:`FIELDS`.

The expression is evaluated by simpleeval, a sandbox that knows no names or
functions beyond the ones given here, so it can only read the item in front of it.
"""

from __future__ import annotations

import ast
import operator
import re
import shlex
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from simpleeval import EvalWithCompoundTypes, InvalidExpression, NameNotDefined

from .errors import MarvinError
from .models import INBOX, ROOT, Category, Task

if TYPE_CHECKING:
    from collections.abc import Callable

    from .mirror import Mirror

Item = Task | Category
Kind = Literal["task", "project", "category"]

BACKBURNER_LABELS_DOC = "strategySettings.backburner.backburnerLabels"
_PRIORITY_STARS = {"low": 1, "mid": 2, "high": 3}
STARS_PRIORITY = {stars: name for name, stars in _PRIORITY_STARS.items()}
SNIPPET_RADIUS = 40


# --- dates ----------------------------------------------------------------------------
def today() -> date:
    """Local date (the server runs on the user's machine)."""
    return date.today()  # noqa: DTZ011


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
            try:
                return date.fromisoformat(word)
            except ValueError as exc:
                raise MarvinError(
                    f"{value!r} is not a date: use YYYY-MM-DD, or today/tomorrow/"
                    "yesterday/week/next-week/month"
                ) from exc


def _ms_date(ms: float | None) -> date | None:
    return datetime.fromtimestamp(ms / 1000).astimezone().date() if ms else None


# --- the normalised view --------------------------------------------------------------
class Lens:
    """One consistent reading of the mirror, built once per tool call."""

    def __init__(self, mirror: Mirror, *, day: date | None = None) -> None:
        """Index ``mirror`` as of ``day`` (default: today)."""
        self.mirror = mirror
        self.today = day or today()
        self.categories = {c.id: c for c in mirror.categories()}
        self.tasks = mirror.tasks()
        self.label_titles = {lb.id: lb.title for lb in mirror.labels()}
        self.backburner_label_ids = _ids(mirror.docs.get(BACKBURNER_LABELS_DOC))
        self._inherited: dict[str, frozenset[str]] = {}
        self._under: dict[str, set[str]] = {}

    def kind(self, item: Item) -> Kind:
        """``task``, ``project`` or ``category``."""
        return "task" if isinstance(item, Task) else item.type

    def labels(self, item: Item) -> list[str]:
        """Label titles (an id is shown when its label no longer exists)."""
        return [self.label_titles.get(i, i) for i in item.label_ids]

    def path(self, parent_id: str) -> str:
        """``Work > Client A > Website`` for a parent id, or ``Inbox``."""
        return " > ".join(self.mirror.path(parent_id))

    def importance(self, item: Item) -> int:
        """Stars: 3 = P1, 2 = P2, 1 = P3, 0 = none; projects store it as priority."""
        if isinstance(item, Task):
            return item.star_level
        return _PRIORITY_STARS.get(item.priority or "", 0)

    def has_dependency(self, item: Item) -> bool:
        """Whether something this item depends on is still open."""
        for dep_id in item.depends_on:
            dep = self.mirror.docs.get(dep_id)
            if dep is not None and not dep.get("done") and not dep.get("deletedAt"):
                return True
        return False

    def _own_reasons(self, item: Item) -> set[str]:
        reasons = set()
        if item.backburner:
            reasons.add("flag")
        if self.backburner_label_ids.intersection(item.label_ids):
            reasons.add("label")
        if item.start is not None and item.start > self.today:
            reasons.add("start_date")
        if self.has_dependency(item):
            reasons.add("dependency")
        return reasons

    def _inherited_reasons(self, parent_id: str) -> frozenset[str]:
        """Return what an item gets from the containers above it (cycle-safe)."""
        chain: list[str] = []
        current = parent_id
        while current not in self._inherited and current in self.categories:
            if current in chain:
                break
            chain.append(current)
            current = self.categories[current].parent_id
        reasons = self._inherited.get(current, frozenset())
        for cat_id in reversed(chain):
            cat = self.categories[cat_id]
            if self._own_reasons(cat):
                reasons |= {"parent"}
            if cat.is_project and cat.done:
                reasons |= {"parent_done"}
            self._inherited[cat_id] = reasons
        return reasons

    def backburner_reasons(self, item: Item) -> list[str]:
        """Why Marvin keeps this out of the active list; empty when it doesn't.

        ``flag`` (sent to the Backburner by hand), ``label`` (a Backburner
        label), ``start_date`` (in the future), ``dependency`` (waits for an
        open item), ``parent`` (a container above it is on the Backburner),
        ``parent_done`` (it sits in a finished project).
        """
        return sorted(self._own_reasons(item) | self._inherited_reasons(item.parent_id))

    def is_under(self, item: Item, name_or_id: str) -> bool:
        """Whether ``item`` sits anywhere below the named category/project."""
        if name_or_id not in self._under:
            root = self.mirror.resolve_parent(name_or_id)
            self._under[name_or_id] = self.mirror.descendants(root)
        return item.parent_id in self._under[name_or_id]

    def items(self, kinds: set[Kind]) -> list[Item]:
        """Every task and/or project/category of the wanted kinds."""
        found: list[Item] = []
        if "task" in kinds:
            found.extend(self.tasks)
        found.extend(c for c in self.categories.values() if c.type in kinds)
        return found


def _ids(doc: dict[str, object] | None) -> frozenset[str]:
    """Ids held by a profile setting, whichever of Marvin's shapes it uses."""
    val = None if doc is None else doc.get("val")
    if isinstance(val, dict):
        return frozenset(str(k) for k, on in val.items() if on)
    if isinstance(val, list):
        ids = (v.get("_id") if isinstance(v, dict) else v for v in val)
        return frozenset(str(i) for i in ids if i)
    return frozenset()


# --- fields of the filter language ----------------------------------------------------
@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One name usable in a ``where`` expression."""

    get: Callable[[Lens, Item], object]
    doc: str = ""


def _subtasks(item: Item) -> list[str]:
    return [s.title for s in item.subtasks.values()] if isinstance(item, Task) else []


FIELDS: dict[str, FieldSpec] = {
    "kind": FieldSpec(lambda ln, i: ln.kind(i), "task/project/category"),
    "title": FieldSpec(lambda _, i: i.title),
    "note": FieldSpec(lambda _, i: i.note),
    "parent": FieldSpec(lambda ln, i: ln.path(i.parent_id), "path"),
    "labels": FieldSpec(lambda ln, i: ln.labels(i), "list"),
    "done": FieldSpec(lambda _, i: i.done),
    "backburner": FieldSpec(lambda ln, i: bool(ln.backburner_reasons(i))),
    "why_backburner": FieldSpec(lambda ln, i: ln.backburner_reasons(i), "list"),
    "has_dependency": FieldSpec(lambda ln, i: ln.has_dependency(i)),
    "importance": FieldSpec(lambda ln, i: ln.importance(i), "0-3 stars"),
    "frog": FieldSpec(lambda _, i: i.frog_level, "0-3"),
    "estimate_min": FieldSpec(lambda _, i: i.estimate_minutes),
    "do_date": FieldSpec(lambda _, i: i.scheduled_day),
    "due_date": FieldSpec(lambda _, i: i.due),
    "end_date": FieldSpec(lambda _, i: i.end),
    "start_date": FieldSpec(lambda _, i: i.start),
    "planned_week": FieldSpec(lambda _, i: i.planned_week, "Monday YYYY-MM-DD"),
    "planned_month": FieldSpec(lambda _, i: i.planned_month, "YYYY-MM"),
    "recurring": FieldSpec(lambda _, i: i.recurring),
    "subtasks": FieldSpec(lambda _, i: _subtasks(i), "list of titles"),
    "created": FieldSpec(lambda _, i: _ms_date(i.created_at), "date"),
    "updated": FieldSpec(lambda _, i: _ms_date(i.updated_at), "date"),
    "done_on": FieldSpec(
        lambda _, i: _ms_date(i.done_at) if isinstance(i, Task) else None, "date"
    ),
    "raw": FieldSpec(lambda ln, i: _Raw(ln.mirror.docs.get(i.id, {})), "raw.<field>"),
}
# Done and Backburner are separate questions about an item. Naming a field of
# one in a filter says the caller is asking about it, so its default is lifted.
_DONE_FIELDS = frozenset({"done", "done_on"})
_BACKBURNER_FIELDS = frozenset(
    {"backburner", "why_backburner", "has_dependency", "start_date"}
)
_DATE_NAMES = ("today", "tomorrow", "yesterday", "week_end", "next_week_end")
_CONSTANTS = frozenset({*_DATE_NAMES, "month_end", "True", "False", "None"})
_FUNCTIONS = frozenset({"under", "date", "days", "len"})


class _Raw(dict[str, object]):
    """A document whose missing fields read as ``None`` (``raw.anything`` is safe)."""

    def __missing__(self, key: str) -> None:
        return None


def field_help() -> str:
    """List the fields for the tool description, from the table we evaluate."""
    return ", ".join(
        f"{name} ({spec.doc})" if spec.doc else name for name, spec in FIELDS.items()
    )


# --- operators: forgiving where Marvin's data is, strict where a typo hides -----------
def _fold(value: str) -> str:
    """Compare text without case, compatibility forms or vowel points."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _pair(left: object, right: object) -> tuple[object, object]:
    """Bring two operands to comparable types."""
    if isinstance(left, date) and isinstance(right, str):
        return left, parse_day(right)
    if isinstance(left, str) and isinstance(right, date):
        return parse_day(left), right
    if isinstance(left, str) and isinstance(right, str):
        return _fold(left), _fold(right)
    return left, right


def _ordering(name: Literal["lt", "le", "gt", "ge"]) -> Callable[..., bool]:
    compare = getattr(operator, name)

    def run(left: object, right: object) -> bool:
        left, right = _pair(left, right)
        if left is None or right is None:  # an unset field never passes <, > …
            return False
        try:
            return bool(compare(left, right))
        except TypeError as exc:
            raise MarvinError(f"cannot compare {left!r} with {right!r}") from exc

    return run


def _equal(left: object, right: object) -> bool:
    if {type(left), type(right)} == {list, str}:
        raise MarvinError(
            'a list field cannot equal a text; write `"x" in labels` instead'
        )
    left, right = _pair(left, right)
    return left == right


def _contains(needle: object, haystack: object) -> bool:
    if haystack is None:
        return False
    if isinstance(haystack, str):
        if not isinstance(needle, str):
            raise MarvinError(f"cannot look for {needle!r} inside a text")
        return _fold(needle) in _fold(haystack)
    if isinstance(haystack, (list, tuple, set, frozenset)):
        if isinstance(needle, str):
            return _fold(needle) in {_fold(h) for h in haystack if isinstance(h, str)}
        return needle in haystack
    raise MarvinError(f"cannot use `in` on {haystack!r}")


_OPERATORS: dict[type, Callable[..., object]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
    ast.Eq: _equal,
    ast.NotEq: lambda a, b: not _equal(a, b),
    ast.Lt: _ordering("lt"),
    ast.LtE: _ordering("le"),
    ast.Gt: _ordering("gt"),
    ast.GtE: _ordering("ge"),
    ast.In: _contains,
    ast.NotIn: lambda a, b: not _contains(a, b),
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


class Where:
    """A parsed and checked ``where`` expression."""

    def __init__(self, expression: str, lens: Lens) -> None:
        """Parse ``expression`` and reject unknown names and labels up front."""
        self.expression = expression
        self._lens = lens
        try:
            self._parsed = EvalWithCompoundTypes.parse(expression)
        except (SyntaxError, InvalidExpression) as exc:
            raise MarvinError(f"cannot read `{expression}`: {exc}") from exc
        nodes = list(ast.walk(self._parsed))
        self.names = {n.id for n in nodes if isinstance(n, ast.Name)}
        unknown = self.names - FIELDS.keys() - _CONSTANTS - _FUNCTIONS
        if unknown:
            raise MarvinError(
                f"unknown name(s) {sorted(unknown)} in `{expression}`. "
                f"Fields: {', '.join(FIELDS)}. Dates: {', '.join(_DATE_NAMES)}, "
                'month_end, date("YYYY-MM-DD"), days(n). Also: under("Category").'
            )
        for node in nodes:
            if isinstance(node, ast.Compare):
                self._check_labels(node)
        self._item: Item | None = None
        base = lens.today
        constants: dict[str, object] = {
            "today": base,
            "tomorrow": base + timedelta(days=1),
            "yesterday": base - timedelta(days=1),
            "week_end": parse_day("week", base=base),
            "next_week_end": parse_day("next-week", base=base),
            "month_end": parse_day("month", base=base),
            "True": True,
            "False": False,
            "None": None,
        }

        def name(node: ast.Name) -> object:
            if node.id in constants:
                return constants[node.id]
            if node.id not in FIELDS:  # a function name: simpleeval looks it up next
                raise NameNotDefined(node.id, expression)
            assert self._item is not None  # noqa: S101
            return FIELDS[node.id].get(lens, self._item)

        def under(name_or_id: str) -> bool:
            assert self._item is not None  # noqa: S101
            return lens.is_under(self._item, name_or_id)

        self._evaluator = EvalWithCompoundTypes(
            operators=_OPERATORS,
            functions={
                "under": under,
                "date": parse_day,
                "days": lambda n: timedelta(days=n),
                "len": len,
            },
            names=name,
        )

    def _check_labels(self, node: ast.Compare) -> None:
        """``"typo" in labels`` would match nothing; say so instead."""
        operands = [node.left, *node.comparators]
        for left, op, right in zip(operands, node.ops, operands[1:], strict=False):
            if (
                isinstance(op, (ast.In, ast.NotIn))
                and isinstance(right, ast.Name)
                and right.id == "labels"
                and isinstance(left, ast.Constant)
                and isinstance(left.value, str)
            ):
                known = {_fold(t) for t in self._lens.label_titles.values()}
                if _fold(left.value) not in known:
                    titles = ", ".join(sorted(self._lens.label_titles.values()))
                    raise MarvinError(f"no label {left.value!r}. Labels: {titles}")

    def matches(self, item: Item) -> bool:
        """Whether ``item`` passes the expression."""
        self._item = item
        try:
            return bool(self._evaluator.eval("", previously_parsed=self._parsed))
        except MarvinError:
            raise
        except Exception as exc:
            raise MarvinError(f"cannot evaluate `{self.expression}`: {exc}") from exc


# --- text matching --------------------------------------------------------------------
# A Hebrew word usually carries its preposition or article as a one-letter prefix.
_HEBREW_PREFIX = "[ובלכמשה]?"
_TITLE, _SUBTASK, _NOTE = 3.0, 2.0, 1.0
_WHOLE_WORD_BONUS = 1.0


def words_of(text: str) -> list[str]:
    """Split a query into words; "quoted phrases" stay together."""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return [w for w in (_fold(p).strip() for p in parts) if w]


def _word_score(word: str, haystack: str, weight: float) -> float:
    if word not in haystack:
        return 0.0
    prefix = _HEBREW_PREFIX if re.search(r"[\u0590-\u05ff]", word) else ""
    whole = re.search(rf"(?<!\w){prefix}{re.escape(word)}(?!\w)", haystack)
    return weight + (_WHOLE_WORD_BONUS if whole else 0.0)


def _snippet(note: str, word: str) -> str | None:
    """Cut a short window of ``note`` around ``word``."""
    flat = " ".join(note.split())
    at = _fold(flat).find(word)
    if at < 0:
        return None
    start, end = max(0, at - SNIPPET_RADIUS), at + len(word) + SNIPPET_RADIUS
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


@dataclass(slots=True)
class Hit:
    """One search result."""

    item: Item
    matched_in: list[str] = field(default_factory=list)
    snippet: str | None = None
    words_matched: int = 0
    score: float = 0.0


def _text_hit(item: Item, words: list[str], *, need_all: bool) -> Hit | None:
    sources = {
        "title": (_fold(item.title), _TITLE),
        "subtasks": (_fold("\n".join(_subtasks(item))), _SUBTASK),
        "note": (_fold(item.note or ""), _NOTE),
    }
    hit = Hit(item)
    for word in words:
        scores = {k: _word_score(word, text, w) for k, (text, w) in sources.items()}
        best = max(scores, key=lambda k: scores[k])
        if not scores[best]:
            if need_all:
                return None
            continue
        hit.words_matched += 1
        hit.score += scores[best]
        hit.matched_in.extend(
            k for k, s in scores.items() if s and k not in hit.matched_in
        )
        if hit.snippet is None and scores["note"] and item.note:
            hit.snippet = _snippet(item.note, word)
    return hit if hit.words_matched else None


# --- search ---------------------------------------------------------------------------
@dataclass(slots=True)
class Scope:
    """Which items a search looked at, and what matched outside of that."""

    include_done: bool
    include_backburner: bool
    done_hidden: int = 0
    backburner_hidden: int = 0


def _sort_key(value: object) -> tuple[int, object]:
    """Unset values last; text without case."""
    if value is None:
        return (1, 0)
    return (0, _fold(value) if isinstance(value, str) else value)


def _order(lens: Lens, hits: list[Hit], sort: str | None, *, ranked: bool) -> None:
    """Sort ``hits`` in place: by a field, else by match quality, else by Do date."""
    if sort:
        name = sort.removeprefix("-")
        if name not in FIELDS or name == "raw":
            raise MarvinError(f"cannot sort by {name!r}; one of {', '.join(FIELDS)}")
        get = FIELDS[name].get
        try:
            hits.sort(key=lambda h: _sort_key(get(lens, h.item)))
        except TypeError as exc:
            raise MarvinError(f"cannot sort by {name!r}") from exc
        if sort.startswith("-"):  # keep unset values last when descending too
            unset = [h for h in hits if get(lens, h.item) is None]
            hits[:] = [
                h for h in reversed(hits) if get(lens, h.item) is not None
            ] + unset
    elif ranked:
        hits.sort(
            key=lambda h: (-h.words_matched, -h.score, h.item.done, _fold(h.item.title))
        )
    else:
        hits.sort(
            key=lambda h: (
                h.item.scheduled_day or date.max,
                h.item.rank,
                _fold(h.item.title),
            )
        )


def search(  # noqa: PLR0913
    lens: Lens,
    *,
    text: str | None = None,
    where: str | None = None,
    kinds: set[Kind] | None = None,
    include_done: bool | None = None,
    include_backburner: bool | None = None,
    match: Literal["all", "any"] = "all",
    sort: str | None = None,
) -> tuple[list[Hit], Scope]:
    """Items passing ``where`` and matching ``text``, best first, and the scope used.

    Done and Backburner are two independent switches, both off by default: a
    plain search sees items that are open and active. Left unset, each one
    turns on when ``where`` itself asks about it (``done``, ``done_on``; or
    ``backburner``, ``has_dependency``, ``start_date``…), so ``where="done"``
    is not silently filtered down to nothing. The scope counts the matches
    that each switch left out.
    """
    check = Where(where, lens) if where else None
    names = check.names if check else set()
    if include_done is None:
        include_done = bool(names & _DONE_FIELDS)
    if include_backburner is None:
        include_backburner = bool(names & _BACKBURNER_FIELDS)
    scope = Scope(include_done, include_backburner)
    words = words_of(text) if text else []
    hits: list[Hit] = []
    for item in lens.items(kinds or {"task", "project"}):
        hit = _text_hit(item, words, need_all=match == "all") if words else Hit(item)
        if hit is None or (check and not check.matches(item)):
            continue
        done_out = item.done and not include_done
        backburner_out = not include_backburner and bool(lens.backburner_reasons(item))
        scope.done_hidden += done_out
        scope.backburner_hidden += backburner_out
        if not (done_out or backburner_out):
            hits.append(hit)
    _order(lens, hits, sort, ranked=bool(words))
    return hits, scope


# --- the tree -------------------------------------------------------------------------
@dataclass(slots=True)
class Branch:
    """A category or project with what it holds."""

    item: Category
    tasks: int = 0
    tasks_below: int = 0
    done_tasks_below: int = 0
    children: list[Branch] = field(default_factory=list)
    more_categories: int = 0
    more_projects: int = 0


@dataclass(slots=True)
class Tree:
    """The containers under a root, down to a depth."""

    root_id: str
    branches: list[Branch]
    tasks: int
    inbox_tasks: int
    done_projects_hidden: int
    backburner_hidden: int


def _counts(
    lens: Lens, shown: Callable[[list[str]], bool]
) -> tuple[dict[str, int], dict[str, int]]:
    """Open (and shown) tasks, and done tasks, directly inside each parent."""
    direct: dict[str, int] = {}
    done: dict[str, int] = {}
    for task in lens.tasks:
        if task.done:
            done[task.parent_id] = done.get(task.parent_id, 0) + 1
        elif shown(lens.backburner_reasons(task)):
            direct[task.parent_id] = direct.get(task.parent_id, 0) + 1
    return direct, done


def tree(
    lens: Lens,
    *,
    root: str = ROOT,
    depth: int = 2,
    include_done: bool = False,
    include_backburner: bool = False,
) -> Tree:
    """Categories and projects below ``root``, ``depth`` levels deep, with counts.

    Counts follow the same rule as what is shown: open tasks, without those on
    the Backburner or inside finished projects unless the flags include them.
    """

    def shown(reasons: list[str]) -> bool:
        waiting = [r for r in reasons if r != "parent_done"]
        return (include_backburner or not waiting) and (
            include_done or "parent_done" not in reasons
        )

    direct, done_direct = _counts(lens, shown)
    scope = lens.mirror.descendants(root)
    children: dict[str, list[Category]] = {}
    hidden = {"done": 0, "backburner": 0}
    for cat in lens.categories.values():
        if cat.parent_id not in scope:
            continue
        if cat.is_project and cat.done and not include_done:
            hidden["done"] += 1
        elif not shown(lens.backburner_reasons(cat)):
            hidden["backburner"] += 1
        else:
            children.setdefault(cat.parent_id, []).append(cat)

    def build(cat: Category, level: int) -> Branch:
        branch = Branch(cat, tasks=direct.get(cat.id, 0))
        branch.tasks_below = branch.tasks
        branch.done_tasks_below = done_direct.get(cat.id, 0)
        for kid in sorted(children.get(cat.id, []), key=lambda c: c.rank):
            sub = build(kid, level + 1)
            branch.tasks_below += sub.tasks_below
            branch.done_tasks_below += sub.done_tasks_below
            branch.more_categories += sub.more_categories + (not kid.is_project)
            branch.more_projects += sub.more_projects + kid.is_project
            if level < depth:
                branch.children.append(sub)
        if branch.children:  # what is listed is not "more"
            branch.more_categories = branch.more_projects = 0
        return branch

    tops = sorted(children.get(root, []), key=lambda c: c.rank)
    return Tree(
        root_id=root,
        branches=[build(c, 1) for c in tops],
        tasks=direct.get(root, 0),
        inbox_tasks=direct.get(INBOX, 0),
        done_projects_hidden=hidden["done"],
        backburner_hidden=hidden["backburner"],
    )
