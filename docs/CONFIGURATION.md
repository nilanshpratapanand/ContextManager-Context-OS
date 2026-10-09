# Configuration

[← Back to README](../README.md)

**Contents:** [The `.env` file](#the-env-file) · [Providers and models](#providers-and-models) · [Routing](#routing) · [Changing a model](#changing-a-model) · [Server options](#server-options) · [Where data is stored](#where-data-is-stored) · [Checking your setup](#checking-your-setup)

---

## The `.env` file

**The quickest way to add keys is the app itself:** the key icon (bottom left) opens
*Set up models*. For each provider it shows signup steps, a link, a paste box, and
**Test** (one small real call per model) and **Save** buttons. Saving:

- writes only known key names, and rejects values with spaces or line breaks, so nothing
  else can be slipped into `.env`;
- keeps every other line and comment, and writes a temporary file then swaps it in, so
  `.env` is never left half-written. On macOS/Linux it's also made readable only by you;
- reloads the models with no restart. If ContextOS started in offline mode only because
  there were no keys, it switches to real models;
- never sends a saved key back to the page, only its last four characters.

All settings live in `.env` in the ContextOS folder. The installer creates it from
[`.env.example`](../.env.example), which lists every provider with a signup link. Put
one setting per line, with no quotes needed:

```ini
GROQ_API_KEY=gsk_...
OPENROUTER_API_KEY=sk-or-...
```

`.env` is ignored by git, and keys are never logged or sent to the browser. Restart
ContextOS after editing it.

## Providers and models

Each provider gives you **two models on one key**: its strongest free model (smart lane)
and its quickest (fast lane). The fast route is named `<provider>-fast`.

| Provider | Key variable | Smart model | Fast model | Free allowance* |
|---|---|---|---|---|
| Groq | `GROQ_API_KEY` | `openai/gpt-oss-120b` | `openai/gpt-oss-20b` | 30 req/min, 1,000 req/day per model |
| OpenRouter | `OPENROUTER_API_KEY` | `nvidia/nemotron-3-ultra-550b-a55b:free` | `z-ai/glm-5.2:free` | 20 req/min, 50 req/day |
| Cloudflare | `CLOUDFLARE_API_KEY` + `CLOUDFLARE_ACCOUNT_ID` | `@cf/openai/gpt-oss-120b` | `@cf/openai/gpt-oss-20b` | 10,000 neurons/day |
| Cohere | `COHERE_API_KEY` | `command-a-plus-05-2026` | `command-r7b-12-2024` | 1,000 calls/month, non-commercial |
| Google Gemini | `GEMINI_API_KEY` | `gemini-flash-latest` | `gemini-flash-lite-latest` | Flash only; Pro is paid |
| Mistral | `MISTRAL_API_KEY` | `mistral-medium-latest` | `ministral-8b-latest` | Free plan, frequent rate limits |
| NVIDIA | `NVIDIA_API_KEY` | `z-ai/glm-5.3` | `nvidia/nemotron-3.5-lightning-30b-a3b` | 40 req/min, free credits |
| Ollama | `OLLAMA_API_KEY=on` | — | `llama3.1:8b` (local) | Unlimited, offline |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` | — | Paid; used only if you add a key |

\* Checked September 2026. Free tiers change often; run [`--check`](#checking-your-setup)
when something stops working.

**Cloudflare account ID:** the 32-character code in your dashboard URL,
`dash.cloudflare.com/<account-id>/…`. Pasting the whole URL also works.

**Not included:** Cerebras (now needs payment), Together (no free tier), GitHub Models
(shut down July 2026).

## Routing

Every message gets a difficulty score from 0 to 1 using fixed rules. No model is called
to decide.

| Raises the score | Lowers it |
|---|---|
| Design / debug / analyse / compare / prove / plan… words | Greetings and thanks (set to 0.05) |
| Code, or a request to write code | Rephrase / translate / summarise / define… |
| Two or more numbers with maths words (%, tax, total, price…) | |
| Long messages, several questions or numbered parts | |

A score at or above the threshold (default **0.5**) goes to the **smart** lane, and
anything lower to the **fast** lane. If every model in a lane fails, the other lane is
tried.

| Setting | Default | Meaning |
|---|---|---|
| `LLM_ROUTING` | `auto` | `auto` scores each message; `smart` or `fast` always use that lane |
| `LLM_ROUTE_THRESHOLD` | `0.5` | Lower it to use the smart lane more often |
| `LLM_SMART_ORDER` | `groq,openrouter,cloudflare,cohere,gemini,mistral,nvidia,anthropic` | Order to try smart models in |
| `LLM_FAST_ORDER` | `groq-fast,openrouter-fast,cloudflare-fast,cohere-fast,gemini-fast,mistral-fast,nvidia-fast,ollama` | Order to try fast models in |

Providers without a key are skipped, so you only set an order to change priority. In
the chat you can also start a message with `/smart` or `/fast`, pick a lane in the top
bar, or choose a model to try first.

**Resting failed models.** When a model fails, it is skipped for a while:

| Failure | Rested for |
|---|---|
| Rate limited (HTTP 429, quota) | 60 s |
| Busy or slow (HTTP 502/503, timeout, network) | 30 s |
| Retired model, paywall or bad key (HTTP 401/402/403/404) | 1 hour |
| Anything else (e.g. a garbled reply) | 15 s |

## Changing a model

Free models get retired. To swap one without editing code, use the provider name in
capitals plus `_MODEL`. Fast routes add `_FAST`:

```ini
GROQ_MODEL=openai/gpt-oss-120b
GROQ_FAST_MODEL=openai/gpt-oss-20b
NVIDIA_MODEL=z-ai/glm-5.3-flash
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

To see which model IDs your key can use:

```bash
python -m contextos.live --models            # every configured provider
python -m contextos.live --models groq       # one provider
```

## Server options

```bash
python -m contextos.server [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--port` | `8000` | Port for http://127.0.0.1:PORT |
| `--data` | `chat_data` | Folder for chats and their memory |
| `--env` | `.env` | Settings file to read |
| `--budget` | `1500` | Tokens of memory sent to the model per message |
| `--offline` | off | Simulated replies, no keys needed |
| `--no-browser` | off | Don't open a browser tab on start |

## Where data is stored

```
chat_data/
├── chats.db          conversations and messages (what you see)
├── ctx/<chat-id>.db  one memory store per chat (what models get)
└── offline/          chats made in --offline mode, kept separate
```

Deleting a chat in the app deletes its memory file too. To wipe everything, delete
`chat_data/` (on Windows, `RUN.bat` → **R** does this). All of it stays on your computer.

## Checking your setup

```bash
python -m contextos.live --check      # one real call to every configured model
```

| Status | Meaning |
|---|---|
| `OK` | Working |
| `LIMITED` | Key is valid; the free tier is rate-limited right now |
| `BUSY` | Key is valid; the provider is overloaded or slow right now |
| `FAIL` | Wrong model name, bad key or paywall. The message says which |
| `no key` | Not configured |

On Windows this is `RUN.bat` → **T**; on macOS/Linux, `./run.sh check`.
