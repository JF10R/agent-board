; agent-board CLAUDE.md v1.0 | 2026-09-06
; scope: repo entry point for agents.

RULE read-first
  read AGENTS.md first — it is the source of truth: store layout, CLI verbs,
  derived views, web routes, roles/ticket workflow, conventions.

RULE store-local
  the store is git-local per served repo: always <git-common-dir>/agent-board
  (the web dashboard's --board-root flag can point at a specific folder instead).
  never commit store contents into the repo it serves.

RULE write-discipline
  --summary caps at 300 chars; put detail in --body-file, never inline.
  no backticks/shell metacharacters in CLI args.
  every open() uses encoding='utf-8'; set PYTHONIOENCODING=utf-8 on Windows.

RULE blocker-rule
  --blocker required iff --status BLOCKED; rejected for every other status.

RULE revision-safety
  roadmap upsert always takes --expected-revision; a mismatch is a conflict,
  not a silent overwrite.

cfg entry-points
  python -B agent_board.py <command> ...       — CLI
  python -B agent_board_web.py --repo <path>   — web dashboard
