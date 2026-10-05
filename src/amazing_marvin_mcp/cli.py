"""CLI for amazing_marvin_mcp.

``serve`` is what an MCP client launches; the other commands are for setup and
troubleshooting from a terminal.
"""

import asyncio
import logging
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from cyclopts import App

from .server import State, build_state, create_server
from .settings import Settings
from .workflow import Workflow

app = App(name="amazing-marvin-mcp")
app.register_install_completion_command()


# --- Commands -------------------------------------------------------------------------
@app.command()
def serve() -> int:
    """Run the MCP server over stdio (what Claude Code / Claude Desktop launch).

    Returns:
        The process exit code.

    Exit Codes:
        0: Success.
    """
    # stdout is the MCP channel; everything else must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    create_server(Settings()).run("stdio")
    return 0


@app.command()
def check() -> int:
    """Check configuration, credentials and the mirror; print what's enabled.

    Returns:
        The process exit code.

    Exit Codes:
        0: Everything configured is working.
        69: A configured credential failed.
    """
    settings = Settings()
    sections = Workflow(settings.workflow_file).sections()
    wf_state = f"{len(sections)} section(s)" if sections else "missing"
    print(f"workflow:      {settings.workflow_file} ({wf_state})")
    print(f"cache dir:     {settings.cache_dir}")
    print(f"api token:     {_status(settings.can_write, 'create/mark_done disabled')}")
    print(f"full access:   {_status(settings.can_edit, 'update_task disabled')}")
    print(f"sync creds:    {_status(settings.can_sync, 'no reads possible')}")
    return asyncio.run(_check(build_state(settings)))


def _status(ok: bool, consequence: str) -> str:  # noqa: FBT001
    return "set" if ok else f"missing ({consequence})"


async def _check(state: State) -> int:
    ok = True
    if state.settings.can_write:
        try:
            me = await state.api.me()
            print(f"REST api:      OK ({me.get('email')})")
        except Exception as exc:  # noqa: BLE001
            print(f"REST api:      FAILED: {exc}")
            ok = False
    if state.settings.can_sync:
        try:
            changed = await state.mirror.refresh(force=True)
            m = state.mirror
            print(
                f"mirror:        OK, {len(m.docs)} documents ({changed} changed), "
                f"{len(m.tasks())} tasks, {len(m.categories())} projects/categories, "
                f"{len(m.labels())} labels"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"mirror:        FAILED: {exc}")
            ok = False
    await state.api.aclose()
    if state.couch is not None:
        await state.couch.aclose()
    return 0 if ok else EX_UNAVAILABLE


@app.command()
def sync() -> int:
    """Refresh the local mirror now.

    Returns:
        The process exit code.

    Exit Codes:
        0: Success.
    """
    state = build_state(Settings())

    async def run() -> None:
        changed = await state.mirror.refresh(force=True)
        print(f"{changed} changed, {len(state.mirror.docs)} documents total")
        if state.couch is not None:
            await state.couch.aclose()

    asyncio.run(run())
    return 0


@app.command(name="init-workflow")
def init_workflow(*, split: bool = False, force: bool = False) -> int:
    """Create the workflow template for you to edit.

    Args:
        split: Write a `workflow/` directory with one file per section
            (planning, labels, triage, ...) instead of a single `workflow.md`.
        force: Overwrite existing files.

    Returns:
        The process exit code.

    Exit Codes:
        0: Success.
    """
    settings = Settings()
    path = Path(settings.workflow_file)  # a concrete Path also keeps pylint happy
    if split and path.suffix == ".md":
        path = path.with_name("workflow")
    written = Workflow(path).init(split=split, force=force)
    if written:
        for file in written:
            print(f"created: {file}")
    else:
        print(f"already exists (use --force): {path}")
    return 0


# --- Entry point ----------------------------------------------------------------------
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_NOPERM = 77


def _fail(exc: Exception, code: int) -> NoReturn:
    """Report `exc` on stderr and exit with `code`."""
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(code)


def main(tokens: Sequence[str] | None = None) -> None:
    """Run the CLI, reporting failures and mapping them onto exit codes.

    Args:
        tokens: The command line to parse. Defaults to `sys.argv[1:]`.
    """
    try:
        # Cyclopts itself calls `sys.exit` with a command's int return value,
        # and exits 2 on invalid usage.
        app(tokens)
    except FileNotFoundError as exc:
        _fail(exc, EX_NOINPUT)
    except PermissionError as exc:
        _fail(exc, EX_NOPERM)
    except ConnectionError as exc:
        _fail(exc, EX_UNAVAILABLE)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(EX_SOFTWARE)
