"use strict";

// Agent Board — inbox multi-select, one source of truth (pure; node-tested by tools/test_agent_board_web_selection.py).
// Every function takes and returns a Set of message ids: the DOM is never read to decide what is selected.

function selectionToggle(selected, id, {range = false, anchor = null, orderedIds = []} = {}) {
  const next = new Set(selected);
  const from = range && anchor !== null ? orderedIds.indexOf(anchor) : -1;
  const to = range ? orderedIds.indexOf(id) : -1;
  if (from >= 0 && to >= 0) {
    const [low, high] = from <= to ? [from, to] : [to, from];
    const add = !next.has(id);
    for (const member of orderedIds.slice(low, high + 1)) add ? next.add(member) : next.delete(member);
  } else if (next.has(id)) next.delete(id);
  else next.add(id);
  return {selected: next, anchor: id};
}

function selectionAdd(selected, ids) {
  const next = new Set(selected);
  for (const id of ids) next.add(id);
  return next;
}

// Prune keeps order-independent membership: an id absent from `validIds` is dropped, every other id survives.
function selectionPrune(selected, validIds) {
  const valid = validIds instanceof Set ? validIds : new Set(validIds);
  return new Set([...selected].filter(id => valid.has(id)));
}

function selectionClear() {
  return new Set();
}

// Copy order is the board's chronological order (created_at, then id), never the on-screen order.
function selectionCopyOrder(selected, messages) {
  return messages
    .filter(item => selected.has(item.id))
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id)))
    .map(item => item.id);
}

function selectionCountLabel(selected) {
  return `${selected.size} selected`;
}
