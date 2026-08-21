"""The user's personal workflow description.

Marvin is deliberately flexible (deadlines vs. daily planning vs. weekly
planning, labels for energy/context/time, ...), so generic tool descriptions
can't know what *this* user means by "short win" or what their inbox triage
checklist is. That lives in Markdown the user edits: either a single
``workflow.md`` or a ``workflow/`` directory of sections (``labels.md``,
``triage.md``, ...). The server feeds it to the model as instructions, as
resources (one per section), and inside prompts.
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATES = resources.files(__package__) / "templates" / "workflow"

NO_WORKFLOW = """\
No workflow file found. Run `marvin-mcp-server init-workflow` to create one; until
then, ask the user how they use labels, scheduling and deadlines before making
assumptions.
"""


def template_sections() -> dict[str, str]:
    """Return the shipped template, one entry per section, in reading order."""
    files = sorted(
        (p for p in _TEMPLATES.iterdir() if p.name.endswith(".md")),
        key=lambda p: p.name,
    )
    return {
        p.name.removesuffix(".md").split("-", 1)[-1]: p.read_text("utf-8")
        for p in files
    }


class Workflow:
    """The user's workflow: a Markdown file or a directory of Markdown sections."""

    def __init__(self, path: Path) -> None:
        """Bind to ``path`` (file or directory); contents are read on each access."""
        self.path = path

    def sections(self) -> dict[str, str]:
        """Section name -> Markdown. A single file is the one section ``workflow``."""
        if self.path.is_dir():
            files = sorted(self.path.glob("*.md"))
            return {p.stem.split("-", 1)[-1]: p.read_text("utf-8") for p in files}
        if self.path.is_file():
            return {"workflow": self.path.read_text("utf-8")}
        return {}

    def text(self, *, prefer: str | None = None) -> str:
        """All sections joined; with ``prefer``, that section goes first."""
        sections = self.sections()
        if not sections:
            return NO_WORKFLOW
        names = sorted(sections, key=lambda n: (n != prefer, list(sections).index(n)))
        return "\n\n".join(sections[n].strip() for n in names) + "\n"

    def section(self, name: str) -> str:
        """One section by name (file stem without its numeric prefix)."""
        sections = self.sections()
        if name not in sections:
            raise KeyError(f"no workflow section {name!r}; have {sorted(sections)}")
        return sections[name]

    def init(self, *, split: bool = False, force: bool = False) -> list[Path]:
        """Write the template to ``path``; returns the files written.

        ``split`` writes a directory of sections (``path`` must then be a
        directory or not exist); otherwise one concatenated file.
        """
        written: list[Path] = []
        if split:
            self.path.mkdir(parents=True, exist_ok=True)
            for entry in sorted(_TEMPLATES.iterdir(), key=lambda p: p.name):
                if not entry.name.endswith(".md"):
                    continue
                target = self.path / entry.name
                if target.exists() and not force:
                    continue
                target.write_text(entry.read_text("utf-8"), encoding="utf-8")
                written.append(target)
            return written
        if self.path.exists() and not force:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n\n".join(t.strip() for t in template_sections().values()) + "\n"
        self.path.write_text(body, encoding="utf-8")
        return [self.path]
