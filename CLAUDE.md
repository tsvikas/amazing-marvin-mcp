# amazing-marvin-mcp — working rules

MCP server for Amazing Marvin. Read `README.md` for what it does; this file is
the design rules that keep additions coherent.

## Architecture rules

- **Reads come from the mirror, writes go through the REST API.** The mirror
  (`mirror.py`) is a local copy of the CouchDB sync database kept fresh with
  `_changes`. Never add a tool that reads via REST endpoints (`todayItems`,
  `children`, …): they have no filtering and a 1440 requests/day budget.
  Never write directly to CouchDB: `/api/doc/update` does the `fieldUpdates`
  bookkeeping Marvin's conflict resolution needs.
- **Every write folds its response into the mirror** (`mirror.apply`) so the
  next read is consistent without another request.
- **No `doc/delete` tool.** Marvin's trash is client-side; API deletes are
  unrecoverable. `MarvinAPI.delete_doc` exists for test cleanup only.
- **No "smart"/opinionated tools** ("what should I focus on"). Tools expose
  Marvin's model; the user's conventions live in their workflow Markdown
  (`workflow.md` or a `workflow/` directory; templates in
  `src/amazing_marvin_mcp/templates/workflow/`), injected into the server
  instructions and exposed as `marvin://workflow[/<section>]`.
- **Use Marvin's UI vocabulary** in tool names, parameters, output fields and
  descriptions: Do date (`day`), Due date (`dueDate`, hard deadline), End date
  (`endDate`, self-imposed), Start date, Planned week/month, Duration estimate,
  Importance (P1 red = 3, P2 = 2, P3 = 1), Eat the Frog, Backburner, Master
  List, Inbox (`parentId="unassigned"`), categories vs projects (`type`).
  Don't merge due date and end date; don't call either "deadline" in a field.
- **Models tolerate Marvin's conventions**: unset fields arrive as `null`,
  `""` or `0` (`timeEstimate: 0` = no estimate). Keep `extra="allow"` so
  undocumented fields pass through (`get_task` shows them under
  `other_fields`).
- **Rate limits are Marvin's ask, not ours to optimise away**: ≤1 REST call
  per 3 s (`MarvinAPI` throttles), mirror polls ≤1/min.

## Adding a write field or a new document shape

1. Check the wiki (`Marvin-Data-Types`) for the field; prefer documented shapes.
1. Add it to `update_task`/`create_*` with the UI name; add a unit test in
   `tests/test_server.py` asserting the exact setters sent.
1. Run it live against the **throwaway** account and, if it's a new document
   shape (like labels were), compare with what the web app writes for the
   same thing. A wrong-shaped profile document can stop Marvin from starting.
1. Re-record the live cassette: `uv run pytest tests/test_live.py --record-mode=rewrite` with `MARVIN_*` in the environment.

## Tooling

- `just format`, `just lint`, `just test` (ruff, mypy strict, ty, pytest,
  deptry, prek hooks). All must pass before a commit.

- Lower-bound version pins only (`>=`), no upper bounds.

- Prefer established packages over hand-rolled helpers: pydantic models in
  `outputs.py` for every tool result (compaction lives in their serializers),
  aiolimiter/tenacity for rate limiting and retries, respx for HTTP mocks,
  pytest-recording for the live cassette.

- Credentials never go in the repo: `.env` files are git-ignored and tooling is
  blocked from writing them; document variables in the README instead.

- Future work and open questions go in `IDEAS.md`, not in code comments.
