"use strict";

// Agent Board — clipboard formats for agents (pure; node-tested by tools/test_agent_board_web_copy.py).
// One message or N messages become Markdown an agent can paste: heading, provenance line, body.

const COPY_ACTOR_LABELS = {"gpt-master":"GPT Master","claude-master":"Claude Master",lead:"Lead",operator:"Operator"};

function copyActor(id) {
  const label = COPY_ACTOR_LABELS[id];
  return label ? `${label} (${id})` : String(id);
}

function copyCompareChronological(a, b) {
  return String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id));
}

function formatMessageMarkdown(message, body) {
  const head = `### [${message.kind}] ${String(message.summary ?? "").trim()}`;
  const meta = [
    `from ${copyActor(message.from)} to ${copyActor(message.to)}`,
    `workstream ${message.workstream}`,
    `priority ${message.priority}`,
    message.requires_ack ? (message.acked ? "acknowledged" : "acknowledgement pending") : null,
    `created ${message.created_at}`,
    message.reply_to ? `reply to ${message.reply_to}` : null,
    `id ${message.id}`,
  ].filter(Boolean).join(" · ");
  const text = String(body ?? "").replace(/\r\n?/g, "\n").trim();
  return `${head}\n_${meta}_\n\n${text || "_(empty body)_"}\n`;
}

function formatMessagesMarkdown(entries) {
  const ordered = [...entries].sort((a, b) => copyCompareChronological(a.message, b.message));
  const blocks = ordered.map(entry => formatMessageMarkdown(entry.message, entry.body).trimEnd());
  const preamble = `<!-- Agent Board: ${ordered.length} message${ordered.length === 1 ? "" : "s"}, oldest first -->`;
  return `${preamble}\n\n${blocks.join("\n\n---\n\n")}\n`;
}

function formatRoadmapItemMarkdown(item) {
  const derived = item.derived || {};
  const lines = [`### ${item.title}`, `_id ${item.id} · ${item.status} · owner ${copyActor(item.owner)} · progress ${item.progress_reported ? `${item.progress} %` : "not reported"} · r${item.revision} · updated ${item.updated_at}_`];
  if (derived.kind && derived.kind !== "UNKNOWN") lines.push(`- kind: ${derived.kind}`);
  if (derived.impact) lines.push(`- impact: ${derived.impact}`);
  if ((item.blockers || []).length) lines.push(`- blockers: ${item.blockers.map(blocker => blocker.text).join("; ")}`);
  if ((derived.depends_on || []).length) lines.push(`- depends on: ${derived.depends_on.map(dep => `${dep.id} (${dep.resolved ? "resolved" : dep.known ? "unresolved" : "unknown"})`).join(", ")}`);
  if ((derived.dependents || []).length) lines.push(`- unblocks: ${derived.dependents.join(", ")}`);
  if ((derived.feeds || []).length) lines.push(`- feeds: ${derived.feeds.join(", ")}`);
  if (derived.standby) lines.push(`- standby: ${derived.standby.reason}`);
  if (derived.startable) lines.push("- ready to start: no blocker, no unresolved dependency");
  return `${lines.join("\n")}\n\n${String(item.summary ?? "").trim()}\n`;
}
