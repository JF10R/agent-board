# Agent Board

A git-local, file-backed coordination board for agent teams. Masters and operators
post append-only Markdown messages, acknowledge them, maintain a roadmap of items
with parent/child structure, blockers, and revisions, and drive tickets through a
stage lifecycle with leases, typed dependencies, and reviewed completion.
A derive layer computes read-only aggregates (startable, standby, blocked-by,
milestone progress) from the roadmap. Everything lives under one git-local directory
per served repo; nothing here is ever committed into the repo it serves.

Two entry points, both plain `python -B <file>.py <command> ...`:
- `agent_board.py` — the CLI (post, ack, inbox, read, roadmap, ticket, actor, status).
- `agent_board_web.py` — a local read/write web dashboard over one or more repos.

## Where the data lives

The store is git-local: always `<git-common-dir>/agent-board/`. `init` (see below)
creates the store and, on first run only, writes `project.v1.json` with that
project's actor list, message participants, roadmap owners, and workstreams. Later
`init` calls never touch an existing `project.v1.json`.

## Using it, step by step

1. **`init`** — run once per repo: `python -B agent_board.py --repo <path> init`.
   Creates the runtime directories and seeds `project.v1.json` if absent.
2. **`post`** — create an immutable Markdown message:
   `post --from NAME --to NAME --kind KIND --workstream NAME --summary "..." --body-file <path>`.
   Always write the body to a file and pass `--body-file`; never inline a long body
   or one containing backticks/shell metacharacters. `--summary` is capped at 300
   characters — it is the one line a reader sees in a listing; put detail in the body.
   `--requires-ack` flags a message that expects an `ack`. `--reply-to <id>` threads it.
3. **`ack`** — acknowledge a message: `ack --actor NAME <message-id>`. Written as a
   separate file; the original message is never mutated.
4. **`inbox`** — list messages addressed to one participant: `inbox --actor NAME`.
5. **`read`** — print one message in full: `read <message-id>`.
6. **`roadmap upsert`** — create or update one item. Requires `--expected-revision N`
   (0 for a new item, else the revision you last read) so two writers racing on the
   same item get a revision-conflict error instead of a silent overwrite. Fields:
   `--id --title --summary --status --owner --progress`, optional `--blocker` (see
   below), `--depends-on ITEM_ID` (repeatable, replaces the list wholesale),
   `--kind {TASK,MILESTONE,OBJECTIVE}`, `--impact TEXT`, `--standby REASON`.
7. **`roadmap annotate`** — attach structure without bumping the v1 revision:
   `--parent ID`, blockers/gates/due date, `--depends-on`, `--kind`, `--impact`,
   `--standby`. Lives in a sidecar (`roadmap-ext.v1.json`) so old v1 readers are
   unaffected.
8. **`roadmap get`**, **`roadmap list`**, **`roadmap tree`** — read views; `tree --json`
   prints the full derived payload (see Derived views below).

Statuses: `PENDING, NOT_STARTED, READY, IN_PROGRESS, BLOCKED, COMPLETE, CLOSED`.
**Blocker rule**: `--blocker TEXT` is required when `--status BLOCKED`, and rejected
for every other status — an item cannot be silently "blocked" without saying why, and
cannot carry a stale blocker once it moves on.
Kinds: `TASK` (default), `MILESTONE`, `OBJECTIVE` — both aggregate child progress when
no owner-reported progress exists for them.
`depends_on` / `parent`: `parent` is tree structure (one parent, many children);
`depends_on` is a same-level dependency edge used by the derived "blocked-by" and
"parallel frontier" views.

### Derived views

`agent_board.derive` computes, from `roadmap tree`'s payload: which items are
startable now, which are on standby (paused deliberately, distinct from blocked),
which are blocked and by what, the parallel frontier (startable items with no shared
dependency), and milestone/objective progress (owner-reported progress wins; else the
mean over open children). Absent data stays absent — an unknown dependency reads as
unresolved, not as zero.

## Tickets

Additive to the board: `agent_board.tickets` is a separate subsystem from the
roadmap above, with its own store. A ticket's source of truth is its per-ticket
hash-chained event log (`ticket-events/<id>.jsonl`); `tickets.v1.json` is a
rebuildable cache of the folded state.

1. **`ticket create --actor NAME --id ID --title TEXT`** — optional `--kind
   {RESEARCH,ENGINEERING,REVIEW,OPS}` (default `ENGINEERING`), `--summary`,
   `--body`/`--body-file`, `--parent ID`, `--acceptance-criterion TEXT` (repeatable),
   `--assignee`, `--reviewer`, `--subagent NAME` (repeatable).
2. **`ticket assign --actor NAME --id ID --assignee NAME`** — self-assign or
   reassign, and open a lease (`--ttl-sec`, default 3600). The lease is the only
   work-in-progress-visibility mechanism; **`ticket heartbeat`** extends it, and it
   closes automatically on **`ticket done`**.
3. **`ticket transition --actor NAME --id ID --stage STAGE`** — one of `BACKLOG,
   ANALYSIS, DEVELOPMENT, QA, INTEGRATION, BLOCKED, CANCELLED` (not `DONE`; see
   below). Moving into an active stage is refused while an open `BLOCKED_BY`
   dependency exists.
4. **`ticket review --actor NAME --id ID --verdict {PASS,CONFIRMED_WITH_FIXES,FAIL}
   --summary TEXT`** — `--finding TEXT` (repeatable) is required for
   `CONFIRMED_WITH_FIXES`/`FAIL`. Reserved for the ticket's owning master. A `FAIL`
   bounces the ticket back to `DEVELOPMENT`.
5. **`ticket done --actor NAME --id ID`** — requires a `PASS`/`CONFIRMED_WITH_FIXES`
   review and no open blockers (`--force` skips both gates); clears the lease.
6. **`ticket comment`** / **`ticket worklog`** — a worklog entry requires an evidence
   pointer (`--repo --sha`, `--test --exit-code`, or `--artifact --content-hash`):
   evidence, never re-narrated prose.
7. **`ticket dep-add --actor NAME --id ID --type TYPE --target ID`** — `TYPE` one of
   `BLOCKED_BY, UNBLOCKS, ADVANCES, SUPERSEDES`. A `BLOCKED_BY` edge is rejected if it
   would create a cycle, and automatically adds the symmetric `UNBLOCKS` edge on the
   target.
8. **`ticket list`**, **`ticket get ID`**, **`ticket tree`** (parent/child nesting),
   **`ticket critical-path`** (longest open `BLOCKED_BY` chain), **`ticket metrics`**
   (throughput, time-in-stage, per-master counts), **`ticket wip`** (assignee ->
   stage -> ticket ids) — read views.
9. **`ticket archive --actor NAME --id ID`** — only for a `DONE`/`CANCELLED` ticket;
   snapshots the event log under `ticket-archive/<id>/` with a content hash.
10. **`ticket export ID`** — renders one ticket as standalone Markdown.
11. **`ticket verify ID`** — recomputes the hash chain and reports the first break,
    if any.
12. **`actor register NAME --role {lead,operator,master,subagent}`** / **`actor
    list`** — a subagent not named `<master>/<name>` must pass `--master`.
    `resolve_reviewer` follows this registry to find a subagent's owning master.

Revision safety matches the roadmap: most mutating verbs accept `--expected-revision`
and fail closed on a mismatch instead of silently overwriting a concurrent write.

## Web server

`python -B agent_board_web.py --repo <path> [--repo <path> ...] [--project NAME=PATH]`
serves a read/write dashboard. `--repo` may repeat (name = the worktree's directory
name); `--project NAME=PATH` names a project explicitly and combines with `--repo`.
The first project listed is the default. Binds to loopback only unless
`--unsafe-allow-non-loopback` is passed explicitly.

Key routes: `/api/state` (full board snapshot for the active project, including
tickets/leases/actors), `/api/version` (cheap poll target — bump-only, for detecting
new activity without re-fetching state), `/api/projects` (the served project list,
for the project switcher), `/api/tickets` (list/create) and `/api/tickets/<id>/
<action>` (assign, heartbeat, transition, comment, worklog, review, done,
dependencies, archive), `/api/actors` (list/register). A client should poll
`/api/version`, not `/api/state`, and only re-fetch state when the version changes.

## Monitoring an inbox from an agent harness

A poll loop suitable for a Monitor-style watcher — prints one line per new message,
then blocks again:

```python
import subprocess, sys, time

seen = set()
while True:
    out = subprocess.run(
        [sys.executable, "-B", "agent_board.py", "--repo", REPO, "inbox", "--actor", ACTOR, "--json"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    for msg in json.loads(out):
        if msg["id"] not in seen:
            seen.add(msg["id"])
            print(f"{msg['id']}: {msg['from']} -> {msg['summary']}")
    time.sleep(POLL_SECONDS)  # run this loop in a background watcher, never a bare sleep in an agent turn
```

Run it as a background task, not inline in an agent's own turn — a `run_in_background`
job started from inside a subagent's turn dies when that turn ends; run the watcher at
the level (harness, orchestrator) that outlives the turn.

## Conventions

- No backticks or shell metacharacters in CLI arguments; write message/roadmap bodies
  to a file and pass `--body-file`.
- Every file the tooling opens uses `encoding="utf-8"`; on Windows, also set
  `PYTHONIOENCODING=utf-8` in the environment before invoking Python, or cp1252
  decode/encode errors surface on both read and print.
- Read exit codes from a file, never through a pipe (`cmd | tail; echo $?` reads the
  pager's exit code, not the command's).

## Roles and ticket workflow

- A **MASTER** agent (Claude Master, Astra, etc.) owns its subagents' reporting: it
  makes sure each subagent's ticket comments state the work actually done, it is the
  one who messages the Lead and tags the Lead on tickets, and it speaks for its
  subagents rather than having them contact the Lead directly.
- The **LEAD** is a high-intelligence agent (Astra Pro, Fable 5.1 Max, etc.) that
  today communicates through the human operator manually — there is no automated Lead
  agent connected to the board yet (tracked as a TODO).
- Subagents are named and assigned inside tickets (`ticket assign`, `--subagent`);
  the corresponding master owns their review. The developer/reviewer exchange
  happens inside the ticket (`ticket comment`/`ticket worklog`), not over the inbox.
  A master sees what is busy through `ticket wip` (assignee -> stage -> tickets) and
  each ticket's lease, not through claims.

| Stage | Who | What happens |
|---|---|---|
| ANALYSIS | Master (or the Lead, if already done) | Scope the ticket, confirm it's ready to start |
| DEVELOPMENT | A subagent, or the master | Does the work; comments in the ticket what was implemented, files changed, and problems raised |
| QA | Reviewer (usually the owning master) | Reviews, and also analyses/fixes the raised problems; tag the Lead here when their opinion is needed |
| INTEGRATION | Master | Merges/lands the reviewed work |
| DONE | Master | Closes the ticket |

Ticket-level assignment (leases), per-stage timers (`time_in_stage`), and typed
dependency edges are built (see Tickets above). A roadmap/ticket event-stream monitor
analogous to the inbox monitor above is not built yet — see TODO.md.

## For masters

A master's board output is an **assessment**, ratified by the operator into a
**ruling** — never present your own read as already-decided. Report format: five
plain-language lines first (what happened, what it means, what's next — no jargon),
then structured Markdown (headings, lists, tables) for anyone who wants the detail.
IDs and hashes go last, as pointers, not inline in the prose.
