# Agent Board

A git-local, file-backed coordination board for agent teams: append-only messages,
acks, a roadmap with parent/child structure and revisions, and tickets with stages,
leases, typed dependencies, and reviewed completion. See [AGENTS.md](AGENTS.md) for
the full agent-facing guide.

## Install

Stdlib only — no third-party dependencies. Requires Python 3.10+.

```
git clone <this repo> agent-board
cd agent-board
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
