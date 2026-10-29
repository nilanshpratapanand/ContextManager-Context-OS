/* ContextOS web edition: the engine in the browser.
 *
 * A JavaScript port of the Python engine (units, store, retrieval, budget, handoff,
 * router, chat turn, provider streaming) plus a `fetch` shim that answers the same
 * /api/* calls as contextos/server.py, so dashboard.html runs unchanged on a static
 * host. Everything stays in this browser: chats and memory in localStorage, API keys
 * in localStorage and sent only to the provider they belong to.
 *
 * Not in the web edition: Build mode, MCP connectors, skills, file attachments and
 * Pipeline mode. They need a machine of their own and live in the desktop app.
 *
 * Keep behaviour in step with the Python files named in each section header.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else { root.ContextOSWeb = api; api.install(root); }
})(typeof self !== 'undefined' ? self : this, function () {
'use strict';

const VERSION = '0.3.0';
// Strictly increasing, so units written in the same millisecond still order by recency
// (Python's time.time() has microsecond resolution; reflect and retrieval rely on that order).
let lastSec = 0;
const nowSec = () => { let t = Date.now() / 1000; if (t <= lastSec) t = lastSec + 1e-6; return (lastSec = t); };
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const uid = () => {
  const b = new Uint8Array(6);
  (globalThis.crypto || require('node:crypto').webcrypto).getRandomValues(b);
  return [...b].map(x => x.toString(16).padStart(2, '0')).join('');
};

// ============================================================ units.py
const ROOTS = ['user', 'project', 'task', 'agent', 'tool', 'artifact'];
const KINDS = ['goal', 'constraint', 'decision', 'fact', 'blocker', 'artifact', 'tool_result', 'preference'];
const LIFETIMES = ['permanent', 'task', 'ephemeral'];
const SEG = /^[a-z0-9][a-z0-9_.\-]*$/;
const MAX_DEPTH = 8;

class AddressError extends Error {}

function normalizeAddress(address) {
  if (typeof address !== 'string' || !address.trim()) throw new AddressError('address must be a non-empty string');
  const a = '/' + address.trim().replace(/^\/+|\/+$/g, '').toLowerCase();
  const parts = a.split('/').filter(Boolean);
  if (!parts.length) throw new AddressError('address must have at least a root segment');
  if (!ROOTS.includes(parts[0])) throw new AddressError(`root '${parts[0]}' not in ${ROOTS}`);
  if (parts.length > MAX_DEPTH) throw new AddressError(`address deeper than ${MAX_DEPTH} segments`);
  for (const p of parts) if (!SEG.test(p)) throw new AddressError(`illegal segment '${p}'`);
  return '/' + parts.join('/');
}
const addressSegments = a => a.split('/').filter(Boolean);

// chars/4, rounded up: the same conservative estimate the Python side uses without tiktoken.
const countTokens = text => (text ? Math.max(1, Math.floor((text.length + 3) / 4)) : 0);

function renderUnit(u) {
  let head = `${u.address} [${u.kind}]`;
  if (u.kind === 'artifact' && u.meta && u.meta.path) {
    head += ` path=${u.meta.path}`;
    if (u.meta.sha256) head += ` sha256=${String(u.meta.sha256).slice(0, 12)}`;
  }
  return `${head}\n${u.value}`;
}

function makeUnit(o) {
  const t = nowSec();
  const u = {
    address: normalizeAddress(o.address), value: o.value, kind: o.kind || 'fact',
    source: o.source || 'unknown', lifetime: o.lifetime || 'task',
    importance: Math.min(1, Math.max(0, Number(o.importance ?? 0.5))),
    confidence: Math.min(1, Math.max(0, Number(o.confidence ?? 1))),
    valid_from: o.valid_from ?? t, valid_to: o.valid_to ?? null,
    created_at: o.created_at ?? t, updated_at: o.updated_at ?? t,
    version: o.version ?? 1, tokens: 0, pinned: !!o.pinned, meta: o.meta || {},
  };
  if (!KINDS.includes(u.kind)) throw new Error(`kind '${u.kind}' not in ${KINDS}`);
  if (!LIFETIMES.includes(u.lifetime)) throw new Error(`lifetime '${u.lifetime}' not in ${LIFETIMES}`);
  u.tokens = countTokens(renderUnit(u));
  return u;
}

// ============================================================ store.py
const WORD = /[A-Za-z0-9_]+/g;
const words = text => (String(text || '').match(WORD) || []);

class Store {
  constructor(data) {
    this.units = new Map();          // address -> unit (live or superseded)
    this.history = [];
    this.conflictRows = [];
    this._nextConflict = 1;
    if (data) {
      for (const u of data.units || []) this.units.set(u.address, u);
      this.history = data.history || [];
      this.conflictRows = data.conflicts || [];
      this._nextConflict = data.next_conflict || this.conflictRows.length + 1;
    }
    this.onChange = null;
  }

  toJSON() {
    return { units: [...this.units.values()], history: this.history,
             conflicts: this.conflictRows, next_conflict: this._nextConflict };
  }
  _changed() { if (this.onChange) this.onChange(); }

  put(address, value, o = {}) {
    address = normalizeAddress(address);
    const t = nowSec();
    const prev = this.get(address);
    if (prev && prev.value.trim() === String(value).trim()) {
      prev.updated_at = t; this._changed(); return prev;
    }
    let version = 1, created = t;
    if (prev) {
      version = prev.version + 1; created = prev.created_at;
      this.history.push({ address: prev.address, value: prev.value, kind: prev.kind, source: prev.source,
                          version: prev.version, valid_from: prev.valid_from, valid_to: t, archived_at: t });
      if (this.history.length > 2000) this.history.splice(0, this.history.length - 2000);
      const source = o.source || 'unknown';
      if (prev.source !== source && !o.supersede) {
        this.conflictRows.push({ id: this._nextConflict++, address, old_value: prev.value, old_source: prev.source,
                                 new_value: value, new_source: source, detected_at: t, resolved: 0 });
      }
    }
    const unit = makeUnit({ ...o, address, value, valid_from: t, valid_to: null,
                            created_at: created, updated_at: t, version });
    this.units.set(address, unit);
    this._changed();
    return unit;
  }

  supersede(address, when) {
    const u = this.get(address);
    if (!u) return false;
    u.valid_to = when || nowSec();
    this._changed();
    return true;
  }

  delete(address) {
    address = normalizeAddress(address);
    const had = this.units.delete(address);
    this._changed();
    return had;
  }

  get(address, includeSuperseded = false) {
    address = normalizeAddress(address);
    const u = this.units.get(address);
    if (!u) return null;
    return includeSuperseded || u.valid_to === null ? u : null;
  }

  list(prefix = '', { liveOnly = true, kinds = null, limit = 10000 } = {}) {
    let out = [...this.units.values()];
    if (prefix) {
      const p = '/' + prefix.replace(/^\/+|\/+$/g, '').toLowerCase();
      out = out.filter(u => u.address === p || u.address.startsWith(p + '/'));
    }
    if (liveOnly) out = out.filter(u => u.valid_to === null);
    if (kinds) out = out.filter(u => kinds.includes(u.kind));
    out.sort((a, b) => cmp(a.address, b.address));
    return out.slice(0, limit);
  }

  conflicts(openOnly = true) {
    return this.conflictRows.filter(c => !openOnly || !c.resolved).slice().reverse();
  }

  stats() {
    const live = [...this.units.values()].filter(u => u.valid_to === null);
    const all = [...this.units.values()];
    const byKind = {};
    for (const u of live) byKind[u.kind] = (byKind[u.kind] || 0) + 1;
    return { live_units: live.length, live_tokens: live.reduce((s, u) => s + u.tokens, 0),
             all_units: all.length, all_tokens: all.reduce((s, u) => s + u.tokens, 0),
             by_kind: byKind, open_conflicts: this.conflictRows.filter(c => !c.resolved).length };
  }

  // BM25 in place of SQLite FTS5 (k1 1.2, b 0.75, FTS5's idf). Returns [address, score] best first.
  bm25(query, limit) {
    const q = [...new Set(words(query).map(w => w.toLowerCase()).filter(w => w.length > 1))].slice(0, 32);
    const terms = [...new Set(q.flatMap(w => w.split('_').filter(Boolean)))];
    if (!terms.length) return [];
    const docs = [];
    for (const u of this.units.values()) {
      const toks = (u.address.replace(/\//g, ' ') + ' ' + u.value).toLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
      docs.push({ address: u.address, toks });
    }
    const N = docs.length;
    if (!N) return [];
    const avg = docs.reduce((s, d) => s + d.toks.length, 0) / N || 1;
    const df = Object.fromEntries(terms.map(t => [t, 0]));
    for (const d of docs) { const set = new Set(d.toks); for (const t of terms) if (set.has(t)) df[t]++; }
    const out = [];
    for (const d of docs) {
      let score = 0;
      const tf = {};
      for (const tok of d.toks) tf[tok] = (tf[tok] || 0) + 1;
      for (const t of terms) {
        if (!tf[t]) continue;
        let idf = Math.log((N - df[t] + 0.5) / (df[t] + 0.5));
        if (idf <= 0) idf = 1e-6;
        score += idf * (tf[t] * 2.2) / (tf[t] + 1.2 * (1 - 0.75 + 0.75 * d.toks.length / avg));
      }
      if (score > 0) out.push([d.address, score]);
    }
    out.sort((a, b) => b[1] - a[1] || cmp(a[0], b[0]));
    return out.slice(0, limit);
  }
}

// ============================================================ retrieval.py
const RRF_K = 60;
const ROOT_HINTS = {
  user: 'user', preference: 'user', preferences: 'user', profile: 'user',
  project: 'project', architecture: 'project', requirement: 'project', requirements: 'project',
  decision: 'project', decided: 'project',
  task: 'task', goal: 'task', progress: 'task', blocker: 'task', blocked: 'task', next: 'task',
  tool: 'tool', search: 'tool', query: 'tool',
  file: 'artifact', code: 'artifact', artifact: 'artifact', diff: 'artifact', patch: 'artifact',
  implementation: 'artifact', agent: 'agent',
};
const KIND_HINTS = {
  block: 'blocker', blocker: 'blocker', stuck: 'blocker', problem: 'blocker',
  decid: 'decision', decision: 'decision', chose: 'decision', choose: 'decision', pick: 'decision', why: 'decision',
  goal: 'goal', objective: 'goal', trying: 'goal',
  constraint: 'constraint', requir: 'constraint', must: 'constraint', rule: 'constraint', allow: 'constraint',
  file: 'artifact', code: 'artifact', wrote: 'artifact', implement: 'artifact', prefer: 'preference',
};
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

function stem(word) {
  const w = word.toLowerCase();
  for (const suf of ['ations', 'ation', 'ing', 'ers', 'er', 'ed', 'es', 's']) {
    if (w.length - suf.length >= 4 && w.endsWith(suf)) return w.slice(0, -suf.length);
  }
  return w;
}
function hintedKinds(query) {
  const kinds = new Set();
  for (const w of words(query)) for (const probe of [w.toLowerCase(), stem(w)]) if (has(KIND_HINTS, probe)) kinds.add(KIND_HINTS[probe]);
  return kinds;
}
function route(query) {
  const roots = [];
  for (const w0 of words(query)) {
    const w = w0.toLowerCase(), s = stem(w);
    const r = (has(ROOT_HINTS, w) && ROOT_HINTS[w]) || (has(ROOT_HINTS, s) && ROOT_HINTS[s]) || (ROOTS.includes(w) ? w : null);
    if (r && !roots.includes(r)) roots.push(r);
  }
  return roots;
}
function addressOverlap(queryWords, unit) {
  const segs = new Set(addressSegments(unit.address));
  for (const s of [...segs]) segs.add(stem(s));
  if (!segs.size) return 0;
  let hits = 0;
  for (const s of segs) if (queryWords.has(s)) hits++;
  if (!hits) return 0;
  return hits / segs.size * (1 + 0.1 * segs.size);
}
function prior(unit, now) {
  const ageDays = Math.max(0, (now - unit.updated_at) / 86400);
  return unit.importance * unit.confidence * (0.5 + 0.5 * Math.exp(-ageDays / 7));
}
function rrf(ranked) {
  const scores = new Map();
  for (const lst of ranked) lst.forEach((addr, i) => scores.set(addr, (scores.get(addr) || 0) + 1 / (RRF_K + i + 1)));
  return scores;
}

function search(store, query, { k = 20, prefix = '', kinds = null, includeSuperseded = false } = {}) {
  const now = nowSec();
  const raw = words(query);
  const queryWords = new Set([...raw.map(w => w.toLowerCase()), ...raw.map(stem)]);
  const wantKinds = hintedKinds(query);
  const candidates = new Map();

  const routed = route(query);
  const scan = prefix ? [prefix] : (routed.length ? routed : ['']);
  for (const p of scan) for (const u of store.list(p, { liveOnly: !includeSuperseded, kinds })) candidates.set(u.address, u);
  if (!prefix && routed.length) for (const u of store.list('', { liveOnly: !includeSuperseded, kinds })) if (!candidates.has(u.address)) candidates.set(u.address, u);

  let bm25Rank = [];
  for (const [addr] of store.bm25(query, k * 5)) {
    const u = candidates.get(addr) || store.get(addr, includeSuperseded);
    if (!u) continue;
    if (kinds && !kinds.includes(u.kind)) continue;
    if (!includeSuperseded && u.valid_to !== null) continue;
    if (!candidates.has(addr)) candidates.set(addr, u);
    bm25Rank.push(addr);
  }
  if (prefix) {
    const p = '/' + prefix.replace(/^\/+|\/+$/g, '').toLowerCase();
    for (const a of [...candidates.keys()]) if (!(a === p || a.startsWith(p + '/'))) candidates.delete(a);
    bm25Rank = bm25Rank.filter(a => candidates.has(a));
  }
  if (!candidates.size) return [];

  const addrs = [...candidates.keys()];
  const overlap = new Map(addrs.map(a => [a, addressOverlap(queryWords, candidates.get(a))]));
  const addrRank = addrs.slice().sort((a, b) => overlap.get(b) - overlap.get(a)).filter(a => overlap.get(a) > 0);
  const priorRank = addrs.slice().sort((a, b) => prior(candidates.get(b), now) - prior(candidates.get(a), now));
  let kindRank = [];
  if (wantKinds.size) kindRank = addrs.filter(a => wantKinds.has(candidates.get(a).kind))
    .sort((a, b) => prior(candidates.get(b), now) - prior(candidates.get(a), now));

  const fused = rrf([addrRank, bm25Rank, kindRank, priorRank].filter(r => r.length));
  const topPrior = new Set(priorRank.slice(0, Math.max(10, k)));
  const hits = [];
  for (const [addr, score] of fused) {
    const why = [];
    if (addrRank.includes(addr)) why.push('addr');
    if (bm25Rank.includes(addr)) why.push('bm25');
    if (kindRank.includes(addr)) why.push('kind');
    if (topPrior.has(addr)) why.push('prior');
    hits.push({ unit: candidates.get(addr), score, why: why.join('+') || 'prior' });
  }
  hits.sort((a, b) => b.score - a.score || cmp(a.unit.address, b.unit.address));
  return hits.slice(0, k);
}

function getOrSearch(store, query, opts) {
  let u = null;
  try { u = store.get(query); } catch { u = null; }
  return u ? [{ unit: u, score: 1, why: 'exact' }] : search(store, query, opts);
}

// ============================================================ budget.py
const TIER0_KINDS = ['goal', 'constraint'];

function pack(candidates, budgetTokens, { mustInclude = [], tokensStored = 0, allowKinds = null } = {}) {
  let scored = candidates.map(c => (c.unit ? [c.score, c.unit] : [c.importance, c]));
  if (allowKinds) scored = scored.filter(([, u]) => allowKinds.includes(u.kind));
  const byAddr = new Map();
  for (const [s, u] of scored) if (!byAddr.has(u.address) || s > byAddr.get(u.address)[0]) byAddr.set(u.address, [s, u]);
  for (const u of mustInclude) if (!byAddr.has(u.address)) byAddr.set(u.address, [Infinity, u]);

  const t0 = [], t1 = [], t2 = [];
  for (const [s, u] of byAddr.values()) {
    if (u.pinned || TIER0_KINDS.includes(u.kind) || s === Infinity) t0.push([s, u]);
    else if (u.kind === 'artifact') t1.push([s, u]);
    else t2.push([s, u]);
  }
  t0.sort((a, b) => b[1].importance - a[1].importance || cmp(a[1].address, b[1].address));
  t1.sort((a, b) => b[0] - a[0] || cmp(a[1].address, b[1].address));
  t2.sort((a, b) => (b[0] / Math.max(1, b[1].tokens)) - (a[0] / Math.max(1, a[1].tokens)) || cmp(a[1].address, b[1].address));

  const sel = { units: [], omitted: [], tokens_selected: 0, tokens_stored: tokensStored || [...byAddr.values()].reduce((s, [, u]) => s + u.tokens, 0), overflow: false };
  let used = 0;
  for (const [, u] of t0) { sel.units.push(u); used += u.tokens; }
  if (used > budgetTokens) sel.overflow = true;
  for (const tier of [t1, t2]) for (const [s, u] of tier) {
    if (used + u.tokens <= budgetTokens) { sel.units.push(u); used += u.tokens; }
    else sel.omitted.push({ address: u.address, kind: u.kind, tokens: u.tokens, score: Math.round(s * 1e5) / 1e5, why: 'budget' });
  }
  sel.units.sort((a, b) => cmp(a.kind, b.kind) || cmp(a.address, b.address));
  sel.tokens_selected = used;
  sel.addresses = () => sel.units.map(u => u.address);
  return sel;
}

function renderUnits(units, header = '') {
  const parts = header ? [header] : [];
  let cur = null;
  for (const u of units) {
    if (u.kind !== cur) { cur = u.kind; parts.push(`\n## ${u.kind}`); }
    parts.push(renderUnit(u));
  }
  return parts.join('\n').trim();
}

// ============================================================ handoff.py
const POLICY = {
  escalate: { kinds: ['goal', 'constraint', 'blocker', 'artifact', 'preference', 'decision', 'fact'], min_importance: 0.4, budget_scale: 0.6,
    rationale: "traj-drop: state and committed decisions travel, the weak model's narrative and low-value residue do not" },
  downshift: { kinds: ['goal', 'constraint', 'blocker', 'artifact', 'preference', 'decision', 'fact', 'tool_result'], min_importance: 0.0, budget_scale: 1.0,
    rationale: "trajectory-preserving: removing the strong model's guidance drops recovery to 28-53%, so it is kept" },
  lateral: { kinds: ['goal', 'constraint', 'blocker', 'artifact', 'preference', 'decision', 'fact'], min_importance: 0.2, budget_scale: 0.8,
    rationale: 'peer transfer: state plus decisions, no raw tool chatter' },
};
const DIFFICULTY_FLOOR = 0.35;
const HANDOFF_TITLES = { constraint: 'Constraints (must hold)', blocker: 'Open blockers', decision: 'Decisions already made',
  artifact: 'Work products on disk', fact: 'Established facts', tool_result: 'Relevant tool results', preference: 'User preferences' };

class HandoffPacket {
  constructor(o) {
    Object.assign(this, { direction: 'lateral', goal: '', units: [], omitted: [], from_model: '', to_model: '',
                          tokens_stored: 0, tokens_selected: 0, notes: [] }, o);
  }
  get reduction() { return this.tokens_stored ? 1 - this.tokens_selected / this.tokens_stored : 0; }
  render() {
    const L = [`# Task handoff (${this.direction})`];
    if (this.from_model || this.to_model) L.push(`_${this.from_model || '?'} -> ${this.to_model || '?'}_`);
    L.push("\nYou are continuing work already in progress. The previous agent's conversation is intentionally not included; its durable state is below.");
    L.push(`\n## Goal\n${this.goal || '(not recorded)'}`);
    for (const kind of ['constraint', 'blocker', 'decision', 'artifact', 'fact', 'tool_result', 'preference']) {
      const us = this.units.filter(u => u.kind === kind);
      if (!us.length) continue;
      L.push(`\n## ${HANDOFF_TITLES[kind]}`);
      for (const u of us) {
        if (kind === 'artifact' && u.meta && u.meta.path) L.push(`- \`${u.meta.path}\` (${u.meta.bytes || 0} bytes, sha256 ${String(u.meta.sha256 || '').slice(0, 12)}) — \`${u.address}\``);
        else L.push(`- ${u.value}  \n  \`${u.address}\``);
      }
    }
    if (this.omitted.length) {
      L.push(`\n## Not included (${this.omitted.length} units, ${this.omitted.reduce((s, o) => s + o.tokens, 0)} tokens)`);
      L.push('These exist in the context store but did not fit the budget. If you need one, fetch it by address rather than guessing:');
      for (const o of this.omitted.slice(0, 25)) L.push(`- \`${o.address}\` (${o.kind}, ${o.tokens} tok)`);
      if (this.omitted.length > 25) L.push(`- ...and ${this.omitted.length - 25} more`);
    }
    if (this.notes.length) L.push('\n## Notes\n' + this.notes.map(n => `- ${n}`).join('\n'));
    return L.join('\n');
  }
}

function classify(fromCap, toCap) {
  if (toCap - fromCap > 0.15) return 'escalate';
  if (fromCap - toCap > 0.15) return 'downshift';
  return 'lateral';
}
function shouldMigrate(direction, difficulty) {
  if (direction === 'escalate' && difficulty < DIFFICULTY_FLOOR) {
    return [false, `task difficulty ${difficulty.toFixed(2)} < ${DIFFICULTY_FLOOR}: escalation interfaces underperform on easy tasks; finish or restart instead`];
  }
  return [true, 'ok'];
}

function fullReplayTokens(store, { direction = 'lateral', goal = '', from_model = '', to_model = '' } = {}) {
  return countTokens(new HandoffPacket({ direction, goal, units: store.list('', { liveOnly: true }), from_model, to_model }).render());
}

function enforceBudget(packet, budget, protectedSet, maxRounds = 200) {
  packet.tokens_selected = countTokens(packet.render());
  if (packet.tokens_selected <= budget) return;
  const droppable = packet.units.filter(u => !protectedSet.has(u.address));
  droppable.sort((a, b) => a.importance - b.importance || b.tokens - a.tokens);
  for (let i = 0; i < maxRounds; i++) {
    if (packet.tokens_selected <= budget || !droppable.length) break;
    const victim = droppable.shift();
    packet.units = packet.units.filter(u => u.address !== victim.address);
    packet.omitted.push({ address: victim.address, kind: victim.kind, tokens: victim.tokens, score: 0, why: 'packet-budget' });
    packet.tokens_selected = countTokens(packet.render());
  }
  if (packet.tokens_selected > budget) {
    packet.notes.push(`Structural context is ${packet.tokens_selected} tokens against a ${budget}-token budget; nothing further can be dropped without losing the goal, a constraint or an open blocker.`);
    packet.tokens_selected = countTokens(packet.render());
  }
}

function buildHandoff(store, { direction = 'lateral', budgetTokens = 4000, goalAddress = '/task/goal', query = '',
                               from_model = '', to_model = '', difficulty = 1 } = {}) {
  if (!POLICY[direction]) throw new Error(`direction must be one of ${Object.keys(POLICY)}`);
  const policy = POLICY[direction];
  const effective = Math.floor(budgetTokens * policy.budget_scale);
  const goalUnit = store.get(goalAddress);
  const goal = goalUnit ? goalUnit.value : '';
  const must = store.list('', { liveOnly: true }).filter(u => u.pinned);
  if (goalUnit) must.push(goalUnit);
  must.push(...store.list('', { liveOnly: true, kinds: ['constraint'] }), ...store.list('', { liveOnly: true, kinds: ['blocker'] }));

  let hits = search(store, query || goal || 'current task state', { k: 500, kinds: policy.kinds });
  const floor = policy.min_importance;
  hits = hits.filter(h => h.unit.importance >= floor || h.unit.pinned);
  const allLive = store.list('', { liveOnly: true });
  const sel = pack(hits, effective, { mustInclude: must, allowKinds: policy.kinds });

  const included = new Set(sel.units.map(u => u.address));
  const already = new Set(sel.omitted.map(o => o.address));
  for (const u of allLive) {
    if (included.has(u.address) || already.has(u.address)) continue;
    const why = !policy.kinds.includes(u.kind) ? 'direction-policy' : u.importance < floor ? 'below-importance-floor' : 'budget';
    sel.omitted.push({ address: u.address, kind: u.kind, tokens: u.tokens, score: 0, why });
  }
  sel.omitted = sel.omitted.filter(o => !included.has(o.address));

  const notes = [policy.rationale];
  const [ok, reason] = shouldMigrate(direction, difficulty);
  if (!ok) notes.push('WARNING: ' + reason);
  const conflicts = store.conflicts(true);
  if (conflicts.length) notes.push(`${conflicts.length} unresolved context conflict(s); do not assume either value is authoritative: ` + conflicts.slice(0, 5).map(c => c.address).join(', '));
  if (sel.overflow) notes.push('Structural context alone exceeds the budget; the task statement may be over-constrained.');

  const packet = new HandoffPacket({ direction, goal, units: sel.units, omitted: sel.omitted, from_model, to_model, notes });
  enforceBudget(packet, effective, new Set(must.map(u => u.address)));
  packet.tokens_stored = fullReplayTokens(store, { direction, goal, from_model, to_model });
  return packet;
}

// ContextOS facade over one conversation's store
class Memory {
  constructor(store) { this.store = store; }
  put(a, v, o) { return this.store.put(a, v, o); }
  get(a) { return this.store.get(a); }
  delete(a) { return this.store.delete(a); }
  list(p, o) { return this.store.list(p, o); }
  stats() { return this.store.stats(); }
  conflicts() { return this.store.conflicts(true); }
  select(query, budgetTokens = 4000) {
    return pack(getOrSearch(this.store, query, { k: 200 }), budgetTokens, { tokensStored: this.stats().live_tokens });
  }
  handoff(o) { return buildHandoff(this.store, o); }
}

// ============================================================ reflect.py
// Keep a long-lived store small and honest without a model call: merge exact duplicates,
// prune old low-value tool results, drop dangling derived_from links. Nothing is deleted,
// units are superseded so history stays.
const PROTECTED_KINDS = new Set(['goal', 'constraint', 'blocker', 'decision', 'artifact', 'preference']);
const normText = v => v.replace(/\s+/g, ' ').trim().toLowerCase();
function reflect(store, { keepTool = 40, toolMaxImportance = 0.3, now = nowSec() } = {}) {
  const out = { merged: 0, pruned: 0, unlinked: 0 };
  const groups = new Map();
  for (const u of store.list()) {
    if (u.pinned || PROTECTED_KINDS.has(u.kind) || u.value.length < 12) continue;
    const k = normText(u.value);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(u);
  }
  for (const units of groups.values()) {
    if (units.length < 2) continue;
    units.sort((a, b) => b.importance - a.importance || b.updated_at - a.updated_at || cmp(a.address, b.address));
    for (const dup of units.slice(1)) if (store.supersede(dup.address, now)) out.merged++;
  }
  const tools = store.list('', { kinds: ['tool_result'] }).filter(u => !u.pinned && u.importance <= toolMaxImportance);
  tools.sort((a, b) => b.updated_at - a.updated_at);
  for (const u of tools.slice(keepTool)) if (store.supersede(u.address, now)) out.pruned++;
  const alive = new Set(store.list().map(u => u.address));
  for (const u of store.list()) {
    const links = u.meta && u.meta.derived_from;
    if (links && links.length) {
      const kept = links.filter(a => alive.has(a));
      if (kept.length !== links.length) { u.meta = { ...u.meta, derived_from: kept }; store._changed(); out.unlinked++; }
    }
  }
  return out;
}

// ============================================================ router.py
const SMART = 'smart', FAST = 'fast', DEFAULT_THRESHOLD = 0.5;
const R_HARD = /\b(prove|derive|design|architect\w*|debug\w*|optimi[sz]\w*|refactor\w*|analy[sz]\w*|compare|trade-?offs?|why|step[- ]by[- ]step|plan|planning|strateg\w*|algorithm\w*|implement\w*|calculat\w*|comput\w*|estimat\w*|evaluat\w*|diagnos\w*|migrat\w*|schema|complexity|bug|traceback|exception|reason\w*|solve|critique|review)\b/gi;
const R_CODE = /```|^\s*(def|class|import|from|public|function|const|let)\s|\bSELECT\b.+\bFROM\b|=>|;\s*$/im;
const R_CODE_ASK = /\b(function|script|program|code|method|api|endpoint|query|regex|sql|python|javascript|typescript|java|c\+\+|rust|golang|react|linked list|recursion)\b/i;
const R_MAKE = /\b(write|create|build|make|generate|fix|convert|code|port|add|update)\b/i;
const R_MATH = /%|\b(percent|tax|discount|interest|total|average|ratio|probability|profit|loss|price|cost)\b/i;
const R_CHITCHAT = /^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|cool|nice|great|good (morning|night|evening))\b/i;
const R_LIGHT = /\b(rephrase|reword|translate|summari[sz]e|tl;?dr|shorten|format|grammar|spell\w*|synonym|define|capital of)\b/i;
const R_OVERRIDE = /^\s*\/(smart|fast)\b\s*/i;

function parseOverride(prompt) {
  const m = R_OVERRIDE.exec(prompt || '');
  return m ? [m[1].toLowerCase(), prompt.slice(m[0].length)] : [null, prompt];
}
function score(prompt) {
  const text = prompt || '';
  const reasons = [];
  if (R_CHITCHAT.test(text) && text.length < 60) return [0.05, ['chit-chat']];
  let s = 0.2;
  const hard = [...new Set([...text.matchAll(R_HARD)].map(m => m[1].toLowerCase()))].sort();
  if (hard.length) { s += Math.min(0.45, 0.2 * hard.length); reasons.push('hard words: ' + hard.slice(0, 4).join(', ')); }
  if (R_CODE.test(text)) { s += 0.3; reasons.push('code'); }
  else if (R_CODE_ASK.test(text) && R_MAKE.test(text)) { s += 0.3; reasons.push('code request'); }
  if ((text.match(/\d+(?:\.\d+)?/g) || []).length >= 2 && R_MATH.test(text)) { s += 0.3; reasons.push('multi-step arithmetic'); }
  if (text.length > 300) { s += 0.1; reasons.push('long prompt'); }
  if (text.length > 1200) s += 0.15;
  const qm = (text.match(/\?/g) || []).length;
  if (qm >= 2 || (text.match(/^\s*\d+[.)]\s/gm) || []).length >= 2) { s += 0.1; reasons.push('several parts'); }
  if (R_LIGHT.test(text) && !hard.length) { s -= 0.2; reasons.push('light edit/lookup'); }
  return [Math.round(Math.max(0, Math.min(1, s)) * 100) / 100, reasons.length ? reasons : ['simple']];
}
// Sampling temperature by kind of prompt (router.temperature_for): precise work low, open-ended writing high.
const R_CREATIVE = /\b(story|poem|poetry|lyrics?|slogan|tagline|brainstorm|ideas?|names? for|creative|imagine|fiction|joke|rap|haiku)\b/i;
const R_HARD_ONE = new RegExp(R_HARD.source, 'i');
const TEMP_PRECISE = 0.2, TEMP_DEFAULT = 0.6, TEMP_CREATIVE = 0.9;
function temperatureFor(prompt) {
  const text = prompt || '';
  if (R_CREATIVE.test(text)) return TEMP_CREATIVE;
  if (R_CODE.test(text) || (R_CODE_ASK.test(text) && R_MAKE.test(text)) || ((text.match(/\d+(?:\.\d+)?/g) || []).length >= 2 && R_MATH.test(text))) return TEMP_PRECISE;
  if (R_LIGHT.test(text) && !R_HARD_ONE.test(text)) return TEMP_PRECISE;
  return TEMP_DEFAULT;
}
function decide(prompt, mode = 'auto', threshold = DEFAULT_THRESHOLD) {
  const [sc, reasons] = score(prompt);
  if (mode === SMART || mode === FAST) return { lane: mode, score: sc, reasons: [...reasons, `forced ${mode}`], forced: true };
  return { lane: sc >= threshold ? SMART : FAST, score: sc, reasons, forced: false };
}
function buildChain(lane, smart, fast, cooling = () => false) {
  const [primary, secondary] = lane === SMART ? [smart, fast] : [fast, smart];
  const seen = [];
  for (const n of [...primary, ...secondary]) if (!seen.includes(n)) seen.push(n);
  return [...seen.filter(n => !cooling(n)), ...seen.filter(n => cooling(n))];
}
const DEAD = ['401', '402', '403', '404', 'payment', 'model_not_found', 'does not exist', 'no longer available', 'invalid api key', 'unauthorized'];
const BUSY = ['503', '502', 'overloaded', 'high demand', 'unavailable', 'timed out', 'timeout', 'network'];
function cooldownFor(error) {
  const e = (error || '').toLowerCase();
  if (DEAD.some(s => e.includes(s))) return 3600;
  if (e.includes('429') || e.includes('rate limit') || e.includes('quota')) return 60;
  if (BUSY.some(s => e.includes(s))) return 30;
  return 15;
}
function retryHint(error) {
  const m = /(?:try again|retry) in\s+(?:(\d+)m)?\s*(\d+(?:\.\d+)?)\s*s/i.exec(error || '');
  return m ? Math.floor(Number(m[1] || 0) * 60 + parseFloat(m[2])) + 1 : null;
}
const DAILY = /per day|daily|RPD|requests per day|free-models-per-day/i;
class Cooldown {
  constructor(clock = () => (typeof performance !== 'undefined' ? performance.now() / 1000 : Date.now() / 1000)) {
    this.until = {}; this.strikes = {}; this.clock = clock;
  }
  hit(name, error) {
    const strikes = this.strikes[name] = (this.strikes[name] || 0) + 1;
    const base = cooldownFor(error), hint = retryHint(error);
    let secs;
    if ((error.includes('429') || error.toLowerCase().includes('quota')) && DAILY.test(error)) secs = 3600;
    else if (hint) secs = Math.min(hint, 3600);
    else secs = Math.min(base * 2 ** (strikes - 1), Math.max(base, 1800));
    this.until[name] = this.clock() + secs;
    return secs;
  }
  ok(name) { delete this.strikes[name]; }
  remaining(name) { return Math.max(0, Math.floor((this.until[name] || 0) - this.clock())); }
  active(name) { return this.remaining(name) > 0; }
  clear(names) {
    if (!names) { this.until = {}; this.strikes = {}; return; }
    for (const n of names) { delete this.until[n]; delete this.strikes[n]; }
  }
}

// ============================================================ live.py (providers)
class ProviderError extends Error {}
const U = {
  groq: 'https://api.groq.com/openai/v1/chat/completions',
  gemini: 'https://generativelanguage.googleapis.com/v1beta/models',
  openrouter: 'https://openrouter.ai/api/v1/chat/completions',
  nvidia: 'https://integrate.api.nvidia.com/v1/chat/completions',
  cloudflare: 'https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions',
  mistral: 'https://api.mistral.ai/v1/chat/completions',
  cohere: 'https://api.cohere.ai/compatibility/v1/chat/completions',
  ollama: 'http://localhost:11434/v1/chat/completions',
  anthropic: 'https://api.anthropic.com/v1/messages',
};
const P = (name, key_env, model, url, o = {}) => ({ name, key_env, model, url, style: 'openai', tier: 0.5, lane: 'smart', extra: {}, ...o });
const PROVIDERS = {
  groq: P('groq', 'GROQ_API_KEY', 'openai/gpt-oss-120b', U.groq, { tier: 0.8, extra: { reasoning_effort: 'medium' } }),
  'groq-fast': P('groq-fast', 'GROQ_API_KEY', 'openai/gpt-oss-20b', U.groq, { tier: 0.6, lane: 'fast', extra: { reasoning_effort: 'low' } }),
  gemini: P('gemini', 'GEMINI_API_KEY', 'gemini-flash-latest', U.gemini, { style: 'gemini', tier: 0.85 }),
  'gemini-fast': P('gemini-fast', 'GEMINI_API_KEY', 'gemini-flash-lite-latest', U.gemini, { style: 'gemini', tier: 0.6, lane: 'fast' }),
  openrouter: P('openrouter', 'OPENROUTER_API_KEY', 'nvidia/nemotron-3-ultra-550b-a55b:free', U.openrouter, { tier: 0.85 }),
  'openrouter-fast': P('openrouter-fast', 'OPENROUTER_API_KEY', 'z-ai/glm-5.2:free', U.openrouter, { tier: 0.55, lane: 'fast' }),
  nvidia: P('nvidia', 'NVIDIA_API_KEY', 'z-ai/glm-5.3', U.nvidia, { tier: 0.9 }),
  'nvidia-fast': P('nvidia-fast', 'NVIDIA_API_KEY', 'nvidia/nemotron-3.5-lightning-30b-a3b', U.nvidia, { tier: 0.55, lane: 'fast' }),
  cloudflare: P('cloudflare', 'CLOUDFLARE_API_KEY', '@cf/openai/gpt-oss-120b', U.cloudflare, { tier: 0.8, extra: { reasoning_effort: 'medium' } }),
  'cloudflare-fast': P('cloudflare-fast', 'CLOUDFLARE_API_KEY', '@cf/openai/gpt-oss-20b', U.cloudflare, { tier: 0.6, lane: 'fast', extra: { reasoning_effort: 'low' } }),
  mistral: P('mistral', 'MISTRAL_API_KEY', 'mistral-medium-latest', U.mistral, { tier: 0.75 }),
  'mistral-fast': P('mistral-fast', 'MISTRAL_API_KEY', 'ministral-8b-latest', U.mistral, { tier: 0.45, lane: 'fast' }),
  cohere: P('cohere', 'COHERE_API_KEY', 'command-a-plus-05-2026', U.cohere, { tier: 0.8 }),
  'cohere-fast': P('cohere-fast', 'COHERE_API_KEY', 'command-r7b-12-2024', U.cohere, { tier: 0.45, lane: 'fast' }),
  ollama: P('ollama', 'OLLAMA_API_KEY', 'llama3.1:8b', U.ollama, { tier: 0.35, lane: 'fast' }),
  anthropic: P('anthropic', 'ANTHROPIC_API_KEY', 'claude-haiku-4-5', U.anthropic, { style: 'anthropic', tier: 0.85 }),
};
const SMART_ORDER = ['groq', 'openrouter', 'cloudflare', 'cohere', 'gemini', 'mistral', 'nvidia', 'anthropic'];
const FAST_ORDER = ['groq-fast', 'openrouter-fast', 'cloudflare-fast', 'cohere-fast', 'gemini-fast', 'mistral-fast', 'nvidia-fast', 'ollama'];
const RELAY_HOSTS = ['api.groq.com', 'generativelanguage.googleapis.com', 'openrouter.ai', 'integrate.api.nvidia.com',
  'api.cloudflare.com', 'api.mistral.ai', 'api.cohere.ai', 'api.anthropic.com'];

function resolveUrl(p, env) {
  if (!p.url.includes('{account_id}')) return p.url;
  const m = /[0-9a-f]{32}/.exec(env.CLOUDFLARE_ACCOUNT_ID || '');
  if (!m) throw new ProviderError('CLOUDFLARE_ACCOUNT_ID is not set (find it on your Cloudflare dashboard URL)');
  return p.url.replace('{account_id}', m[0]);
}

const THINK_RE = /<(think|thinking|reasoning)>[\s\S]*?<\/\1>/gi;
function stripReasoning(text) {
  let out = (text || '').replace(THINK_RE, '');
  if (out.toLowerCase().includes('<think>')) out = out.split(/<think>/i)[0];
  return out.trim();
}

function hostOf(url) { try { return new URL(url).host; } catch { return url; } }

// One streaming request. `net` = { relay: bool, fetch }.
async function* sseLines(url, payload, headers, { signal, net, idleMs = 60000 }) {
  const doFetch = (net && net.fetch) || globalThis.fetch.bind(globalThis);
  const target = net && net.relay && RELAY_HOSTS.includes(hostOf(url))
    ? `/relay/${hostOf(url)}${new URL(url).pathname}${new URL(url).search}` : url;
  const ctl = new AbortController();
  const onAbort = () => ctl.abort();
  if (signal) { if (signal.aborted) ctl.abort(); else signal.addEventListener('abort', onAbort, { once: true }); }
  let timer = setTimeout(() => ctl.abort(new Error('timeout')), idleMs);
  const bump = () => { clearTimeout(timer); timer = setTimeout(() => ctl.abort(new Error('timeout')), idleMs); };
  try {
    let res;
    try {
      res = await doFetch(target, { method: 'POST', signal: ctl.signal,
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...headers }, body: JSON.stringify(payload) });
    } catch (e) {
      if (signal && signal.aborted) throw e;
      if (ctl.signal.aborted) throw new ProviderError('network: timed out waiting for the provider');
      const err = new ProviderError(`network: could not reach ${hostOf(url)} from this browser (it may block browser requests, or you are offline)`);
      err.unreachable = true;
      throw err;
    }
    if (!res.ok) {
      const detail = (await res.text().catch(() => '')).slice(0, 600);
      throw new ProviderError(`HTTP ${res.status}: ${detail.slice(0, 300)}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      let chunk;
      try { chunk = await reader.read(); }
      catch (e) {
        if (signal && signal.aborted) throw e;
        throw new ProviderError(`network: ${ctl.signal.aborted ? 'timed out' : String(e.message || e)}`);
      }
      bump();
      if (chunk.done) break;
      buf += dec.decode(chunk.value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') return;
        if (data) yield data;
      }
    }
    const tail = buf.trim();
    if (tail.startsWith('data:') && tail.slice(5).trim() && tail.slice(5).trim() !== '[DONE]') yield tail.slice(5).trim();
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
}

/** Yields ["text", chunk] or ["think", chunk]. Any failure is a ProviderError. */
async function* streamEvents(provider, system, user, env, { maxTokens = 4096, temperature = 0.7, signal, net } = {}) {
  const key = env[provider.key_env];
  if (!key) throw new ProviderError(`${provider.key_env} is not set`);
  const model = provider.model;
  let url, payload, headers;
  if (provider.style === 'gemini') {
    url = `${provider.url}/${model}:streamGenerateContent?alt=sse`;
    payload = { systemInstruction: { parts: [{ text: system }] }, contents: [{ role: 'user', parts: [{ text: user }] }],
                generationConfig: { temperature, maxOutputTokens: maxTokens } };
    headers = { 'x-goog-api-key': key };
  } else if (provider.style === 'anthropic') {
    url = resolveUrl(provider, env);
    payload = { model, system, max_tokens: maxTokens, temperature, stream: true, messages: [{ role: 'user', content: user }] };
    headers = { 'x-api-key': key, 'anthropic-version': '2023-06-01', 'anthropic-dangerous-direct-browser-access': 'true' };
  } else {
    url = resolveUrl(provider, env);
    payload = { model, stream: true, messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
                max_tokens: maxTokens, temperature, ...provider.extra };
    headers = { Authorization: `Bearer ${key}` };
  }
  let finish = null, thought = 0, texted = 0;
  for await (const data of sseLines(url, payload, headers, { signal, net })) {
    let obj;
    try { obj = JSON.parse(data); } catch { continue; }
    if (obj.error || obj.type === 'error') throw new ProviderError(`stream error: ${JSON.stringify(obj.error || obj).slice(0, 300)}`);
    if (provider.style === 'gemini') {
      for (const cand of obj.candidates || []) {
        finish = cand.finishReason || finish;
        for (const part of (cand.content && cand.content.parts) || []) {
          if (part.text) { const kind = part.thought ? 'think' : 'text'; if (kind === 'think') thought++; else texted++; yield [kind, part.text]; }
        }
      }
    } else if (provider.style === 'anthropic') {
      const delta = obj.delta || {};
      if (obj.type === 'content_block_delta') {
        if (delta.text) { texted++; yield ['text', delta.text]; }
        else if (delta.thinking) { thought++; yield ['think', delta.thinking]; }
      }
      finish = delta.stop_reason || finish;
    } else {
      for (const ch of obj.choices || []) {
        const delta = ch.delta || {};
        finish = ch.finish_reason || finish;
        const think = delta.reasoning || delta.reasoning_content;
        if (think) { thought++; yield ['think', think]; }
        if (delta.content) { texted++; yield ['text', delta.content]; }
      }
    }
  }
  if (['length', 'MAX_TOKENS', 'max_tokens'].includes(finish) && thought && !texted) {
    throw new ProviderError('ran out of output tokens while still reasoning');
  }
}

async function completeOnce(provider, system, user, env, o = {}) {
  const once = async relay => {
    let text = '';
    for await (const [kind, chunk] of streamEvents(provider, system, user, env, { maxTokens: 1500, temperature: 0, ...o, net: { ...(o.net || {}), relay } })) if (kind === 'text') text += chunk;
    return stripReasoning(text);
  };
  try { return await once(false); }
  catch (e) { if (e && e.unreachable && o.allowRelay) return once(true); throw e; }
}

// ============================================================ keys.py
const KEY_INFO = [
  { id: 'groq', label: 'Groq', env: 'GROQ_API_KEY', recommended: true, url: 'https://console.groq.com/keys', prefix: 'gsk_',
    free: 'Fastest (about 1 s replies). 30 requests/min, 1,000/day per model.', steps: ['Sign in with Google or GitHub.', 'Click Create API Key.', 'Copy the key (starts with gsk_).'] },
  { id: 'openrouter', label: 'OpenRouter', env: 'OPENROUTER_API_KEY', recommended: true, url: 'https://openrouter.ai/settings/keys', prefix: 'sk-or-',
    free: 'One key for 20 free models. 20 requests/min, 50/day.', steps: ['Sign in.', 'Click Create Key (no credit needed).', 'Copy the key (starts with sk-or-).'] },
  { id: 'cloudflare', label: 'Cloudflare Workers AI', env: 'CLOUDFLARE_API_KEY', recommended: true, url: 'https://dash.cloudflare.com/profile/api-tokens', prefix: '',
    free: '10,000 neurons/day. Needs a token and your account ID.',
    steps: ['Create Token, then use the Workers AI template.', 'Copy the token.', "Account ID: the long code in your dashboard's address bar (you can paste the whole address)."],
    extra: { env: 'CLOUDFLARE_ACCOUNT_ID', label: 'Account ID', placeholder: '32-character ID or your dashboard URL' } },
  { id: 'cohere', label: 'Cohere', env: 'COHERE_API_KEY', recommended: true, url: 'https://dashboard.cohere.com/api-keys', prefix: '',
    free: '1,000 calls/month. Non-commercial use only.', steps: ['Sign in.', 'Copy the Trial key shown on the API Keys page.'] },
  { id: 'gemini', label: 'Google Gemini', env: 'GEMINI_API_KEY', url: 'https://aistudio.google.com/app/apikey', prefix: 'AIza',
    free: 'Strong Flash models; often busy at peak hours. Pro is paid.', steps: ['Sign in with your Google account.', 'Click Create API key.', 'Copy it (starts with AIza).'] },
  { id: 'nvidia', label: 'NVIDIA', env: 'NVIDIA_API_KEY', url: 'https://build.nvidia.com/settings/api-keys', prefix: 'nvapi-',
    free: '80+ open models, 40 requests/min, free credits. Slow, so used late.', steps: ['Sign in (free developer account).', 'Click Generate API Key.', 'Copy it (starts with nvapi-).'] },
  { id: 'mistral', label: 'Mistral', env: 'MISTRAL_API_KEY', url: 'https://console.mistral.ai/api-keys', prefix: '',
    free: 'Free Experiment plan; often rate-limited.', steps: ['Sign in and choose the free Experiment plan.', 'Create a new key and copy it.'] },
  { id: 'ollama', label: 'Ollama (this computer)', env: 'OLLAMA_API_KEY', url: 'https://ollama.com/download', prefix: '', local: true,
    free: 'Runs on your own PC: no key, no limits, works offline.',
    steps: ['Install Ollama.', 'Run: ollama pull llama3.1:8b', 'Allow this site: set OLLAMA_ORIGINS to this site\'s address, then restart Ollama.', "Type anything below (it's an on/off switch, not a secret)."] },
];
const KEY_BY_ID = Object.fromEntries(KEY_INFO.map(p => [p.id, p]));
const ALLOWED = new Set(KEY_INFO.flatMap(p => [p.env, ...(p.extra ? [p.extra.env] : [])]));
const VALUE_RE = /^[\x21-\x7e]{1,400}$/;
class KeySetupError extends Error {}
const mask = v => (!v ? '' : v.length >= 12 ? `…${v.slice(-4)}` : 'set');
function cleanKey(envName, value) {
  if (!ALLOWED.has(envName)) throw new KeySetupError(`${envName} is not a setting this screen can change`);
  const v = String(value || '').trim().replace(/^["']|["']$/g, '');
  if (envName === 'CLOUDFLARE_ACCOUNT_ID' && v) {
    const m = /[0-9a-f]{32}/.exec(v);
    if (!m) throw new KeySetupError("Cloudflare account ID should be 32 characters of 0-9 and a-f (or paste your dashboard address)");
    return m[0];
  }
  if (v && !VALUE_RE.test(v)) throw new KeySetupError(`${envName}: paste just the key - one line, no spaces`);
  return v;
}
function warnKey(envName, value) {
  const p = KEY_INFO.find(x => x.env === envName);
  return p && p.prefix && value && !value.startsWith(p.prefix) ? `${p.label} keys usually start with ${p.prefix} - check you copied the right one` : null;
}
function keyStatus(env) {
  return KEY_INFO.map(p => {
    const row = { id: p.id, label: p.label, env: p.env, url: p.url, free: p.free, steps: p.steps,
                  recommended: !!p.recommended, local: !!p.local, set: !!env[p.env], masked: mask(env[p.env] || '') };
    if (p.extra) row.extra = { ...p.extra, set: !!env[p.extra.env], masked: mask(env[p.extra.env] || '') };
    return row;
  });
}

// ============================================================ storage
function makeStorage(backing) {
  let mem = {};
  const ls = backing === undefined ? (() => { try { return globalThis.localStorage || null; } catch { return null; } })() : backing;
  return {
    get(k) { try { const v = ls ? ls.getItem(k) : mem[k]; return v == null ? null : JSON.parse(v); } catch { return null; } },
    set(k, v) { try { const s = JSON.stringify(v); if (ls) ls.setItem(k, s); else mem[k] = s; return true; } catch { return false; } },
    del(k) { try { if (ls) ls.removeItem(k); else delete mem[k]; } catch { /* ignore */ } },
  };
}

// ============================================================ chats.py
const titleFrom = (prompt, nWords = 7) => {
  const parts = prompt.split(/\s+/).filter(Boolean);
  const title = parts.slice(0, nWords).join(' ');
  return ((title + (parts.length > nWords ? '…' : '')).slice(0, 80)) || 'New chat';
};

class ChatStore {
  constructor(storage) { this.s = storage; this.index = storage.get('ctxos:v1:chats') || {}; this.cache = {}; }
  _saveIndex() { return this.s.set('ctxos:v1:chats', this.index); }
  create(title = 'New chat') {
    const t = nowSec(), id = uid();
    this.index[id] = { id, title, created: t, updated: t };
    this.cache[id] = []; this.s.set(`ctxos:v1:msgs:${id}`, []); this._saveIndex();
    return { ...this.index[id] };
  }
  list(query = '') {
    const q = query.toLowerCase();
    return Object.values(this.index).filter(c => !q || c.title.toLowerCase().includes(q)
      || this.messages(c.id).some(m => m.content.toLowerCase().includes(q)))
      .sort((a, b) => b.updated - a.updated).map(c => ({ ...c }));
  }
  get(id) { const c = this.index[id]; return c ? { ...c, messages: this.messages(id) } : null; }
  messages(id) { if (!this.cache[id]) this.cache[id] = this.s.get(`ctxos:v1:msgs:${id}`) || []; return this.cache[id].map(m => ({ ...m })); }
  rename(id, title) { if (!this.index[id]) return false; this.index[id].title = String(title).trim().slice(0, 80) || 'New chat'; this._saveIndex(); return true; }
  delete(id) {
    if (!this.index[id]) return false;
    delete this.index[id]; delete this.cache[id]; this.s.del(`ctxos:v1:msgs:${id}`); this.s.del(`ctxos:v1:ctx:${id}`); this._saveIndex();
    return true;
  }
  add(id, role, content, meta = {}) {
    const t = nowSec(); this.messages(id);
    const list = this.cache[id];
    const seq = list.reduce((m, x) => Math.max(m, x.seq), 0) + 1;
    const msg = { id: uid(), conv_id: id, seq, role, content, meta: meta || {}, created: t };
    list.push(msg); this.index[id].updated = t;
    this.s.set(`ctxos:v1:msgs:${id}`, list); this._saveIndex();
    return { ...msg };
  }
  truncateFrom(id, mid) {
    const list = this.messages(id); const row = list.find(m => m.id === mid);
    if (!row) return [];
    const removed = list.filter(m => m.seq >= row.seq);
    this.cache[id] = list.filter(m => m.seq < row.seq);
    this.s.set(`ctxos:v1:msgs:${id}`, this.cache[id]);
    return removed;
  }
}

// ============================================================ server.py (Engine)
const SYSTEM = `You are a capable engineering assistant working on an ongoing task.

You are given only the RELEVANT context for this turn, not the whole conversation.
Trust it. If something you need is missing, say so plainly rather than inventing it.

A different model may take over this task at any moment and will see ONLY what you
save. So you MUST START your reply with a <context> block that saves every input
the user gave this turn (numbers, names, requirements, rules, choices) and every
result you will give. Then write your answer below it. One line per item:

kind | /address/with-hyphens | value in one line

kind is one of: decision, constraint, blocker, fact, goal.
Example - user asked "a shirt costs 800, 10% off, then 18% tax; final price?":

<context>
fact | /task/inputs/shirt-price | shirt base price is 800
constraint | /task/rules/discount | 10% discount applied first
constraint | /task/rules/tax | 18% tax applied on the discounted price
fact | /task/results/final-price | discounted 720, final price 849.60
</context>

Addresses are lowercase; the first segment must be user, project, task, agent, tool
or artifact. Reuse an existing address when updating the same item. Skip the block
only for pure small talk (greetings, thanks).`;
const CONTINUE = '\n\n## Note\nYour context block for this turn is already saved. Reply to the user now - write only the answer, with no <context> block.';
const CONTEXT_RE = /<context>([\s\S]*?)<\/context>/gi;
const hasContext = t => /<context>[\s\S]*?<\/context>/i.test(t);
const RUN_RE = /(\S)\1{29,}/;
const PARTIAL_TAGS = ['<context>', '<think>', '<thinking>', '<reasoning>'];

function isDegenerate(text) {
  const body = (text || '').replace(CONTEXT_RE, '').trim();
  if (!body) return false;
  const stripped = body.replace(new RegExp(RUN_RE.source, 'g'), '').trim();
  if (RUN_RE.test(body) && stripped.length < body.length * 0.5) return true;
  return (body.match(/[\p{L}\p{N}]/gu) || []).length < body.length * 0.2;
}
function visibleText(raw) {
  let t = stripReasoning((raw || '').replace(CONTEXT_RE, ''));
  const cut = t.toLowerCase().indexOf('<context');
  if (cut >= 0) t = t.slice(0, cut);
  const low = t.toLowerCase();
  for (const tag of PARTIAL_TAGS) {
    for (let k = tag.length - 1; k > 0; k--) if (low.endsWith(tag.slice(0, k))) { t = t.slice(0, -k); break; }
  }
  return t.trim();
}

const OFFLINE_TIERS = { 'offline-a': 0.85, 'offline-b': 0.55, 'offline-c': 0.5 };
const PORTABLE = { compact: { budget: 700, chars: 4800, recent: 1, clip: 500 },
                   standard: { budget: 2500, chars: null, recent: 3, clip: 1500 },
                   full: { budget: 1000000, chars: null, recent: null, clip: null } };
const SECTIONS = [['constraint', 'Rules that must hold'], ['decision', 'Decisions already made'], ['blocker', 'Open problems'],
  ['fact', 'Key facts'], ['preference', 'My preferences'], ['artifact', 'Files involved'], ['tool_result', 'Tool results']];
const LANE_OF = {};

class Engine {
  constructor({ storage = makeStorage(), net = {}, budget = 1500, sleep = ms => new Promise(r => setTimeout(r, ms)) } = {}) {
    this.s = storage; this.chats = new ChatStore(storage); this.ctxs = {};
    this.env = storage.get('ctxos:v1:keys') || {};
    this.settings = storage.get('ctxos:v1:settings') || { relay: false };
    this.net = net; this.budget = budget; this.sleep = sleep;
    this.events = []; this.forced = new Set(); this.cooldown = new Cooldown(); this.blocked = new Set();
    this.mode = 'auto'; this.threshold = DEFAULT_THRESHOLD; this.current = null;
    this._lanes();
  }
  get offline() { return !Object.values(PROVIDERS).some(p => this.env[p.key_env]); }
  _lanes() {
    if (this.offline) { this.smart = ['offline-a']; this.fast = ['offline-b', 'offline-c']; }
    else {
      const ok = n => PROVIDERS[n] && this.env[PROVIDERS[n].key_env];
      this.smart = SMART_ORDER.filter(ok); this.fast = FAST_ORDER.filter(ok);
    }
    this.current = this.order[0] || null;
  }
  get order() { return [...this.smart, ...this.fast.filter(n => !this.smart.includes(n))]; }
  tier(n) { return OFFLINE_TIERS[n] ?? (PROVIDERS[n] ? PROVIDERS[n].tier : 0.5); }
  model(n) { return this.offline ? 'simulated' : PROVIDERS[n].model; }
  providerInfo() {
    return this.order.map(n => ({ name: n, lane: this.smart.includes(n) ? 'smart' : 'fast', tier: this.tier(n), model: this.model(n),
      current: n === this.current, failed: this.forced.has(n), cooldown: this.cooldown.remaining(n) }));
  }
  state() {
    return { version: VERSION, offline: this.offline, providers: this.providerInfo(), events: this.events.slice(-30), budget: this.budget,
      routing: { mode: this.mode, threshold: this.threshold }, needs_setup: this.offline, auto_offline: this.offline, web: true };
  }

  ctxFor(cid) {
    if (!this.ctxs[cid]) {
      const store = new Store(this.s.get(`ctxos:v1:ctx:${cid}`));
      store.onChange = () => this.s.set(`ctxos:v1:ctx:${cid}`, store.toJSON());
      this.ctxs[cid] = new Memory(store);
    }
    return this.ctxs[cid];
  }
  deleteConversation(cid) { delete this.ctxs[cid]; return this.chats.delete(cid); }
  memory(cid) {
    const ctx = this.ctxFor(cid);
    const units = ctx.list('', { liveOnly: true }).map(u => ({ address: u.address, kind: u.kind, value: u.value, source: u.source,
      tokens: u.tokens, pinned: u.pinned, version: u.version }));
    units.sort((a, b) => cmp(a.kind, b.kind) || cmp(a.address, b.address));
    return { units, stats: ctx.stats(), conflicts: ctx.conflicts() };
  }
  forget(cid, address) { return this.ctxFor(cid).delete(address); }
  exportText(cid) {
    const conv = this.chats.get(cid) || { title: 'Chat', messages: [] };
    const out = [`# ${conv.title}`, ''];
    for (const m of conv.messages) out.push(`**${m.role === 'user' ? 'You' : (m.meta.provider || 'Assistant')}:**`, '', m.content, '');
    return out.join('\n');
  }

  portable(cid, size = 'compact') {
    const spec = PORTABLE[size] || PORTABLE.compact;
    const conv = this.chats.get(cid) || { title: 'Chat', messages: [] };
    const ctx = this.ctxFor(cid);
    let budget = spec.budget, packet, text;
    for (;;) {
      packet = ctx.handoff({ direction: 'lateral', budgetTokens: budget });
      text = this._renderPortable(conv, packet, spec);
      if (!spec.chars || text.length <= spec.chars || budget <= 100) break;
      budget = Math.floor(budget * 0.8);
    }
    return { text, chars: text.length, tokens: countTokens(text), items: packet.units.length, left_out: packet.omitted.length, size, title: conv.title };
  }
  static labelled(u) {
    const label = u.address.replace(/\/+$/, '').split('/').pop().replace(/-/g, ' ').replace(/_/g, ' ');
    const ws = label.toLowerCase().split(/\s+/).filter(w => w.length > 2);
    const generic = new Set(['step', 'item', 'note', 'notes', 'value', 'info', 'detail', 'misc']);
    if (!ws.length || ws.every(w => generic.has(w)) || /\d/.test(label) || ws.every(w => u.value.toLowerCase().includes(w))) return u.value;
    return `${label.slice(0, 1).toUpperCase()}${label.slice(1)}: ${u.value}`;
  }
  _renderPortable(conv, packet, spec) {
    const msgs = conv.messages.filter(m => m.content.trim());
    const L = [`# Context: ${conv.title}`, '',
      "I'm continuing a conversation I started in another AI tool. Below is what we established there. Treat it as already agreed - don't repeat it back. Reply with one line saying what we're working on, then wait for my next message.",
      '', '## Goal', packet.goal || conv.title];
    for (const [kind, title] of SECTIONS) {
      const vals = packet.units.filter(u => u.kind === kind && u.value !== packet.goal).map(Engine.labelled);
      if (vals.length) L.push('', `## ${title}`, ...vals.map(v => `- ${v}`));
    }
    const take = spec.recent === null ? msgs : msgs.slice(-2 * spec.recent);
    if (take.length) {
      L.push('', spec.recent === null ? '## Conversation so far' : '## Where we left off');
      for (const m of take) {
        let body = m.content.trim();
        if (spec.clip && body.length > spec.clip) body = body.slice(0, spec.clip).trimEnd() + ' …';
        L.push('', `**${m.role === 'user' ? 'Me' : 'AI'}:** ${body}`);
      }
    }
    if (packet.omitted.length) L.push('', `_${packet.omitted.length} less important saved items were left out to keep this short. Ask me if something seems missing._`);
    return L.join('\n').trim() + '\n';
  }

  commit(text, source, ctx) {
    const lines = [];
    for (const m of (text || '').matchAll(CONTEXT_RE)) lines.push(...m[1].split(/\r?\n/));
    const written = [];
    for (const line of lines) {
      const parts = line.split('|').map(p => p.trim());
      if (parts.length < 3) continue;
      const kind = parts[0].toLowerCase(), address = parts[1], value = parts.slice(2).join('|').trim();
      if (!['decision', 'constraint', 'blocker', 'fact', 'goal'].includes(kind) || !value) continue;
      const importance = { goal: 1.0, constraint: 0.95, blocker: 0.85, decision: 0.9, fact: 0.7 }[kind] ?? 0.5;
      try {
        const existed = ctx.get(address) !== null;
        const u = ctx.put(address, value, { kind, source, importance, pinned: kind === 'goal' });
        written.push({ address: u.address, kind: u.kind, value: u.value, created: !existed });
      } catch { /* a malformed address must never break the turn */ }
    }
    return written;
  }
  undo(cid, removed) {
    const ctx = this.ctxFor(cid);
    for (const m of removed) for (const w of (m.meta.written || [])) if (w.created) { try { ctx.delete(w.address); } catch { /* ignore */ } }
  }

  offlineReply(name, user) {
    const asked = (user.split('## Now').pop() || '').trim() || user.trim();
    const seen = [...new Set(user.match(/\/(?:user|project|task|agent|tool|artifact)\/[a-z0-9/_-]+/g) || [])].slice(0, 6);
    const slug = asked.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24) || 'note';
    return `<context>\nfact | /project/notes/${slug} | user asked: ${asked.slice(0, 120)}\n</context>\n**[${name}]** simulated reply - no model was called.\n\n`
      + `You asked: *${asked.slice(0, 300)}*\n\nContext I was given covers: ${seen.length ? seen.join(', ') : 'nothing yet'}`
      + '\n\nThis is the demo mode: add a free API key under **Set up models** to talk to real models.';
  }

  async *stream(name, system, user, signal, temperature = 0.7) {
    if (this.forced.has(name)) throw new ProviderError('429 rate limit exceeded (simulated for demo)');
    if (this.offline) {
      for (const w of this.offlineReply(name, user).split(/(\s+)/)) { await this.sleep(4); if (signal && signal.aborted) throw new DOMException('aborted', 'AbortError'); yield w; }
      return;
    }
    const p = PROVIDERS[name];
    const host = hostOf(resolveUrl(p, this.env));
    const run = async function* (self, relay) {
      for await (const [kind, chunk] of streamEvents(p, system, user, self.env, { signal, temperature, net: { ...self.net, relay } })) yield kind === 'text' ? chunk : ['think', chunk];
    };
    // Direct from the browser first. If the provider blocks browser calls and the user
    // allowed the relay, go through it, and remember so the next call skips the failed try.
    const viaRelay = this.settings.relay && this.blocked.has(host);
    let started = false;
    try {
      for await (const c of run(this, viaRelay)) { started = true; yield c; }
    } catch (e) {
      if (e && e.unreachable && !started && !viaRelay && this.settings.relay) {
        this.blocked.add(host);
        for await (const c of run(this, true)) yield c;
      } else {
        if (e && e.unreachable && !this.settings.relay) e.message += '. Turn on the relay in "Set up models" or use the desktop app';
        throw e;
      }
    }
  }

  async *chatStream(body, signal) {
    let { conversation_id: cid, prompt = '', lane = null, route = null, regenerate = null, edit = null } = body;
    if (!this.order.length) { yield { type: 'error', error: 'No providers available. Add an API key under Set up models.' }; return; }
    if (!cid || !this.chats.get(cid)) cid = this.chats.create().id;
    const ctx = this.ctxFor(cid);
    if (body.attachments && body.attachments.length) { yield { type: 'error', error: 'File attachments are not available in the web edition yet.' }; return; }
    if (lane === 'pipeline' || /^\s*\/pipeline\b/i.test(prompt || '')) { yield { type: 'error', error: 'Pipeline mode is not available in the web edition yet.' }; return; }

    let userRec;
    if (regenerate) {
      this.undo(cid, this.chats.truncateFrom(cid, regenerate));
      const users = this.chats.messages(cid).filter(m => m.role === 'user');
      if (!users.length) { yield { type: 'error', error: 'Nothing to regenerate.' }; return; }
      userRec = users[users.length - 1]; prompt = userRec.content;
    } else {
      prompt = String(prompt || '').trim();
      if (!prompt) { yield { type: 'error', error: 'Empty message.' }; return; }
      if (edit) {
        const removed = this.chats.truncateFrom(cid, edit);
        this.undo(cid, removed);
        if (removed.length && !this.chats.messages(cid).length) {
          const old = parseOverride(removed[0].content)[1];
          if (this.chats.get(cid).title === titleFrom(old)) this.chats.rename(cid, 'New chat');
          try { ctx.delete('/task/goal'); } catch { /* ignore */ }
        }
      }
      userRec = this.chats.add(cid, 'user', prompt);
    }

    const [forced, textIn] = parseOverride(prompt);
    let conv = this.chats.get(cid);
    if (conv.title === 'New chat') { this.chats.rename(cid, titleFrom(textIn)); conv = this.chats.get(cid); }
    yield { type: 'start', conversation: { id: conv.id, title: conv.title, created: conv.created, updated: conv.updated }, user_message: userRec };

    const wanted = forced || ([SMART, FAST].includes(lane) ? lane : this.mode);
    const decision = decide(textIn, wanted, this.threshold);
    const temperature = temperatureFor(textIn);
    yield { type: 'route', lane: decision.lane, difficulty: decision.score, reasons: decision.reasons, forced: decision.forced, temperature };

    if (!ctx.get('/task/goal')) ctx.put('/task/goal', textIn.trim().slice(0, 400), { kind: 'goal', importance: 1, pinned: true, source: 'user' });

    const history = this.chats.messages(cid).filter(m => m.seq < userRec.seq);
    const recent = history.slice(-2).map(m => `${m.role}: ${m.content.slice(0, 400)}`).join('\n');
    const selection = ctx.select(textIn, this.budget);
    const contextText = renderUnits(selection.units) || '(nothing on file yet)';
    let userMsg = `## Context on file\n${contextText}\n\n` + (recent ? `## Last exchange\n${recent}\n\n` : '') + `## Now\n${textIn}`;

    let chain = buildChain(decision.lane, this.smart, this.fast, n => this.cooldown.active(n));
    if (route && this.order.includes(route)) chain = [route, ...chain.filter(n => n !== route)];
    const attempts = [], switched = [];
    let raw = '', shown = '', used = null, name = chain[0];
    const t0 = Date.now();
    let thinking = 0, tThink = null;

    const saveStopped = () => {
      if (shown.trim()) this.chats.add(cid, 'assistant', shown, { provider: name, model: this.model(name), stopped: true,
        lane: decision.lane, difficulty: decision.score, reasons: decision.reasons, forced: decision.forced });
    };

    for (let i = 0; i < chain.length; i++) {
      name = chain[i];
      yield { type: 'model', provider: name, model: this.model(name), lane: this.smart.includes(name) ? 'smart' : 'fast' };
      raw = ''; shown = ''; thinking = 0; tThink = null;
      let blockOnly = '';
      try {
        for (let attempt = 0; attempt < 2; attempt++) {
          const ask = userMsg + (attempt ? CONTINUE : '');
          for await (const chunk of this.stream(name, SYSTEM, ask, signal, temperature)) {
            if (Array.isArray(chunk)) {
              thinking += chunk[1].length; tThink = tThink || Date.now();
              yield { type: 'thinking', text: chunk[1] };
              continue;
            }
            raw += chunk;
            const vis = visibleText(raw);
            if (vis.length >= 30 && isDegenerate(vis)) throw new ProviderError('garbled reply (repeated characters)');
            const prev = shown; shown = vis;
            if (vis.startsWith(prev)) { if (vis.length > prev.length) yield { type: 'delta', text: vis.slice(prev.length) }; }
            else yield { type: 'replace', text: vis };
          }
          if (shown.trim() || attempt || !hasContext(raw)) break;
          blockOnly = raw; raw = '';
        }
        if (!shown.trim()) throw new ProviderError('empty reply (the model may have spent its whole output budget on reasoning)');
        raw = blockOnly + raw; used = name; break;
      } catch (exc) {
        if (signal && signal.aborted || (exc && exc.name === 'AbortError')) { saveStopped(); return; }
        if (!(exc instanceof ProviderError)) throw exc;
        const benched = this.cooldown.hit(name, String(exc.message));
        attempts.push({ provider: name, error: String(exc.message).slice(0, 200), cooldown: benched });
        if (shown) { yield { type: 'reset' }; shown = ''; }
        const nxt = i + 1 < chain.length ? chain[i + 1] : null;
        if (nxt === null) break;
        const direction = classify(this.tier(name), this.tier(nxt));
        const packet = ctx.handoff({ direction, budgetTokens: this.budget, from_model: name, to_model: nxt, difficulty: decision.score });
        const [ok, why] = shouldMigrate(direction, decision.score);
        userMsg = `${packet.render()}\n\n` + (recent ? `## Last exchange\n${recent}\n\n` : '') + `## Now\n${textIn}`;
        const sw = { from: name, to: nxt, direction, reason: String(exc.message).slice(0, 160), packet_tokens: packet.tokens_selected,
          full_replay_tokens: packet.tokens_stored, omitted: packet.omitted.length, gate: ok ? null : why };
        switched.push(sw);
        this.events.push({ ts: nowSec(), kind: 'switch', ...sw });
        yield { type: 'switch', ...sw };
      }
    }

    if (used === null) {
      this.events.push({ ts: nowSec(), kind: 'all_failed', detail: attempts });
      yield { type: 'error', error: 'Every provider failed.', attempts, switched };
      return;
    }
    this.current = used; this.cooldown.ok(used);
    let written = this.commit(raw, used, ctx);
    if (!written.length && decision.score > 0.05) {
      const addr = `/task/inputs/turn-${userRec.seq}`;
      const existed = ctx.get(addr) !== null;
      const u = ctx.put(addr, textIn.trim().slice(0, 500), { kind: 'fact', source: 'user', importance: 0.75 });
      written = [{ address: u.address, kind: u.kind, value: u.value, created: !existed }];
    }
    const stats = ctx.stats();
    const meta = { provider: used, model: this.model(used), lane_used: this.smart.includes(used) ? 'smart' : 'fast',
      lane: decision.lane, difficulty: decision.score, reasons: decision.reasons, forced: decision.forced, attempts, switched, written,
      context_sent: selection.addresses(), thought_ms: tThink && thinking ? Date.now() - tThink : 0,
      tokens: { stored: stats.live_tokens, sent: countTokens(contextText), omitted_units: selection.omitted.length }, ms: Date.now() - t0 };
    const msg = this.chats.add(cid, 'assistant', visibleText(raw), meta);
    try { reflect(ctx.store); } catch { /* tidying must never break a reply */ }
    yield { type: 'done', message: msg };
  }

  // ---- keys
  keysStatus() { return { providers: keyStatus(this.env), offline: this.offline, auto_offline: this.offline,
    env_file: 'this browser (never sent to our server)', web: true, relay: !!this.settings.relay }; }
  saveKeys(values) {
    const updates = {};
    for (const [k, v] of Object.entries(values || {})) updates[String(k)] = cleanKey(String(k), String(v || ''));
    if (!Object.keys(updates).length) throw new KeySetupError('nothing to save');
    const old = { ...this.env };
    for (const [k, v] of Object.entries(updates)) { if (v) this.env[k] = v; else delete this.env[k]; }
    this.s.set('ctxos:v1:keys', this.env);
    const changed = Object.entries(PROVIDERS).filter(([, p]) => old[p.key_env] !== this.env[p.key_env]).map(([n]) => n);
    this.cooldown.clear(changed);
    this._lanes();
    const warnings = Object.entries(updates).map(([k, v]) => warnKey(k, v)).filter(Boolean);
    return { ...this.keysStatus(), warnings };
  }
  setRelay(on) { this.settings.relay = !!on; this.s.set('ctxos:v1:settings', this.settings); return this.keysStatus(); }
  async testKey(pid, values) {
    const p = KEY_BY_ID[pid];
    if (!p) throw new KeySetupError('unknown provider');
    const trial = { ...this.env };
    for (const [k, v] of Object.entries(values || {})) if (v) trial[k] = cleanKey(k, String(v));
    const secrets = Object.entries(trial).filter(([k, v]) => ALLOWED.has(k) && v).map(([, v]) => v);
    const rows = [];
    for (const name of [pid, `${pid}-fast`]) {
      const route = PROVIDERS[name];
      if (!route) continue;
      if (!trial[route.key_env]) { rows.push({ route: name, model: route.model, status: 'no key', detail: 'paste a key first' }); continue; }
      try {
        const text = await completeOnce(route, 'Reply with the single word: ok', 'Say ok.', trial,
          { net: this.net, allowRelay: !!this.settings.relay });
        rows.push({ route: name, model: route.model, status: text ? 'OK' : 'FAIL', detail: text ? 'working' : 'empty reply' });
      } catch (e) {
        let msg = String(e.message || e);
        for (const s of secrets) msg = msg.split(s).join('…');
        const st = /429/.test(msg) ? 'LIMITED' : /50[23]|imeout/.test(msg) ? 'BUSY' : /\b40[13]\b/.test(msg) ? 'REJECTED' : 'FAIL';
        const hint = { LIMITED: 'the key works; this free tier is busy or used up for now', BUSY: 'the key works; the provider is overloaded right now',
                       REJECTED: 'the provider rejected this key - copy it again' }[st] || '';
        const blocked = e.unreachable ? ' If it is a browser block, turn on the relay below.' : '';
        rows.push({ route: name, model: route.model, status: st, detail: (hint ? hint + ' - ' : '') + msg.slice(0, 220) + blocked });
      }
    }
    return rows;
  }
  async checkAll() {
    if (this.offline) return { offline: true, rows: [] };
    const rows = [];
    for (const name of this.order) {
      const route = PROVIDERS[name];
      try {
        const text = await completeOnce(route, 'Reply with the single word: ok', 'Say ok.', this.env, { net: this.net, allowRelay: !!this.settings.relay });
        rows.push({ provider: name, status: text ? 'OK' : 'FAIL', model: route.model, detail: text ? '' : 'empty reply' });
      } catch (e) { rows.push({ provider: name, status: 'FAIL', model: route.model, detail: String(e.message || e).slice(0, 200) }); }
    }
    return { offline: false, rows };
  }
  toggleFailure(name) {
    if (this.forced.has(name)) { this.forced.delete(name); this.cooldown.clear([name]); return false; }
    this.forced.add(name); return true;
  }
  previewHandoff(cid, direction) {
    const p = this.ctxFor(cid).handoff({ direction, budgetTokens: this.budget, difficulty: 0.7 });
    return { markdown: p.render(), tokens: p.tokens_selected, full_replay: p.tokens_stored, reduction: p.reduction, omitted: p.omitted.slice(0, 40), notes: p.notes };
  }
}

// ============================================================ the /api shim
const CONV_RE = /^\/api\/conversations\/([0-9a-f]+)(?:\/(\w+))?$/;
function jsonRes(obj, status = 200, headers = {}) {
  return new Response(JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers } });
}
function textRes(text, name) {
  return new Response(text, { status: 200, headers: { 'Content-Type': 'text/markdown; charset=utf-8',
    'Content-Disposition': `attachment; filename="${name.replace(/[^\w -]/g, '') || 'chat'}.md"` } });
}

async function handleApi(engine, url, init = {}) {
  const u = new URL(url, 'http://local');
  const path = u.pathname, q = u.searchParams;
  const method = (init.method || 'GET').toUpperCase();
  let body = {};
  if (method === 'POST' && init.body) { try { body = JSON.parse(init.body); } catch { body = {}; } }
  try {
    if (path === '/api/state') return jsonRes(engine.state());
    if (path === '/api/conversations') {
      if (method === 'POST') return jsonRes(engine.chats.create(body.title || 'New chat'));
      return jsonRes({ conversations: engine.chats.list(q.get('q') || '') });
    }
    if (path === '/api/builds') return method === 'POST' ? jsonRes({ error: 'Build mode needs a computer of its own: it runs commands and writes files. Use the desktop app.' }, 400) : jsonRes({ builds: [] });
    if (path === '/api/connectors') return jsonRes({ servers: [], builtin: [] });
    if (path === '/api/connectors/reload') return jsonRes({ ok: true });
    if (path === '/api/skills') return jsonRes({ skills: [] });
    if (path === '/api/keys') return jsonRes(engine.keysStatus());
    if (path === '/api/keys/save') return jsonRes(engine.saveKeys(body.values));
    if (path === '/api/keys/test') return jsonRes({ rows: await engine.testKey(body.provider, body.values) });
    if (path === '/api/relay') return jsonRes(engine.setRelay(body.on));
    if (path === '/api/fail') return jsonRes({ failed: engine.toggleFailure(body.provider) });
    if (path === '/api/check') return jsonRes(await engine.checkAll());
    if (path === '/api/chat' && method === 'POST') return chatResponse(engine, body, init.signal);
    const m = CONV_RE.exec(path);
    if (m) {
      const [, cid, action] = m;
      if (!engine.chats.get(cid)) return jsonRes({ error: 'no such conversation' }, 404);
      if (!action) return jsonRes(engine.chats.get(cid));
      if (action === 'memory') return jsonRes(engine.memory(cid));
      if (action === 'rename') { engine.chats.rename(cid, body.title || ''); return jsonRes({ ok: true }); }
      if (action === 'delete') { engine.deleteConversation(cid); return jsonRes({ ok: true }); }
      if (action === 'forget') return jsonRes({ ok: engine.forget(cid, body.address) });
      if (action === 'handoff') return jsonRes(engine.previewHandoff(cid, body.direction || 'escalate'));
      if (action === 'portable') {
        const out = engine.portable(cid, q.get('size') || 'compact');
        return q.get('download') ? textRes(out.text, `${out.title.slice(0, 40)} - context`) : jsonRes(out);
      }
      if (action === 'export') return textRes(engine.exportText(cid), engine.chats.get(cid).title.slice(0, 40));
    }
    return jsonRes({ error: 'not found' }, 404);
  } catch (e) {
    if (e instanceof KeySetupError || e instanceof AddressError) return jsonRes({ error: e.message }, 400);
    return jsonRes({ error: String((e && e.message) || e) }, 500);
  }
}

function chatResponse(engine, body, outerSignal) {
  const ctl = new AbortController();
  if (outerSignal) outerSignal.addEventListener('abort', () => ctl.abort(), { once: true });
  const gen = engine.chatStream(body, ctl.signal);
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    async pull(c) {
      try {
        const { value, done } = await gen.next();
        if (done) { c.close(); return; }
        c.enqueue(enc.encode(JSON.stringify(value) + '\n'));
      } catch (e) {
        c.enqueue(enc.encode(JSON.stringify({ type: 'error', error: String((e && e.message) || e) }) + '\n')); c.close();
      }
    },
    async cancel() { ctl.abort(); try { await gen.return(); } catch { /* ignore */ } },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson; charset=utf-8', 'Cache-Control': 'no-store' } });
}

function install(win) {
  const engine = new Engine({ net: {} });
  const realFetch = win.fetch.bind(win);
  win.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url && url.startsWith('/api/')) return handleApi(engine, url, init || {});
    return realFetch(input, init);
  };
  win.__contextos = engine;
}

return { VERSION, Engine, Store, Memory, ChatStore, Cooldown, ProviderError, KeySetupError, AddressError,
  PROVIDERS, SMART_ORDER, FAST_ORDER, RELAY_HOSTS, KEY_INFO,
  normalizeAddress, countTokens, search, pack, renderUnits, buildHandoff, classify, shouldMigrate, score, decide, buildChain,
  parseOverride, temperatureFor, reflect, visibleText, isDegenerate, stripReasoning, streamEvents, makeStorage, handleApi, titleFrom, install };
});
