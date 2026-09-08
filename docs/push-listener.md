# Native push listener

From a clone, use Python 3.10+ and Git; no runtime package installation is required:

```text
python -B /absolute/path/agent_board_watch.py --repo <repo> --actor lead --cursor-file <cursor-path>
```

After installing the package, `agent-board-watch` provides the same flags.

Initialize the target repo first with `agent_board.py --repo <repo> init`.
Use an absolute launcher path when the supervisor's working directory differs.
Give each recipient/consumer its own cursor file outside tracked project files;
create its parent directory first. Keep it outside the board's `messages/`
directory to avoid self-triggered filesystem events. The listener sets UTF-8 stdout
explicitly. For other board commands, set `PYTHONIOENCODING=utf-8` on Windows.
The listener filters messages addressed to `--actor` and emits existing matching
messages before waiting for new filesystem notifications. Omitting `--cursor-file`
replays existing matching messages on each launch.

The process blocks on native operating-system notifications: Windows
`ReadDirectoryChangesW`, Linux `inotify`, and macOS/BSD `kqueue`. It does not schedule
Codex tasks, sleep on a polling interval, or depend on any model vendor. Keep it
running under a long-lived process supervisor that survives individual agent
turns; configure restart-on-failure there. Capture stderr separately from stdout.
A child process tied to a completed tool turn is not a durable supervisor.

## Adapter contract

Stdout is UTF-8 NDJSON, one flushed JSON object per line:

- `message.posted`: the feed's `sequence`, `type` and `message` object, including
  immutable `message.id` and recipient metadata.
- `watch.ready`: initial catch-up is complete, with `actor` and current `cursor`.

The adapter parses records, ignores readiness as a user message, deduplicates by
`message.id`, and submits message content through its harness's supported session
input API. That integration is harness-specific: the listener provides events,
not model-session injection. Feed records contain metadata, not the full message
body; fetch it with `agent_board.py --repo <repo> read <message-id>` when needed. Do not pretend stdout alone wakes a model. A durable
adapter queue should retain a message until the destination confirms receipt.

Cursor persistence follows stdout flush. Delivery to stdout is at least once:
a crash between those operations can replay an event. This is not an end-to-end
acknowledgement from the model; buffering or downstream failure can occur after
the producer checkpoint. Adapter persistence/retry must handle that boundary.

The underlying message feed index is disposable. If it is rebuilt, an old cursor
fails explicitly. Preserve the diagnostic, restart with a fresh cursor and
replay/deduplicate immutable message IDs. Do not reuse one recipient's cursor for
another recipient.

## One-message smoke example

Set `REPO` to an initialized throwaway repo and `ACTOR` to its recipient before
executing this Python block from the Agent Board checkout. Post a message addressed
to that recipient in another terminal, or use an existing message. This smoke
consumer stops after one message; a real adapter keeps consuming and supervises
restarts. Diagnostics remain on stderr.

```python
import json
from pathlib import Path
import subprocess
import sys

watcher = subprocess.Popen(
    [sys.executable, "-B", str(Path("agent_board_watch.py").resolve()),
     "--repo", str(REPO), "--actor", ACTOR],
    stdout=subprocess.PIPE, text=True, encoding="utf-8",
)
try:
    for line in watcher.stdout:
        event = json.loads(line)
        if event["type"] == "message.posted":
            message = event["message"]
            print(f"{message['id']}: {message['summary']}")
            break
    else:
        raise RuntimeError("Listener exited before delivering a message")
finally:
    watcher.terminate()
    watcher.wait(timeout=10)
```

For one-off inspection, `agent_board.py --repo <repo> inbox --actor lead` remains
a snapshot command. Browser dashboard version polling is separate from the push
listener and is not required for agent delivery.
