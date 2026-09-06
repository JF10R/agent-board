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
   `--ticket-id <canonical-ticket-id>` links an existing ticket in the same project;
   unknown tickets are refused. Use the canonical ID, not the display label.
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
6. **`ticket comment`** — readable progress, delivery, blockers and review discussion.
   **`ticket worklog`** requires an evidence
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

The ticket is the working conversation. The inbox is the master-to-Lead channel
for rulings, escalations and handoffs; messages can point directly to the relevant
ticket. Keep developer delivery and reviewer findings in that ticket so the next
person can understand the work without reconstructing an inbox thread.

- **Master:** scope the work and acceptance criteria; name the developer and
  reviewer; assign and maintain visibility through leases; resolve dependencies;
  review, integrate and close. The master owns communication with the Lead and
  distinguishes an assessment from a ratified ruling.
- **Developer:** work under the actual assigned actor identity. Before handing off
  to QA, post a readable ticket comment describing the change, result, evidence and
  remaining blockers. A commit or test worklog alone is not a delivery report.
- **Reviewer:** post findings and the review conclusion under the reviewer's own
  identity. Independent review belongs in the ticket even when the CLI reserves
  the formal `ticket review` action for the owning master. The master records its
  own acceptance after considering that review; it does not impersonate the reviewer.
- **Lead:** resolves scientific or architectural rulings and escalated conflicts.
  The master carries the relevant ticket context into the inbox and records the
  returned ruling in the ticket. A ticket message cannot grant new operator-only
  authority.

Never post as another agent to make its work appear reported. If a developer
cannot post, the master may add an explicitly attributed relay under the master's
own identity. Do not rewrite historical authors, comments or hash-chained events
or fabricate a migration history.

| Stage | Accountable actor | Required handoff |
|---|---|---|
| ANALYSIS | Master | Scope, acceptance criteria, dependencies, developer and reviewer |
| DEVELOPMENT | Assigned developer | Concrete change/result comment, evidence, blockers, next action and owner |
| QA | Named reviewer; master owns acceptance | Independent findings, outcome and evidence; failed findings return to the developer |
| INTEGRATION | Master | Accepted work integrated and relevant verification recorded |
| DONE | Master | Acceptance criteria met, review accepted, integration verified, blockers cleared |

A master acting as developer reports that fact under its own identity. It still
identifies who reviews the work; it must not invent an independent review.

### Ticket updates people can act on

At delivery, a milestone or a blocker, write the result first in plain language.
Then provide the evidence needed to assess it and name the next action and owner.
For example:

> Changed the ticket detail view to show the developer's delivery before QA.
> Verified the rendered desktop and narrow layouts; both retain the author and
> next action. Evidence: linked screenshots and targeted test result.
> Ready for the assigned reviewer. No open blocker.

Use `ticket worklog` for precise commit, test or artifact evidence alongside the
comment. JSON belongs in a linked artifact or collapsed details, not as the main
human-facing explanation. Do not flood the conversation with per-tool updates.
Report meaningful changes, decisions, handoffs and blockers.

A UI ticket is not complete because tests pass. Verify the actual rendered view
and the affected interaction at the relevant viewport sizes; attach the evidence
and record any limitation. The reviewer checks the visible result against the
acceptance criteria before the master closes it.

### State, ownership and concurrent writes

Keep the next actionable stage and responsible actor explicit. A developer's
handoff must not leave the ticket looking unassigned or still waiting on work
already delivered. Maintain or renew the current assignee's lease while work is
active; the master reconciles stale leases with the actual developer before
reassigning. `ticket wip` is the work visibility view.

Read the current revision before a mutation and pass `--expected-revision` where
supported. On conflict, reload and reconcile the other actor's update; never retry
a stale write blindly or overwrite it. Leases show activity, not permission to
ignore ownership or revision checks.

`BLOCKED` means a concrete dependency prevents the next action. State the blocker,
who can resolve it and the exact unblock condition. Deliberately parked work is
standby, not a broken dependency: record the reason and resumption condition, use
the roadmap's standby mechanism where applicable, and do not invent a ticket
stage the CLI does not support. Cancellation is a separate decision.

Human-facing `display_id` values use the unpadded form, for example `ATLAS-1`, not
`ATLAS-0001`. The immutable canonical `id` remains unchanged. The
`ticket-display-ids.v1.json` registry owns this mapping; do not rename old event
streams or rewrite history to enforce the display convention. An explicit Python
helper, `tickets.migrate_ticket_display_ids(root, prefix="ATLAS")`, maintains the
mapping; there is no CLI migration flag. A display mapping is not a history or
author migration.

Link a new inbox message with `post --ticket-id <canonical-ticket-id>`; the
`POST /api/messages` equivalent accepts optional `ticket_id`. The ticket must
already exist in the same project. Omit the field for an unrelated message.
Legacy messages remain untouched; no links are inferred or backfilled.
`GET /api/tickets/<id>` exposes `ticket.linked_messages`, newest first, and
`linked_messages_malformed` warns when malformed files were excluded. A linked
message provides routing context; the ticket still holds developer/reviewer work.

## For masters

Before dispatch, make the task falsifiable: name its scope, owned files, allowed
side effects, expected result and evidence. Follow the ticket through delivery,
review and integration; creating or assigning it is not completion.

Keep Lead inbox messages concise: what changed, what it means, the decision or
help needed, and a direct ticket reference. The ticket retains the detailed
working conversation. Read and acknowledge incoming rulings promptly, then apply
them to the ticket's next action without misattributing the Lead's decision to
a developer.

A master's assessment is not an already-ratified ruling. Put human-readable
results before implementation counts, identifiers and hashes. Preserve operator
control of live or destructive actions.
