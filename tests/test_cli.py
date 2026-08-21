from pathlib import Path

import pytest

from amazing_marvin_mcp import __version__, cli
from amazing_marvin_mcp.cli import EX_SOFTWARE, EX_USAGE, app, main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("--version")
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app("--help")
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for command in ("serve", "check", "sync", "init-workflow"):
        assert command in out


def test_init_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "wf.md"
    monkeypatch.setenv("MARVIN_WORKFLOW_FILE", str(target))
    with pytest.raises(SystemExit) as exc_info:
        app(["init-workflow"])
    assert exc_info.value.code == 0
    assert target.read_text().startswith("# How I use Amazing Marvin")
    with pytest.raises(SystemExit):
        app(["init-workflow"])
    assert "already exists" in capsys.readouterr().out


def test_main_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--not-an-option"])
    assert exc_info.value.code == EX_USAGE


def test_main_unhandled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(cli, "app", explode)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == EX_SOFTWARE
