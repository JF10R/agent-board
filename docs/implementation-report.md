# Implementation report

Date: 2026-09-08

## Outcome

Implemented the audit's correctness fixes, coordination utilities, interface redesign and documentation improvements. Added a harness-independent native message listener. Existing event history and live board data were not modified. Changes are grouped by concern for publication.

## Delivered

| Area | Result |
|---|---|
| Acceptance | Completion requires acceptance of the current delivery. A later failed review, changed criteria or reviewer reassignment invalidates previous acceptance. |
| Ownership | Atomic claims, explicit expired-lease recovery and lease tokens prevent concurrent ownership and stale-worker renewal. |
| Handoff | A single delivery event records evidence, next owner and stage. The UI supports claiming and structured delivery. |
| Recovery | Ticket listings reconstruct state from authoritative logs when the disposable cache is missing, stale or corrupt. Cache verification checks content. |
| Event integrity | Sequence and event validation supplement hash checks. Explicit tail recovery preserves original bytes for inspection. |
| Dependencies | Append-only removal maintains reciprocal edges through a durable recovery journal. Incomplete transactions cannot silently expose partial views. |
| Retries | Payload-bound keys deduplicate supported create, comment, worklog, claim, handoff and dependency-removal requests. Uncertain durable writes produce an explicit HTTP 503 with `committed: null`. |
| Agent context | Actor-specific next actions and machine-readable capabilities reduce repeated context reconstruction. |
| Changes | Scoped resumable ticket and message feeds handle restart and avoid timestamp-ordering loss. |
| Architecture | Storage primitives, identity/configuration and shared exceptions live in neutral modules. Ticket logic no longer imports CLI utilities. Compatibility exports preserve existing integrations. |
| Documentation | Updated bootstrap, complete generated command reference, HTTP contracts, recovery, executable workflow and native listener guidance. Removed misleading comments and stale product examples. |
| Distribution | Added installed CLI, web and watcher commands; source archives include documentation, root scripts and test support. |
| Development | Added dev dependencies, static correctness checks, package smoke tests and one Ubuntu CI job using Python 3.13. |

## Interface

- Blue light/dark theme with consistent spacing, typography, buttons and focus states.
- Global custom project picker available in Inbox, Roadmap, Tickets and Team.
- All-project views show project cards; selecting a project establishes an explicit action scope. No silent first-project fallback.
- Mobile navigation is compact; ticket title, next step and delivery precede properties.
- Filters and secondary signals are disclosed on demand. Removed the domain-specific CLI example from the roadmap empty state.
- Corrected skip navigation, keyboard tab navigation, theme-button semantics and view persistence after reload.
- Compacted section headers to approximately 125 px on desktop by removing duplicate workspace chrome and subtitles and reducing vertical spacing. Verified dark desktop and narrow mobile layouts.
- Verified a claim followed by delivery to QA using the actual HTTP endpoint on disposable project data.

## Native push notifications

After installation:

```sh
agent-board-watch --repo /path/to/repository --actor master --cursor-file /path/to/checkpoint.json
```

Without installation, use `python -B agent_board_watch.py` with the same flags from the checkout.

The listener blocks on native filesystem events: Windows `ReadDirectoryChangesW`, Linux inotify or macOS kqueue. It emits recipient-filtered NDJSON and checkpoints after flushing stdout. It does not require a Codex timer or polling loop.

The harness must supervise the process and connect stdout to its message-injection mechanism. Initial startup replays existing addressed messages; checkpoints resume later runs. Delivery is at least once at the output boundary, so consumers deduplicate by message ID. A flush is not an acknowledgement by a model. See [Push listener](push-listener.md).

## Verification

- Full pytest suite: **233 passed in 54.31 seconds**. Ruff static correctness checks and `git diff --check` passed.
- Independent reviews covered ticket invariants, interrupted dependency recovery, native listener lifetime and message-feed robustness.
- Native Windows tests exercised real subprocess delivery, recipient filtering, restart and blocking behavior.
- Browser verification used two disposable projects, desktop and 390 x 844 mobile views. Project isolation, compose scope, keyboard navigation, reload and delivery were exercised. Mobile detail had no horizontal overflow; title appeared at approximately y=224 and next step at y=429.
- Built source and wheel distributions. Installed the wheel into an isolated environment outside the checkout and ran all three installed commands. Verified the source manifest includes agent guidance, root entry points, documentation and test support.
- Temporary UI servers were stopped and their ports confirmed closed.

## Performance measurement

Same local Windows/Python 3.13.7 workload: 20 tickets, 20 comments per ticket, 256-byte comment bodies, two writer threads. The baseline used the original HEAD source; the updated run used the implementation checkout.

| Measurement | Baseline | Updated |
|---|---:|---:|
| Write wall time | 9.4731 s | 8.2661 s |
| Median mutation latency | 24.797 ms | 22.043 ms |
| Median list latency | 17.834 ms | 8.885 ms |

Stat-validated event/display projections and serialized fold snapshots avoid repeated parsing and expensive deep copies without removing event verification. These are local workload measurements, not a universal throughput guarantee. Reproduce with [benchmark_tickets.py](../tools/benchmark_tickets.py).

## Limits and operational notes

- Linux/macOS listener backends are implemented. Local verification exercised Windows; CI covers Ubuntu only to limit runner usage. macOS and other Python versions are not currently CI-tested.
- The project-wide write lock remains to protect coordinated updates; no unmeasured distributed-storage migration was introduced.
- Browser checks used representative fixtures, not an exhaustive screen-reader or large-board accessibility certification.
- Some temporary test/package artifacts remain because automatic approval review rejected cleanup with `blocked by policy`. They are ignored by Git and were not substituted for source changes.
- Restart an existing dashboard process to load the updated Python backend. No existing production process was restarted by this work.
