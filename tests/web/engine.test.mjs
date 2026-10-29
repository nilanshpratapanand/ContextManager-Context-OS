// Chat-flow tests for the web engine against a fake OpenAI-compatible provider.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const E = require(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'webapp', 'engine.js'));

// ---- fake provider: behaviour per host is set by the test
function startFake() {
  const calls = [];
  let behave = {};
  const srv = http.createServer((req, res) => {
    let body = ''; req.on('data', d => body += d);
    req.on('end', () => {
      const host = req.headers['x-fake-host'];
      const payload = body ? JSON.parse(body) : {};
      calls.push({ host, url: req.url, headers: req.headers, payload });
      (behave[host] || behave.default)(req, res, payload, calls.length);
    });
  });
  return new Promise(r => srv.listen(0, '127.0.0.1', () => r({
    srv, calls, port: srv.address().port, set(b) { behave = b; },
    close: () => new Promise(x => srv.close(x)),
  })));
}
const sse = (res, deltas, { end = true, delay = 0 } = {}) => {
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  let i = 0;
  const tick = () => {
    if (i < deltas.length) { res.write(`data: ${JSON.stringify({ choices: [{ delta: { content: deltas[i++] } }] })}\n\n`); delay ? setTimeout(tick, delay) : tick(); }
    else if (end) { res.write('data: [DONE]\n\n'); res.end(); }
  };
  tick();
};
const reply = text => (req, res) => sse(res, text.match(/.{1,12}/gs));
const fail = (code, msg) => (req, res) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: msg } })); };

function mkEngine(fake, keys = { GROQ_API_KEY: 'gsk_test_key_123456', OPENROUTER_API_KEY: 'sk-or-test-key-123456' }) {
  const storage = E.makeStorage(null);
  storage.set('ctxos:v1:keys', keys);
  const net = { fetch: (url, init) => fetch(`http://127.0.0.1:${fake.port}/x`, { ...init, headers: { ...init.headers, 'x-fake-host': new URL(url).host } }) };
  return new E.Engine({ storage, net, sleep: () => Promise.resolve() });
}
async function turn(engine, body, signal) {
  const ev = [];
  for await (const e of engine.chatStream(body, signal)) ev.push(e);
  return ev;
}
const types = ev => ev.map(e => e.type);
const CTX = '<context>\nfact | /task/inputs/shirt-price | shirt base price is 800\nconstraint | /task/rules/tax | 18% tax applied last\n</context>\n';

test('a turn streams, saves memory and titles the chat', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: reply(CTX + 'The final price is 849.60.') });
  const eng = mkEngine(fake);
  const ev = await turn(eng, { prompt: 'a shirt costs 800, 10% off, then 18% tax - final price?' });
  assert.deepEqual(types(ev).filter((t, i, a) => a.indexOf(t) === i), ['start', 'route', 'model', 'delta', 'done']);
  const done = ev.at(-1).message;
  assert.equal(done.content, 'The final price is 849.60.');
  assert.equal(done.meta.written.length, 2);
  assert.equal(done.meta.lane, 'smart');                       // multi-step arithmetic goes to the smart lane
  const cid = ev[0].conversation.id;
  assert.equal(eng.chats.get(cid).title, 'a shirt costs 800, 10% off, then…');
  const mem = eng.memory(cid);
  assert.ok(mem.units.some(u => u.address === '/task/goal') && mem.units.some(u => u.address === '/task/rules/tax'));
  assert.ok(!ev.some(e => e.type === 'delta' && /context/i.test(e.text)), 'the context block never reaches the screen');
});

test('provider failure hands off with the memory, to the next model', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  fake.set({ 'api.groq.com': fail(429, 'rate limit exceeded, try again in 7s'), default: reply(CTX + 'Continuing: 849.60.') });
  const eng = mkEngine(fake);
  const first = await turn(eng, { prompt: 'a shirt costs 800, 10% off, then 18% tax - final price?', lane: 'smart' });
  const sw = first.find(e => e.type === 'switch');
  assert.ok(sw, 'a switch event');
  assert.equal(sw.from, 'groq'); assert.ok(sw.packet_tokens > 0);
  assert.equal(first.at(-1).message.meta.provider, 'openrouter');
  assert.equal(first.at(-1).message.meta.switched.length, 1);
  const second = fake.calls.filter(c => c.host === 'openrouter.ai').at(-1);
  assert.match(second.payload.messages[1].content, /# Task handoff/);
  assert.match(second.payload.messages[1].content, /shirt costs 800/);
  assert.ok(eng.cooldown.active('groq'), 'the failed route rests');
});

test('a failure in the middle of a reply resets and continues elsewhere', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  fake.set({ 'api.groq.com': (req, res) => sse(res, ['Half an answ', 'er that is long enough to show'], { end: false }),
             default: reply(CTX + 'Complete answer from the second model.') });
  // groq's stream stalls: emulate a mid-stream break by destroying the socket
  fake.set({ 'api.groq.com': (req, res) => { sse(res, ['Half an answer that is long enough to show'], { end: false }); setTimeout(() => res.destroy(), 20); },
             default: reply(CTX + 'Complete answer from the second model.') });
  const eng = mkEngine(fake);
  const ev = await turn(eng, { prompt: 'Design a database schema for a hostel booking app', lane: 'smart' });
  const t = types(ev);
  assert.ok(t.includes('delta') && t.includes('reset') && t.includes('switch'));
  assert.ok(t.indexOf('reset') < t.indexOf('switch'));
  assert.equal(ev.at(-1).message.content, 'Complete answer from the second model.');
});

test('every provider failing ends in one clear error with the attempts', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: fail(503, 'overloaded') });
  const eng = mkEngine(fake);
  const ev = await turn(eng, { prompt: 'Design a schema' });
  const err = ev.at(-1);
  assert.equal(err.type, 'error'); assert.match(err.error, /Every provider failed/);
  assert.ok(err.attempts.length >= 2);
});

test('Stop keeps what was shown and commits nothing', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  fake.set({ default: (req, res) => sse(res, ['Here is the beginning of a rather long answer ', 'and more words ', 'and more words ', 'and more'], { delay: 40 }) });
  const eng = mkEngine(fake);
  const ctl = new AbortController();
  const ev = [];
  for await (const e of eng.chatStream({ prompt: 'Design a database schema' }, ctl.signal)) {
    ev.push(e);
    if (e.type === 'delta' && !ctl.signal.aborted) ctl.abort();
  }
  const cid = ev[0].conversation.id;
  const msgs = eng.chats.messages(cid);
  const last = msgs.at(-1);
  assert.equal(last.role, 'assistant'); assert.equal(last.meta.stopped, true);
  assert.ok(last.content.length > 0);
  assert.ok(!eng.memory(cid).units.some(u => u.source === 'groq'), 'no memory committed from a stopped reply');
});

test('regenerate and edit undo exactly what the dropped reply wrote', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  let n = 0;
  fake.set({ default: (req, res) => sse(res, [(++n === 1 ? '<context>\nfact | /task/inputs/first | first answer fact\n</context>\n' : '<context>\nfact | /task/inputs/second | second answer fact\n</context>\n') + 'Reply number ' + n + ' with enough words.']) });
  const eng = mkEngine(fake);
  const a = await turn(eng, { prompt: 'Design a database schema' });
  const cid = a[0].conversation.id, bot = a.at(-1).message;
  assert.ok(eng.memory(cid).units.some(u => u.address === '/task/inputs/first'));
  const b = await turn(eng, { conversation_id: cid, regenerate: bot.id });
  const addrs = eng.memory(cid).units.map(u => u.address);
  assert.ok(!addrs.includes('/task/inputs/first') && addrs.includes('/task/inputs/second'));
  assert.equal(eng.chats.messages(cid).length, 2);
});

test('an edited opening message resets the title and the goal', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: reply(CTX + 'Fine.') });
  const eng = mkEngine(fake);
  const a = await turn(eng, { prompt: 'First idea about the project' });
  const cid = a[0].conversation.id; const userMsg = a[0].user_message;
  await turn(eng, { conversation_id: cid, edit: userMsg.id, prompt: 'Completely different idea' });
  assert.equal(eng.chats.get(cid).title, 'Completely different idea');
  assert.equal(eng.ctxFor(cid).get('/task/goal').value, 'Completely different idea');
});

test('no keys means demo mode with simulated replies, no network', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  const eng = mkEngine(fake, {});
  assert.equal(eng.state().offline, true);
  const ev = await turn(eng, { prompt: 'hello there friend' });
  assert.match(ev.at(-1).message.content, /simulated reply/);
  assert.equal(fake.calls.length, 0);
});

test('keys: validated, masked, never echoed in test errors', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: fail(401, 'Invalid API key: gsk_secret_value_1234') });
  const eng = mkEngine(fake, {});
  assert.throws(() => eng.saveKeys({ NOT_A_KEY: 'x' }), /not a setting/);
  assert.throws(() => eng.saveKeys({ GROQ_API_KEY: 'two words' }), /one line/);
  const saved = eng.saveKeys({ GROQ_API_KEY: 'gsk_secret_value_1234' });
  assert.equal(saved.offline, false);
  assert.equal(saved.providers.find(p => p.id === 'groq').masked, '…1234');
  assert.ok(!JSON.stringify(saved).includes('gsk_secret_value_1234'));
  const rows = await eng.testKey('groq', {});
  assert.equal(rows[0].status, 'REJECTED');
  assert.ok(!JSON.stringify(rows).includes('gsk_secret_value_1234'));
  assert.equal(eng.saveKeys({ OPENROUTER_API_KEY: 'wrongprefix123456' }).warnings.length, 1);
  assert.equal(eng.saveKeys({ GROQ_API_KEY: '' }).providers.find(p => p.id === 'groq').set, false);
});

test('blocked browser call: clear message, and the relay is used only when allowed', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: reply(CTX + 'via the relay it works fine.') });
  const calls = [];
  const storage = E.makeStorage(null); storage.set('ctxos:v1:keys', { GROQ_API_KEY: 'gsk_test_key_123456' });
  const net = { fetch: (url, init) => { calls.push(url); if (!url.startsWith('/relay/')) return Promise.reject(new TypeError('Failed to fetch'));
    return fetch(`http://127.0.0.1:${fake.port}/x`, { ...init, headers: { ...init.headers, 'x-fake-host': 'relay' } }); } };
  const eng = new E.Engine({ storage, net, sleep: () => Promise.resolve() });
  let ev = await turn(eng, { prompt: 'Design a schema' });
  assert.match(ev.at(-1).attempts[0].error, /Turn on the relay/);
  assert.ok(calls.every(u => !u.startsWith('/relay/')), 'the relay is off by default');
  eng.cooldown.clear(); eng.setRelay(true);
  ev = await turn(eng, { prompt: 'Design a schema again please' });
  assert.equal(ev.at(-1).type, 'done');
  assert.ok(calls.some(u => u.startsWith('/relay/api.groq.com/')));
});

test('the /api shim speaks the same contract as the Python server', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: reply(CTX + 'Answer text here.') });
  const eng = mkEngine(fake);
  const call = async (p, body) => { const r = await E.handleApi(eng, p, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }); return r; };
  const chat = await call('/api/chat', { prompt: 'Design a database schema' });
  assert.match(chat.headers.get('content-type'), /ndjson/);
  const lines = (await chat.text()).trim().split('\n').map(JSON.parse);
  const cid = lines[0].conversation.id;
  assert.equal(lines.at(-1).type, 'done');
  assert.equal((await (await call('/api/conversations')).json()).conversations.length, 1);
  assert.ok((await (await call(`/api/conversations/${cid}/memory`)).json()).units.length >= 2);
  const port = await (await call(`/api/conversations/${cid}/portable?size=compact`)).json();
  assert.match(port.text, /^# Context:/); assert.ok(port.chars < 4800);
  const dl = await call(`/api/conversations/${cid}/export`);
  assert.match(dl.headers.get('content-disposition'), /attachment; filename=".*\.md"/);
  const h = await (await call(`/api/conversations/${cid}/handoff`, { direction: 'escalate' })).json();
  assert.match(h.markdown, /# Task handoff \(escalate\)/);
  assert.equal((await call('/api/builds')).status, 200);
  assert.equal((await call('/api/builds', { goal: 'x' })).status, 400);
  assert.equal((await call('/api/conversations/aaaaaaaaaaaa')).status, 404);
  await call(`/api/conversations/${cid}/rename`, { title: 'Renamed' });
  assert.equal(eng.chats.get(cid).title, 'Renamed');
  await call(`/api/conversations/${cid}/delete`, {});
  assert.equal(eng.chats.list().length, 0);
  assert.equal(eng.ctxs[cid], undefined);
});

test('state survives a reload of the page', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close()); fake.set({ default: reply(CTX + 'Saved answer here please.') });
  const storage = E.makeStorage(null); storage.set('ctxos:v1:keys', { GROQ_API_KEY: 'gsk_test_key_123456' });
  const net = { fetch: (url, init) => fetch(`http://127.0.0.1:${fake.port}/x`, { ...init, headers: { ...init.headers, 'x-fake-host': 'api.groq.com' } }) };
  const a = new E.Engine({ storage, net });
  const ev = await turn(a, { prompt: 'Design a database schema' });
  const cid = ev[0].conversation.id;
  const b = new E.Engine({ storage, net });                  // new page load, same storage
  assert.equal(b.chats.list().length, 1);
  assert.equal(b.chats.messages(cid).length, 2);
  assert.ok(b.memory(cid).units.some(u => u.address === '/task/rules/tax'));
});

test('sampling temperature follows the kind of prompt', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  fake.set({ default: reply(CTX + 'Done, with enough words to show.') });
  const eng = mkEngine(fake);
  const route = async prompt => (await turn(eng, { prompt })).find(e => e.type === 'route');
  assert.equal((await route('write a poem about the sea')).temperature, 0.9);
  assert.equal((await route('Write a Python function to reverse a linked list')).temperature, 0.2);
  assert.equal((await route('tell me about the weather today')).temperature, 0.6);
  const sent = fake.calls.map(c => c.payload.temperature);
  assert.deepEqual(sent, [0.9, 0.2, 0.6]);
});

test('the store is tidied after each turn without losing history', async (tc) => {
  const fake = await startFake(); tc.after(() => fake.close());
  fake.set({ default: reply('<context>\nfact | /task/notes/a | the very same sentence here\nfact | /task/notes/b | The  very same SENTENCE here\n</context>\nAnswer with enough words.') });
  const eng = mkEngine(fake);
  const ev = await turn(eng, { prompt: 'Design a database schema' });
  const cid = ev[0].conversation.id;
  const live = eng.memory(cid).units.map(u => u.address).filter(a => a.startsWith('/task/notes/'));
  assert.equal(live.length, 1, 'the duplicate was merged');
  assert.ok(eng.ctxFor(cid).store.units.get('/task/notes/a') || eng.ctxFor(cid).store.units.get('/task/notes/b'), 'superseded, not deleted');
});
