# Storage and bootstrap

Run `python -B agent_board.py --repo <repo> init` once. It creates
`<git-common-dir>/agent-board/` and seeds `project.v1.json` only if absent.
Worktrees share this directory. Never commit runtime files.

The initial vocabulary is:

```json
{"identities": ["master"], "message_participants": ["lead", "operator"], "roadmap_owners": ["shared", "unassigned"], "workstreams": []}
```

Identities may publish operational board state and are also message participants
and roadmap owners. Message participants may send and acknowledge messages.
An empty workstream list permits safe workstream names. To customize a project,
edit this configuration in its runtime directory before launching agents; retain
at least one identity. `init` does not reset it. Stores with no configuration use
the fallback `gpt-master` / `claude-master` vocabulary until explicitly initialized.
Existing project configurations and historical records are not renamed automatically.
Historical roadmap owners named `sol-master` remain readable when `gpt-master` is
an allowed owner. New writes require the current configured vocabulary.

The ticket actor registry (`actors.v1.json`) is separate. `actor register master
--role master` records ticket role and ownership relationships; it does not edit
project vocabulary or grant board-message permissions. Register developer/reviewer
subagents under their actual identities, using `--master master` when their names
do not have the `master/` prefix. Naming an actor is attribution, not authentication.

## Roadmap representations

The current CLI and HTTP roadmap use `roadmap.v1.json`. Tree annotations,
parent links, gates and observation history live in `roadmap-ext.v1.json`.
`roadmap annotate` updates this sidecar without advancing the base revision.
Use `roadmap tree --json --no-journal` for inspection without journaling.
The ordinary tree command records observations; deriving aggregates is pure,
but loading the journal-enabled tree is not a read-only operation.

`agent_board.roadmap.RoadmapStore` is a separate Python API backed by
`roadmap.v2.sqlite3`, with SQLite transactions, verification and migration support.
It is not the live backend of CLI/HTTP roadmap commands. The two representations
are not automatically synchronized; do not edit one expecting the other to change.
Their status vocabularies also differ. Explicit migration belongs to the Python
API and must be tested on a copy before an operator chooses to apply it.

## Ticket history

`ticket-events/<id>.jsonl` is authoritative; `tickets.v1.json` is a rebuildable
projection. Display mappings and actor registrations are separate runtime files.
Hash links detect corruption; unkeyed hashes do not prove who authored an event.
Never rewrite authors or fabricate event history. Archive and recovery operations
must preserve provenance. Back up the complete runtime directory before maintenance.

`maintenance` removes stale legacy lock dotfiles; it is not ticket recovery or a
SQLite migration. `--root` explicitly selects its board directory.

## Change consumers

`message-changes --actor lead` returns an opaque cursor. Save it only after
processing the page, and deduplicate by immutable message ID across restarts.
The feed creates a disposable `message-feed.sqlite3` discovery index; it does not
modify historical messages. Cursors are tied to the index epoch and recipient.
If the index is lost, the server rejects an old cursor explicitly: restart without
a cursor and deduplicate replayed messages. Ticket feeds use `ticket changes`;
keep ticket and message cursors separate and treat both as opaque values.

## Restricted agent contexts

A writable repository checkout does not imply a writable git common directory.
A harness may allow source edits while protecting `.git` as read-only. Because
Agent Board stores its runtime under `<git-common-dir>/agent-board/`, that context
cannot perform board mutations, even under the correct assigned actor identity.
Some read commands also initialize runtime files, acquire writable locks, or
refresh projections; do not assume that a listing requires only filesystem reads.

An error such as `cannot open filesystem lock for writing` with `errno=13`
indicates an access failure before lock acquisition. Changing the actor, lease
token, retry key, or timeout does not grant filesystem permissions. A failed lock
open does not append the requested ticket event.

The operator must use the harness's supported permission configuration or approval
mechanism to authorize the assigned agent's access to the actual board store.
If the current context cannot change permissions, the operator must start a new
context with the required access and preserve the assigned actor's task context.
Adding the checkout as a writable root is insufficient when `.git` remains
explicitly protected. Check effective permissions in the new context before
resuming; Agent Board cannot change harness policy.

After access is authorized, the assigned agent reloads the ticket revision and
lease, reconciles any intervening updates, and performs its own handoff. Reuse the
idempotency key for the same logical request. If the lease expired, use the normal
explicit recovery workflow before delivery. A master must not impersonate the
assigned agent to make the handoff appear completed.

While access is blocked, report the permission error and next action to the
coordinator through the available harness channel. Independently authorized
source work can continue. Do not delete lock files, remove sandbox ACL rules, or
route mutations through a more privileged process to bypass the restriction.
