# Ideas and open questions

Things worth doing later, roughly by payoff. Move an item to the CHANGELOG when
it ships; delete it when it stops being a good idea.

## Roadmap to real-account use

> Drafted 2026-10-06 with Claude and not yet reviewed by the author. Remove
> this note after review.

Ordered. Stage 0 needs no code; stage 1 gates giving the server the
full-access token on a real account. Facts below were checked against the
help-center API docs (moved from the GitHub wiki on 2026-10-04) on 2026-10-06.

The author runs Marvin on Windows desktop, Mac desktop and Android, so
edits from other devices during a session are normal (hence the fresh read
before a read-modify-write), and the device-local Trash can be emptied from
any of them. This checkout runs in WSL: the Windows desktop app's automatic
backups are reachable under `/mnt/c/Users/<user>/OneDrive/Documents/AmazingMarvinBackups`
(about 0.7 MB per compressed backup on 2026-10-06, so the first sync should
be small). The backups there had a gap from 2026-08-23 to 2026-10-01, which
is exactly what the backup-age check is for.

### Stage 0: read-only trial on the real account (no code needed)

- Configure only `MARVIN_SYNC_*` (no API tokens, so no write tool can work),
  run `check`, register the server, use the read tools.
- Record what it shows: document count, first-sync time and size, token cost
  of `get_structure`, whether importance levels and backburner look right.
- Find the profile setting that lists the Backburner labels (look for
  `backburner` among the `ProfileItems` ids in the real mirror).

### Stage 1: before any write on the real account

- **Enforce the read-only database boundary.** `CouchClient` only ever sends
  `GET _changes`; make that impossible to break (reject non-GET in the
  client, plus a test). The sync credentials can write, so the guarantee has
  to be ours.
- **Request budget.** Marvin's limits ("1 item per second", "1 query each 3
  seconds", "1440 queries per day") apply to the REST API *and* direct
  database access, so mirror polls count. Today the REST limiter and the
  poll timer are separate and nothing counts the day. Wanted: one shared
  limiter, a persisted rolling-24h counter shown by `check` and in
  `get_structure`'s mirror info, a warning threshold and a hard stop below
  1440\.
- **Poll less.** A read needs a fresh mirror at session start, on
  `sync_marvin`, before a write, and when more than X minutes have passed
  since the last poll (`MARVIN_MIRROR_MAX_AGE`, today 60 s; pick a larger
  default). Polling every minute while reading spends budget for nothing
  when this server is the only writer.
- **Fresh read before read-modify-write.** Labels, subtasks, `append_note`
  and the profile documents behind `create_label` rewrite a whole value from
  a mirror up to 60 s old. Fetch the current document (`GET /api/doc`, or a
  forced poll) immediately before such a write.
- **Write journal and undo.** Append every write to a local JSONL file
  (time, tool, document id, before/after of the changed fields). An
  `undo_last`/`undo(id)` tool replays the "before" values through
  `/api/doc/update`.
- **Local snapshots.** Copy the mirror file to a dated snapshot before the
  first write of each day; keep N. The mirror is a complete copy of the
  database, so this is a full local backup. A `restore_item(id, snapshot)`
  tool can put one document's fields back through the REST API.
- **Marvin's own backups.** Restore-from-backup in the app is the real
  recovery path. Desktop app: automatic daily/weekly backups to a directory
  (`.json.lzma`). Add an optional `MARVIN_BACKUP_DIR`; `check` reports the
  newest backup's age and writes warn (or refuse) when it is older than N
  days. There is no API to trigger a backup.
- **Removing items without a delete tool.** `/api/doc/delete` is permanent,
  and the app's Trash is local to the device that deleted (not synced, not
  backed up), so no API call can put an item in it. Use a holding category
  ("To delete", name from the workflow): a `discard` tool moves the item
  there and clears its dates, the "active" filter hides that category, and
  the user empties it in the app, which goes through the app's own Trash.
  Open: tasks have documented `deletedAt`/`restoredAt` fields; test on the
  throwaway account what setting `deletedAt` through the API does in the app.
- **"Active" filter.** `backburner=False` only tests the manual flag. Marvin
  also backburners by label (Backburner labels in the strategy settings),
  unfinished dependency (`dependsOn`), future start date and inheritance from
  a parent. Compute all five in the mirror and add `active_only` to
  `search_tasks` and `get_structure`.
- **Instructions are truncated.** Claude Code cuts server instructions and
  each tool description at 2,048 characters
  (`CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` overrides). The generic text is
  already 1,762, so an injected workflow is cut off. Decide between, or
  combine: trim the generic text; split it (short instructions, the rest and
  the workflow behind a `get_workflow` tool the instructions say to call
  first, read on every call so edits need no restart); raise the limit in
  the documented setup. Raising it only helps Claude Code users who set the
  variable. Instructions themselves are only sent at connect; `/mcp`
  reconnects.
  Constraint for the split: the workflow must not cost ~2k tokens on every
  call. Call `get_workflow` once per session, not per tool call; have it
  return a short index of sections by default and one section on request
  (`get_workflow(section="triage")`), so a session loads only what its task
  needs.

### Stage 2: makes triage good

- **Workflow interview**: a prompt that reads the real structure and labels
  and writes the workflow files with the user (replaces the blank template).
- **Any-word search**: `search_tasks(words=[…], match="any"|"all")` ranked by
  number of matching words, for "find similar tasks". `search_tasks` is ours,
  not Marvin's; the API has no search.
- **Paging**: `offset` on the list tools (the 200 cap has no way past it).
- **Merge and split** as documented recipes over existing tools (create,
  update, discard), journaled as one undoable group.
- **Setup skill** for install, `check`, registration; the user still pastes
  the credentials (tooling may not write `.env`).
- **`show-instructions` command** that prints exactly what the model sees
  (instructions and tool descriptions with lengths), for review.

### Open questions for Marvin support

Asked 2026-10-06 (draft email): do limits apply to the desktop local API
server; which endpoints it serves now that the list is gone from the docs;
whether writes count as "queries"; whether the API can trash instead of
delete; whether a backup can be triggered or verified by API.

## Tools and data

- **Done history**: `search_tasks(done_from=, done_to=)` on `doneAt` for
  "what did I finish this week". Data is in the mirror already.
- **Remaining write fields**: daily/custom sections (`dailySection`,
  `customSection`), dependencies (`dependsOn`), pinned tasks, snooze, `rank`
  (ordering within a day/project), subtask reordering and estimates.
- **Time tracking**: `start/stop_tracking`. Marvin expects the client to
  maintain `times`/`duration` on stop (`/api/track` only records the event),
  so this needs a small state machine, not a one-liner.
- **Recurring tasks**: mark instances vs templates in summaries; show the
  template's rule (`RecurringTasks` db) in `get_task`.
- **Reminders**: read/set via `/api/reminder/*`; also mirror `reminderTime` on
  the task.
- **Events and time blocks**: "what's my day look like" including calendar
  events (`Events`) and planner blocks (`PlannerItems`).
- **Smart lists**: expose the user's saved smart lists (`SmartLists` db) as
  named views; parsing Marvin's filter language is the hard part.
- **Done-project children**: `get_task` on a project shows open tasks only;
  add `include_done`.
- **Bulk operations**: `update_tasks(ids, …)` batched behind the rate limiter,
  with progress notifications, for "move all of these to Home".

## Desktop local API server

The desktop app can serve a subset of the API on `http://localhost:12082`
(API strategy → "local API server"; same `X-API-Token` / `X-Full-Access-Token`
headers and paths; marvin-cli tries it first and falls back to the cloud).
Not used yet. Worth adding for:

- **`GET /api/list?filter=<smart-list filter>&done=`** (desktop only): runs
  Marvin's own advanced-filter language over all open (or recently done)
  items. Lets a `run_filter` tool and the user's saved Smart Lists work
  exactly as in the app, instead of us reimplementing the filter syntax.
- **Users with cloud sync disabled**: the mirror can't exist; local `list`
  could be the read backend instead.
- Writes bypass the internet and presumably the cloud rate limits (unverified).
- Limits: the desktop app must be running; `done` items only go back 6 weeks
  unless the app is in archive mode; endpoint set is smaller than the cloud's.

## Sync and performance

- **Two-phase initial sync** if a real database is large: Mango `_find` for
  `done: false` first, full `_changes` in the background. Measure first with
  the user's real (read-only) sync credentials.
- **Prune** done tasks older than N days from the local mirror file; the
  `_changes` sequence keeps working.
- **Cap `get_structure`** output (the tree has no `limit`); measure its token
  cost on a real account.
- **Push instead of poll**: Marvin webhooks could invalidate the mirror, but
  they need a public endpoint; probably not worth it for a local server.

## Assistant quality

- **Default workflow from a real one**: once the author's own workflow files
  are written, derive a minimal sample workflow from them to ship as the
  template (concrete label/triage examples beat the current blank prompts).

- **Conversation evals** with the Claude Agent SDK: scenario prompts ("what's in
  my inbox", "I'm at the car with 10 minutes", "rename X") asserting which
  tools were called with which arguments. Only way to test the instructions.
  Manual `just evals`, not CI (costs tokens).

- **Snapshot tests** (syrupy) for tool output shapes, paired with the
  recorded cassette.

- **Instructions size budget**: warn in `check` when the workflow text is
  large; switch to "index inline + read resource" automatically.

- **Strategy-aware hints**: if Duration Estimates is off, don't suggest
  estimates; if Planning Ahead is off, don't offer planned weeks.

## Packaging

- **httpx 1.0**: `respx` still imports `httpcore` (renamed `httpcore2` in
  httpx 1.0), so the tests cannot run against the httpx 1.0 prereleases. The
  CI job that tried was removed; revisit when respx ships support.

- `uvx --from git+… amazing-marvin-mcp serve` one-liner; PyPI release once the
  tool set settles.

- Claude Desktop / other clients: verify the `.env` lookup works when the
  client's working directory is `/`.

- A `amazing-marvin-mcp doctor` that prints the tree and label summary, to
  check the mirror without an MCP client.

## Open questions

- Is there any schema/discovery endpoint? None found: the wiki's
  `marvin-api.yaml` (2024) is a hand-written OpenAPI file, not served by the
  API, and marvin-cli ships its endpoint docs as a static table. Our answer
  is `get_task(...).other_fields`, which surfaces whatever fields exist in
  live documents.

- Should the server expose Marvin's own REST read endpoints at all (e.g.
  `todayItems` honours rollover/auto-schedule settings that the mirror-based
  `list_today` reimplements)? Current answer: no, but compare results on a
  real account.

- Is `isStarred` 1/2/3 ↔ P3/P2/P1 right in the current app, or did the
  Importance Levels rework change the encoding? Verify on a task starred in
  the UI.
