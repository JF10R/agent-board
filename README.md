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

## Windows notes

Set `PYTHONIOENCODING=utf-8` before running under PowerShell/cmd to avoid cp1252
decode errors when reading or printing UTF-8 content.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
