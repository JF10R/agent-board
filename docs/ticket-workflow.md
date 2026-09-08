# Claim, deliver, review and integrate

Use the actual developer identity. An atomic `ticket claim` accepts only unleased
work; an expired lease requires `--recover`, and an active lease cannot be stolen.
Keep the returned lease `token` for heartbeat and handoff. A handoff records result,
evidence, next assignee and stage in one event, then clears the previous lease.
The next named actor claims the work to open its own lease; an unrelated actor
cannot claim that handoff. Explicit assignment changes the named owner. Handoff
requires an active lease. Replacing an existing lease fences the new lease;
legacy first assignments retain token-optional compatibility.

The named reviewer posts findings under its own identity. The owning master records
the formal review. Only the latest positive review of the current accepted scope
permits completion: a later FAIL, changed title/summary/body/criteria/reviewer,
new delivery or worklog requires fresh acceptance. Assignment changes to the
reviewer also invalidate acceptance. Comments and integration-stage
changes do not by themselves invalidate acceptance.

## Executable isolated workflow

Run this from the repository root. It creates a temporary Git repository and never
uses the live board. The example executes a real smoke command before reporting its
exit code. Real work must attach evidence from its own verification.

```python
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

launcher = Path("agent_board.py").resolve()
with tempfile.TemporaryDirectory(prefix="agent-board-example-") as directory:
    repo = Path(directory)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    base = [sys.executable, "-B", str(launcher), "--repo", str(repo)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def run(*args):
        completed = subprocess.run(base + list(args), check=True, capture_output=True,
                                   text=True, encoding="utf-8", env=env)
        return completed.stdout if args[0] == "init" else json.loads(completed.stdout)

    run("init")
    run("actor", "register", "master", "--role", "master")
    run("actor", "register", "dev", "--role", "subagent", "--master", "master")
    run("actor", "register", "qa", "--role", "subagent", "--master", "master")
    run("ticket", "create", "--actor", "master", "--id", "demo", "--title", "Verify example",
        "--reviewer", "master", "--acceptance-criterion", "Smoke command exits zero")

    def mutate(action, actor, *args):
        current = run("ticket", "get", "demo")
        return run("ticket", action, "--actor", actor, "--id", "demo",
                   "--expected-revision", str(current["revision"]), *args)

    mutate("transition", "master", "--stage", "DEVELOPMENT")
    claimed = mutate("claim", "dev", "--idempotency-key", "demo-claim")
    smoke = subprocess.run([sys.executable, "-c", "print('example smoke OK')"],
                           capture_output=True, check=True)
    mutate("handoff", "dev", "--summary", "Smoke command passed; ready for review",
           "--test", "python example smoke", "--exit-code", str(smoke.returncode),
           "--next-actor", "qa", "--stage", "QA", "--lease-token", claimed["lease"]["token"],
           "--idempotency-key", "demo-delivery")
    mutate("comment", "qa", "--summary", "Reviewed captured smoke result; criterion met")
    mutate("review", "master", "--verdict", "PASS", "--summary", "Accepted reviewer findings")
    mutate("transition", "master", "--stage", "INTEGRATION")
    completed = mutate("done", "master")
    assert completed["stage"] == "DONE"
    print("Workflow completed")
```

## Retries and recovery

Create, comment, worklog, claim, handoff and dependency removal accept
`--idempotency-key`. HTTP create requires an explicit `id` when using a key. Reuse one key
only for the identical logical request. A durable event may commit even if cache
publication fails: the returned `persistence` object reports `committed: true`,
`projection_updated: false` and the error. Do not treat that as rejection and
repeat with a fresh key. Reads recover visibility from authoritative logs;
`ticket cache-verify` and `ticket cache-rebuild` inspect and repair the projection.

An append flush/fsync failure has an uncertain outcome: HTTP returns 503 with
`committed: null`, `ticket_id` and `revision`; CLI exits nonzero with the uncertainty.
Inspect the event stream before retrying. After repair, retry the identical logical
request with the same key; do not turn an uncertain write into a fresh operation.

`ticket verify ID` checks event structure, contiguous sequence and hash links.
`ticket recover --actor master --id ID` permits only incomplete invalid JSON at
the very end, without a terminating newline, after a valid prefix. It saves the
original bytes under `ticket-recovery/`, with an actor/hash/count sidecar, before
removing that tail. Interior damage,
valid-but-invalid-schema events and hash failures need investigation; they are
not silently repaired. Preserve the reported backup and inspect verification
again. Recovery does not change attribution or invent replacement events.

Dependency additions/removals record durable intent under `ticket-transactions/`
before updating both streams. Partial completion blocks reads explicitly; the next
mutation or cache rebuild recovers the pending operation before new work proceeds.
`ticket dep-remove` preserves history and handles reciprocal blocker edges.
`actor context NAME` summarizes current revision, blockers, delivery, reviews and
candidate actions; validate current state when acting. `capabilities` exposes
project-aware command options, choices and supported idempotent operations.

For a writable checkout with a protected `.git` directory, follow the
[restricted-context procedure](storage.md#restricted-agent-contexts). The operator
authorizes the context; the assigned agent remains responsible for its handoff.
