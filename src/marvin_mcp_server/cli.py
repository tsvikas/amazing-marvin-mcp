"""CLI for marvin_mcp_server.

``serve`` is what an MCP client launches; the other commands are for setup and
troubleshooting from a terminal.
"""

import asyncio
import logging
import sys
import traceback
from collections.abc import Sequence
from typing import NoReturn

from cyclopts import App, CycloptsError

from .server import State, build_state, create_server
from .settings import Settings
from .workflow import init_workflow as _init_workflow

app = App(name="marvin-mcp-server")
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
    wf_state = "found" if settings.workflow_file.is_file() else "missing"
    print(f"workflow file: {settings.workflow_file} ({wf_state})")
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
def init_workflow(*, force: bool = False) -> int:
    """Create the workflow file from the template, then open it in your editor.

    Args:
        force: Overwrite an existing file.

    Returns:
        The process exit code.

    Exit Codes:
        0: Success.
    """
    path = Settings().workflow_file
    created = _init_workflow(path, force=force)
    print(f"{'created' if created else 'already exists (use --force)'}: {path}")
    return 0


# --- Entry point ----------------------------------------------------------------------
EX_USAGE = 2
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
        # Cyclopts itself calls `sys.exit` with a command's int return value.
        app(tokens, exit_on_error=False)
    except CycloptsError:
        sys.exit(EX_USAGE)
    except FileNotFoundError as exc:
        _fail(exc, EX_NOINPUT)
    except PermissionError as exc:
        _fail(exc, EX_NOPERM)
    except ConnectionError as exc:
        _fail(exc, EX_UNAVAILABLE)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(EX_SOFTWARE)
