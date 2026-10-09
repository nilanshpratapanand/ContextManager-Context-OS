# API reference

[← Back to README](../README.md)

ContextOS can be used four ways: through the **chat app's HTTP API**, as a **Python
library**, from the **command line**, or as an **MCP server** that other AI tools
connect to.

**Contents:** [HTTP API](#http-api) · [Streaming events](#streaming-events) · [Python library](#python-library) · [Command line](#command-line) · [MCP server](#mcp-server)

---

## HTTP API

Start the server with `python -m contextos.server`. It listens on `http://127.0.0.1:8000`
only.

> **Security:** the server holds your API keys, so it accepts only same-origin requests.
> `Host` must be `127.0.0.1` or `localhost`, any `Origin` must be this server, and
> `POST` bodies must be `Content-Type: application/json`. Everything else gets `403`.

### Conversations

| Method | Path | Body / query | Returns |
|---|---|---|---|
| `GET` | `/api/conversations` | `?q=text` searches titles and messages | `{conversations: [{id, title, created, updated}]}` |
| `POST` | `/api/conversations` | `{}` | The new conversation |
| `GET` | `/api/conversations/<id>` | — | The conversation with `messages` |
| `POST` | `/api/conversations/<id>/rename` | `{title}` | The updated conversation |
| `POST` | `/api/conversations/<id>/delete` | `{}` | `{deleted}`. Also deletes its memory |

### Chat

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/api/chat` | see below | A stream of [events](#streaming-events), one JSON object per line |

```json
{
  "conversation_id": "a1b2c3d4e5f6",   // omit to start a new chat
  "prompt": "Design the bookings table",
  "lane": "auto",                      // "auto" | "smart" | "fast"
  "route": "groq",                     // optional: try this model first
  "regenerate": "<assistant message id>",   // optional: replace that reply
  "edit": "<user message id>"               // optional: replace that message and what follows
}
```

Closing the connection stops the reply. The partial answer is saved, and no memory
is written from it.

### Memory and export

| Method | Path | Body / query | Returns |
|---|---|---|---|
| `GET` | `/api/conversations/<id>/memory` | — | `{units, stats, conflicts}` |
| `POST` | `/api/conversations/<id>/forget` | `{address}` | `{forgotten}` |
| `POST` | `/api/conversations/<id>/handoff` | `{direction}` | The handoff packet a new model would get |
| `GET` | `/api/conversations/<id>/portable` | `?size=compact\|standard\|full` | `{text, chars, tokens, items, left_out}`: paste-ready Markdown for another AI |
| `GET` | `/api/conversations/<id>/portable` | `…&download=1` | The same, as a `.md` file download |
| `GET` | `/api/conversations/<id>/export` | — | The full transcript as a `.md` download |

### Models

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/state` | `{offline, providers: [{name, lane, model, cooldown, failed}], routing}` |
| `POST` | `/api/check` | One real call per configured model: `{rows: [{provider, status, model, detail}]}` |
| `POST` | `/api/fail` | `{provider}` toggles a simulated failure, for demonstrating handoffs |

### API keys

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/api/keys` | — | Each provider: signup link, steps, free tier, `set`, `masked` (last 4 characters only) |
| `POST` | `/api/keys/test` | `{provider, values: {ENV_NAME: key}}` | One real call per model: `OK`, `LIMITED`, `BUSY`, `REJECTED` or `FAIL`. Nothing is saved |
| `POST` | `/api/keys/save` | `{values: {ENV_NAME: key or ""}}` | Writes `.env` (an empty value removes the key) and reloads the models |

### Build mode, connectors, skills

`/api/builds…`, `/api/connectors` and `/api/skills` are documented in
[AGENT.md → HTTP API](AGENT.md#http-api).

### Example

```bash
curl -N http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A shirt costs 800, 10% off, then 18% tax. Final price?"}'
```

## Streaming events

`POST /api/chat` returns [NDJSON](https://github.com/ndjson/ndjson-spec): one JSON object
per line, in this order.

| `type` | When | Fields |
|---|---|---|
| `start` | First | `conversation`, `user_message` |
| `route` | Lane chosen | `lane`, `difficulty`, `reasons`, `forced` |
| `model` | A model is being tried | `provider`, `model`, `lane` |
| `thinking` | The model streams its reasoning | `text` |
| `delta` | New answer text | `text` (append it) |
| `replace` | Visible text changed shape | `text` (replace everything) |
| `reset` | The model failed mid-reply; its partial answer is discarded | — |
| `switch` | Handing off to the next model | `from`, `to`, `direction`, `reason`, `packet_tokens`, `full_replay_tokens`, `omitted`, `gate` |
| `error` | Every model failed | `error`, `attempts` |
| `done` | Last | `message`: the saved reply, whose `meta` has `provider`, `model`, `lane_used`, `difficulty`, `switched`, `written`, `tokens`, `thought_ms`, `ms` |

## Python library

```python
from contextos import ContextOS

ctx = ContextOS("project.db")
ctx.put("/task/goal", "Replace cookie sessions with OAuth2", kind="goal", pinned=True)
ctx.put("/project/decisions/oauth-lib", "authlib, already vendored in the monorepo",
        kind="decision", source="architect", importance=0.9)
ctx.put_artifact("/artifact/auth/token", "src/auth/token.py", source_code)

packet = ctx.handoff(direction="escalate", budget_tokens=1200,
                     from_model="haiku-4.5", to_model="opus-4.7", difficulty=0.75)
print(packet.render())         # 13,373 stored tokens -> 634 sent
```

Conflicts are surfaced, never silently resolved:

```python
ctx.put("/project/decisions/oauth-lib", "oauthlib", source="second-opinion-agent")
ctx.conflicts()   # architect vs second-opinion-agent, both values preserved
```

The packet then carries a warning rather than picking a winner.

| Method | What it does |
|---|---|
| `put(address, value, kind=, source=, importance=, pinned=, supersede=)` | Save or update a unit |
| `put_artifact(address, path, content)` | Record a file by path and sha256 |
| `get(address)` / `list(prefix)` / `delete(address)` | Read, list or remove |
| `search(query)` | Hybrid search over all units |
| `select(query, budget_tokens=)` | The most relevant units that fit a token budget |
| `handoff(direction=, budget_tokens=, …)` | A direction-aware packet for another model |
| `conflicts()` / `stats()` | Unresolved conflicts / store size |

Addresses start with `/user`, `/project`, `/task`, `/agent`, `/tool` or `/artifact`.
See [RESEARCH.md](RESEARCH.md#address-space).

## Command line

```bash
python -m contextos.cli put /project/decisions/db "PostgreSQL 16" --kind decision --importance .9
python -m contextos.cli search "what database did we pick"
python -m contextos.cli handoff --direction escalate --budget 2000
python -m contextos.cli bench
```

| Command | What it does |
|---|---|
| `put` / `get` / `ls` | Save, read, list units |
| `search` / `select` | Hybrid search / pack context under a budget |
| `handoff` | Build a handoff packet |
| `stats` / `conflicts` | Store statistics / unresolved conflicts |
| `bench` / `demo` | Run the benchmark / the end-to-end demo |

After `pip install .` the same commands are available as `contextos …`.

## MCP server

Let any MCP-capable AI tool read and write ContextOS memory:

```json
{ "mcpServers": { "contextos": {
    "command": "python", "args": ["-m", "contextos.mcp_server", "--db", "./project.db"] } } }
```

Seven tools: `context_put`, `context_get`, `context_search`, `context_select`,
`context_handoff`, `context_stats` and `context_conflicts`. It uses raw JSON-RPC over
stdio, with no SDK dependency.
