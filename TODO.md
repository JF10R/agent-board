# TODO

Backlog only. One line per item, at most one pointer. Tickets, leases, typed
dependencies, review-gated DONE, an actor registry, archival, and metrics landed
(see AGENTS.md "Tickets"); claims were retired in favor of leases. What's below is
what that pass did not cover.

## Roles and ticket lifecycle

- A roadmap/ticket event-stream monitor, mirroring the inbox monitor in AGENTS.md,
  so an agent is notified of ticket events without polling (operator request).
- An automated Lead agent connected to the board — today the Lead communicates only
  through the human operator (see AGENTS.md "For masters").

## Dependencies and monitoring

- Inbox messages can reference tickets and be linked to them (operator request).
- Automatic inbox notification (not just a ticket comment) when a blocker closes
  (Claude Master).

## Cross-project and actors

- Cross-project ticket references with a global id `<project>#<id>` (Claude Master).
- Cross-harness use — Codex agents through the same CLI — store never committed,
  optional `board export` snapshot for history (Claude Master).

## CLI and web

- Web dashboard views for tickets: kanban by stage, per-agent WIP board,
  dependency graph, stale flags, inbox-to-ticket links (Claude Master). The CLI/API
  are done; the browser UI is not built.
- An event-stream endpoint `/api/events?since=` plus a `tail` CLI for watchers, and a
  push hook for operator-facing events (Claude Master).

## Metrics

- Time-to-review, reopen rate, and a weekly digest cadence on top of the existing
  per-stage/per-master `ticket metrics` (Claude Master).

## Discipline

- Keep the write-short discipline everywhere: summary <= 300 chars, bodies as
  pointers, never re-narrated (Claude Master).
