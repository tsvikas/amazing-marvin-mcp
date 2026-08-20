# marvin-mcp-server

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
Claude ──MCP (stdio)──▶ marvin-mcp-server
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

- **Marvin semantics** are built in: Inbox is `parentId="unassigned"`,
  categories/projects nest arbitrarily, `day` (scheduled) vs `dueDate`
  (deadline), estimates in minutes, stars/frogs/backburner, labels and label
  groups.
- **Your structure** (`get_structure`): the category/project tree with ids and
  open-task counts, labels by group, and which Marvin strategies you have
  enabled.
- **Your workflow** (`workflow.md`): how *you* use Marvin: what labels mean,
  how you plan, your inbox-triage checklist, what "short win" means. Marvin is
  flexible, so this file is what lets the assistant act the way you would.
  It is injected into the server instructions and exposed as `marvin://workflow`.

### Tools

| Tool                                    | What it does                                                                      |
| --------------------------------------- | --------------------------------------------------------------------------------- |
| `get_structure`                         | Category/project tree with ids, labels by group, strategies in use                |
| `list_inbox`                            | Open inbox tasks, oldest first                                                    |
| `list_today [day]`                      | Scheduled that day, scheduled earlier but not done, due by then                   |
| `list_due [by]`                         | Open tasks due by a date (`week`, `month`, `YYYY-MM-DD`)                          |
| `search_tasks …`                        | Any combination of text, parent, labels, schedule window, due-by, estimate, flags |
| `get_task id`                           | Full detail incl. note, subtasks, dates, unmodelled fields                        |
| `list_children parent`                  | Direct tasks and sub-projects of a project/category                               |
| `create_task` / `create_project`        | Names for parent/labels are resolved for you                                      |
| `update_task id …`                      | Rename, move, relabel, (re)schedule, deadline, estimate, note, stars, clear…      |
| `mark_done id`                          | Complete a task/project                                                           |
| `sync_marvin`                           | Force a mirror refresh                                                            |
| Prompts: `triage_inbox`, `daily_review` | Built from your workflow file                                                     |

There is deliberately no delete tool: Marvin's trash is client-side, so API
deletes are unrecoverable.

## Install

```bash
uv tool install git+https://github.com/tsvikas/marvin-mcp-server.git
```

## Setup

1. **Credentials.** Open <https://app.amazingmarvin.com/pre?api> (or Marvin's
   *API* strategy settings) and export:

   ```bash
   export MARVIN_API_TOKEN=...           # create / mark done
   export MARVIN_FULL_ACCESS_TOKEN=...   # edit existing items (optional: omit for no edits)
   export MARVIN_SYNC_SERVER=...         # CouchDB: the read mirror
   export MARVIN_SYNC_DATABASE=...
   export MARVIN_SYNC_USER=...
   export MARVIN_SYNC_PASSWORD=...
   ```

   A `.env` file in the working directory works too. **While developing, use a
   second, throwaway Marvin account** (14-day trial, no card): the full-access
   token can damage data.

1. **Check and first sync:**

   ```bash
   marvin-mcp-server check     # verifies tokens, pulls the database, prints counts
   ```

1. **Describe your workflow:**

   ```bash
   marvin-mcp-server init-workflow   # writes a template; edit it
   ```

1. **Register with Claude Code:**

   ```bash
   claude mcp add marvin -- marvin-mcp-server serve
   ```

   For Claude Desktop, add the same command under `mcpServers` in its config.
   The MCP client passes its environment through, so either export the
   variables in the shell that launches it, or add them with
   `claude mcp add -e MARVIN_API_TOKEN=... marvin ...`.

Other settings (shown by `check`): `MARVIN_WORKFLOW_FILE`, `MARVIN_CACHE_DIR`,
`MARVIN_MIN_REQUEST_INTERVAL` (seconds between REST calls, default 3),
`MARVIN_MIRROR_MAX_AGE` (seconds before a read re-polls, default 60).

The mirror file holds all your tasks; it is written with owner-only permissions
under the cache directory.

## Contributing

Interested in contributing?
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

[codecov-badge]: https://codecov.io/gh/tsvikas/marvin-mcp-server/graph/badge.svg
[codecov-link]: https://codecov.io/gh/tsvikas/marvin-mcp-server
[github-discussions-badge]: https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github
[github-discussions-link]: https://github.com/tsvikas/marvin-mcp-server/discussions
[prs-welcome-badge]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg
[prs-welcome-link]: https://opensource.guide/how-to-contribute/
[ruff-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json
[ruff-link]: https://github.com/astral-sh/ruff
[template-badge]: https://img.shields.io/badge/%F0%9F%9A%80_Made_Using-tsvikas%2Fpython--template-gold
[template-link]: https://github.com/tsvikas/python-template
[tests-badge]: https://github.com/tsvikas/marvin-mcp-server/actions/workflows/ci.yml/badge.svg
[tests-link]: https://github.com/tsvikas/marvin-mcp-server/actions/workflows/ci.yml
[uv-badge]: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json
[uv-link]: https://github.com/astral-sh/uv
