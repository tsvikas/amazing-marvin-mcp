"""The user's personal workflow description.

Marvin is deliberately flexible (deadlines vs. daily planning vs. weekly
planning, labels for energy/context/time, ...), so generic tool descriptions
can't know what *this* user means by "short win" or what their inbox triage
checklist is. That lives in a Markdown file the user edits; the server feeds
it to the model as instructions, as a resource, and inside prompts.
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATE = "default_workflow.md"


def default_workflow() -> str:
    """Return the template shipped with the package."""
    return resources.files(__package__).joinpath(_TEMPLATE).read_text(encoding="utf-8")


def load_workflow(path: Path) -> str | None:
    """Return the user's workflow file, or ``None`` if they haven't written one yet."""
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def init_workflow(path: Path, *, force: bool = False) -> bool:
    """Write the template to ``path``; returns ``False`` if it already existed."""
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_workflow(), encoding="utf-8")
    return True
