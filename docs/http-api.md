# HTTP contract

Start `python -B agent_board_web.py --repo <repo> --host 127.0.0.1 --port 8765`.
Use the literal bound IP and actual port in every URL and Host header; `localhost`
is not accepted in place of `127.0.0.1`. IPv6 Host values require brackets.
The default bind is loopback. Non-loopback binding requires explicit
`--unsafe-allow-non-loopback`; this is not an authentication service.

All API routes accept `?project=<served-name>`; omission selects the first project.
POST also accepts a `project` field, which overrides the query selection.
Do not rely on the default when coordinating several projects.

POST requires `Content-Type: application/json`, a Content-Length, an exact
`Origin: http://127.0.0.1:8765` and `X-Agent-Board-Token`. Obtain the process-local
token from the `agent-board-token` meta element in `GET /`; it changes on restart.
Do not save it in source or shell command history. JSON must be an object with no
duplicate or unknown keys. Send actual integers and booleans, not string versions.

## Executable request example

This example creates one ticket in the selected project. Run only against a
throwaway server while learning the API. Python sets Content-Length and Host.

```python
import json
import re
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8765"
page = urlopen(base + "/").read().decode("utf-8")
token = re.search(r'name="agent-board-token" content="([^"]+)"', page).group(1)
request = Request(
    base + "/api/tickets",
    data=json.dumps({"actor": "master", "id": "example", "title": "Example ticket"}).encode("utf-8"),
    headers={"Content-Type": "application/json", "Origin": base, "X-Agent-Board-Token": token},
    method="POST",
)
with urlopen(request) as response:
    print(json.load(response))
```

Use `/api/capabilities` for current project-aware command options, required fields,
choices, route map and idempotency support. Only reuse an idempotency key for the
same logical write; preserve the exact request when retrying. Create, comment,
worklog, claim, handoff and dependency removal accept `idempotency_key`.
Create requires an explicit `id` when a key is supplied.

## Read routes

| GET route | Result / query |
|---|---|
| `/` | Dashboard HTML with fresh write token |
| `/tickets/<project>/<reference>` | Dashboard deep link; canonical or display reference |
| `/app.js`, `/styles.css`, `/copy.js`, `/markdown.js`, `/selection.js` | Dashboard assets |
| `/api/projects` | Served names, roots and default |
| `/api/version` | Data/UI versions and per-project versions; preferred poll target |
| `/api/state` | Snapshot; optional `standby` hours |
| `/api/messages/<id>` | Metadata, body, raw Markdown and acknowledgement |
| `/api/messages/<id>/thread` | Message thread |
| `/api/messages-folder` | Messages directory path |
| `/api/ack-backlog` | Unacknowledged counts |
| `/api/roadmap` | Base roadmap items |
| `/api/roadmap/<id>` | One base item |
| `/api/roadmap-tree` | Derived tree; `since` and `standby` hours |
| `/api/tickets` | Filters: `stage`, `assignee`, `parent`, `include_archived=true` |
| `/api/tickets/tree` | Parent/child tree |
| `/api/tickets/critical-path` | Open blocker chain |
| `/api/tickets/metrics` | Throughput and stage metrics |
| `/api/tickets/wip` | Assignee/stage work visibility |
| `/api/tickets/resolve/<reference>` | Resolve canonical/display reference |
| `/api/tickets/<id>` | Detail including linked messages |
| `/api/tickets/<id>/export` | Standalone Markdown |
| `/api/tickets/<id>/verify` | `chain_ok` and diagnostic detail |
| `/api/actors` | Actor registry; optional `role` |
| `/api/capabilities` | Machine-readable operation contracts |
| `/api/actors/<name>/context` | Compact next-action context |
| `/api/tickets/changes` | Resumable events; `cursor`, `limit` |
| `/api/tickets/cache/verify` | Projection verification |
| `/api/messages/changes` | Resumable message feed; `cursor`, `limit`, `actor` |

## Write routes

| POST route | Operation |
|---|---|
| `/api/messages` | Send message; optional existing canonical `ticket_id` |
| `/api/acks` | Acknowledge with `actor`, `message_id` |
| `/api/roadmap` | Upsert; revision required; optional sidecar extension |
| `/api/tickets` | Create; required `actor`, `title`; optional `id` |
| `/api/tickets/<id>/upsert` | Update ticket fields with revision check |
| `/api/tickets/<id>/assign` | Assign and lease |
| `/api/tickets/<id>/heartbeat` | Renew current lease |
| `/api/tickets/<id>/transition` | Change stage; assigned developers may start DEVELOPMENT with an active lease (`lease_token` required for fenced leases) |
| `/api/tickets/<id>/comment` | Human-readable update |
| `/api/tickets/<id>/worklog` | Evidence pointer |
| `/api/tickets/<id>/review` | Owning master's acceptance |
| `/api/tickets/<id>/done` | Reviewed completion |
| `/api/tickets/<id>/dependencies` | Add typed dependency |
| `/api/tickets/<id>/archive` | Archive terminal ticket |
| `/api/actors` | Register `name`, `role`; optional `display`, `master` |
| `/api/tickets/<id>/claim` | Atomic lease claim; explicit expired-lease recovery |
| `/api/tickets/<id>/handoff` | Delivery, evidence and next actor/stage together |
| `/api/tickets/<id>/dep-remove` | Append-only dependency removal |
| `/api/tickets/<id>/recover` | Explicit damaged-tail recovery preserving original bytes |
| `/api/tickets/cache/rebuild` | Rebuild projection from authoritative logs |
| `/api/open-messages-folder` | Open Windows Explorer; loopback server/client only; 204 |

Handoff requires `actor`, `summary`, an `evidence` object and `next_actor` (the
HTTP spelling of CLI `--next-actor`). Optional fields are `body`, `stage`,
`expected_revision`, `lease_token` and `idempotency_key`. It requires the current
assignee's active lease. The next actor claims the handed-off work to open a lease.

Ticket actions require `actor`. Use `expected_revision` wherever supported and
reload on conflict. Read-only dashboard tree loads do not journal; the CLI tree
default differs (see [storage](storage.md)).

## Errors

JSON failures return `{"ok": false, "error": "..."}`. Permission/Host/Origin/token
failures return 403; malformed POST fields return 400; revision/ownership conflicts
return 409; unknown routes return 404; filesystem failures return 500. An append flush/fsync failure returns 503 with
`{"ok": false, "error": "...", "committed": null, "ticket_id": "...", "revision": 1}`:
the write outcome is uncertain. Inspect the event stream before retrying, then use
the same idempotency key for the identical request after repair. GET domain
lookup/validation errors currently return 404. Check `chain_ok` for verification:
a successful HTTP response does not imply an intact chain. Never retry a stale
revision blindly. A write token authenticates the local browser session, not the
actor name supplied by a caller.
