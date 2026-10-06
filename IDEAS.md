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
(0.7 MB compressed on 2026-10-01, 1.7 MB / 11 MB of JSON on 2026-10-06 after
the app caught up on five weeks of sync). The backups there had a gap from
2026-08-23 to 2026-10-01 because the computer was off: the app only backs
up while running, which is exactly what the backup-age check is for.

### Stage 0: read-only trial on the real account (no code needed)

- Configure only `MARVIN_SYNC_*` (no API tokens, so no write tool can work),
  run `check`, register the server, use the read tools.
- Record what it shows: document count, first-sync time and size, token cost
  of `get_structure`, whether importance levels and backburner look right.
- Find the profile setting that lists the Backburner labels (look for
  `backburner` among the `ProfileItems` ids in the real mirror).

Measured on the real account, 2026-10-06 (read-only, sync credentials only):

- First sync: 10.8 s, 10,479 documents (16,039 changes), 16 MB mirror file.
- 7,710 tasks (1,775 open, 5,935 done), 137 open in the Inbox, 138
  categories, 325 projects (270 of them done), 41 labels.
- **`get_structure` is too big**: 50,640 characters (about 12,700 tokens),
  over Claude Code's 50,000-character limit for an inline tool result. It
  lists the 270 done projects; hide done projects by default.
- Backburner on open tasks: 136 by manual flag, 4 by future start date, 16
  with `dependsOn`; 18 projects/categories carry the flag (inheritance).
  The Backburner labels are in `strategySettings.backburner.backburnerLabels`.
- The `sequentialProjects` and `nextSteps` strategies are on: check whether
  "only the first task of a sequential project is actionable" belongs in
  the "active" filter.
- `STRATEGY_NAMES` covers a minority of the 68 enabled strategy keys; the
  rest show as raw keys.
- Still to check by eye in a session: importance levels (31 tasks at 3, 6
  at 2, 15 at 1) against the app.
- Data oddities to understand: 6 tasks in the synced database carry
  `deletedAt` (support says only a device's Trash copy should), and 8
  documents have no `db`.

Still to do in stage 0: register the server (`claude mcp add --scope user marvin -- amazing-marvin-mcp serve`), use the read tools in a session, and
note what reads wrong. Then Claude Desktop on Windows, which has to launch
the WSL install through `wsl.exe` (untested).

Things to keep in mind once writes are on: task titles and notes go into the
model's context, and notes are untrusted text (email-to-Marvin, pasted web
content) that could try to steer an assistant holding the full-access
token. In Claude Code, allow the read tools and leave the write tools on
"ask" at first; the `anthropic/requiresUserInteraction` tool annotation can
force a prompt for the destructive ones.

Found in the first real session, 2026-10-07:

- **Importance encoding confirmed** against the app: `isStarred` 3 = P1
  (red, three stars), 2 = P2 (orange), 1 = P3 (yellow, one star).
- **Project importance is invisible.** Projects store it as `priority`
  (`low`/`mid`/`high` = 1/2/3 stars in the app), not `isStarred`. Summaries
  show no `importance` for a project and `search_tasks(important=True)`
  skips them. Map `priority` onto `importance` in both directions.
- **`search_tasks` does not search projects** (or events, or subtask
  titles): a project found by name needed a read of the mirror file.
  Include projects, and say what is not searched in the description.
- **Reversed Hebrew in a query.** A session searched for a name spelled
  backwards and got 2 wrong hits instead of 31 (8 open). The search is
  correct; the prompt mixed one line typed in logical order with lines
  pasted in visual (reversed) order, and the model "fixed" the wrong one.
  Cheap guard: when a query contains right-to-left text, also count matches
  for its reverse and mention it if that finds more.

### Stage 1: before any write on the real account

- **Enforce the read-only database boundary.** `CouchClient` only ever sends
  `GET _changes`; make that impossible to break (reject non-GET in the
  client, plus a test). The sync credentials can write, so the guarantee has
  to be ours.
- **Request budget.** Every request is a query, reads and writes alike, in
  one budget shared by the REST API and the database, so mirror polls count:
  at most 1 per 3 seconds, and about 1 per minute on average (1440 a day is
  a guideline; Marvin counts nothing per day). Today the REST limiter and
  the poll timer are separate and nothing counts the day. Wanted: one shared
  limiter and a persisted rolling-24h counter shown by `check` and in
  `get_structure`'s mirror info, with a warning as it nears 1440.
- **Back off properly on 429.** A burst gets a 429 ("Too many AM API
  requests") and a block of about a minute. The retry policy waits 2 to 20
  seconds, so its retries land inside the block. On 429, from the API or the
  database, wait over a minute and slow down afterwards.
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
  Never set `deletedAt` on a live task: it exists only on the device's Trash
  copy, and a live task with it stays in the lists while some features
  ignore it. Alternative Marvin support suggests: keep a local copy of the
  document (the journal does), then `/api/doc/delete`, with undo by
  re-creating it. That would be a delete tool, which the working rules
  forbid today; decide whether the journal changes that.
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

### Answers from Marvin support (2026-10-06)

Marvin added these to the help-center API articles too.

- **Limits**: every request counts as a query, including `/api/doc/update`
  and `/api/markDone`. "1 item per second" only caps bulk creation and is
  not a separate budget. Nothing is counted per day; 1440 means about one
  request a minute on average, shared by the public API and the database.
  One `_changes?since=` request is one query whatever it returns. Usage
  can't be inspected. A burst gets a 429 and a block of about a minute.
- **Desktop local API server**: no rate limits; still needs the API token.
  Serves `/api/test`, `/api/addTask`, `/api/addProject`, `/api/doc` (GET
  only), `/api/list?filter=…` (`done=true` for completed items),
  `/api/todayItems`, `/api/dueItems`, `/api/categories`, `/api/children`,
  `/api/labels`, `/api/trackedItem`, `/api/me`, `/api/kudos`. Does not
  serve `/api/doc/update`, `/api/markDone` or `/api/doc/delete`.
- **Trash**: no supported way to reach it from the API (see "Removing
  items" above).
- **Backups**: the API can't trigger or report one; checking the folder is
  right. Files are `AmazingMarvinBackup_YYYY-MM-DD-HH-MM.json[.lzma]`. The
  desktop app backs up only while running and skips a turn while syncing or
  in active use; failures show under ☰ → Account → Backups.

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
- No rate limits (confirmed by support), but it cannot edit: no
  `/api/doc/update` or `/api/markDone`, so it only helps reads and creation.
  `GET /api/doc` there could serve the fresh read before a read-modify-write
  at no budget cost.
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

- **Name collision**: `github.com/bgheneti/Amazing-Marvin-MCP` is a different
  project with the same distribution name and the same `amazing-marvin-mcp`
  command (v1.0.1, FastMCP). Installing one replaces the other, and it has
  no `check` command, so a wrong install just starts its server. Decide on
  a distinct name or command before any PyPI release; meanwhile `check`
  could print the package origin, and the README should say how to tell.

- **Clients**: Claude apps on Android and the web only reach remote HTTP
  servers; this server is stdio-only. Supporting them means hosting it,
  with the credentials and mirror on that host.

- **README**: describe the read-only-first setup (sync credentials only).

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

## Repo chores

Noted in the 2026-10-05 review, none done yet.

- `CHANGELOG.md` is still the template stub; nothing recorded for #2, #3 or
  the dependency and template updates.
- The Discussions badge in the README and the link in `CONTRIBUTING.md` 404:
  Discussions is disabled on the repo. Enable it or remove both.
- `pip-audit` runs only in PR/push CI, not in the weekly scheduled run, so a
  new advisory stays invisible until the next push.
- `docs/index.md` is a one-line stub and the mkdocs setup is unused (no
  Pages, no RTD); it is also what pulls in `requests`/`urllib3`.
- GitHub was down on 2026-10-06: CI never ran for `828e8fa` (template
  v0.31.0) or later commits, and Dependabot had not rebased PR #4. Check
  both.
