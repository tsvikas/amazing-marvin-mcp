import os
from pathlib import Path

import httpx
import pytest
import respx

from amazing_marvin_mcp import __version__, cli
from amazing_marvin_mcp.api import BASE_URL
from amazing_marvin_mcp.cli import (
    EX_NOINPUT,
    EX_NOPERM,
    EX_SOFTWARE,
    EX_UNAVAILABLE,
    app,
    main,
)
from amazing_marvin_mcp.settings import Settings

COUCH = "https://db.example.com"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real credentials away from the commands that build their own Settings.

    Without this a developer's `.env` (or an exported `MARVIN_*`) would reach
    the network from a unit test.
    """
    for key in [k for k in os.environ if k.startswith("MARVIN_")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )
    monkeypatch.setenv("MARVIN_WORKFLOW_FILE", str(tmp_path / "workflow.md"))
    monkeypatch.setenv("MARVIN_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MARVIN_MIN_REQUEST_INTERVAL", "0")


def _with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARVIN_API_TOKEN", "api")
    monkeypatch.setenv("MARVIN_SYNC_SERVER", COUCH)
    monkeypatch.setenv("MARVIN_SYNC_DATABASE", "u123")
    monkeypatch.setenv("MARVIN_SYNC_USER", "user")
    monkeypatch.setenv("MARVIN_SYNC_PASSWORD", "pw")


def _changes_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "last_seq": "1-a",
            "results": [
                {
                    "id": "t1",
                    "seq": "1-a",
                    "doc": {"_id": "t1", "db": "Tasks", "title": "A"},
                }
            ],
        },
    )


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
    # Cyclopts >=5 exits 2 on invalid usage, as argparse, click and clap do.
    # sysexits(3) would say 64, but 2 is the far wider convention.
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError("missing.txt"), EX_NOINPUT),
        (PermissionError("locked.txt"), EX_NOPERM),
        (ConnectionError("down"), EX_UNAVAILABLE),
        # a subclass lands on its parent's code
        (ConnectionRefusedError("refused"), EX_UNAVAILABLE),
    ],
)
def test_main_reported_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(cli, "app", explode)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == code
    assert capsys.readouterr().err == f"error: {error}\n"


def test_main_unhandled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError

    monkeypatch.setattr(cli, "app", explode)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == EX_SOFTWARE


def test_check_without_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        app(["check"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "workflow:" in out
    assert "api token:     missing (create/mark_done disabled)" in out
    assert "full access:   missing (update_task disabled)" in out
    assert "sync creds:    missing (no reads possible)" in out


@respx.mock
def test_check_reports_working_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_credentials(monkeypatch)
    respx.get(f"{BASE_URL}/me").respond(json={"email": "me@example.com"})
    respx.get(f"{COUCH}/u123/_changes").mock(return_value=_changes_response())
    with pytest.raises(SystemExit) as exc_info:
        app(["check"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "REST api:      OK (me@example.com)" in out
    assert "mirror:        OK, 1 documents (1 changed), 1 tasks" in out


@respx.mock
def test_check_reports_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_credentials(monkeypatch)
    respx.get(f"{BASE_URL}/me").respond(401, text="bad token")
    respx.get(f"{COUCH}/u123/_changes").respond(401, text="nope")
    with pytest.raises(SystemExit) as exc_info:
        app(["check"])
    assert exc_info.value.code == EX_UNAVAILABLE
    out = capsys.readouterr().out
    assert "REST api:      FAILED: 401 from me" in out
    assert "mirror:        FAILED: 401 from _changes" in out


@respx.mock
def test_sync_refreshes_the_mirror(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_credentials(monkeypatch)
    route = respx.get(f"{COUCH}/u123/_changes").mock(return_value=_changes_response())
    with pytest.raises(SystemExit) as exc_info:
        app(["sync"])
    assert exc_info.value.code == 0
    assert route.call_count == 1
    assert capsys.readouterr().out.strip() == "1 changed, 1 documents total"
