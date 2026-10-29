// Optional relay for the web edition: forwards one streaming request to an allow-listed
// AI provider for browsers that cannot call it directly (the provider sends no CORS
// headers). The user turns it on in "Set up models"; it is off by default.
//
// What it does and does not do:
//   - forwards only to the hosts in ALLOW, over https, POST only, same-site callers only
//   - passes the caller's own key through for that one request: it holds no key of its
//     own, and nothing is stored or logged here
//   - streams the answer straight back
// It is an open pipe to those hosts for anyone who can reach this site and has a key, so
// the usual cost to you is edge-function invocations. Delete this file to switch the relay
// off entirely; the app then simply reports that the provider blocks browser calls.

export const ALLOW = new Set([
  'api.groq.com', 'generativelanguage.googleapis.com', 'openrouter.ai', 'integrate.api.nvidia.com',
  'api.cloudflare.com', 'api.mistral.ai', 'api.cohere.ai', 'api.anthropic.com',
]);
const FORWARD = ['content-type', 'accept', 'authorization', 'x-api-key', 'x-goog-api-key',
  'anthropic-version', 'anthropic-dangerous-direct-browser-access'];
const MAX_BODY = 1_000_000;

const plain = (status, text) => new Response(text, { status, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } });

export async function handle(request, upstreamFetch = fetch) {
  const url = new URL(request.url);
  const m = /^\/relay\/([^/]+)(\/.*)?$/.exec(url.pathname);
  if (!m) return plain(404, 'Not found');
  const host = decodeURIComponent(m[1]).toLowerCase();
  if (!ALLOW.has(host)) return plain(403, 'That host is not allowed');
  if (request.method !== 'POST') return plain(405, 'POST only');
  if (request.headers.get('origin') !== url.origin) return plain(403, 'Cross-site use is not allowed');

  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return plain(413, 'Request too large');
  const headers = new Headers();
  for (const name of FORWARD) { const v = request.headers.get(name); if (v) headers.set(name, v); }

  let upstream;
  try {
    upstream = await upstreamFetch(`https://${host}${m[2] || '/'}${url.search}`, { method: 'POST', headers, body, redirect: 'manual' });
  } catch {
    return plain(502, 'Could not reach the provider');
  }
  const out = new Headers({ 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
  const type = upstream.headers.get('content-type');
  if (type) out.set('Content-Type', type);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export default (request) => handle(request);
export const config = { path: '/relay/*' };
