from pathlib import Path

import pytest

from amazing_marvin_mcp.workflow import NO_WORKFLOW, Workflow, template_sections


def test_template_has_the_expected_sections() -> None:
    assert list(template_sections()) == [
        "intro",
        "planning",
        "labels",
        "structure",
        "triage",
        "daily",
        "quick-picks",
    ]


def test_missing_workflow(tmp_path: Path) -> None:
    wf = Workflow(tmp_path / "workflow.md")
    assert wf.sections() == {}
    assert wf.text() == NO_WORKFLOW
    with pytest.raises(KeyError, match="no workflow section"):
        wf.section("labels")


def test_single_file(tmp_path: Path) -> None:
    path = tmp_path / "workflow.md"
    path.write_text("# mine\nshort-win = under 10m\n")
    wf = Workflow(path)
    assert wf.sections() == {"workflow": "# mine\nshort-win = under 10m\n"}
    assert "short-win" in wf.text()


def test_directory_sections_and_prefer(tmp_path: Path) -> None:
    d = tmp_path / "workflow"
    d.mkdir()
    (d / "10-planning.md").write_text("## Planning\nby week\n")
    (d / "20-triage.md").write_text("## Triage\nstep one\n")
    wf = Workflow(d)
    assert list(wf.sections()) == ["planning", "triage"]
    assert wf.section("triage").startswith("## Triage")
    assert wf.text().index("Planning") < wf.text().index("Triage")
    preferred = wf.text(prefer="triage")
    assert preferred.index("Triage") < preferred.index("Planning")


def test_init_single_and_split(tmp_path: Path) -> None:
    single = Workflow(tmp_path / "workflow.md")
    assert single.init() == [tmp_path / "workflow.md"]
    assert single.init() == []  # exists, not forced
    assert "Inbox triage checklist" in single.text()

    split = Workflow(tmp_path / "workflow")
    written = split.init(split=True)
    assert [p.name for p in written] == [
        "00-intro.md",
        "10-planning.md",
        "20-labels.md",
        "30-structure.md",
        "40-triage.md",
        "50-daily.md",
        "60-quick-picks.md",
    ]
    assert split.init(split=True) == []
    assert list(split.sections()) == list(template_sections())
