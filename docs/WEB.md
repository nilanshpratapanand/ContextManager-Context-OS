# Web edition

The same chat app, running entirely in the browser, so it can be hosted on a static site such as Netlify. There is no server: the engine (memory store, retrieval, handoff, routing) is a JavaScript port of the Python one in `webapp/engine.js`, and the page talks to the AI providers directly.

## What is different from the desktop app

| | Desktop app | Web edition |
|---|---|---|
| Chat, streaming, smart/fast routing | yes | yes |
| Memory ledger, handoff on failure, "Continue in another AI" | yes | yes |
| Where chats and memory live | SQLite files on your computer | this browser (localStorage) |
| Where API keys live | `.env` on your computer | this browser, sent only to the provider they belong to |
| Build mode, connectors (MCP), skills | yes | no (they run commands and write files) |
| File attachments, Pipeline mode | yes | not yet |
| Works offline | yes (with Ollama) | the page needs the network; Ollama on `localhost` still works |

With no key saved, the web edition runs in **demo mode** with simulated replies so visitors can look around before adding one.

## Deploy to Netlify

1. In Netlify choose **Add new project, Import an existing project, GitHub** and pick this repository (the screen with "Import a Git repository").
2. Leave the settings as they are. `netlify.toml` already sets the publish directory to `web` and there is no build command.
3. Click **Deploy**. Open the site, click the key icon, paste a free key and chat.

To publish a change, edit the sources and rebuild `web/`:

```bash
python tools/build_web.py     # regenerates web/ from contextos/dashboard.html, webapp/engine.js and contextos/static/
```

`web/` is committed so Netlify needs no build step. The test suite fails if it is out of date.

## How the keys are protected

Nobody's key goes through your server. Each visitor pastes their own, and it is stored in their browser.

- `netlify.toml` sets a Content Security Policy whose `connect-src` allows only this site, the listed AI providers and a local Ollama. Page code therefore cannot send a key anywhere else, even if something went wrong. The browser tests check this.
- Model output is escaped before it is shown, and links are limited to `http(s)` and `mailto`.
- Keys are masked after saving and removed from error messages. **Remove** deletes a key from the browser.
- On a shared computer, remove your keys when you are done. Clearing the site's data removes them and the chats.

The policy allows inline scripts, because the page's script is inline. Treat that as the weak point: keep the page's rendering code free of `innerHTML` on unescaped text.

## Providers and browser access

A web page can call a provider directly only if that provider answers browser (CORS) pre-flight requests. Checked on 2026-10-29 from a test machine: **Google Gemini** accepts calls from an arbitrary web origin. The other providers could not be reached from that machine, so their behaviour is not verified here: use the **Test** button in *Set up models*, which reports a clear message if a provider blocks the browser.

For a provider that does, an optional **relay** (`netlify/edge-functions/relay.js`) can forward the call. It is off by default and the user turns it on in *Set up models*. It forwards only to the allow-listed hosts, over HTTPS, for same-site callers, and passes the user's key through for that one request without storing or logging it. Delete the file to remove the relay. Ollama needs `OLLAMA_ORIGINS` set to your site's address.

## Limits

- Browser storage is about 5 MB per site. Long chats fill it; use **Continue in another AI** to export before clearing.
- Chats do not sync between devices.
- The relay has not been run on Netlify itself (its logic is unit-tested); try it once after your first deploy.

## Tests

```bash
node --test tests/web/*.test.mjs                 # engine, handoff, API contract, relay, parity with the Python engine
python tests/test_contextos.py                   # includes "web/ is up to date" and the policy check
PWMOD=$(npm root -g)/playwright node tests/web/e2e.mjs   # real browser: keys, handoff, reload, export, policy
```
