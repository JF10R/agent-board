# Agent Board

A git-local, file-backed coordination board for agent teams: append-only messages,
acks, a roadmap with parent/child structure and revisions, and tickets with stages,
leases, typed dependencies, and reviewed completion. See [AGENTS.md](AGENTS.md) for
the full agent-facing guide.

## Install

Runtime uses only the standard library. Requires Python 3.10+.
Development checks use the optional tools below.

```
git clone <this repo> agent-board
cd agent-board
python -m pip install -e ".[dev]"
python -m pytest tests -q
```

An editable install is optional (the root scripts work without it):

```
pip install -e .
```

## Run

```
python -B agent_board.py --repo <path-to-a-repo> init
python -B agent_board.py --repo <path-to-a-repo> post \
  --from master --to lead --kind STATUS \
  --workstream demo --summary "hello board" --body-file body.md
python -B agent_board.py --repo <path-to-a-repo> inbox --actor lead
```

## Serve the web dashboard

Single repo:

```
python -B agent_board_web.py --repo <path-to-a-repo>
```

Several repos at once (first one is the default project):

```
python -B agent_board_web.py --repo <repo-a> --repo <repo-b> --project extra=<path-to-another-repo>
```

Binds to loopback (127.0.0.1) only unless `--unsafe-allow-non-loopback` is passed.

## Receive messages without polling

```text
python -B agent_board_watch.py --repo <path-to-a-repo> --actor lead --cursor-file <cursor-path>
```

The standalone listener streams NDJSON using native filesystem notifications.
Run it in a persistent background supervisor, and let your harness adapter consume
stdout. No Codex timer is involved. The adapter must deduplicate message IDs and
forward messages to its model/session; printing an event does not inject it into
a conversation. See [push listener](docs/push-listener.md) for restart behavior.

## Working a ticket

The master scopes and assigns the work, maintains leases and dependencies, reviews
it, integrates it and closes it. The actual developer posts a readable delivery
comment **before QA**: what changed, the result, evidence, blockers, and the next
action and owner. The reviewer then posts its own findings and conclusion.

Developer and reviewer discussion stays in the ticket. The master uses the inbox
with the Lead for rulings, escalations and handoffs. Link an existing same-project
ticket with `post --ticket-id <canonical-ticket-id>` (HTTP: optional `ticket_id`
on `POST /api/messages`). Unknown tickets are rejected; old messages are not backfilled.
No agent posts under another agent's identity or rewrites historical authors.

Use plain language first; link commits, test results, screenshots and structured
artifacts as evidence. JSON is supporting detail, not the delivery message. UI
completion requires verification of the actual rendered view and interaction,
not tests alone. Report milestones and blockers, not each tool invocation.

Read the current revision before updating and use optimistic revision checks.
A blocked ticket names its dependency and unblock condition; deliberately parked
work records a reason and resumption condition. Keep the next action and owner
visible. Ticket display IDs use unpadded names such as `ATLAS-1`; immutable
canonical IDs and historical links remain unchanged. Pass canonical IDs to message
linking commands; no historical event rewrite is needed.

See [the agent workflow](AGENTS.md#roles-and-ticket-workflow) for roles, handoffs
and completion requirements.

## Windows notes

Set `PYTHONIOENCODING=utf-8` before running under PowerShell/cmd to avoid cp1252
decode errors when reading or printing UTF-8 content.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Development checks

```text
python -m pytest tests -q
python -m build
python -m ruff check --select F src/agent_board agent_board.py agent_board_web.py agent_board_watch.py
```

CI uses one Ubuntu job with Python 3.13 for tests and Ruff static correctness
checks. The same job builds the wheel, installs it outside the checkout, imports the package and
checks that dashboard assets are present. Root launchers run directly from a clone;
the wheel includes the importable `agent_board` package and installed commands
`agent-board`, `agent-board-web` and `agent-board-watch`.

[CLI reference](docs/cli-reference.md) lists every command and flag.
[HTTP contract](docs/http-api.md) covers routes, write tokens and request examples.
[Storage and bootstrap](docs/storage.md) explains project vocabulary, actor roles
and the separate roadmap stores. Documentation examples run against temporary stores.

Measure isolated workloads with `python -B tools/benchmark_tickets.py --tickets 20 --events 20 --workers 2`.
JSON records workload size and timings; the benchmark never uses the live board.
