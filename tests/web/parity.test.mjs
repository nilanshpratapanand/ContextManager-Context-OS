// Compare the JS engine with the Python engine on tests/web/scenario.json.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const E = require(path.join(here, '..', '..', 'webapp', 'engine.js'));
const py = JSON.parse(execFileSync(process.env.PYTHON || 'python3', [path.join(here, 'parity_py.py')], { encoding: 'utf8' }));
const sc = JSON.parse(readFileSync(path.join(here, 'scenario.json'), 'utf8'));

const store = new E.Store(); const mem = new E.Memory(store);
for (const [addr, val, kind, src, imp, pinned] of sc.units) {
  mem.put(addr, val, { kind, source: src, importance: imp, pinned,
    ...(kind === 'artifact' ? { meta: { path: 'api/routes.py', bytes: val.length, sha256: require('node:crypto').createHash('sha256').update(val).digest('hex') } } : {}) });
}
const strip = o => JSON.parse(JSON.stringify(o));
const jsProviders = Object.fromEntries(Object.entries(E.PROVIDERS).map(([n, p]) => [n, { key_env: p.key_env, model: p.model, url: p.url, style: p.style, tier: p.tier, lane: p.lane, extra: p.extra }]));
let fails = 0, checks = 0; const diffs = [];
const eq = (label, a, b) => { checks++; if (JSON.stringify(a) !== JSON.stringify(b)) { fails++; diffs.push(`${label}\n  py: ${JSON.stringify(a).slice(0, 300)}\n  js: ${JSON.stringify(b).slice(0, 300)}`); } };

const sortKeys = o => JSON.parse(JSON.stringify(o, (k, v) => (v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort()) : v)));
eq('stats', sortKeys(py.stats), sortKeys(mem.stats()));
for (const q of sc.queries) for (const b of sc.budgets) {
  const s = mem.select(q, b), want = py.select[`${q}|${b}`];
  eq(`select ${q}|${b} addrs`, want.addrs, s.units.map(u => u.address));
  eq(`select ${q}|${b} tokens`, want.tokens, s.tokens_selected);
  eq(`select ${q}|${b} omitted`, want.omitted, s.omitted.map(o => o.address).sort());
  eq(`select ${q}|${b} render`, want.render, E.renderUnits(s.units));
}
for (const d of ['escalate', 'downshift', 'lateral']) for (const b of sc.budgets) {
  const p = mem.handoff({ direction: d, budgetTokens: b, from_model: 'a', to_model: 'b', difficulty: 0.7 }), want = py.handoff[`${d}|${b}`];
  eq(`handoff ${d}|${b} units`, want.units, p.units.map(u => u.address));
  eq(`handoff ${d}|${b} omitted`, want.omitted, p.omitted.map(o => o.address).sort());
  eq(`handoff ${d}|${b} tokens`, [want.tokens, want.full], [p.tokens_selected, p.tokens_stored]);
  eq(`handoff ${d}|${b} render`, want.render, p.render());
}
for (const t of sc.prompts) {
  const [s, r] = E.score(t), want = py.route[t];
  eq(`route ${JSON.stringify(t)}`, [want.score, want.reasons, want.override], [s, r, E.parseOverride(t)]);
}
for (const t of [...sc.prompts, ...sc.creative]) eq(`temperature ${JSON.stringify(t)}`, py.temp[t], E.temperatureFor(t));
{
  const rs = new E.Store(), rm = new E.Memory(rs);
  for (const [a, v, k, imp, meta] of sc.reflect) rm.put(a, v, { kind: k, importance: imp, ...(meta ? { meta } : {}) });
  const counts = E.reflect(rs, { keepTool: 3 });
  eq('reflect counts', py.reflect.counts, counts);
  eq('reflect live units', py.reflect.live, rs.list().map(u => u.address));
  eq('reflect links', py.reflect.links, Object.fromEntries(rs.list().filter(u => u.meta && u.meta.derived_from !== undefined).map(u => [u.address, u.meta.derived_from])));
}
for (const a of sc.addresses) { let got; try { got = E.normalizeAddress(a); } catch { got = 'ERR'; } eq(`addr ${a}`, py.addr[a], got); }
for (const t of sc.texts) eq(`text ${JSON.stringify(t)}`, py.text[t], { visible: E.visibleText(t), degenerate: E.isDegenerate(t) });
eq('providers (names, models, urls, tiers, lanes)', strip(py.providers), strip(jsProviders));
eq('lane orders', py.orders, { smart: E.SMART_ORDER, fast: E.FAST_ORDER });
// Ollama's steps differ on purpose: a web page needs OLLAMA_ORIGINS set to be allowed to call it.
const keyRow = k => ({ ...k, recommended: !!k.recommended, local: !!k.local, ...(k.id === 'ollama' ? { steps: undefined } : {}) });
eq('key setup info', strip(py.keys.map(keyRow)), strip(E.KEY_INFO.map(keyRow)));
test(`the JS engine matches the Python engine (${checks} checks)`, () => { assert.equal(fails, 0, diffs.join('\n')); });
