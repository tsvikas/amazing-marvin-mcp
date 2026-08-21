# amazing-marvin-mcp

[![Tests][tests-badge]][tests-link]
[![uv][uv-badge]][uv-link]
[![Ruff][ruff-badge]][ruff-link]
[![codecov][codecov-badge]][codecov-link]
\
[![Made Using tsvikas/python-template][template-badge]][template-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]
[![PRs Welcome][prs-welcome-badge]][prs-welcome-link]

## Overview

An [MCP](https://modelcontextprotocol.io) server for
[Amazing Marvin](https://amazingmarvin.com), so an assistant such as Claude can
answer "what's in my inbox?", "what's due this week?", "I'm at the car, what can
I do?", and act on it: rename, relabel, reschedule, move, split into projects,
save research into a note.

### How it works

```
Claude ──MCP (stdio)──▶ amazing-marvin-mcp
                          ├─ local mirror ◀── CouchDB `_changes` (your sync database)
                          │    reads, search and filters run here: no rate-limit cost
                          └─ writes ──▶ Marvin REST API (addTask, doc/update, markDone)
                                        throttled to Marvin's limits (1 request / 3 s)
```

Marvin's REST API has no "list" or "search" endpoint and a budget of 1440
requests per day, so reads come from a local copy of your database kept fresh
with an incremental `_changes` poll (at most once a minute). Writes go through
the REST API, which handles Marvin's conflict-resolution bookkeeping
(`fieldUpdates`) server-side.

### What the assistant knows

- **Marvin semantics**, in the UI's own words. Inbox = not filed anywhere;
  categories and projects nest arbitrarily (the Master List). The four dates
  are kept apart: **Do date** (the day you plan to do it, "scheduled"),
  **Due date** (hard external deadline), **End date** (self-imposed target;
  Planning Ahead's planned week/month is the softer form), **Start date**
  (hidden on the Backburner until then). Plus Duration estimates (minutes),
  Importance (P1–P3 stars), Eat-the-Frog, Backburner, labels and label
  groups.
- **Your structure** (`get_structure`): the category/project tree with ids and
  open-task counts, labels by group, and which Marvin strategies you have
  enabled (by their names in the Strategies screen).
- **Your workflow** (`workflow.md` or a `workflow/` directory of sections):
  how *you* use Marvin: what labels mean, how you plan, your inbox-triage
  checklist, what "short win" means. Marvin is flexible, so this is what lets
  the assistant act the way you would. It is injected into the server
  instructions and exposed as `marvin://workflow` (and `marvin://workflow/<section>`).

### Tools

| Tool                                    | What it does                                                                     |
| --------------------------------------- | -------------------------------------------------------------------------------- |
| `get_structure`                         | Category/project tree with ids, labels by group, strategies in use               |
| `list_inbox`                            | Open inbox tasks, oldest first                                                   |
| `list_today [day]`                      | Do date that day, do date earlier but not done, due by then                      |
| `list_due [by]`                         | Open tasks with a due date by a date (`week`, `month`, `YYYY-MM-DD`)             |
| `search_tasks …`                        | Any mix of text, parent, labels, do-date window, due/end by, estimate, flags     |
| `get_task id`                           | Full detail of a task (note, subtasks, dates) or a project (its open children)   |
| `list_children parent`                  | Direct tasks and sub-projects of a project/category                              |
| `create_task` / `create_project`        | Names for parent/labels are resolved for you                                     |
| `create_category` / `create_label`      | New folder in the Master List; new label (in an existing or new group)           |
| `update_subtasks id …`                  | Add, complete, reopen, rename, remove subtasks                                   |
| `update_task id …`                      | Rename, move, relabel, do/due/end/start date, estimate, note, importance, clear… |
| `mark_done id`                          | Complete a task/project                                                          |
| `sync_marvin`                           | Force a mirror refresh                                                           |
| Prompts: `triage_inbox`, `daily_review` | Built from your workflow file                                                    |

There is deliberately no delete tool: Marvin's trash is client-side, so API
deletes are unrecoverable.

## Install and set up (using it)

1. **Install** (needs [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv tool install git+https://github.com/tsvikas/amazing-marvin-mcp.git
   ```

1. **Credentials.** In Marvin, enable the *API* strategy (Strategies → API →
   settings). Put its values in `~/.config/amazing-marvin-mcp/.env`
   (Linux; `amazing-marvin-mcp check` prints the exact path on your OS):

   ```bash
   MARVIN_API_TOKEN=...           # create / mark done
   MARVIN_FULL_ACCESS_TOKEN=...   # edit existing items; omit for no edits
   MARVIN_SYNC_SERVER=...         # CouchDB: the read mirror
   MARVIN_SYNC_DATABASE=...
   MARVIN_SYNC_USER=...
   MARVIN_SYNC_PASSWORD=...
   ```

   Environment variables and a `.env` in the working directory also work
   (and override the per-user file). **While trying things out, use a
   second, throwaway Marvin account**: the full-access token can damage data.

1. **Check and first sync:**

   ```bash
   amazing-marvin-mcp check     # verifies tokens, pulls the database, prints counts
   ```

1. **Describe your workflow:**

   ```bash
   amazing-marvin-mcp init-workflow            # one workflow.md to edit, or
   amazing-marvin-mcp init-workflow --split    # a workflow/ directory: planning, labels, triage, daily…
   ```

   Everything in it goes into the model's instructions (and each section is
   also a `marvin://workflow/<section>` resource), so keep it short and
   concrete.

1. **Register with your MCP client.**

   Claude Code:

   ```bash
   claude mcp add marvin -- amazing-marvin-mcp serve
   ```

   Claude Desktop (`claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "marvin": { "command": "amazing-marvin-mcp", "args": ["serve"] }
     }
   }
   ```

   Both read the per-user `.env`, so no secrets go in the client config.
   Then ask: "what's in my Marvin inbox?"

Other settings (shown by `check`): `MARVIN_WORKFLOW_FILE` (file or directory), `MARVIN_CACHE_DIR`,
`MARVIN_MIN_REQUEST_INTERVAL` (seconds between REST calls, default 3),
`MARVIN_MIRROR_MAX_AGE` (seconds before a read re-polls, default 60).

The mirror file holds all your tasks; it is written with owner-only permissions
under the cache directory.

## Developing

```bash
git clone https://github.com/tsvikas/amazing-marvin-mcp && cd amazing-marvin-mcp
uv sync && just prepare                     # deps + pre-commit hooks
just test && just lint
```

Run the server from the checkout (a `.env` in the repo root is git-ignored and
is picked up when the client starts from this directory; otherwise use
`-e VAR=value` or the per-user file):

```bash
claude mcp add marvin-dev -- uv run --directory "$PWD" amazing-marvin-mcp serve
```

`tests/test_live.py` runs every tool end to end and replays a recorded
cassette by default (`tests/cassettes/`), which doubles as a fixture of real
Marvin documents. To re-record against a **throwaway** account (it creates
and then deletes a few `[live]` items):

```bash
MARVIN_API_TOKEN=... MARVIN_FULL_ACCESS_TOKEN=... MARVIN_SYNC_SERVER=... \
MARVIN_SYNC_DATABASE=... MARVIN_SYNC_USER=... MARVIN_SYNC_PASSWORD=... \
uv run pytest tests/test_live.py --record-mode=rewrite
```

Design rules for contributions are in [CLAUDE.md](CLAUDE.md).

## Contributing

Interested in contributing?
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

[codecov-badge]: https://codecov.io/gh/tsvikas/amazing-marvin-mcp/graph/badge.svg
[codecov-link]: https://codecov.io/gh/tsvikas/amazing-marvin-mcp
[github-discussions-badge]: https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github
[github-discussions-link]: https://github.com/tsvikas/amazing-marvin-mcp/discussions
[prs-welcome-badge]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg
[prs-welcome-link]: https://opensource.guide/how-to-contribute/
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-link]: https://github.com/astral-sh/ruff
[template-badge]: https://img.shields.io/badge/%F0%9F%9A%80_Made_Using-tsvikas%2Fpython--template-gold
[template-link]: https://github.com/tsvikas/python-template
[tests-badge]: https://github.com/tsvikas/amazing-marvin-mcp/actions/workflows/ci.yml/badge.svg
[tests-link]: https://github.com/tsvikas/amazing-marvin-mcp/actions/workflows/ci.yml
[uv-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json
[uv-link]: https://github.com/astral-sh/uv
