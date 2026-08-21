# Ideas and open questions

Things worth doing later, roughly by payoff. Move an item to the CHANGELOG when
it ships; delete it when it stops being a good idea.

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

- `uvx --from git+… marvin-mcp-server serve` one-liner; PyPI release once the
  tool set settles.
- Claude Desktop / other clients: verify the `.env` lookup works when the
  client's working directory is `/`.
- A `marvin-mcp-server doctor` that prints the tree and label summary, to
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
