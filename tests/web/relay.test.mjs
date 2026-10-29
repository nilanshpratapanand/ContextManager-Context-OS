import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const { handle, ALLOW } = await import(pathToFileURL(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'netlify', 'edge-functions', 'relay.js')).href);
const E = (await import('node:module')).createRequire(import.meta.url)(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'webapp', 'engine.js'));

const req = (p, { method = 'POST', origin = 'https://site.example', headers = {}, body = '{"x":1}' } = {}) =>
  new Request('https://site.example' + p, { method, headers: { ...(origin ? { origin } : {}), 'content-type': 'application/json', ...headers }, body: method === 'POST' ? body : undefined });

test('forwards an allowed host, passing only the needed headers, and streams back', async () => {
  let seen;
  const up = async (url, init) => { seen = { url, init }; return new Response('data: hi\n\n', { status: 200, headers: { 'content-type': 'text/event-stream', 'set-cookie': 'a=b' } }); };
  const r = await handle(req('/relay/api.groq.com/openai/v1/chat/completions?x=1', { headers: { authorization: 'Bearer k', cookie: 'secret=1', 'x-forwarded-for': '1.2.3.4' } }), up);
  assert.equal(r.status, 200); assert.equal(await r.text(), 'data: hi\n\n');
  assert.equal(seen.url, 'https://api.groq.com/openai/v1/chat/completions?x=1');
  assert.equal(seen.init.headers.get('authorization'), 'Bearer k');
  assert.equal(seen.init.headers.get('cookie'), null); assert.equal(seen.init.headers.get('x-forwarded-for'), null);
  assert.equal(r.headers.get('set-cookie'), null);
  assert.equal(r.headers.get('cache-control'), 'no-store');
});

test('refuses unknown hosts, other methods, other sites and oversized bodies', async () => {
  const up = async () => { throw new Error('must not be called'); };
  assert.equal((await handle(req('/relay/evil.example/x'), up)).status, 403);
  assert.equal((await handle(req('/relay/api.groq.com.evil.example/x'), up)).status, 403);
  assert.equal((await handle(req('/relay/api.groq.com/x', { method: 'GET' }), up)).status, 405);
  assert.equal((await handle(req('/relay/api.groq.com/x', { origin: 'https://other.example' }), up)).status, 403);
  assert.equal((await handle(req('/relay/api.groq.com/x', { origin: null }), up)).status, 403);
  assert.equal((await handle(req('/relay/api.groq.com/x', { body: 'x'.repeat(1_000_001) }), up)).status, 413);
  assert.equal((await handle(req('/other'), up)).status, 404);
});

test('an unreachable provider is a clean 502, and the relay list matches the engine', async () => {
  const r = await handle(req('/relay/api.groq.com/x'), async () => { throw new Error('boom'); });
  assert.equal(r.status, 502);
  assert.deepEqual([...ALLOW].sort(), [...E.RELAY_HOSTS].sort());
});
