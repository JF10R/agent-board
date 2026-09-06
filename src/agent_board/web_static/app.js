"use strict";

// Agent Board — web client v2 (2026-09-05). No build step, no external code (CSP default-src 'self').
// Loads after markdown.js (rendering), copy.js (clipboard formats) and selection.js (multi-select). Sections: utilities · state/refresh ·
// rail · inbox (rows, selection, multi-copy, detail, thread) · roadmap (overview / tree / kanban / progression /
// detail / editor) · presence · compose (test-extracted block) · input.

const token = document.querySelector('meta[name="agent-board-token"]').content;
const $ = id => document.getElementById(id);
const emptyTree = {items:[],roots:[],moved:[],counts:{},warnings:[],views:null};
const emptyState = {messages:[],messages_meta:{total:0,returned:0,has_more:false,malformed:0,oversized:0},status:[],roadmap:[],roadmap_tree:emptyTree,ack_backlog:{},tickets:[],leases:[],actors:[],choices:{identities:[],message_senders:[],message_recipients:[],message_actors:[],kinds:[],priorities:[],roadmap_statuses:[],roadmap_owners:[],roadmap_kinds:[],ticket_stages:[],ticket_kinds:[],ticket_dep_types:[],ticket_review_verdicts:[]}};
const actorLabels = {"sol-master":"Astra Master","claude-master":"Claude Master",lead:"Lead",operator:"Operator (you)",BOTH:"Both masters",shared:"Shared",unassigned:"Unassigned"};
const absoluteET = new Intl.DateTimeFormat("en-CA", {timeZone:"America/Toronto", month:"short", day:"numeric", year:"numeric", hour:"numeric", minute:"2-digit", second:"2-digit", timeZoneName:"short"});
const shortET = new Intl.DateTimeFormat("en-CA", {timeZone:"America/Toronto", hour:"numeric", minute:"2-digit", timeZoneName:"short"});
const compactET = new Intl.DateTimeFormat("en-CA", {timeZone:"America/Toronto", month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
const dayET = new Intl.DateTimeFormat("en-CA", {timeZone:"America/Toronto", month:"short", day:"numeric"});
const relativeFormatter = new Intl.RelativeTimeFormat("en", {numeric:"auto"});
const STORAGE = {theme:"agent-board.theme.v1", view:"agent-board.view.v1", expanded:"agent-board.tree.expanded.v1", rmode:"agent-board.roadmap.mode.v2", lmode:"agent-board.messages.mode.v1", standby:"agent-board.standby-hours.v1", unacked:"agent-board.unacked-first.v1", project:"agent-board.project.v1"};
const MAX_MULTI_COPY = 60;

let state = emptyState;
let selectedMessage = null;
let selectedMessageValue = null;
let selectedThread = null;
let selectedRoadmap = null;
let roadmapDetailSignature = "";
let roadmapEditorOpener = null;
let initialized = false;
let refreshInFlight = false;
let refreshGeneration = 0;
let refreshAbort = null;
let refreshFailures = 0;
let lastUpdated = null;
let knownMessageIds = new Set();
const pendingMessageIds = new Set();
let deferredMessagePage = null;
let currentView = "messages";
let listMode = store(STORAGE.lmode) || "threads";
let roadmapMode = store(STORAGE.rmode) || "overview";
let collapsedParents = new Set(JSON.parse(store(STORAGE.expanded) || "[]"));
let selectedIds = new Set();          // the ONLY source of truth for the inbox multi-select
let selectionAnchor = null;           // last toggled id, for Shift-range
let dataVersion = null;
let uiVersion = null;
let uiOutdated = false;
let versionTimer = null;
const POLL_SECONDS = Math.max(1, Number(new URLSearchParams(location.search).get("poll")) || 10);
const ALL_PROJECTS = "__all";
let projects = [];                    // [{name, board_root}] served by this instance
let activeProject = null;             // a project name, or ALL_PROJECTS (inbox only)

function store(key, value) {
  try { if (value === undefined) return window.localStorage.getItem(key); window.localStorage.setItem(key, value); return value; }
  catch { return null; }
}

// ---------- projects ----------

// One store per repo. ALL_PROJECTS merges the inboxes only: roadmap, presence and derive stay per project.
function activeProjectName() { return activeProject === ALL_PROJECTS ? (projects[0]?.name || "") : (activeProject || ""); }
function multiProject() { return projects.length > 1; }
function messageProject(id) { return (state.messages.find(item => item.id === id) || {}).project || activeProjectName(); }
function withProject(path, project) { return `${path}${path.includes("?") ? "&" : "?"}project=${encodeURIComponent(project || activeProjectName())}`; }

async function loadProjects() {
  try {
    const value = await api("/api/projects");
    projects = value.projects || [];
    const stored = store(STORAGE.project);
    const known = name => name === ALL_PROJECTS ? multiProject() : projects.some(item => item.name === name);
    activeProject = known(params.get("project")) ? params.get("project") : known(stored) ? stored : (value.default || projects[0]?.name || "");
  } catch (error) { projects = []; activeProject = ""; }
  renderProjectSwitcher();
}

function renderProjectSwitcher() {
  const wrap = $("project-switch");
  wrap.classList.toggle("hidden", !multiProject());
  const select = $("project-select");
  const options = [...projects.map(item => [item.name, item.name]), ...(multiProject() ? [[ALL_PROJECTS, "All projects"]] : [])];
  select.innerHTML = options.map(([value, label]) => `<option value="${escapeText(value)}">${escapeText(label)}</option>`).join("");
  select.value = activeProject;
  syncOptions($("compose-project"), projects.map(item => item.name));
  $("compose-project-field").classList.toggle("hidden", !multiProject());
}

function setProject(name, syncTicketRoute = true) {
  activeProject = name;
  store(STORAGE.project, name);
  refreshGeneration += 1;
  if (refreshAbort) refreshAbort.abort();   // the previous project's state is no longer wanted
  refreshInFlight = false;
  selectedIds = selectionClear(); selectionAnchor = null; selectedMessage = null; selectedMessageValue = null;
  dataVersion = null;
  selectedTicket = null; ticketDetailSignature = "";
  if (syncTicketRoute && currentView === "tickets") history.replaceState(null, "", ticketUrl(null));
  return refresh();
}

// ---------- utilities ----------

function relativeTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown time";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const units = [[60,"second"],[60,"minute"],[24,"hour"],[7,"day"],[4.345,"week"],[12,"month"],[Infinity,"year"]];
  let amount = seconds;
  for (const [limit, unit] of units) {
    if (Math.abs(amount) < limit) return relativeFormatter.format(Math.round(amount), unit);
    amount /= limit;
  }
  return relativeFormatter.format(Math.round(amount), "year");
}

function ageLabel(hours) {
  if (hours === null || hours === undefined) return "age unknown";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${hours.toFixed(hours < 10 ? 1 : 0)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

function applyTimestamp(node, value, compact=false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) { node.textContent = "Unknown time"; node.removeAttribute("datetime"); node.removeAttribute("title"); return; }
  node.dateTime = date.toISOString();
  node.title = absoluteET.format(date);
  node.textContent = compact ? `${relativeTime(value)} · ${compactET.format(date)}` : `${relativeTime(value)} · ${absoluteET.format(date)}`;
  node.setAttribute("aria-label", `${relativeTime(value)}; ${absoluteET.format(date)}`);
  node.dataset.timestamp = value;
  if (compact) node.dataset.compact = "true";
}

function timeMarkup(value, compact=true) { return `<time data-timestamp="${escapeText(value)}"${compact ? ' data-compact="true"' : ""}></time>`; }

function updateVisibleTimes() {
  document.querySelectorAll("time[data-timestamp]").forEach(node => applyTimestamp(node, node.dataset.timestamp, node.dataset.compact === "true"));
  if (lastUpdated) $("last-updated").textContent = `Refreshed ${shortET.format(lastUpdated)}`;
}

function toneForKind(kind) { return ({QUESTION:"blue",ANSWER:"violet",BLOCKER:"red",ALERT:"red",DECISION:"green",HANDOFF:"amber",PROPOSAL:"violet",STATUS:"quiet"})[kind] || "quiet"; }
function toneForPriority(priority) { return ({CRITICAL:"red",HIGH:"amber",NORMAL:"quiet",LOW:"quiet"})[priority] || "quiet"; }
function statusTone(status) { return status==="COMPLETE"||status==="CLOSED"?"green":status==="BLOCKED"?"red":status==="IN_PROGRESS"?"blue":status==="READY"?"violet":"quiet"; }
function statusLabel(status) { return String(status || "").replaceAll("_", " "); }
function pillMarkup(status) { return `<span class="pill ${statusTone(status)}">${escapeText(statusLabel(status))}</span>`; }
function tagMarkup(text, tone="", title="") { return `<span class="tag ${tone}"${title ? ` title="${escapeText(title)}"` : ""}>${escapeText(text)}</span>`; }
function priorityMarkup(priority) { return priority === "CRITICAL" ? tagMarkup("CRITICAL", "solid-red") : priority === "HIGH" ? tagMarkup("HIGH", "amber") : priority === "LOW" ? tagMarkup("LOW") : ""; }
function ackMarkup(item) { return item.requires_ack ? (item.acked ? tagMarkup("ACKED", "green") : tagMarkup("ACK PENDING", "solid")) : ""; }
// Ratified naming: the human label first, the raw id verbatim beside it.
function actorMarkup(id, {raw=true}={}) { const label = actorLabels[id]; return label ? `<span class="actor">${escapeText(label)}${raw ? `<span class="raw">${escapeText(id)}</span>` : ""}</span>` : `<span class="actor mono">${escapeText(id)}</span>`; }
function actorText(id) { return actorLabels[id] ? `${actorLabels[id]} (${id})` : String(id); }

// Progress rail: 10 segments; dashed hollow when the item never reported progress (absence is not 0).
function railMarkup(item, tone="") {
  const reported = item.progress_reported;
  const closed = item.status === "CLOSED" || item.status === "COMPLETE";
  const filled = reported ? Math.round(item.progress / 10) : 0;
  const cls = `${reported ? "" : "unreported"} ${closed ? "closed" : ""} ${item.status === "BLOCKED" ? "blocked" : ""} ${tone}`.trim();
  const segments = Array.from({length: 10}, (_v, i) => `<i class="${i < filled ? "on" : ""}"></i>`).join("");
  const label = reported ? `${item.progress} %` : "not reported";
  return `<span class="progress" role="img" aria-label="Progress: ${escapeText(label)}"><span class="rail-bar ${cls}" aria-hidden="true">${segments}</span><span class="rail-value ${reported ? "" : "unreported"}">${reported ? `${item.progress}<span class="quiet"> %</span>` : "—"}</span></span>`;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.remove("visible"), 2200);
}

async function copy(text, message) {
  try { await navigator.clipboard.writeText(text); toast(message); return true; }
  catch {
    // Guarded fallback only: navigator.clipboard is the primary path; execCommand("copy") is deprecated
    // but still the one route that works when the async API is denied (permissions, non-secure context).
    const area = document.createElement("textarea"); area.value = text; area.setAttribute("readonly", ""); area.className = "sr-only"; document.body.append(area); area.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch { ok = false; } area.remove();
    toast(ok ? message : "Clipboard unavailable"); return ok;
  }
}

async function api(path, init={}) {
  const isRead = !init.method || init.method === "GET";
  const controller = isRead ? new AbortController() : null;
  const timeout = controller ? setTimeout(() => controller.abort(), 8000) : null;
  const outer = init.signal || null;   // the caller aborts this one when its result is no longer wanted
  const relay = () => controller && controller.abort();
  if (outer) outer.addEventListener("abort", relay, {once: true});
  try {
    const response = await fetch(path, {...init, ...(controller?{signal:controller.signal}:{}), headers:{...(init.body?{"Content-Type":"application/json","X-Agent-Board-Token":token}:{}), ...(init.headers||{})}});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  } catch (error) {
    if (error.name === "AbortError") throw outer && outer.aborted ? staleError() : new Error("Request timed out");
    throw error;
  } finally { if(timeout)clearTimeout(timeout); if(outer)outer.removeEventListener("abort", relay); }
}

// A superseded request is not a failure: it must not toast, log, or count towards the offline backoff.
function staleError() { return Object.assign(new Error("superseded request"), {stale: true}); }

function setConnection(mode) {
  const node = $("connection");
  if (node.dataset.mode === mode) return;
  node.dataset.mode = mode;
  node.className = `connection ${mode.toLowerCase()}`;
  node.querySelector(".connection-label").textContent = mode;
}

function syncOptions(select, values, labels={}) {
  const selected = select.value;
  const fixed = [...select.options].filter(option => option.dataset.fixed === "true" || option.value === "");
  const existing = new Map([...select.options].filter(option => option.value && option.dataset.fixed !== "true").map(option => [option.value, option]));
  values.forEach(value => {
    let option = existing.get(value);
    if (!option) { option = document.createElement("option"); option.value = value; select.append(option); }
    option.textContent = labels[value] || value;
    existing.delete(value);
  });
  existing.forEach(option => option.remove());
  fixed.forEach((option, index) => { if (select.children[index] !== option) select.insertBefore(option, select.children[index] || null); });
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}

function populateChoices() {
  syncOptions($("filter-actor"),state.choices.message_actors,actorLabels);
  syncOptions($("compose-actor"),state.choices.message_senders,actorLabels);
  const recipients = state.choices.message_recipients || state.choices.message_actors || [];
  syncOptions($("compose-to"),["BOTH",...recipients],actorLabels);
  syncOptions($("filter-workstream"), [...new Set(state.messages.map(item => item.workstream))].sort());
  [["roadmap-actor",state.choices.identities],["filter-kind",state.choices.kinds],["compose-kind",state.choices.kinds],["filter-priority",state.choices.priorities],["compose-priority",state.choices.priorities],["roadmap-status",state.choices.roadmap_statuses],["filter-rstatus",state.choices.roadmap_statuses],["roadmap-kind",state.choices.roadmap_kinds||[]],["filter-rkind",state.choices.roadmap_kinds||[]]].forEach(([id, values]) => syncOptions($(id), values));
  syncOptions($("roadmap-owner"), state.choices.roadmap_owners, actorLabels);
  syncOptions($("filter-rowner"), state.choices.roadmap_owners, actorLabels);
  syncOptions($("ticket-actor"), state.choices.identities, actorLabels);
  syncOptions($("ticket-kind"), state.choices.ticket_kinds || []);
  if (!$("compose-priority").value) $("compose-priority").value = "NORMAL";
}

function reconcileKeyed(container, items, createNode, updateNode) {
  const existing = new Map([...container.children].filter(node => node.dataset.id).map(node => [node.dataset.id, node]));
  let cursor = container.firstElementChild;
  for (const item of items) {
    const id = String(item.id ?? item.identity ?? item.key);
    let node = existing.get(id);
    if (!node) { node = createNode(item); node.dataset.id = id; }
    updateNode(node, item);
    if (node !== cursor) container.insertBefore(node, cursor);
    cursor = node.nextElementSibling;
    existing.delete(id);
  }
  existing.forEach(node => node.remove());
}

// ---------- views, theme, rail ----------

const VIEWS = ["messages","roadmap","tickets","presence"];
function setView(name, {focus=false}={}) {
  if (!VIEWS.includes(name)) name = "messages";
  currentView = name;
  store(STORAGE.view, name);
  for (const view of VIEWS) {
    $(`view-${view}`).hidden = view !== name;
    $(`nav-${view}`).setAttribute("aria-selected", String(view === name));
  }
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (focus) $(`nav-${name}`).focus();
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme-toggle").textContent = theme === "dark" ? "Light theme" : "Dark theme";
  $("theme-toggle").setAttribute("aria-pressed", String(theme === "light"));
  store(STORAGE.theme, theme);
}

function initTheme() {
  const stored = store(STORAGE.theme);
  applyTheme(stored === "light" || stored === "dark" ? stored : (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
}

function renderBacklog() {
  const backlog = state.ack_backlog || {};
  const actors = Object.keys(backlog).sort((a, b) => (backlog[b].count - backlog[a].count) || a.localeCompare(b));
  const container = $("ack-backlog");
  const activeActor = $("filter-actor").value, activeAck = $("filter-ack").value;
  reconcileKeyed(container, actors.map(actor => ({id: actor, ...backlog[actor]})), () => {
    const node = document.createElement("button"); node.type = "button"; node.className = "rail-row";
    node.innerHTML = '<span><span class="name"></span><span class="since"></span></span><span class="n"></span>';
    node.addEventListener("click", () => {
      const already = $("filter-actor").value === node.dataset.id && $("filter-ack").value === "pending";
      $("filter-actor").value = already ? "" : node.dataset.id; $("filter-ack").value = already ? "" : "pending";
      setView("messages"); renderMessages(); renderBacklog();
    });
    return node;
  }, (node, entry) => {
    node.classList.toggle("hot", entry.count > 0);
    node.classList.toggle("active", activeActor === entry.id && activeAck === "pending");
    node.querySelector(".name").textContent = actorLabels[entry.id] || entry.id;
    node.querySelector(".since").textContent = entry.count ? `oldest ${relativeTime(entry.oldest_created_at)}` : "nothing pending";
    node.querySelector(".n").textContent = entry.count;
    node.setAttribute("aria-label", `${actorLabels[entry.id] || entry.id}: ${entry.count} message${entry.count === 1 ? "" : "s"} awaiting acknowledgement`);
    node.setAttribute("aria-pressed", String(activeActor === entry.id && activeAck === "pending"));
  });
  const total = actors.reduce((sum, actor) => sum + backlog[actor].count, 0);
  const navCount = $("nav-messages-count");
  navCount.textContent = total ? `${total} to ack` : String(state.messages_meta?.total ?? state.messages.length);
  navCount.classList.toggle("hot", total > 0);
  navCount.title = total ? `${total} messages awaiting acknowledgement across all actors` : "Messages on the board";
}

function renderSignals() {
  const views = treePayload().views;
  const counts = views ? {startable: views.startable.length, standby: views.standby.length, blocked: views.blocked.length} : {startable: "—", standby: "—", blocked: "—"};
  for (const key of ["startable","standby","blocked"]) $(`signal-${key}`).querySelector(".n").textContent = counts[key];
  $("signal-standby-note").textContent = `in progress, silent ${standbyHours()} h+ or flagged`;
}

// ---------- inbox ----------

function needsAck(item) { return item.requires_ack && !item.acked; }

function messageMatches(item) {
  const actor=$("filter-actor").value, workstream=$("filter-workstream").value, kind=$("filter-kind").value, priority=$("filter-priority").value, ack=$("filter-ack").value;
  const query = $("search").value.trim().toLowerCase();
  if (pendingMessageIds.has(item.id)) return false;
  if (actor && item.from !== actor && item.to !== actor) return false;
  if (workstream && item.workstream !== workstream) return false;
  if (kind && item.kind !== kind) return false;
  if (priority && item.priority !== priority) return false;
  if (ack === "acked" && !item.acked) return false;
  if (ack === "pending" && !needsAck(item)) return false;
  if (ack === "required" && !item.requires_ack) return false;
  if (query) {
    const haystack = `${item.summary} ${item.id} ${item.workstream} ${item.from} ${item.to} ${item.kind} ${actorLabels[item.from] || ""} ${actorLabels[item.to] || ""}`.toLowerCase();
    if (!query.split(/\s+/).every(part => haystack.includes(part))) return false;
  }
  return true;
}

function threadRootOf(item, byId) {
  let cursor = item, guard = 0;
  while (cursor.reply_to && byId.has(cursor.reply_to) && guard < 200) { cursor = byId.get(cursor.reply_to); guard += 1; }
  return cursor.id;
}

function threadedRows(matching, byId) {
  const groups = new Map();
  for (const item of matching) {
    const root = threadRootOf(item, byId);
    if (!groups.has(root)) groups.set(root, {root, newest: item.created_at, members: []});
    const group = groups.get(root);
    group.members.push(item);
    if (item.created_at > group.newest) group.newest = item.created_at;
  }
  const rows = [];
  for (const group of [...groups.values()].sort((a, b) => b.newest.localeCompare(a.newest))) {
    const members = group.members.sort((a, b) => a.created_at.localeCompare(b.created_at));
    const rootIndex = members.findIndex(item => item.id === group.root);
    const head = rootIndex >= 0 ? members.splice(rootIndex, 1)[0] : members.shift();
    rows.push({id: head.id, item: head, reply: false, threadSize: members.length + 1, continues: head.id !== group.root || Boolean(head.reply_to && !byId.has(head.reply_to))});
    members.forEach(item => rows.push({id: item.id, item, reply: true, threadSize: 0}));
  }
  return rows;
}

// Unacked first: pending acknowledgements form their own group at the top, newest first; the rest keeps the chosen mode.
function visibleMessageRows() {
  const matching = state.messages.filter(messageMatches);
  const byId = new Map(state.messages.map(item => [item.id, item]));
  const unackedFirst = $("unacked-first").checked;
  const pending = unackedFirst ? matching.filter(needsAck).sort((a, b) => b.created_at.localeCompare(a.created_at)) : [];
  const rest = unackedFirst ? matching.filter(item => !needsAck(item)) : matching;
  const restRows = listMode === "flat" ? rest.map(item => ({id: item.id, item, reply: false, threadSize: 0})) : threadedRows(rest, byId);
  if (!pending.length) return restRows;
  return [{id: "__label:pending", label: `Needs acknowledgement · ${pending.length}`, hot: true}, ...pending.map(item => ({id: item.id, item, reply: false, threadSize: 0})), {id: "__label:rest", label: `Everything else · ${restRows.length}`}, ...restRows];
}

// Visible row ids come from the state-derived rows, never from the DOM: the DOM can lag a render, the state cannot.
function messageRowIds() { return visibleMessageRows().filter(row => !row.label).map(row => row.id); }

function toggleChecked(id, {range=false}={}) {
  const next = selectionToggle(selectedIds, id, {range, anchor: selectionAnchor, orderedIds: messageRowIds()});
  selectedIds = next.selected; selectionAnchor = next.anchor;
  renderMessages();
}

function clearSelection() { if (!selectedIds.size) return false; selectedIds = selectionClear(); selectionAnchor = null; renderMessages(); return true; }

// Rule: a filter/search/mode change drops ids whose row is no longer visible; a data refresh keeps every id still on the board.
function pruneSelectionToVisible() {
  const before = selectedIds.size;
  selectedIds = selectionPrune(selectedIds, messageRowIds());
  if (selectedIds.size !== before && !selectedIds.has(selectionAnchor)) selectionAnchor = null;
}

function renderSelectionBar() {
  const n = selectedIds.size;
  $("selection-bar").classList.toggle("hidden", n === 0);
  $("selection-count").textContent = selectionCountLabel(selectedIds);
  $("copy-selected").textContent = n === 1 ? "Copy 1 body" : `Copy ${n} bodies`;
}

function createMessageRow() {
  const node = document.createElement("div");
  node.className = "message-row"; node.setAttribute("role", "option");
  node.innerHTML = '<label class="row-check"><input type="checkbox" aria-label="Select message"></label><button type="button" class="row-main"><span class="row-title"><span class="message-summary"></span></span><span class="message-route"></span><span class="message-badges"></span></button><span class="row-side"><time data-compact="true"></time><button type="button" class="row-copy" title="Copy title, provenance and body as Markdown (y)">Copy body</button></span>';
  return node;  // every click is handled by the one delegated listener on #message-list (no rebinding, no duplicates)
}

function createLabelRow() { const node = document.createElement("div"); node.className = "list-group-label"; node.setAttribute("role", "presentation"); return node; }

function updateMessageRow(node, row) {
  if (row.label) { node.textContent = row.label; node.classList.toggle("hot", Boolean(row.hot)); return; }
  const item = row.item;
  const pending = needsAck(item);
  node.classList.toggle("selected", selectedMessage === item.id);
  node.classList.toggle("reply", row.reply);
  node.classList.toggle("needs-me", pending);
  node.classList.toggle("checked", selectedIds.has(item.id));
  node.querySelector(".row-check input").checked = selectedIds.has(item.id);
  node.setAttribute("aria-selected", String(selectedMessage === item.id));
  node.querySelector(".row-main").setAttribute("aria-label", `${item.kind} from ${actorText(item.from)} to ${actorText(item.to)}: ${item.summary}${pending ? "; acknowledgement pending" : ""}`);
  node.querySelector(".message-summary").innerHTML = renderInlineMarkdown(item.summary, {links:false});
  applyTimestamp(node.querySelector("time"), item.created_at, true);
  node.querySelector(".message-route").innerHTML = `${actorMarkup(item.from, {raw:false})}<span class="arrow" aria-hidden="true">→</span>${actorMarkup(item.to, {raw:false})}<span class="quiet">·</span><span class="mono">${escapeText(item.workstream)}</span>${row.threadSize > 1 ? `<span class="thread-count">${row.threadSize} in thread${row.continues ? " · older part not loaded" : ""}</span>` : row.continues ? '<span class="thread-count">continues an older thread</span>' : ""}`;
  node.querySelector(".message-badges").innerHTML = `${multiProject() && item.project ? tagMarkup(item.project, "solid") : ""}${tagMarkup(item.kind, toneForKind(item.kind))}${priorityMarkup(item.priority)}${ackMarkup(item)}`;
}

function renderMessages() {
  renderSelectionBar();  // derived first: the banner can never lag the rows, whatever a row render does
  const list = $("message-list");
  const scrollTop = list.scrollTop;
  const rows = visibleMessageRows();
  const shown = rows.filter(row => !row.label).length;
  const meta = state.messages_meta || {total:state.messages.length,returned:state.messages.length,has_more:false};
  const pageLabel = meta.has_more ? `latest ${meta.returned} of ${meta.total}` : String(meta.returned);
  $("message-count").textContent = shown === state.messages.length ? pageLabel : `${shown} shown · ${pageLabel}`;
  $("message-count").title = meta.has_more ? `The board holds ${meta.total} messages; the latest ${meta.returned} are loaded` : `All ${meta.total} messages are loaded`;
  reconcileKeyed($("message-list"), rows, row => row.label ? createLabelRow() : createMessageRow(), updateMessageRow);
  let empty = $("message-list").querySelector(".empty-list");
  if (!shown && !empty) { empty=document.createElement("div"); empty.className="empty-list"; empty.innerHTML="<strong>No messages match</strong><span>Clear a filter, or compose the first message of this workstream.</span>"; $("message-list").append(empty); }
  if (shown && empty) empty.remove();
  $("mode-threads").setAttribute("aria-pressed", String(listMode === "threads"));
  $("mode-flat").setAttribute("aria-pressed", String(listMode === "flat"));
  if (list.scrollTop !== scrollTop) list.scrollTop = scrollTop;
  updateNewMessagePill();
}

function updateNewMessagePill() {
  const pill = $("new-message-pill");
  pill.classList.toggle("hidden", pendingMessageIds.size === 0);
  const next = `${pendingMessageIds.size} new message${pendingMessageIds.size === 1 ? "" : "s"} · show`;
  if (pill.textContent !== next) pill.textContent = next;
}

async function loadMessage(id) {
  const value = await api(withProject(`/api/messages/${encodeURIComponent(id)}`, messageProject(id)));
  return {message: {...value.message, acked: value.acked}, body: value.body, raw: value.raw};
}

async function copyMessageBody(id) {
  try {
    const cached = selectedMessageValue && selectedMessageValue.message.id === id ? {message: {...selectedMessageValue.message, acked: selectedMessageValue.acked}, body: selectedMessageValue.body} : await loadMessage(id);
    await copy(formatMessageMarkdown(cached.message, cached.body), "Message copied as Markdown");
  } catch (error) { toast(error.message); }
}

async function copySelectedBodies() {
  const ids = selectionCopyOrder(selectedIds, state.messages);
  if (!ids.length) return;
  if (ids.length > MAX_MULTI_COPY) { toast(`Select at most ${MAX_MULTI_COPY} messages at once`); return; }
  $("copy-selected").disabled = true;
  try {
    const entries = await Promise.all(ids.map(loadMessage));
    await copy(formatMessagesMarkdown(entries), `${entries.length} message${entries.length === 1 ? "" : "s"} copied, oldest first`);
  } catch (error) { toast(error.message); }
  finally { $("copy-selected").disabled = false; }
}

function messageDetailMarkup(value) {
  const item = value.message;
  return `<div class="detail-head"><div class="badges" id="detail-badges">${tagMarkup(item.kind, toneForKind(item.kind))}${tagMarkup(item.priority, toneForPriority(item.priority))}${ackMarkup({...item, acked: value.acked})}</div><h2>${renderInlineMarkdown(item.summary)}</h2><div class="detail-meta"><span>${actorMarkup(item.from)} → ${actorMarkup(item.to)}</span><span class="mono">${escapeText(item.workstream)}</span>${timeMarkup(item.created_at, false)}<span class="mono quiet">${escapeText(item.id)}</span></div></div>
  <div class="detail-actions"><button id="copy-body" type="button" class="primary" title="Title, provenance and body as Markdown an agent can paste (y)">Copy body</button><button id="reply" type="button" class="secondary">Reply</button>${item.requires_ack ? '<button id="ack-message" type="button" class="danger-button"></button>' : ""}<button id="copy-id" type="button" class="ghost">Copy id</button>${item.ticket_id ? '<button id="message-open-ticket" type="button" class="secondary">Open ticket</button>' : ""}</div>
  <div class="message-body markdown-body">${renderMarkdown(value.body) || '<span class="muted">Empty body</span>'}</div>
  <div id="thread" class="thread"><h3 class="label">Thread</h3><div id="thread-items"><p class="quiet">Loading thread…</p></div></div>
  <details class="raw-details"><summary>Raw message</summary><div class="detail-actions"><button id="copy-whole" type="button" class="ghost">Copy raw</button></div><pre>${escapeText(value.raw)}</pre></details>`;
}

function renderThread(thread) {
  const container = $("thread-items");
  if (!container) return;
  if (!thread || !thread.messages) { container.innerHTML = '<p class="quiet">Thread unavailable.</p>'; return; }
  if (thread.messages.length <= 1) { container.innerHTML = '<p class="quiet">No replies yet. Reply to start the thread.</p>'; return; }
  container.innerHTML = thread.messages.map(item => `<div class="thread-item ${item.id === selectedMessage ? "current" : ""}"><div class="thread-item-head"><button type="button" data-message-id="${escapeText(item.id)}">${escapeText(actorLabels[item.from] || item.from)}</button><span aria-hidden="true">→</span><span>${escapeText(actorLabels[item.to] || item.to)}</span>${tagMarkup(item.kind, toneForKind(item.kind))}${ackMarkup(item)}${timeMarkup(item.created_at)}<button type="button" class="row-copy" data-copy-id="${escapeText(item.id)}">Copy body</button></div><div class="message-summary">${renderInlineMarkdown(item.summary, {links:false})}</div>${item.body ? `<div class="markdown-body">${renderMarkdown(item.body)}</div>` : ""}</div>`).join("") + (thread.truncated ? '<p class="quiet">Thread truncated to 50 messages.</p>' : "");
  container.querySelectorAll("button[data-message-id]").forEach(button => button.addEventListener("click", () => showMessage(button.dataset.messageId)));
  container.querySelectorAll("button[data-copy-id]").forEach(button => button.addEventListener("click", () => copyMessageBody(button.dataset.copyId)));
  updateVisibleTimes();
}

function reconcileMessageDetail(value) {
  if (!value || selectedMessage !== value.message.id || !$("detail-badges")) return;
  const current = state.messages.find(item => item.id === value.message.id) || value.message;
  value.message = {...value.message, ...current}; value.acked = Boolean(current.acked ?? value.acked);
  $("detail-badges").innerHTML = `${tagMarkup(current.kind, toneForKind(current.kind))}${tagMarkup(current.priority, toneForPriority(current.priority))}${ackMarkup({...current, acked: value.acked})}`;
  const ack = $("ack-message");
  if (ack) { ack.disabled = value.acked; ack.textContent = value.acked ? "Acknowledged" : `Acknowledge as ${actorLabels[current.to] || current.to}`; ack.className = value.acked ? "secondary" : "danger-button"; }
}

function revealOnMobile(node){if(matchMedia("(max-width: 960px)").matches){node.scrollIntoView({block:"start",behavior:"auto"});node.focus({preventScroll:true});}}

async function acknowledgeSelected() {
  const value = selectedMessageValue;
  if (!value || value.acked || !value.message.requires_ack) return;
  try {
    await api("/api/acks", {method:"POST", body:JSON.stringify({project:messageProject(value.message.id), actor:value.message.to, message_id:value.message.id})});
    toast("Acknowledged");
    await refresh();
    reconcileMessageDetail(value);
  } catch (error) { toast(error.message); }
}

async function showMessage(id) {
  selectedMessage = id; selectedMessageValue = null; selectedThread = null; renderMessages();
  const detail = $("message-detail"); detail.className = "detail-panel"; detail.innerHTML = '<p class="quiet">Loading message…</p>';
  try {
    const [value, thread] = await Promise.all([api(withProject(`/api/messages/${encodeURIComponent(id)}`, messageProject(id))), api(withProject(`/api/messages/${encodeURIComponent(id)}/thread`, messageProject(id))).catch(() => null)]);
    if (selectedMessage !== id) return;
    selectedMessageValue = value; selectedThread = thread;
    detail.innerHTML = messageDetailMarkup(value); reconcileMessageDetail(value); renderThread(thread); updateVisibleTimes();
    $("copy-body").onclick = () => copyMessageBody(value.message.id);
    $("copy-id").onclick = () => copy(value.message.id, "Message id copied");
    $("copy-whole").onclick = () => copy(value.raw, "Raw message copied");
    $("reply").onclick = () => openCompose(value.message);
    if ($("message-open-ticket")) $("message-open-ticket").onclick = async () => { const project = messageProject(value.message.id); if (project !== activeProjectName()) await setProject(project, false); setView("tickets"); selectTicket(value.message.ticket_id); };
    if ($("ack-message")) $("ack-message").onclick = acknowledgeSelected;
    revealOnMobile(detail);
  } catch (error) { if (selectedMessage !== id) return; detail.innerHTML = `<p class="error">${escapeText(error.message)}</p>`; }
}

// ---------- roadmap ----------

function treePayload() { return state.roadmap_tree && Array.isArray(state.roadmap_tree.items) ? state.roadmap_tree : emptyTree; }
function treeItems() { return treePayload().items; }
function treeById() { return new Map(treeItems().map(item => [item.id, item])); }
function derived(item) { return item.derived || {kind:"UNKNOWN",impact:null,depends_on:[],unresolved_deps:[],dependents:[],feeds:[],age_hours:null,standby:null,blocked_by:null,startable:false,waiting:false,parallel:false,on_cycle:false,aggregate:null}; }
function isClosed(item) { return item.status === "CLOSED" || item.status === "COMPLETE"; }
function sinceHours() { return Number($("roadmap-since").value) || 24; }
function standbyHours() { return Number($("standby-hours").value) || 48; }
function sinceCutoff() { return Date.now() - sinceHours() * 3600 * 1000; }
function movedRecently(item) { const t = Date.parse(item.updated_at); return !Number.isNaN(t) && t >= sinceCutoff(); }

function roadmapMatches(item) {
  const status = $("filter-rstatus").value, owner = $("filter-rowner").value, kind = $("filter-rkind").value;
  if (status && item.status !== status) return false;
  if (owner && item.owner !== owner) return false;
  if (kind && derived(item).kind !== kind) return false;
  const query = $("roadmap-search").value.trim().toLowerCase();
  if (!query) return true;
  const haystack = `${item.id} ${item.title} ${item.owner} ${item.status} ${item.summary} ${derived(item).impact || ""}`.toLowerCase();
  return query.split(/\s+/).every(part => haystack.includes(part));
}

function visibleRoadmapItems() {
  const showClosed = $("roadmap-show-closed").checked;
  return treeItems().filter(item => roadmapMatches(item) && (showClosed || !isClosed(item) || item.children.length));
}

function changeMarkup(changes) {
  return changes.map(change => {
    if (change.field === "progress") return `<span class="chg">${escapeText(String(change.from))} → <b>${escapeText(String(change.to))} %</b></span>`;
    return `<span class="chg">${escapeText(statusLabel(change.from))} → <b>${escapeText(statusLabel(change.to))}</b></span>`;
  }).join("");
}

function lastDelta(item) {
  const history = item.history || [];
  if (history.length < 2) return null;
  const [previous, current] = [history[history.length - 2], history[history.length - 1]];
  return ["status","progress","owner"].filter(field => previous[field] !== current[field]).map(field => ({field, from: previous[field], to: current[field]}));
}

function renderMovedStrip() {
  const hours = sinceHours();
  $("moved-window").textContent = hours >= 48 ? `in the last ${Math.round(hours / 24)} d` : `in the last ${hours} h`;
  const moved = treeItems().filter(movedRecently).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  $("moved-count").textContent = moved.length;
  $("moved-note").textContent = moved.length ? "Diffs come from the board's revision journal; a first-seen revision has no known prior." : "";
  const list = $("moved-list");
  if (!moved.length) { list.innerHTML = '<p class="moved-empty">Nothing changed in this window. Widen it, or wait for the next roadmap update.</p>'; return; }
  reconcileKeyed(list, moved, () => {
    const node = document.createElement("button"); node.type = "button"; node.className = "moved-chip";
    node.innerHTML = '<strong></strong><span class="delta"></span><time data-compact="true"></time>';
    node.addEventListener("click", () => selectRoadmap(node.dataset.id));
    return node;
  }, (node, item) => {
    node.querySelector("strong").textContent = item.title;
    node.querySelector("strong").title = item.id;
    const delta = lastDelta(item);
    const change = item.last_change || {};
    let markup;
    if (item.revision === 1) markup = `<span class="chg">new · ${escapeText(statusLabel(item.status))}</span>`;
    else if (delta && delta.length) markup = changeMarkup(delta);
    else if (change.prior_known === false) markup = `<span class="chg">r${item.revision} · prior revision unknown</span>`;
    else markup = `<span class="chg">r${item.revision} · text-only edit</span>`;
    node.querySelector(".delta").innerHTML = `${pillMarkup(item.status)}${markup}`;
    applyTimestamp(node.querySelector("time"), item.updated_at, true);
    node.setAttribute("aria-label", `${item.title}: ${node.querySelector(".delta").textContent}, ${relativeTime(item.updated_at)}`);
  });
}

// Derived signals, in reading order: what it is, whether it can move, what stops it.
function signalsMarkup(item) {
  const d = derived(item);
  const bits = [];
  if (d.kind === "MILESTONE" || d.kind === "OBJECTIVE") bits.push(tagMarkup(d.kind, "fill-violet"));
  if (d.startable) bits.push(tagMarkup("READY TO START", "fill-green", "NOT_STARTED, no blocker, no unresolved dependency"));
  if (d.standby) bits.push(tagMarkup(`STANDBY ${ageLabel(d.standby.age_hours)}`, "fill-amber", d.standby.reason));
  if (d.waiting) bits.push(tagMarkup(`WAITS ON ${d.unresolved_deps.length}`, "amber", d.unresolved_deps.join(", ")));
  if (item.blockers.length) bits.push(tagMarkup(`${item.blockers.length} blocker${item.blockers.length === 1 ? "" : "s"}`, "red", item.blockers.map(blocker => blocker.text).join(" · ")));
  if (d.on_cycle) bits.push(tagMarkup("DEP CYCLE", "red"));
  for (const gate of item.gates.slice(0, 2)) bits.push(tagMarkup(`${gate.name} ${gate.state}`, gate.state === "PASS" ? "green" : gate.state === "FAIL" ? "red" : "amber"));
  if (item.gates.length > 2) bits.push(tagMarkup(`+${item.gates.length - 2} gates`));
  if (item.due) bits.push(tagMarkup(`due ${item.due}`, Date.parse(item.due) < Date.now() && !isClosed(item) ? "red" : ""));
  return bits.join("");
}

function rollupText(item) {
  const rollup = item.rollup;
  if (!rollup) return "";
  const parts = Object.entries(rollup.by_status).map(([status, n]) => `${n} ${statusLabel(status).toLowerCase()}`);
  const mean = rollup.reported ? `mean ${rollup.mean_reported_progress} % over ${rollup.reported} reported` : "no child progress reported";
  return `${rollup.children} children · ${parts.join(" · ")} · ${mean}${rollup.blocked ? ` · ${rollup.blocked} with blockers` : ""}`;
}

function aggregateMarkup(item) {
  const aggregate = derived(item).aggregate;
  if (!aggregate) return "";
  const mean = aggregate.mean_reported_progress;
  // Headline: the milestone's own reported number wins; else the mean over OPEN members
  // (CLOSED = superseded/negative never counts as progress); the raw mean stays secondary.
  const headline = aggregate.self_reported_progress !== null && aggregate.self_reported_progress !== undefined && aggregate.self_reported_progress > 0
    ? {value: aggregate.self_reported_progress, label: "reported by owner"}
    : (aggregate.mean_open_progress !== null && aggregate.mean_open_progress !== undefined
      ? {value: aggregate.mean_open_progress, label: "mean over open members"}
      : {value: mean, label: mean === null ? "no member reported progress" : `mean over ${aggregate.reported} reported (incl. closed)`});
  const statusBits = Object.entries(aggregate.by_status).map(([status, n]) => `<span class="pill ${statusTone(status)}">${n} ${escapeText(statusLabel(status))}</span>`).join("");
  return `<div class="agg"><span class="big">${headline.value === null || headline.value === undefined ? "—" : `${headline.value} %`}</span><span class="agg-label">${escapeText(headline.label)}</span><span>${mean === null ? "no member reported progress" : `mean over ${aggregate.reported} reported of ${aggregate.count}`}</span><span>·</span><span><b>${aggregate.closed}</b> / ${aggregate.count} closed</span>${aggregate.blocked ? `<span class="error">${aggregate.blocked} blocked</span>` : ""}${aggregate.standby ? `<span class="muted">${aggregate.standby} in standby</span>` : ""}${aggregate.startable ? `<span>${aggregate.startable} ready to start</span>` : ""}</div><div class="badges">${statusBits}</div>`;
}

function itemChip(id, byId) {
  const item = byId.get(id);
  if (!item) return `<span class="chip quiet">${escapeText(id)}</span>`;
  return `<button type="button" class="chip ${selectedRoadmap === id ? "selected" : ""}" data-select="${escapeText(id)}" title="${escapeText(`${item.id} · ${statusLabel(item.status)} · ${item.progress_reported ? `${item.progress} %` : "progress not reported"}`)}">${escapeText(item.title)}</button>`;
}

function overviewRow(item, subMarkup, sideMarkup="") {
  return `<button type="button" class="ov-row ${selectedRoadmap === item.id ? "selected" : ""}" data-select="${escapeText(item.id)}"><strong title="${escapeText(item.title)}">${escapeText(item.title)}</strong><span class="side">${sideMarkup}</span><span class="sub"><span class="raw">${escapeText(item.id)}</span> · ${subMarkup}</span></button>`;
}

function renderOverview(container) {
  const payload = treePayload();
  const views = payload.views;
  const byId = treeById();
  const visible = new Set(visibleRoadmapItems().map(item => item.id));
  const pick = ids => ids.map(id => byId.get(id)).filter(item => item && (visible.has(item.id) || (isClosed(item) && roadmapMatches(item))));
  if (!views) { container.innerHTML = '<div class="panel ov-card wide-card"><p class="ov-empty">Derived views are unavailable while the roadmap tree cannot be read.</p></div>'; return; }
  const milestones = pick(views.milestones);
  const startable = pick(views.startable);
  const standby = pick(views.standby);
  const blocked = views.blocked.map(entry => ({...entry, item: byId.get(entry.id)})).filter(entry => entry.item && visible.has(entry.id));
  const waiting = pick(views.waiting);
  const owners = Object.entries(views.parallel.by_owner).map(([owner, ids]) => [owner, pick(ids)]).filter(([, items]) => items.length);
  const milestoneMarkup = milestones.length ? milestones.map(item => {
    const d = derived(item);
    return `<article class="milestone"><header><button type="button" data-select="${escapeText(item.id)}">${escapeText(item.title)}</button>${tagMarkup(d.kind, "fill-violet")}${pillMarkup(item.status)}<span class="raw">${escapeText(item.id)}</span><span class="raw">${escapeText(actorText(item.owner))}</span></header>${d.impact ? `<p class="impact"><b>Impact</b> · ${escapeText(d.impact)}</p>` : '<p class="impact quiet">No impact line recorded.</p>'}${aggregateMarkup(item)}${d.aggregate && d.aggregate.open.length ? `<div class="members"><span class="label">Open</span>${d.aggregate.open.map(id => itemChip(id, byId)).join("")}</div>` : ""}</article>`;
  }).join("") : `<div class="ov-hint">No item is declared as a MILESTONE or OBJECTIVE yet. Declare one with its members, then this card aggregates their own progress (nothing is invented):<code>python -B tools/agent_board.py roadmap annotate --actor claude-master --id TRAIN_A --kind MILESTONE --depends-on A2_G1_DVT_V2_INDEPENDENT_RESOLUTION --depends-on A2_G2_FER_V1 --impact "first Train A candidate resolved on untouched history"</code></div>`;
  const ownerMarkup = owners.length ? `<div class="owner-groups">${owners.map(([owner, items]) => `<section class="owner-group"><header><span>${actorMarkup(owner)}</span><span class="quiet">${items.length}</span></header><div class="chips">${items.map(item => itemChip(item.id, byId)).join("")}</div></section>`).join("")}</div>` : '<p class="ov-empty">Nothing is actionable without an open dependency or blocker.</p>';
  container.innerHTML = `
    <section class="panel ov-card wide-card" data-focus="milestones"><header><h2>Milestones &amp; objectives <span class="count">${milestones.length}</span></h2><p class="rule">Aggregate of members (children + dependencies): mean of reported progress, closed / total. Members that reported nothing are not counted.</p></header>${milestoneMarkup}</section>
    <section class="panel ov-card" data-focus="startable"><header><h2>Ready to start <span class="count">${startable.length}</span></h2><p class="rule">NOT_STARTED, no blocker, no unresolved dependency.</p></header><div class="ov-list">${startable.length ? startable.map(item => overviewRow(item, `${escapeText(actorText(item.owner))}${derived(item).feeds.length ? ` · feeds <b>${escapeText(derived(item).feeds.join(", "))}</b>` : ""}`)).join("") : '<p class="ov-empty">Nothing is ready to start.</p>'}${waiting.length ? `<p class="ov-empty">${waiting.length} not-started item${waiting.length === 1 ? " waits" : "s wait"} on an open dependency: ${waiting.map(item => `<button type="button" class="link-button" data-select="${escapeText(item.id)}">${escapeText(item.title)}</button>`).join(", ")}</p>` : ""}</div></section>
    <section class="panel ov-card" data-focus="standby"><header><h2>In standby <span class="count">${standby.length}</span></h2><p class="rule">IN_PROGRESS with no update for ${standbyHours()} h or more, or explicitly flagged.</p></header><div class="ov-list">${standby.length ? standby.map(item => { const s = derived(item).standby; return overviewRow(item, `${s.kind === "explicit" ? `<b>flagged</b> by ${escapeText(s.set_by || "?")} · ${escapeText(s.reason)}` : `<b>${escapeText(ageLabel(s.age_hours))}</b> without update`} · ${escapeText(actorText(item.owner))}`, `${item.progress_reported ? `${item.progress} %` : "—"}`); }).join("") : '<p class="ov-empty">Every in-progress item moved recently.</p>'}</div></section>
    <section class="panel ov-card" data-focus="blocked"><header><h2>Blocked <span class="count">${blocked.length}</span></h2><p class="rule">The blocker text is the fact; dependencies still open are listed beside it.</p></header><div class="ov-list">${blocked.length ? blocked.map(entry => overviewRow(entry.item, `${entry.blockers.map(text => `<b>${escapeText(text)}</b>`).join(" · ") || '<span class="quiet">no blocker text</span>'}${entry.waiting_on.length ? ` · waits on ${entry.waiting_on.map(id => `<button type="button" class="link-button" data-select="${escapeText(id)}">${escapeText(byId.get(id)?.title || id)}</button>`).join(", ")}` : ""} · ${escapeText(actorText(entry.item.owner))}`)).join("") : '<p class="ov-empty">Nothing is blocked.</p>'}</div></section>
    <section class="panel ov-card wide-card" data-focus="parallel"><header><h2>Can run in parallel now <span class="count">${views.parallel.items.length}</span></h2><p class="rule">Actionable (in progress, ready, or ready to start) with no blocker and no open dependency: none of these waits on another. Grouped by owner, since one owner is one queue.</p></header>${ownerMarkup}</section>`;
  container.querySelectorAll("[data-select]").forEach(button => button.addEventListener("click", () => selectRoadmap(button.dataset.select)));
}

function createTreeRow() {
  const node = document.createElement("button");
  node.type = "button"; node.className = "tree-row"; node.setAttribute("role", "option");
  node.innerHTML = '<span class="tree-caret" role="button" tabindex="-1"></span><span class="tree-name"><span class="tree-title"></span><span class="tree-id"></span><span class="tree-rollup"></span></span><span class="tree-status"></span><span class="tree-progress"></span><span class="tree-signals"></span><span class="tree-when"><span class="tree-by"></span><time data-compact="true"></time></span>';
  node.addEventListener("click", event => {
    if (event.target.closest(".tree-caret")) { toggleParent(node.dataset.id); return; }
    selectRoadmap(node.dataset.id);
  });
  return node;
}

function updateTreeRow(node, item) {
  const parent = item.children.length > 0;
  const d = derived(item);
  node.dataset.depth = String(Math.min(item.depth, 3));
  node.classList.toggle("parent", parent && item.depth === 0);
  node.classList.toggle("selected", selectedRoadmap === item.id);
  node.setAttribute("aria-selected", String(selectedRoadmap === item.id));
  const caret = node.querySelector(".tree-caret");
  caret.classList.toggle("leaf", !parent);
  caret.textContent = parent ? "▸" : "";
  caret.setAttribute("aria-expanded", String(parent && !collapsedParents.has(item.id)));
  caret.setAttribute("aria-label", parent ? `${collapsedParents.has(item.id) ? "Expand" : "Collapse"} ${item.title}` : "");
  node.querySelector(".tree-title").textContent = item.title;
  node.querySelector(".tree-id").innerHTML = `${item.moved ? '<span class="dot-moved" title="updated within the window" aria-hidden="true"></span>' : ""}<span>${escapeText(item.id)}</span><span class="quiet">r${item.revision}</span><span class="tree-owner">${escapeText(item.owner)}</span>`;
  node.querySelector(".tree-rollup").textContent = rollupText(item);
  node.querySelector(".tree-rollup").classList.toggle("hidden", !item.rollup);
  node.querySelector(".tree-status").innerHTML = pillMarkup(item.status);
  node.querySelector(".tree-progress").innerHTML = railMarkup(item, d.standby ? "standby" : "");
  node.querySelector(".tree-signals").innerHTML = signalsMarkup(item);
  node.querySelector(".tree-by").textContent = item.updated_by ? `by ${item.updated_by}` : "";
  applyTimestamp(node.querySelector("time"), item.updated_at, true);
  node.setAttribute("aria-label", `${item.title}, ${statusLabel(item.status)}, ${item.progress_reported ? `${item.progress} percent` : "progress not reported"}, owner ${actorText(item.owner)}, updated ${relativeTime(item.updated_at)}${item.blockers.length ? `, ${item.blockers.length} blockers` : ""}${d.startable ? ", ready to start" : ""}${d.standby ? ", in standby" : ""}`);
}

function toggleParent(id) {
  if (collapsedParents.has(id)) collapsedParents.delete(id); else collapsedParents.add(id);
  store(STORAGE.expanded, JSON.stringify([...collapsedParents]));
  renderRoadmap();
}

function treeRows() {
  const byId = treeById();
  const visible = new Set(visibleRoadmapItems().map(item => item.id));
  const rows = [];
  const walk = (id, forceVisible) => {
    const item = byId.get(id);
    if (!item) return;
    const descendantVisible = item.children.some(child => visible.has(child) || byId.get(child)?.children.length);
    if (!visible.has(id) && !descendantVisible && !forceVisible) return;
    rows.push(item);
    if (collapsedParents.has(id)) return;
    item.children.forEach(child => walk(child, false));
  };
  const roots = treePayload().roots || [];
  const programs = roots.filter(id => byId.get(id)?.children.length);
  const standalone = roots.filter(id => !byId.get(id)?.children.length);
  const groups = [];
  if (programs.length) { groups.push({label: "Programs", ids: programs}); }
  if (standalone.length) { groups.push({label: programs.length ? "Standalone items" : "Items", ids: standalone}); }
  return groups.map(group => { rows.length = 0; group.ids.forEach(id => walk(id, false)); return {label: group.label, items: [...rows]}; });
}

function renderTree(panel) {
  const groups = treeRows();
  panel.innerHTML = `<div class="tree-head label" aria-hidden="true"><span></span><span>Item · id · owner</span><span>Status</span><span>Progress</span><span>Signals</span><span class="ta-right">Updated</span></div>`;
  let total = 0;
  for (const group of groups) {
    if (!group.items.length) continue;
    total += group.items.length;
    const label = document.createElement("div"); label.className = "tree-group-label label"; label.textContent = group.label; panel.append(label);
    const holder = document.createElement("div"); holder.dataset.group = group.label; panel.append(holder);
    reconcileKeyed(holder, group.items, createTreeRow, updateTreeRow);
  }
  if (!total) panel.insertAdjacentHTML("beforeend", '<div class="empty-list"><strong>No roadmap items to show</strong><span>Clear the search or filters, tick "Show closed", or create the first item.</span></div>');
}

const LANES = [["NOT_STARTED","Not started"],["IN_PROGRESS","In progress"],["READY","Ready"],["BLOCKED","Blocked"],["CLOSED","Closed"],["PENDING","Pending (legacy)"]];
function laneOf(status) { return isClosed({status}) ? "CLOSED" : status; }

function renderKanban(panel) {
  const byId = treeById();
  const items = visibleRoadmapItems().filter(item => $("roadmap-show-closed").checked || !isClosed(item));
  const lanes = LANES.filter(([key]) => key !== "PENDING" || items.some(item => item.status === "PENDING")).filter(([key]) => key !== "CLOSED" || $("roadmap-show-closed").checked);
  panel.innerHTML = '<div class="kanban"></div>';
  const board = panel.firstElementChild;
  for (const [key, title] of lanes) {
    const laneItems = items.filter(item => laneOf(item.status) === key).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    const lane = document.createElement("section"); lane.className = "lane"; lane.setAttribute("aria-label", title);
    lane.innerHTML = `<header>${pillMarkup(key)}<span class="n">${laneItems.length}</span></header><div class="lane-cards"></div>`;
    const cards = lane.querySelector(".lane-cards");
    reconcileKeyed(cards, laneItems, () => {
      const card = document.createElement("button"); card.type = "button"; card.className = "card"; card.setAttribute("role", "option");
      card.innerHTML = '<span class="parent-tag"></span><strong></strong><span class="progress-slot"></span><span class="tree-signals"></span><span class="card-foot"><span class="owner"></span><time data-compact="true"></time></span>';
      card.addEventListener("click", () => selectRoadmap(card.dataset.id));
      return card;
    }, (card, item) => {
      card.classList.toggle("selected", selectedRoadmap === item.id);
      card.setAttribute("aria-selected", String(selectedRoadmap === item.id));
      const parent = item.parent_id ? byId.get(item.parent_id) : null;
      card.querySelector(".parent-tag").textContent = parent ? `↳ ${parent.title}` : item.children.length ? `program · ${item.children.length} children` : "";
      card.querySelector("strong").textContent = item.title;
      card.querySelector(".progress-slot").innerHTML = railMarkup(item, derived(item).standby ? "standby" : "");
      card.querySelector(".tree-signals").innerHTML = signalsMarkup(item);
      card.querySelector(".owner").innerHTML = `${item.moved ? '<span class="dot-moved" aria-hidden="true"></span> ' : ""}${escapeText(item.owner)}`;
      applyTimestamp(card.querySelector("time"), item.updated_at, true);
    });
    if (!laneItems.length) cards.innerHTML = '<p class="lane-empty">Empty</p>';
    board.append(lane);
  }
}

function renderTimeline(panel) {
  const hours = sinceHours();
  const now = Date.now(), start = now - hours * 3600 * 1000;
  // CSP (style-src 'self') drops inline style attributes: positions travel as data-x/data-r and are applied from JS.
  const x = t => Math.max(0, Math.min(100, ((t - start) / (now - start)) * 100)).toFixed(2);
  const place = root => root.querySelectorAll("[data-x],[data-r]").forEach(node => { if (node.dataset.x !== undefined) node.style.left = `${node.dataset.x}%`; if (node.dataset.r !== undefined) node.style.right = `${node.dataset.r}%`; });
  const items = visibleRoadmapItems()
    .map(item => ({item, points: (item.history || []).map(entry => ({...entry, t: Date.parse(entry.updated_at)})).filter(point => !Number.isNaN(point.t))}))
    .filter(({item, points}) => points.some(point => point.t >= start) || movedRecently(item))
    .sort((a, b) => b.item.updated_at.localeCompare(a.item.updated_at));
  const ticks = hours <= 24 ? 6 : hours <= 72 ? 6 : Math.min(10, Math.round(hours / 24));
  const tickMarkup = Array.from({length: ticks + 1}, (_v, i) => { const t = start + (now - start) * (i / ticks); return `<span data-x="${x(t)}">${hours <= 72 ? shortET.format(new Date(t)).replace(/ [A-Z]+$/, "") : dayET.format(new Date(t))}</span>`; }).join("");
  panel.innerHTML = `<div class="timeline-wrap"><div class="timeline-axis"><span>Item · revisions recorded in the window</span><div class="ticks">${tickMarkup}</div></div><div class="timeline-rows"></div></div>`;
  place(panel);
  const rows = panel.querySelector(".timeline-rows");
  if (!items.length) { rows.innerHTML = '<div class="empty-list"><strong>No recorded movement in this window</strong><span>The journal only knows revisions observed while the board was open; widen the window to see older ones.</span></div>'; return; }
  reconcileKeyed(rows, items.map(entry => ({...entry, id: entry.item.id})), () => {
    const row = document.createElement("button"); row.type = "button"; row.className = "timeline-row"; row.setAttribute("role", "option");
    row.innerHTML = '<span class="timeline-label"><strong></strong><small></small></span><span class="track"></span>';
    row.addEventListener("click", () => selectRoadmap(row.dataset.id));
    return row;
  }, (row, {item, points}) => {
    row.classList.toggle("selected", selectedRoadmap === item.id);
    row.setAttribute("aria-selected", String(selectedRoadmap === item.id));
    row.querySelector("strong").textContent = item.title;
    row.querySelector("small").textContent = `${item.id} · ${statusLabel(item.status)} · ${item.progress_reported ? `${item.progress} %` : "progress not reported"}`;
    const inWindow = points.filter(point => point.t >= start);
    const before = points.filter(point => point.t < start);
    const first = inWindow[0];
    let markup = `<span class="now-line" data-x="100" aria-hidden="true"></span>`;
    if (before.length) markup += `<span class="line" data-x="0" data-r="${(100 - parseFloat(x(first ? first.t : now))).toFixed(2)}"></span>`;
    if (inWindow.length) markup += `<span class="line progress" data-x="${x(inWindow[0].t)}" data-r="${(100 - parseFloat(x(inWindow[inWindow.length - 1].t))).toFixed(2)}"></span>`;
    inWindow.forEach((point, index) => {
      const cls = isClosed(point) ? "closed" : point.status === "BLOCKED" ? "blocked" : "";
      const firstSeen = index === 0 && !before.length && point.revision > 1;
      const label = point.revision === 1 ? "new" : firstSeen ? `r${point.revision} · first seen` : (point.progress > 0 || isClosed(point)) ? `${point.progress} %` : statusLabel(point.status).toLowerCase();
      markup += `<span class="pt ${cls} ${firstSeen ? "first" : ""}" data-x="${x(point.t)}" title="r${point.revision} · ${statusLabel(point.status)} · ${point.progress} % · ${absoluteET.format(new Date(point.t))}"></span><span class="lbl ${index % 2 ? "below" : ""}" data-x="${x(point.t)}">${escapeText(label)}</span>`;
    });
    row.querySelector(".track").innerHTML = markup;
    place(row);
    row.setAttribute("aria-label", `${item.title}: ${inWindow.length} revision${inWindow.length === 1 ? "" : "s"} in the window${before.length ? ", earlier history exists" : ""}`);
  });
}

function renderRoadmap() {
  // The roadmap is never merged across projects: say which store this view reads.
  $("roadmap-project").textContent = multiProject() ? activeProjectName() : "";
  const payload = treePayload();
  const errorNode = $("roadmap-tree-error");
  errorNode.classList.toggle("hidden", !payload.error);
  if (payload.error) errorNode.textContent = `Roadmap tree unavailable: ${payload.error}. The flat item list below still reflects roadmap.v1.json.`;
  const warnings = (payload.warnings || []);
  $("roadmap-warnings").classList.toggle("hidden", !warnings.length);
  $("roadmap-warnings").innerHTML = warnings.length ? `<strong>${warnings.length} data warning${warnings.length === 1 ? "" : "s"}</strong>${warnings.map(text => `<span>${escapeText(text)}</span>`).join("")}` : "";
  const counts = payload.counts || {};
  $("roadmap-count").textContent = counts.items !== undefined ? `${counts.items} items · ${counts.roots ?? 0} roots` : String(state.roadmap.length);
  $("nav-roadmap-count").textContent = String(counts.items ?? state.roadmap.length);
  for (const mode of ["overview","tree","kanban","timeline"]) $(`rmode-${mode}`).setAttribute("aria-pressed", String(roadmapMode === mode));
  renderMovedStrip();
  renderSignals();
  const overview = $("overview");
  overview.classList.toggle("hidden", roadmapMode !== "overview");
  const panel = $("roadmap-panel");
  panel.dataset.mode = roadmapMode;
  $("roadmap-layout").dataset.mode = roadmapMode;
  if (roadmapMode === "overview") renderOverview(overview); else if (roadmapMode === "kanban") renderKanban(panel); else if (roadmapMode === "timeline") renderTimeline(panel); else renderTree(panel);
  if (selectedRoadmap && !treeById().has(selectedRoadmap)) selectedRoadmap = null;
  renderRoadmapDetail();
  updateVisibleTimes();
}

function selectRoadmap(id) { selectedRoadmap = id; roadmapDetailSignature = ""; renderRoadmap(); revealOnMobile($("roadmap-detail")); }

function setRoadmapMode(mode) { roadmapMode = mode; store(STORAGE.rmode, mode); renderRoadmap(); }

function focusOverviewCard(key) {
  setView("roadmap"); setRoadmapMode("overview");
  const card = $("overview").querySelector(`[data-focus="${key}"]`);
  if (card) { card.scrollIntoView({block: "start"}); card.querySelector("h2")?.setAttribute("tabindex", "-1"); card.querySelector("h2")?.focus({preventScroll: true}); }
}

function depListMarkup(deps) {
  return `<ul class="list-plain">${deps.map(dep => `<li>${dep.resolved ? tagMarkup("RESOLVED", "green") : dep.known ? tagMarkup("OPEN", "amber") : tagMarkup("UNKNOWN ID", "red")}${dep.known ? `<button type="button" class="link-button" data-select="${escapeText(dep.id)}">${escapeText(dep.title)}</button>` : `<span>${escapeText(dep.id)}</span>`}<small>${escapeText(dep.id)}${dep.status ? ` · ${escapeText(statusLabel(dep.status))}` : ""}</small></li>`).join("")}</ul>`;
}

function idListMarkup(ids, byId) {
  return `<ul class="list-plain">${ids.map(id => { const target = byId.get(id); return `<li>${target ? pillMarkup(target.status) : ""}<button type="button" class="link-button" data-select="${escapeText(id)}">${escapeText(target ? target.title : id)}</button><small>${escapeText(id)}</small></li>`; }).join("")}</ul>`;
}

function renderRoadmapDetail() {
  const detail = $("roadmap-detail");
  const byId = treeById();
  const item = selectedRoadmap ? byId.get(selectedRoadmap) : null;
  if (!item) { if (!detail.classList.contains("empty")) { detail.className = "detail-panel roadmap-detail empty"; detail.innerHTML = '<div><h2>Pick a roadmap item</h2><p>Status, progress, what blocks it, what it waits on, what it unblocks, which milestone it feeds, and every recorded revision appear here.</p></div>'; } return; }
  const signature = JSON.stringify(item) + sinceHours();
  if (signature === roadmapDetailSignature) return;
  roadmapDetailSignature = signature;
  detail.className = "detail-panel roadmap-detail";
  const d = derived(item);
  const parent = item.parent_id ? byId.get(item.parent_id) : null;
  const children = item.children.map(id => byId.get(id)).filter(Boolean);
  const history = [...(item.history || [])].reverse();
  const historyMarkup = history.length ? history.map((entry, index) => {
    const previous = history[index + 1];
    const changes = previous ? ["status","progress","owner"].filter(field => previous[field] !== entry[field]).map(field => ({field, from: previous[field], to: entry[field]})) : [];
    const note = entry.revision === 1 ? "created" : previous ? (changes.length ? "" : "text-only edit") : "first revision seen by this board (prior unknown)";
    return `<div class="history-entry"><span class="history-dot" aria-hidden="true"></span><div><strong>r${entry.revision} · ${escapeText(statusLabel(entry.status))} · ${entry.progress > 0 || isClosed(entry) ? `${entry.progress} %` : "progress not reported"}${note ? ` <span class="quiet">· ${escapeText(note)}</span>` : ""}</strong>${timeMarkup(entry.updated_at, false)}${changes.length ? `<div class="delta">${changeMarkup(changes)}</div>` : ""}</div></div>`;
  }).join("") : '<p class="quiet">No revision has been journaled yet; the journal starts when the board first observes an item.</p>';
  const situation = [];
  if (d.startable) situation.push('<div class="callout info"><strong>Ready to start</strong><span>Not started, no blocker, no unresolved dependency.</span></div>');
  if (d.standby) situation.push(`<div class="callout warn"><strong>In standby · ${escapeText(ageLabel(d.standby.age_hours))} since the last update</strong><span>${escapeText(d.standby.kind === "explicit" ? `Flagged by ${d.standby.set_by || "?"}: ${d.standby.reason}` : d.standby.reason)}</span></div>`);
  if (d.blocked_by) situation.push(`<div class="callout danger"><strong>${item.status === "BLOCKED" ? "Blocked by" : "Waits on"}</strong>${d.blocked_by.blockers.map(text => `<span>${escapeText(text)}</span>`).join("")}${d.blocked_by.waiting_on.length ? `<span>Open dependenc${d.blocked_by.waiting_on.length === 1 ? "y" : "ies"}: ${d.blocked_by.waiting_on.map(id => `<button type="button" class="link-button" data-select="${escapeText(id)}">${escapeText(byId.get(id)?.title || id)}</button>`).join(", ")}</span>` : ""}</div>`);
  detail.innerHTML = `<div class="detail-head"><div class="badges">${pillMarkup(item.status)}${d.kind !== "UNKNOWN" ? tagMarkup(d.kind, "fill-violet") : ""}${item.moved ? tagMarkup("moved in window", "amber") : ""}${tagMarkup(`r${item.revision}`)}</div><h2>${escapeText(item.title)}</h2><div class="detail-meta"><span class="mono">${escapeText(item.id)}</span>${parent ? `<span>↳ <button type="button" class="link-button" data-select="${escapeText(parent.id)}">${escapeText(parent.title)}</button></span>` : ""}</div></div>
  <div class="detail-actions"><button id="edit-roadmap" type="button" class="secondary">Edit item</button><button id="copy-roadmap-md" type="button" class="secondary" title="Title, status, dependencies, impact and summary as Markdown">Copy as Markdown</button><button id="copy-roadmap-id" type="button" class="ghost">Copy id</button></div>
  ${situation.join("")}
  <dl class="kv"><dt class="label">Progress</dt><dd>${railMarkup(item, d.standby ? "standby" : "")}${item.progress_reported ? "" : ' <span class="quiet">— the v1 store carries 0; nobody has reported a value</span>'}</dd><dt class="label">Owner</dt><dd>${actorMarkup(item.owner)}</dd><dt class="label">Kind</dt><dd>${d.kind === "UNKNOWN" ? '<span class="quiet">not set (treated as a task)</span>' : escapeText(d.kind)}</dd><dt class="label">Impact</dt><dd>${d.impact ? escapeText(d.impact) : '<span class="quiet">not recorded</span>'}</dd><dt class="label">Updated</dt><dd>${timeMarkup(item.updated_at, false)} <span class="quiet">· ${escapeText(ageLabel(d.age_hours))} ago</span></dd><dt class="label">Updated by</dt><dd>${item.updated_by ? escapeText(item.updated_by) : '<span class="quiet">UNKNOWN — the v1 upsert does not record the actor</span>'}</dd>${item.due ? `<dt class="label">Due</dt><dd>${escapeText(item.due)}</dd>` : ""}${item.rollup ? `<dt class="label">Children</dt><dd>${escapeText(rollupText(item))}</dd>` : ""}</dl>
  ${d.aggregate ? `<h3 class="label">Aggregate · ${d.aggregate.count} members</h3><div class="milestone">${aggregateMarkup(item)}${d.aggregate.members.length ? `<div class="members">${d.aggregate.members.map(id => itemChip(id, byId)).join("")}</div>` : '<p class="quiet">No member yet: add children or dependencies.</p>'}</div>` : ""}
  ${d.depends_on.length ? `<h3 class="label">Depends on (${d.depends_on.length})</h3>${depListMarkup(d.depends_on)}` : ""}
  ${d.dependents.length ? `<h3 class="label">Unblocks (${d.dependents.length})</h3>${idListMarkup(d.dependents, byId)}` : ""}
  ${d.feeds.length ? `<h3 class="label">Feeds</h3>${idListMarkup(d.feeds, byId)}` : ""}
  ${item.gates.length ? `<h3 class="label">Gates</h3><ul class="list-plain">${item.gates.map(gate => `<li>${tagMarkup(gate.state, gate.state === "PASS" ? "green" : gate.state === "FAIL" ? "red" : "amber")}<span>${escapeText(gate.name)}</span>${gate.note ? `<span class="muted">${escapeText(gate.note)}</span>` : ""}<small>${escapeText(gate.updated_by || "")} · ${escapeText(relativeTime(gate.updated_at))}</small></li>`).join("")}</ul>` : ""}
  <h3 class="label">Summary</h3><div class="markdown-body">${renderMarkdown(item.summary)}</div>
  ${children.length ? `<h3 class="label">Children (${children.length})</h3><ul class="list-plain">${children.map(child => `<li>${pillMarkup(child.status)}<button type="button" class="link-button" data-select="${escapeText(child.id)}">${escapeText(child.title)}</button><small>${child.progress_reported ? `${child.progress} %` : "not reported"} · ${escapeText(relativeTime(child.updated_at))}</small></li>`).join("")}</ul>` : ""}
  <h3 class="label">Revisions</h3><div class="history">${historyMarkup}</div>`;
  detail.querySelectorAll("[data-select]").forEach(button => button.addEventListener("click", () => selectRoadmap(button.dataset.select)));
  $("edit-roadmap").onclick = () => openRoadmap(item);
  $("copy-roadmap-md").onclick = () => copy(formatRoadmapItemMarkdown(item), "Roadmap item copied as Markdown");
  $("copy-roadmap-id").onclick = () => copy(item.id, "Item id copied");
  updateVisibleTimes();
}

// ---------- presence ----------

function createPresenceChip(){const node=document.createElement("span");node.className="presence-chip";node.innerHTML='<span class="presence-dot"></span><span class="presence-name"></span><span class="presence-state quiet"></span>';return node;}
function updatePresenceChip(node,item){node.querySelector(".presence-name").textContent=actorLabels[item.identity]||item.identity;node.querySelector(".presence-state").textContent=item.state;node.querySelector(".presence-dot").className=`presence-dot ${item.state.toLowerCase()}`;node.title=`${item.summary} · ${absoluteET.format(new Date(item.updated_at))}`;}
function createInfoCard(){const node=document.createElement("article");node.className="info-card";node.innerHTML="<strong></strong><p></p><time data-compact=\"true\"></time>";return node;}

function renderPresence(){
  reconcileKeyed($("presence-strip"),state.status,createPresenceChip,updatePresenceChip);
  reconcileKeyed($("statuses"),state.status,createInfoCard,(node,item)=>{node.querySelector("strong").innerHTML=`${actorMarkup(item.identity)} · ${escapeText(item.state)}${item.head?` · <span class="mono">${escapeText(item.head)}</span>`:""}`;node.querySelector("p").textContent=`${item.summary}${item.workstream?` · ${item.workstream}`:""}${item.paths?.length?` · paths: ${item.paths.join(", ")}`:""}`;applyTimestamp(node.querySelector("time"),item.updated_at,true);});
  reconcileKeyed($("leases"),state.leases,createInfoCard,(node,item)=>{node.className=`info-card${item.stale?" lease-stale":""}`;node.querySelector("strong").innerHTML=`${escapeText(item.ticket_id)}${item.stale?' <span class="pill red">stale</span>':""}`;node.querySelector("p").innerHTML=`${actorMarkup(item.assignee)} · granted by ${actorMarkup(item.granted_by)}`;applyTimestamp(node.querySelector("time"),item.granted_at,true);});
  if(!state.status.length)$("statuses").innerHTML='<p class="quiet">No master has published a status.</p>';
  if(!state.leases.length)$("leases").innerHTML='<p class="quiet">No active leases.</p>';
  $("presence-summary").textContent=`${state.status.length} master${state.status.length===1?"":"s"} · ${state.leases.length} lease${state.leases.length===1?"":"s"}`;
  $("nav-presence-count").textContent=String(state.leases.length);
}

// ---------- tickets ----------

const TICKET_LANES = [["BACKLOG","Backlog"],["ANALYSIS","Analysis"],["DEVELOPMENT","Development"],["QA","QA"],["INTEGRATION","Integration"],["BLOCKED","Blocked"],["DONE","Done"],["CANCELLED","Cancelled"]];
const TICKET_TERMINAL = new Set(["DONE","CANCELLED"]);
function ticketStageTone(stage) { return stage==="DONE"?"green":stage==="CANCELLED"?"quiet":stage==="BLOCKED"?"red":(stage==="QA"||stage==="INTEGRATION")?"blue":(stage==="DEVELOPMENT"||stage==="ANALYSIS")?"violet":"quiet"; }
function ticketStagePill(stage) { return `<span class="pill ${ticketStageTone(stage)}">${escapeText(statusLabel(stage))}</span>`; }
let selectedTicket = null;
let ticketDetailSignature = "";
let ticketFormOpener = null;
let ticketStageFilter = "all";
const ticketDiscussions = new Map();
const ticketDiscussionLoads = new Set();

function ticketsById() { return new Map(state.tickets.map(item => [item.id, item])); }

function ticketOpenBlockers(item) {
  return (item.open_blockers || []).map(id => ({id, ticket: ticketsById().get(id)}));
}

function ticketBlockerMarkup(item) {
  const blockers = ticketOpenBlockers(item);
  if (!blockers.length) return item.stage === "BLOCKED" ? '<div class="ticket-blocker-banner"><strong>Marked blocked</strong><span>No open blocking dependency is recorded.</span></div>' : "";
  return `<div class="ticket-blocker-banner"><strong>Waiting on ${blockers.length} blocking ${blockers.length === 1 ? "ticket" : "tickets"}</strong><span>${blockers.map(({id, ticket}) => `<a href="${escapeText(ticketUrl(id).href)}">${escapeText(ticket?.display_id || id)}${ticket ? ` · ${escapeText(ticket.title)}` : ' · dependency unavailable'}</a>`).join("<br>")}</span></div>`;
}

function ticketMatches(item) {
  const assignee = $("filter-tassignee").value;
  if (assignee && item.assignee !== assignee) return false;
  const query = $("ticket-search").value.trim().toLowerCase();
  if (!query) return true;
  const haystack = `${item.id} ${item.display_id || ""} ${item.title} ${item.assignee || ""} ${ticketActorLabel(item.assignee)} ${item.reviewer || ""}`.toLowerCase();
  return query.split(/\s+/).every(part => haystack.includes(part));
}

function visibleTickets() {
  const showClosed = $("ticket-show-closed").checked;
  return state.tickets.filter(item => ticketMatches(item) && (showClosed || !TICKET_TERMINAL.has(item.stage)));
}

async function ticketApi(path, body) {
  return api(withProject(path, activeProjectName()), {method: "POST", body: JSON.stringify({project: activeProjectName(), ...body})});
}

async function ticketAction(id, action, body) { return ticketApi(`/api/tickets/${encodeURIComponent(id)}/${action}`, body); }

function renderTicketKanban() {
  const panel = $("ticket-panel");
  const items = visibleTickets();
  const stages = TICKET_LANES.filter(([key]) => $("ticket-show-closed").checked || !TICKET_TERMINAL.has(key));
  if (!stages.some(([key]) => key === ticketStageFilter)) ticketStageFilter = "all";
  const shown = items.filter(item => ticketStageFilter === "all" || item.stage === ticketStageFilter).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  panel.innerHTML = `<div class="ticket-stage-filters" role="group" aria-label="Filter tickets by stage">${[["all", "All tickets"], ...stages].map(([key, label]) => `<button type="button" data-ticket-stage="${key}" aria-pressed="${ticketStageFilter === key}">${escapeText(label)}<span>${key === "all" ? items.length : items.filter(item => item.stage === key).length}</span></button>`).join("")}</div><div class="ticket-list-heading"><span>${shown.length} ticket${shown.length === 1 ? "" : "s"}</span><span>Last updated</span></div><div class="ticket-list" role="listbox" aria-label="Tickets"></div>`;
  panel.querySelectorAll("[data-ticket-stage]").forEach(button => button.onclick = () => { ticketStageFilter = button.dataset.ticketStage; renderTicketKanban(); panel.querySelector(`[data-ticket-stage="${ticketStageFilter}"]`).focus(); });
  const list = panel.querySelector(".ticket-list");
  reconcileKeyed(list, shown, () => {
    const row = document.createElement("button"); row.type = "button"; row.className = "ticket-row card"; row.setAttribute("role", "option");
    row.innerHTML = '<span class="ticket-row-stage"></span><span class="ticket-row-name"><strong></strong><span class="ticket-row-meta"></span></span><span class="ticket-row-owner"></span><time data-compact="true"></time><span class="ticket-row-arrow" aria-hidden="true">›</span>';
    row.onclick = () => selectTicket(row.dataset.id);
    return row;
  }, (row, item) => {
    row.setAttribute("aria-selected", String(selectedTicket === item.id));
    row.querySelector(".ticket-row-stage").innerHTML = ticketStagePill(item.stage);
    row.querySelector("strong").textContent = item.title;
    row.querySelector(".ticket-row-meta").innerHTML = `${escapeText(item.display_id || item.id)} · ${escapeText(item.kind)}${item.lease_stale ? ' · <span class="error">Lease expired</span>' : ""}${ticketOpenBlockers(item).length ? ` · <span class="ticket-blocked-label" title="${escapeText(ticketOpenBlockers(item).map(({id, ticket}) => ticket?.display_id || id).join(', '))}">Blocked by ${ticketOpenBlockers(item).length} ${ticketOpenBlockers(item).length === 1 ? 'ticket' : 'tickets'}</span>` : ''}`;
    row.querySelector(".ticket-row-owner").textContent = ticketActorLabel(item.assignee);
    row.querySelector(".ticket-row-owner").title = item.assignee || "Unassigned";
    applyTimestamp(row.querySelector("time"), item.updated_at, true);
  });
  if (!shown.length) list.innerHTML = '<div class="empty-list"><strong>No tickets in this view</strong><span>Choose another stage or adjust your filters.</span></div>';
}

function ticketDepsMarkup(deps, byId) {
  if (!deps.length) return '<p class="quiet">No dependencies.</p>';
  return `<ul class="list-plain">${deps.map(dep => {
    const target = byId.get(dep.target);
    return `<li>${tagMarkup(dep.type)}${target ? `<button type="button" class="link-button" data-select-ticket="${escapeText(dep.target)}">${escapeText(target.title)}</button>` : `<span>${escapeText(dep.target)}</span>`}<small>${escapeText(target?.display_id || dep.target)}${target ? ` · ${escapeText(target.stage)}` : ""}</small></li>`;
  }).join("")}</ul>`;
}

function ticketActorLabel(id) {
  if (!id) return "Unassigned";
  const [master, ...worker] = id.split("/");
  return worker.length ? `${actorLabels[master] || master} / ${worker.join("/").replaceAll("_", " ")}` : actorLabels[id] || id;
}

async function loadTicketDiscussion(id) {
  const project = activeProjectName(); const key = `${project}/${id}`;
  if (ticketDiscussionLoads.has(key)) return;
  ticketDiscussionLoads.add(key);
  try {
    const value = await api(withProject(`/api/tickets/${encodeURIComponent(id)}`, project));
    ticketDiscussions.set(key, {messages: value.ticket.linked_messages || [], malformed: value.ticket.linked_messages_malformed || 0});
  } catch (error) { ticketDiscussions.set(key, {error: error.message}); }
  finally {
    ticketDiscussionLoads.delete(key);
    if (selectedTicket === id && activeProjectName() === project) renderTicketDetail();
  }
}

function ticketMessageUrl(id) {
  const url = new URL(location.href); url.hash = "";
  url.searchParams.set("view", "messages"); url.searchParams.set("project", activeProjectName());
  url.searchParams.delete("ticket"); url.searchParams.set("message", id); return url;
}

function ticketMessageStatus(message) {
  const status = [];
  if (message.requires_ack && message.acked === false) status.push(tagMarkup("Awaiting ACK", "amber"));
  if (message.requires_ack && message.acked === true) status.push(tagMarkup("Acknowledged", "green"));
  if (message.answered === true) status.push(tagMarkup("Replied", "blue"));
  if (message.answered === false) status.push(tagMarkup("No reply"));
  return status.join("");
}

function ticketDiscussionMarkup(discussion) {
  if (!discussion) return '<p class="quiet">Loading linked inbox discussion…</p>';
  if (discussion.error) return '<p class="quiet">Linked inbox discussion is unavailable. Refresh to retry.</p>';
  if (!discussion.messages.length && !discussion.malformed) return "";
  return `<section class="ticket-inbox-discussion"><h3>Linked inbox discussion <span class="quiet">${discussion.messages.length}</span></h3>${discussion.malformed ? '<p class="quiet">Some message files could not be read; this list may be incomplete.</p>' : ""}${[...discussion.messages].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id)).map(message => `<article class="ticket-inbox-entry"><a class="ticket-inbox-link" href="${escapeText(ticketMessageUrl(message.id).href)}" data-ticket-message="${escapeText(message.id)}"><strong>${escapeText(message.summary)}</strong><span>${escapeText(ticketActorLabel(message.from))} → ${escapeText(ticketActorLabel(message.to))} · ${timeMarkup(message.created_at, true)}</span><small>Open in Inbox ↗</small></a><div class="ticket-message-status">${ticketMessageStatus(message)}</div>${message.replies?.length ? `<div class="ticket-message-replies"><span class="quiet">Replies</span>${[...message.replies].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id)).map(reply => `<a href="${escapeText(ticketMessageUrl(reply.id).href)}" data-ticket-message="${escapeText(reply.id)}">${escapeText(reply.summary)} ↗</a>`).join("")}</div>` : ""}</article>`).join("")}</section>`;
}

function ticketAuthorMarkup(id) {
  const registered = (state.actors || []).find(actor => actor.name === id);
  const [prefix, ...worker] = id.split("/");
  const isDeveloper = worker.length > 0 || registered?.role === "subagent";
  const name = isDeveloper ? (worker.join("/") || id).replaceAll("_", " ") : ticketActorLabel(id);
  const affiliation = isDeveloper ? `Developer · ${ticketActorLabel(registered?.master || prefix)}` : registered?.role === "operator" || id === "operator" ? "Operator" : registered?.role === "lead" || id === "lead" ? "Lead" : "Master";
  return `<span class="ticket-author" title="${escapeText(id)}"><strong>${escapeText(name)}</strong><small>${escapeText(affiliation)}</small></span>`;
}

function ticketCommentMarkup(entry) {
  const body = entry.body || entry.summary || "";
  try {
    const value = JSON.parse(body);
    if (value !== null && typeof value === "object") return `${entry.body && entry.summary ? `<p>${escapeText(entry.summary)}</p>` : ""}<details class="ticket-technical"><summary>Technical evidence</summary><pre><code>${escapeText(JSON.stringify(value, null, 2))}</code></pre></details>`;
  } catch { /* Ordinary prose and Markdown remain prose. */ }
  return renderMarkdown(body);
}

function ticketActivityMarkup(item) {
  const entries = [
    ...item.comments.map(entry => ({...entry, kind: "comment"})),
    ...item.worklog.map(entry => ({...entry, kind: "worklog"})),
    ...item.reviews.map(entry => ({...entry, kind: "review"})),
  ].sort((a, b) => b.ts.localeCompare(a.ts) || (b.seq || 0) - (a.seq || 0) || b.kind.localeCompare(a.kind) || b.actor.localeCompare(a.actor));
  if (!entries.length) return '<p class="ticket-no-activity">No updates yet. Start the conversation below.</p>';
  return `<div class="ticket-timeline">${entries.map(entry => `<article class="ticket-event"><span class="ticket-avatar" aria-hidden="true">${escapeText(ticketActorLabel(entry.actor).slice(0, 1))}</span><div class="ticket-event-content"><header>${ticketAuthorMarkup(entry.actor)}<span class="quiet">${entry.kind === "comment" ? "commented" : entry.kind === "review" ? "reviewed" : "logged work"}</span>${timeMarkup(entry.ts, true)}</header>${entry.verdict ? `<p>${tagMarkup(entry.verdict, entry.verdict === "FAIL" ? "red" : "green")}</p>` : ""}<div class="markdown-body">${ticketCommentMarkup(entry)}</div>${entry.findings?.length ? `<ul>${entry.findings.map(finding => `<li>${escapeText(finding)}</li>`).join("")}</ul>` : ""}${entry.evidence ? `<dl class="ticket-evidence">${Object.entries(entry.evidence).map(([key, value]) => `<dt>${escapeText(key)}</dt><dd>${escapeText(String(value))}</dd>`).join("")}</dl>` : ""}</div></article>`).join("")}</div>`;
}

function renderTicketDetail() {
  const detail = $("ticket-detail");
  const byId = ticketsById();
  const item = selectedTicket ? byId.get(selectedTicket) : null;
  $("ticket-panel").hidden = Boolean(item);
  detail.hidden = !item;
  if (!item) { detail.innerHTML = ""; detail.dataset.ticketId = ""; return; }
  const discussion = ticketDiscussions.get(`${activeProjectName()}/${item.id}`);
  const signature = JSON.stringify([item, discussion]);
  if (signature === ticketDetailSignature) return;
  ticketDetailSignature = signature;
  const sameTicket = detail.dataset.ticketId === item.id;
  const draft = sameTicket ? [...detail.querySelectorAll("input, select, textarea")].filter(node => node.id === "ta-actor" || node.value !== node.dataset.initialValue).map(node => [node.id, node.value]) : [];
  const openDialog = sameTicket ? detail.querySelector("dialog[open]")?.id : null;
  const focused = sameTicket && detail.contains(document.activeElement) ? document.activeElement : null;
  const focusState = focused ? {id: focused.id, start: focused.selectionStart, end: focused.selectionEnd} : null;
  const descriptionOpen = sameTicket ? detail.querySelector(".ticket-description")?.open : item.body.length < 1200;
  detail.dataset.ticketId = item.id;
  detail.className = "detail-panel ticket-issue";
  const identities = state.choices.identities || [];
  const lease = item.lease;
  const options = TICKET_LANES.filter(([key]) => key !== "DONE").map(([key, title]) => `<option value="${key}" ${key === item.stage ? "selected" : ""}>${escapeText(title)}</option>`).join("");
  detail.innerHTML = `<div class="ticket-breadcrumb"><button id="ticket-back" type="button" class="ghost">← Tickets</button><span>/</span><span class="ticket-project-name">${escapeText(activeProjectName() || "Project")}</span><span>/</span><button id="ticket-copy-id" type="button" class="ghost mono" title="Copy ticket ID">${escapeText(item.display_id || item.id)}</button><button id="ticket-copy-link" type="button" class="ghost">Copy link</button><button id="ticket-copy-md" type="button" class="ghost ticket-export">Copy Markdown</button></div>
    <header class="ticket-issue-head"><div class="badges">${ticketStagePill(item.stage)}${tagMarkup(item.kind)}</div><h2>${escapeText(item.title)}</h2><div class="ticket-command-bar"><button id="ticket-reply" type="button" class="secondary">Comment</button><button id="ticket-review-open" type="button" class="secondary">Record review</button><button id="ta-done" type="button" class="ghost">✓ Mark done</button><label class="ticket-acting">Acting as <select id="ta-actor" aria-label="Acting master">${identities.map(id => `<option value="${escapeText(id)}">${escapeText(ticketActorLabel(id))}</option>`).join("")}</select></label></div></header>
    ${ticketBlockerMarkup(item)}<p id="ta-error" class="error" role="alert"></p>
    <div class="ticket-issue-grid"><div class="ticket-main">
      ${item.summary ? `<p class="ticket-summary">${escapeText(item.summary)}</p>` : ""}
      ${item.body ? `<details class="ticket-description" ${descriptionOpen ? "open" : ""}><summary>Description <span class="quiet">${item.body.length >= 1200 ? "Full context" : ""}</span></summary><div class="markdown-body">${renderMarkdown(item.body)}</div></details>` : ""}
      ${item.acceptance_criteria.length ? `<section class="ticket-criteria"><h3>Acceptance criteria</h3><ul>${item.acceptance_criteria.map(text => `<li>${escapeText(text)}</li>`).join("")}</ul></section>` : ""}
      ${ticketDiscussionMarkup(discussion)}
      <section class="ticket-discussion" aria-labelledby="ticket-activity-title"><div class="ticket-discussion-head"><h3 id="ticket-activity-title">Activity</h3><span>${item.comments.length} comment${item.comments.length === 1 ? "" : "s"}</span></div>
        <div class="ticket-composer"><label class="sr-only" for="ta-comment">Write a comment</label><textarea id="ta-comment" rows="4" maxlength="32768" placeholder="Write a comment, share an update, or ask a question…"></textarea><div class="ticket-composer-foot"><span class="quiet">Markdown supported</span><button id="ta-comment-btn" type="button" class="primary">Send comment</button></div><p id="ta-comment-error" class="error" role="alert"></p></div>${ticketActivityMarkup(item)}
      </section>
    </div><aside class="ticket-properties" aria-label="Ticket properties"><h3>Properties</h3><dl>
      <dt>Status</dt><dd><div class="ticket-status-control"><select id="ta-stage" aria-label="Move ticket to stage">${item.stage === "DONE" ? '<option value="" selected disabled>Done</option>' : ""}${options}</select><button id="ta-transition" type="button" class="ghost">Apply</button></div></dd>
      <dt>Assignee</dt><dd><button id="ticket-assign-open" type="button" class="ticket-property-button" title="${escapeText(item.assignee || "Unassigned")}">${escapeText(ticketActorLabel(item.assignee))}<span aria-hidden="true">↗</span></button></dd>
      <dt>Reviewer</dt><dd title="${escapeText(item.reviewer || "")}">${item.reviewer ? escapeText(ticketActorLabel(item.reviewer)) : '<span class="quiet">Not assigned</span>'}</dd>
      <dt>Lease</dt><dd>${lease ? `<span>${item.lease_stale ? 'Expired' : `Expires ${escapeText(relativeTime(lease.expires_at))}`}</span>` : '<span class="quiet">No active lease</span>'}<button id="ta-heartbeat" type="button" class="link-button">Renew my lease</button></dd>
      <dt>Updated</dt><dd>${timeMarkup(item.updated_at, true)}</dd></dl>
      ${item.parent_id ? `<div class="ticket-property-section"><h3>Parent ticket</h3><button type="button" class="link-button" data-select-ticket="${escapeText(item.parent_id)}">${escapeText(byId.get(item.parent_id)?.title || item.parent_id)}</button></div>` : ""}
      <div class="ticket-property-section"><h3>Dependencies</h3>${ticketDepsMarkup(item.deps, byId)}</div>
    </aside></div>
    <dialog id="ticket-assign-dialog" class="ticket-dialog" aria-labelledby="ticket-assign-title"><form id="ticket-assign-form"><div class="dialog-head"><h2 id="ticket-assign-title">Assign ticket</h2><button type="button" class="ghost" data-close-ticket-dialog>Close</button></div><p class="muted">Assign an owner and start their work lease.</p><div class="form-grid"><label class="wide">Assignee<input id="ta-assignee" maxlength="128" value="${escapeText(item.assignee || "")}" placeholder="${escapeText(identities[0] || "master")}/worker" required></label></div><p id="ta-assign-error" class="error" role="alert"></p><div class="form-actions"><button id="ta-assign" type="submit" class="primary">Assign & start lease</button></div></form></dialog>
    <dialog id="ticket-review-dialog" class="ticket-dialog" aria-labelledby="ticket-review-title"><form id="ticket-review-form"><div class="dialog-head"><h2 id="ticket-review-title">Record a review</h2><button type="button" class="ghost" data-close-ticket-dialog>Close</button></div><p class="muted">A passing review is required before completion.</p><div class="form-grid"><label class="wide">Verdict<select id="ta-verdict"><option value="PASS">Pass</option><option value="CONFIRMED_WITH_FIXES">Confirmed with fixes</option><option value="FAIL">Changes requested</option></select></label><label class="wide">Review summary<input id="ta-review-summary" maxlength="300" required></label><label class="wide">Findings <small>One per line; required for fixes or changes requested</small><textarea id="ta-findings" rows="4"></textarea></label></div><p id="ta-review-error" class="error" role="alert"></p><div class="form-actions"><button id="ta-review" type="submit" class="primary">Save review</button></div></form></dialog>`;
  $("ticket-back").onclick = () => { const previous = selectedTicket; selectedTicket = null; ticketDetailSignature = ""; history.pushState(null, "", ticketUrl(null)); renderTickets(); [...$("ticket-panel").querySelectorAll(".card")].find(card => card.dataset.id === previous)?.focus(); };
  $("ticket-reply").onclick = () => { $("ta-comment").scrollIntoView({block: "center"}); $("ta-comment").focus({preventScroll: true}); };
  for (const kind of ["assign", "review"]) {
    const dialog = $(`ticket-${kind}-dialog`);
    const opener = $(`ticket-${kind}-open`);
    opener.onclick = () => dialog.showModal();
    dialog.querySelector("[data-close-ticket-dialog]").onclick = () => dialog.close();
    dialog.addEventListener("close", () => opener.focus({preventScroll: true}));
  }
  detail.querySelectorAll("input, select, textarea").forEach(node => { node.dataset.initialValue = node.value; });
  for (const [id, value] of draft) if ($(id)) $(id).value = value;
  if (openDialog) $(openDialog).showModal();
  if (focusState && $(focusState.id)) {
    const node = $(focusState.id); node.focus({preventScroll: true});
    if (typeof focusState.start === "number") node.setSelectionRange(focusState.start, focusState.end);
  }
  detail.querySelectorAll("[data-select-ticket]").forEach(button => button.onclick = () => selectTicket(button.dataset.selectTicket));
  detail.querySelectorAll("[data-ticket-message]").forEach(link => link.onclick = event => { if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); history.pushState(null, "", link.href); setView("messages"); showMessage(link.dataset.ticketMessage); });
  $("ticket-copy-id").onclick = () => copy(item.display_id || item.id, "Ticket id copied");
  $("ticket-copy-link").onclick = () => copy(ticketUrl(item.id).href, "Ticket link copied");
  $("ticket-copy-md").onclick = async () => {
    try { const response = await fetch(withProject(`/api/tickets/${encodeURIComponent(item.id)}/export`, activeProjectName())); await copy(await response.text(), "Ticket copied as Markdown"); }
    catch (error) { toast(error.message); }
  };
  const actor = () => $("ta-actor").value;
  const perform = async (button, action, body, errorId = "ta-error", onSuccess = () => {}) => {
    $(errorId).textContent = ""; button.disabled = true;
    try { await ticketAction(item.id, action, {actor: actor(), ...body}); onSuccess(); await refresh(); }
    catch (error) { $(errorId).textContent = error.message; }
    finally { if (button.isConnected) button.disabled = false; }
  };
  $("ticket-assign-form").onsubmit = event => { event.preventDefault(); perform($("ta-assign"), "assign", {assignee: $("ta-assignee").value}, "ta-assign-error", () => $("ticket-assign-dialog").close()); };
  $("ta-heartbeat").onclick = () => perform($("ta-heartbeat"), "heartbeat", {});
  $("ta-transition").onclick = () => { if ($("ta-stage").value) perform($("ta-transition"), "transition", {stage: $("ta-stage").value}); };
  $("ta-comment-btn").onclick = () => {
    const body = $("ta-comment").value.trim(); const summary = body.split("\n")[0].slice(0, 300);
    if (!body) { $("ta-comment-error").textContent = "Write a comment before sending."; $("ta-comment").focus(); return; }
    perform($("ta-comment-btn"), "comment", {summary, body}, "ta-comment-error", () => { $("ta-comment").value = ""; });
  };
  $("ticket-review-form").onsubmit = event => {
    event.preventDefault(); const findings = $("ta-findings").value.split("\n").map(line => line.trim()).filter(Boolean);
    perform($("ta-review"), "review", {verdict: $("ta-verdict").value, summary: $("ta-review-summary").value, findings}, "ta-review-error", () => $("ticket-review-dialog").close());
  };
  $("ta-done").onclick = () => perform($("ta-done"), "done", {});
  updateVisibleTimes();
}

function ticketUrl(id) {
  const url = new URL(location.href);
  url.hash = ""; url.searchParams.set("view", "tickets");
  url.searchParams.set("project", activeProjectName());
  if (id) url.searchParams.set("ticket", id); else url.searchParams.delete("ticket");
  return url;
}

function selectTicket(id, navigate = true) {
  selectedTicket = id; ticketDetailSignature = "";
  if (navigate && location.href !== ticketUrl(id).href) history.pushState(null, "", ticketUrl(id));
  renderTicketDetail(); loadTicketDiscussion(id); $("ticket-back")?.focus(); $("view-tickets").scrollIntoView({block: "start"});
}

window.addEventListener("popstate", async () => {
  const route = new URL(location.href);
  const project = route.searchParams.get("project");
  if (project && project !== activeProjectName() && projects.some(item => item.name === project)) await setProject(project, false);
  setView(route.searchParams.get("view") || route.hash.slice(1) || "messages");
  if (currentView === "tickets") { selectedTicket = route.searchParams.get("ticket"); ticketDetailSignature = ""; renderTickets(); }
  else if (currentView === "messages" && route.searchParams.get("message")) showMessage(route.searchParams.get("message"));
});

function renderTickets() {
  $("ticket-count").textContent = String(state.tickets.length);
  $("nav-tickets-count").textContent = String(state.tickets.filter(item => !TICKET_TERMINAL.has(item.stage)).length);
  syncOptions($("filter-tassignee"), [...new Set(state.tickets.map(item => item.assignee).filter(Boolean))]);
  const parent = $("ticket-parent"); const parentValue = parent.value;
  parent.innerHTML = '<option value="">No parent</option>' + state.tickets.map(item => `<option value="${escapeText(item.id)}">${escapeText(item.display_id || item.id)} · ${escapeText(item.title)}</option>`).join("");
  parent.value = parentValue;
  renderTicketKanban();
  if (selectedTicket && !ticketsById().has(selectedTicket)) selectedTicket = null;
  renderTicketDetail();
  if (selectedTicket) loadTicketDiscussion(selectedTicket);
  updateVisibleTimes();
}

function openTicketForm() { ticketFormOpener = document.activeElement; $("ticket-form").classList.remove("hidden"); $("ticket-form-error").textContent = ""; $("ticket-form").reset(); $("ticket-form").scrollIntoView({block: "nearest"}); $("ticket-title").focus(); }
function closeTicketForm() { $("ticket-form").classList.add("hidden"); (ticketFormOpener?.isConnected ? ticketFormOpener : $("new-ticket"))?.focus(); ticketFormOpener = null; }

// ---------- refresh loop ----------

async function refresh(){
  if(refreshInFlight)return;refreshInFlight=true;
  const generation=++refreshGeneration;
  refreshAbort=new AbortController();
  try{
    const next=await fetchBoardState(refreshAbort.signal); const away=$("message-list").scrollTop>32;
    if(generation!==refreshGeneration)return;
    const nextIds=new Set(next.messages.map(item=>item.id));
    if(initialized&&away){
      next.messages.forEach(item=>{if(!knownMessageIds.has(item.id))pendingMessageIds.add(item.id);});
      deferredMessagePage={messages:next.messages,messages_meta:next.messages_meta};
      next.messages=state.messages;next.messages_meta=state.messages_meta;
    }else{deferredMessagePage=null;pendingMessageIds.clear();}
    knownMessageIds=nextIds;state={...emptyState,...next};initialized=true;refreshFailures=0;lastUpdated=new Date();setConnection("Live");
    selectedIds=selectionPrune(selectedIds,nextIds);if(!selectedIds.has(selectionAnchor))selectionAnchor=null;
    populateChoices();renderBacklog();renderPresence();renderMessages();reconcileMessageDetail(selectedMessageValue);renderRoadmap();renderTickets();updateVisibleTimes();
  }catch(error){if(error.stale||generation!==refreshGeneration)return;refreshFailures+=1;setConnection(refreshFailures>=3?"Offline":"Reconnecting");if(refreshFailures===1||refreshFailures===3)console.warn("Agent board refresh failed",error);}
  finally{refreshInFlight=false;}
}

// One project, or every project merged into one inbox. Each project's messages carry their own project name.
async function fetchProjectState(name,signal){
  const value=await api(`/api/state?standby=${encodeURIComponent(standbyHours())}&project=${encodeURIComponent(name)}`,{signal});
  value.messages.forEach(item=>{item.project=value.project||name;});
  return value;
}

async function fetchBoardState(signal){
  if(activeProject!==ALL_PROJECTS||!multiProject())return fetchProjectState(activeProjectName(),signal);
  const values=await Promise.all(projects.map(item=>fetchProjectState(item.name,signal)));
  const messages=values.flatMap(value=>value.messages).sort((a,b)=>b.created_at.localeCompare(a.created_at));
  const meta=values.reduce((sum,value)=>({total:sum.total+value.messages_meta.total,returned:sum.returned+value.messages_meta.returned,has_more:sum.has_more||value.messages_meta.has_more,malformed:sum.malformed+(value.messages_meta.malformed||0),oversized:sum.oversized+(value.messages_meta.oversized||0)}),{total:0,returned:0,has_more:false,malformed:0,oversized:0});
  return {...values[0],messages,messages_meta:meta,project:ALL_PROJECTS};
}

// Live without a restart: poll the cheap change token, fetch the whole state only when the store actually moved.
// The token also carries ui_version (mtimes of the served HTML/JS/CSS), which raises the reload banner.
async function pollVersion(){
  clearTimeout(versionTimer);
  if(document.hidden){scheduleVersionPoll();return;}
  try{
    const value=await api("/api/version");
    if(uiVersion===null)uiVersion=value.ui_version;else if(value.ui_version!==uiVersion)markUiOutdated();
    const token=activeProject===ALL_PROJECTS?JSON.stringify(value.projects||{}):(value.projects||{})[activeProjectName()]??value.data_version;
    if(token!==dataVersion){dataVersion=token;await refresh();}
    else{refreshFailures=0;setConnection("Live");}
  }catch(error){if(error.stale)return;refreshFailures+=1;setConnection(refreshFailures>=3?"Offline":"Reconnecting");if(refreshFailures===1)console.warn("Agent board version poll failed",error);}
  finally{scheduleVersionPoll();}
}

function scheduleVersionPoll(){clearTimeout(versionTimer);const base=POLL_SECONDS*1000;const delay=document.hidden?Math.max(30000,base*3):refreshFailures?Math.min(60000,base*(2**refreshFailures)):base;versionTimer=setTimeout(pollVersion,delay);}

// Never auto-reload: the operator may hold a selection or a half-written message. The banner waits for a click.
function markUiOutdated(){if(uiOutdated)return;uiOutdated=true;$("ui-update").classList.remove("hidden");}

// ---------- editors ----------

function parseIdList(value){return [...new Set(String(value||"").split(/[\s,;]+/).map(part=>part.trim()).filter(Boolean))];}
function openRoadmap(item=null){const d=item?derived(item):null;roadmapEditorOpener=document.activeElement;$("roadmap-form").classList.remove("hidden");$("roadmap-form-title").textContent=item?`Update ${item.id}`:"New roadmap item";$("roadmap-error").textContent="";$("roadmap-revision").value=item?.revision??0;$("roadmap-id").value=item?.id??"";$("roadmap-id").disabled=!!item;$("roadmap-title").value=item?.title??"";$("roadmap-summary").value=item?.summary??"";$("roadmap-status").value=item?.status??"NOT_STARTED";$("roadmap-owner").value=item?.owner??"unassigned";$("roadmap-progress").value=item?.progress??0;$("roadmap-blocker").value=item?.blocker??"";$("roadmap-kind").value=d&&d.kind!=="UNKNOWN"?d.kind:"";$("roadmap-depends").value=item?(item.depends_on_ids||[]).join(", "):"";$("roadmap-impact").value=d?.impact??"";$("roadmap-standby").value=item?.standby_flag?.reason??"";$("roadmap-form").dataset.baseline=JSON.stringify(editorExtension());syncRoadmapFields();$("roadmap-form").scrollIntoView({block:"nearest"});(item?$("roadmap-title"):$("roadmap-id")).focus();}
function editorExtension(){return {kind:$("roadmap-kind").value||null,depends_on:parseIdList($("roadmap-depends").value),impact:$("roadmap-impact").value.trim()||null,standby:$("roadmap-standby").value.trim()||null};}
// Only the sidecar fields the user changed travel with the POST: an untouched field must not rewrite the sidecar.
function changedExtension(){const baseline=JSON.parse($("roadmap-form").dataset.baseline||"{}");const current=editorExtension();const out={};for(const key of ["kind","depends_on","impact","standby"])if(JSON.stringify(baseline[key])!==JSON.stringify(current[key]))out[key]=current[key];return out;}
function closeRoadmapEditor(){$("roadmap-form").classList.add("hidden");const fallback=$("roadmap-panel").querySelector(`[data-id="${CSS.escape(selectedRoadmap||"")}"]`);const target=roadmapEditorOpener?.isConnected?roadmapEditorOpener:fallback;roadmapEditorOpener=null;target?.focus();}
function syncRoadmapFields(){const blocked=$("roadmap-status").value==="BLOCKED";$("roadmap-blocker").required=blocked;if(!blocked)$("roadmap-blocker").value="";if($("roadmap-status").value==="COMPLETE"||$("roadmap-status").value==="CLOSED")$("roadmap-progress").value=100;$("roadmap-standby").disabled=$("roadmap-status").value!=="IN_PROGRESS";}
// Compose rules (operator, 2026-09-01): a reply defaults to kind ANSWER, carries reply_to and the
// original workstream, and the 300-character summary cap is refused client-side before the CLI does.
const MAX_SUMMARY_CHARS = 300;

function replyDefaults(reply, identities) {
  const fromMaster = identities.includes(reply.from);
  return {
    actor: fromMaster ? reply.to : reply.from,
    to: fromMaster ? reply.from : reply.to,
    kind: "ANSWER",
    priority: "NORMAL",
    workstream: reply.workstream,
    reply_to: reply.id,
    summary: `Re: ${reply.summary}`.slice(0, MAX_SUMMARY_CHARS),
  };
}

function summaryStatus(value) {
  const length = String(value ?? "").trim().length;
  return {length, remaining: MAX_SUMMARY_CHARS - length, over: length > MAX_SUMMARY_CHARS, empty: length === 0};
}

function summaryCounterText(value) {
  const status = summaryStatus(value);
  return status.over ? `${status.length} / ${MAX_SUMMARY_CHARS} — ${status.length - MAX_SUMMARY_CHARS} over the cap` : `${status.length} / ${MAX_SUMMARY_CHARS}`;
}

function updateSummaryCounter(){const status=summaryStatus($("compose-summary").value);const counter=$("compose-summary-count");counter.textContent=summaryCounterText($("compose-summary").value);counter.classList.toggle("error",status.over);$("compose-summary").setAttribute("aria-invalid",String(status.over));}
let composeTicketGeneration = 0;
async function populateComposeTickets(preferred = "") {
  const generation = ++composeTicketGeneration;
  const select = $("compose-ticket");
  const project = $("compose-project").value || activeProjectName();
  select.innerHTML = '<option value="">No linked ticket</option>' + (preferred ? `<option value="${escapeText(preferred)}">${escapeText(preferred)}</option>` : "");
  select.value = preferred; select.disabled = true;
  $("compose-ticket-note").textContent = "optional";
  try {
    const tickets = project === activeProjectName() ? state.tickets : (await api(withProject("/api/tickets", project))).tickets;
    if (generation !== composeTicketGeneration) return;
    select.innerHTML = '<option value="">No linked ticket</option>' + tickets.map(item => `<option value="${escapeText(item.id)}">${escapeText(item.display_id || item.id)} · ${escapeText(item.title)}</option>`).join("");
    if (preferred && !tickets.some(item => item.id === preferred)) select.add(new Option(preferred, preferred));
    select.value = preferred;
  } catch {
    if (generation === composeTicketGeneration) $("compose-ticket-note").textContent = "Ticket list unavailable; existing link retained";
  } finally { if (generation === composeTicketGeneration) select.disabled = false; }
}

function openCompose(reply=null){$("compose-form").reset();$("compose-project").value=reply?.project || (reply ? messageProject(reply.id) : activeProjectName());$("compose-result").textContent="";$("reply-to").value="";$("compose-priority").value="NORMAL";const note=$("compose-reply-note");note.classList.toggle("hidden",!reply);if(reply){const defaults=replyDefaults(reply,state.choices.identities);$("reply-to").value=defaults.reply_to;$("compose-actor").value=defaults.actor;$("compose-to").value=defaults.to;$("compose-kind").value=defaults.kind;$("compose-priority").value=defaults.priority;$("compose-workstream").value=defaults.workstream;$("compose-summary").value=defaults.summary;note.textContent=`Replying to ${reply.id} · ${actorText(reply.from)} → ${actorText(reply.to)} · kind defaults to ANSWER (change it if this is not an answer)`;}populateComposeTickets(reply?.ticket_id || "");updateSummaryCounter();$("compose-dialog").showModal();(reply?$("compose-body"):$("compose-summary")).focus();}

// ---------- keyboard ----------

function listRowsForView(){return currentView==="messages"?[...$("message-list").querySelectorAll(".message-row .row-main")]:currentView==="roadmap"?[...$("roadmap-panel").querySelectorAll('[role="option"]')]:currentView==="tickets" && !selectedTicket?[...$("ticket-panel").querySelectorAll('[role="option"]')]:[];}
function focusedMessageId(){return document.activeElement?.closest?.(".message-row")?.dataset.id||null;}
function moveSelection(delta){const rows=listRowsForView();if(!rows.length)return;const focused=rows.indexOf(document.activeElement);const currentId=currentView==="messages"?selectedMessage:currentView==="tickets"?selectedTicket:selectedRoadmap;let index=focused>=0?focused:rows.findIndex(row=>(row.closest("[data-id]")||row).dataset.id===currentId);index=index<0?(delta>0?0:rows.length-1):Math.max(0,Math.min(rows.length-1,index+delta));rows[index].focus();rows[index].scrollIntoView({block:"nearest"});}

document.addEventListener("keydown",event=>{
  const target=event.target;
  const editing=(target instanceof Element&&target.matches("input, textarea, select, [contenteditable]"))||document.querySelector("dialog[open]");
  if(event.key==="Escape"&&!editing&&currentView==="messages"&&clearSelection()){event.preventDefault();return;}
  if(editing)return;
  if(event.ctrlKey||event.metaKey||event.altKey)return;
  switch(event.key){
    case "1":setView("messages",{focus:true});break;
    case "2":setView("roadmap",{focus:true});break;
    case "3":setView("tickets",{focus:true});break;
    case "4":setView("presence",{focus:true});break;
    case "/":event.preventDefault();(currentView==="roadmap"?$("roadmap-search"):currentView==="tickets"?$("ticket-search"):$("search")).focus();break;
    case "j":event.preventDefault();moveSelection(1);break;
    case "k":event.preventDefault();moveSelection(-1);break;
    case "x":if(currentView==="messages"){const id=focusedMessageId()||selectedMessage;if(id){event.preventDefault();toggleChecked(id,{range:event.shiftKey});}}break;
    case "y":if(currentView==="messages"){event.preventDefault();if(selectedIds.size)copySelectedBodies();else{const id=focusedMessageId()||selectedMessage;if(id)copyMessageBody(id);}}break;
    case "c":if(currentView==="messages"){event.preventDefault();openCompose();}break;
    case "a":if(currentView==="messages"){event.preventDefault();acknowledgeSelected();}break;
    case "t":event.preventDefault();applyTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");break;
    case "?":event.preventDefault();$("shortcuts-dialog").showModal();break;
    default:return;
  }
});

// ---------- wiring ----------

document.querySelectorAll(".views button").forEach(button=>button.addEventListener("click",()=>setView(button.dataset.view)));
window.addEventListener("hashchange",()=>setView(location.hash.slice(1)));
$("theme-toggle").onclick=()=>applyTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");
async function loadMessagesFolderPath(){
  try{
    const value=await api(withProject("/api/messages-folder"));
    $("open-messages-folder").title=value.path;
  }catch(error){/* tooltip is a nicety; a failed lookup just leaves it blank */}
}
$("shortcuts-button").onclick=()=>{$("shortcuts-dialog").showModal();loadMessagesFolderPath();};$("close-shortcuts").onclick=()=>$("shortcuts-dialog").close();
$("open-messages-folder").onclick=async()=>{
  $("open-messages-folder-result").textContent="";
  try{
    const response=await fetch(withProject("/api/open-messages-folder"),{method:"POST",headers:{"X-Agent-Board-Token":token}});
    if(!response.ok){const value=await response.json().catch(()=>({}));throw new Error(value.error||`HTTP ${response.status}`);}
    toast("Opened the messages folder in Explorer");
  }catch(error){$("open-messages-folder-result").textContent=error.message||"Could not open the messages folder";}
};
$("new-message-pill").onclick=()=>{if(deferredMessagePage){state={...state,...deferredMessagePage};knownMessageIds=new Set(state.messages.map(item=>item.id));deferredMessagePage=null;}pendingMessageIds.clear();renderMessages();$("message-list").scrollTo({top:0,behavior:"smooth"});};
$("mode-threads").onclick=()=>{listMode="threads";store(STORAGE.lmode,listMode);pruneSelectionToVisible();renderMessages();};$("mode-flat").onclick=()=>{listMode="flat";store(STORAGE.lmode,listMode);pruneSelectionToVisible();renderMessages();};
$("clear-filters").onclick=()=>{["filter-actor","filter-kind","filter-workstream","filter-priority","filter-ack","search"].forEach(id=>$(id).value="");pruneSelectionToVisible();renderMessages();renderBacklog();};
["filter-actor","filter-kind","filter-workstream","filter-priority","filter-ack"].forEach(id=>$(id).addEventListener("change",()=>{pruneSelectionToVisible();renderMessages();renderBacklog();}));
$("search").addEventListener("input",()=>{pruneSelectionToVisible();renderMessages();});
$("unacked-first").checked=store(STORAGE.unacked)!=="false";
$("unacked-first").addEventListener("change",()=>{store(STORAGE.unacked,String($("unacked-first").checked));pruneSelectionToVisible();renderMessages();});
$("copy-selected").onclick=copySelectedBodies;
$("select-shown").onclick=()=>{selectedIds=selectionAdd(selectedIds,messageRowIds());renderMessages();};
$("clear-selection").onclick=clearSelection;
$("project-select").addEventListener("change",()=>setProject($("project-select").value));
$("ui-update").onclick=()=>location.reload();
// One listener for the whole list: rows are recycled by reconcileKeyed, so per-row handlers would pile up.
$("message-list").addEventListener("click",event=>{
  const target=event.target;if(!(target instanceof Element))return;
  const row=target.closest(".message-row");if(!row||!row.dataset.id)return;
  if(target.closest(".row-copy")){event.stopPropagation();copyMessageBody(row.dataset.id);return;}
  if(target.matches(".row-check input")){event.preventDefault();toggleChecked(row.dataset.id,{range:event.shiftKey});return;}
  if(target.closest(".row-main"))showMessage(row.dataset.id);
});
["rmode-overview","rmode-tree","rmode-kanban","rmode-timeline"].forEach(id=>$(id).onclick=()=>setRoadmapMode(id.replace("rmode-","")));
["signal-startable","signal-standby","signal-blocked"].forEach(id=>$(id).onclick=()=>focusOverviewCard($(id).dataset.focus));
$("roadmap-since").addEventListener("change",renderRoadmap);$("roadmap-show-closed").addEventListener("change",renderRoadmap);$("roadmap-search").addEventListener("input",renderRoadmap);
["filter-rstatus","filter-rowner","filter-rkind"].forEach(id=>$(id).addEventListener("change",renderRoadmap));
const storedStandby=store(STORAGE.standby);if(storedStandby&&[...$("standby-hours").options].some(option=>option.value===storedStandby))$("standby-hours").value=storedStandby;
$("standby-hours").addEventListener("change",()=>{store(STORAGE.standby,$("standby-hours").value);renderSignals();refresh();});
$("new-roadmap").onclick=()=>openRoadmap();$("cancel-roadmap").onclick=closeRoadmapEditor;$("roadmap-status").addEventListener("change",syncRoadmapFields);
$("roadmap-form").addEventListener("submit",async event=>{event.preventDefault();try{const extension=changedExtension();await api("/api/roadmap",{method:"POST",body:JSON.stringify({project:activeProjectName(),actor:$("roadmap-actor").value,id:$("roadmap-id").value,title:$("roadmap-title").value,summary:$("roadmap-summary").value,status:$("roadmap-status").value,owner:$("roadmap-owner").value,progress:Number($("roadmap-progress").value),blocker:$("roadmap-blocker").value,expected_revision:Number($("roadmap-revision").value),...extension})});const id=$("roadmap-id").value;closeRoadmapEditor();toast("Roadmap item saved");await refresh();selectRoadmap(id);}catch(error){$("roadmap-error").textContent=error.message;}});
$("new-ticket").onclick=()=>openTicketForm();$("cancel-ticket").onclick=closeTicketForm;
$("ticket-form").addEventListener("submit",async event=>{event.preventDefault();try{const created=await ticketApi("/api/tickets",{actor:$("ticket-actor").value,title:$("ticket-title").value,kind:$("ticket-kind").value,summary:$("ticket-summary").value,body:$("ticket-body").value,parent:$("ticket-parent").value||undefined,assignee:$("ticket-assignee").value||undefined,reviewer:$("ticket-reviewer").value||undefined});const id=created.ticket.id;closeTicketForm();toast(`${created.ticket.display_id || "Ticket"} created`);await refresh();selectTicket(id);}catch(error){$("ticket-form-error").textContent=error.message;}});
["filter-tassignee","ticket-show-closed"].forEach(id=>$(id).addEventListener("change",renderTickets));
$("ticket-search").addEventListener("input",renderTickets);
$("show-compose").onclick=()=>openCompose();$("close-compose").onclick=()=>$("compose-dialog").close();
$("compose-summary").addEventListener("input",updateSummaryCounter);
$("compose-project").addEventListener("change",()=>populateComposeTickets());
$("compose-form").addEventListener("submit",async event=>{event.preventDefault();const result=$("compose-result");result.className="";const status=summaryStatus($("compose-summary").value);if(status.over||status.empty){result.className="error";result.textContent=status.empty?"Summary is required: the one line a reader sees in the list.":`Summary is ${status.length} characters, ${status.length-MAX_SUMMARY_CHARS} over the ${MAX_SUMMARY_CHARS}-character cap. Move the detail into the body.`;$("compose-summary").focus();return;}try{const value=await api("/api/messages",{method:"POST",body:JSON.stringify({project:$("compose-project").value||activeProjectName(),actor:$("compose-actor").value,to:$("compose-to").value,kind:$("compose-kind").value,priority:$("compose-priority").value,workstream:$("compose-workstream").value,summary:$("compose-summary").value,body:$("compose-body").value,reply_to:$("reply-to").value||null,ticket_id:$("compose-ticket").value||null,requires_ack:$("compose-ack").checked})});result.textContent=value.results.map(item=>`${item.recipient}: ${item.ok?"sent":item.error}`).join("; ");if(value.ok){setTimeout(()=>$("compose-dialog").close(),600);await refresh();}}catch(error){result.className="error";result.textContent=error.message;}});
document.addEventListener("visibilitychange",()=>{if(document.hidden)scheduleVersionPoll();else pollVersion();});

// Deep links: ?view=roadmap&rmode=overview&theme=light&since=72&standby=72&closed=1&item=<id>&message=<id>
const params=new URLSearchParams(location.search);
initTheme();
if(params.get("theme")==="light"||params.get("theme")==="dark")applyTheme(params.get("theme"));
if(["overview","tree","kanban","timeline"].includes(params.get("rmode")||""))roadmapMode=params.get("rmode");
if([...$("roadmap-since").options].some(option=>option.value===params.get("since")))$("roadmap-since").value=params.get("since");
if([...$("standby-hours").options].some(option=>option.value===params.get("standby")))$("standby-hours").value=params.get("standby");
if(params.get("closed")==="1")$("roadmap-show-closed").checked=true;
setView(params.get("view")||location.hash.slice(1)||store(STORAGE.view)||"messages");
loadProjects().then(refresh).then(()=>{if(params.get("item"))selectRoadmap(params.get("item"));if(params.get("message"))showMessage(params.get("message"));if(params.get("ticket"))selectTicket(params.get("ticket"),false);}).finally(pollVersion);
