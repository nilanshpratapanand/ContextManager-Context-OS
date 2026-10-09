"""Live cross-model handoff evaluation.

`bench.py` measures state *sufficiency* offline - a necessary condition. This
module measures the thing sufficiency only bounds: does the task still come out
right after the model changes?

Method, scaled down from arXiv 2608.24358:

    phase A   model A works the first k steps and commits state as it goes
    handoff   the transcript is converted to a transfer by one of four interfaces
    phase B   model B continues from that transfer alone and produces an answer
    score     the answer is checked programmatically - no LLM judge

The tasks are built so that a constraint established in an early step changes the
correct final answer. If the interface drops that constraint, model B produces a
specific, predictable wrong answer. That makes "did the context survive" directly
observable in the output rather than inferred.

    python -m contextos.live --list-providers
    python -m contextos.live --a groq --b gemini --repeats 3

Keys are read from .env in the working directory. Nothing is logged that contains
a key.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from . import ContextOS
from .budget import render
from .units import count_tokens

# --------------------------------------------------------------------------
# env
# --------------------------------------------------------------------------
def load_env(path: str = ".env") -> dict[str, str]:
    """Parse a .env. Tolerates CRLF and a UTF-8 BOM, which is how a file edited
    on Windows usually arrives."""
    out: dict[str, str] = {}
    p = pathlib.Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v:
            out[k.strip()] = v
    return out


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
@dataclass
class Provider:
    name: str
    key_env: str
    model: str
    url: str
    style: str = "openai"          # "openai" | "gemini" | "anthropic"
    tier: float = 0.5              # rough capability; picks the handoff direction
    lane: str = "smart"            # "smart" | "fast" - see router.py
    # Extra request fields. gpt-oss models spend their whole output budget
    # thinking unless told how hard to think (measured: 2046/2048 tokens).
    extra: dict[str, Any] = field(default_factory=dict)

    def available(self, env: dict[str, str]) -> bool:
        return bool(env.get(self.key_env))


# Every provider appears twice: its strongest free model (smart lane) and its
# quickest (fast lane, name ends in "-fast"). Both share one key.
# Model IDs verified against each provider's live model list on 2026-09-24.
# Where a provider offers a "-latest" alias it is used, because retired model
# names are the most common way a free setup silently breaks.
_GROQ = "https://api.groq.com/openai/v1/chat/completions"
_GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
_NVIDIA = "https://integrate.api.nvidia.com/v1/chat/completions"
_CLOUDFLARE = ("https://api.cloudflare.com/client/v4/accounts/"
               "{account_id}/ai/v1/chat/completions")
_MISTRAL = "https://api.mistral.ai/v1/chat/completions"
_COHERE = "https://api.cohere.ai/compatibility/v1/chat/completions"

PROVIDERS: dict[str, Provider] = {
    # Groq: no card, 30 RPM / 1,000 RPD per model. Fastest. Llama was removed
    # from the free plan in 2026; gpt-oss is what remains.
    "groq": Provider("groq", "GROQ_API_KEY", "openai/gpt-oss-120b", _GROQ, tier=0.8,
                     extra={"reasoning_effort": "medium"}),
    "groq-fast": Provider("groq-fast", "GROQ_API_KEY", "openai/gpt-oss-20b", _GROQ,
                          tier=0.6, lane="fast", extra={"reasoning_effort": "low"}),
    # Google AI Studio: no card. Flash only - Pro moved behind billing.
    "gemini": Provider("gemini", "GEMINI_API_KEY", "gemini-flash-latest", _GEMINI,
                       style="gemini", tier=0.85),
    "gemini-fast": Provider("gemini-fast", "GEMINI_API_KEY", "gemini-flash-lite-latest",
                            _GEMINI, style="gemini", tier=0.6, lane="fast"),
    # OpenRouter: no card, 20 RPM / 50 RPD (1,000 RPD after a one-time $10 top-up).
    "openrouter": Provider("openrouter", "OPENROUTER_API_KEY",
                           "nvidia/nemotron-3-ultra-550b-a55b:free", _OPENROUTER,
                           tier=0.85),
    "openrouter-fast": Provider("openrouter-fast", "OPENROUTER_API_KEY",
                                "z-ai/glm-5.2:free", _OPENROUTER,
                                tier=0.55, lane="fast"),
    # NVIDIA build.nvidia.com: no card, free credits, 40 RPM. Correct but slow
    # (30-40 s per reply measured), so it sits late in both lanes.
    "nvidia": Provider("nvidia", "NVIDIA_API_KEY", "z-ai/glm-5.3", _NVIDIA,
                       tier=0.9),
    "nvidia-fast": Provider("nvidia-fast", "NVIDIA_API_KEY",
                            "nvidia/nemotron-3.5-lightning-30b-a3b", _NVIDIA,
                            tier=0.55, lane="fast"),
    # Cloudflare Workers AI: no card, 10,000 neurons/day. URL carries the account id.
    "cloudflare": Provider("cloudflare", "CLOUDFLARE_API_KEY", "@cf/openai/gpt-oss-120b",
                           _CLOUDFLARE, tier=0.8, extra={"reasoning_effort": "medium"}),
    "cloudflare-fast": Provider("cloudflare-fast", "CLOUDFLARE_API_KEY",
                                "@cf/openai/gpt-oss-20b", _CLOUDFLARE, tier=0.6,
                                lane="fast", extra={"reasoning_effort": "low"}),
    # Mistral: no card, free plan, trains on your data by default. Often 429s.
    "mistral": Provider("mistral", "MISTRAL_API_KEY", "mistral-medium-latest", _MISTRAL,
                        tier=0.75),
    "mistral-fast": Provider("mistral-fast", "MISTRAL_API_KEY", "ministral-8b-latest",
                             _MISTRAL, tier=0.45, lane="fast"),
    # Cohere: no card, 1,000 calls/month, non-commercial use only.
    "cohere": Provider("cohere", "COHERE_API_KEY", "command-a-plus-05-2026", _COHERE,
                       tier=0.8),
    "cohere-fast": Provider("cohere-fast", "COHERE_API_KEY", "command-r7b-12-2024",
                            _COHERE, tier=0.45, lane="fast"),
    # Your own machine: no key, no quota, works offline. OLLAMA_API_KEY=local enables it.
    "ollama": Provider("ollama", "OLLAMA_API_KEY", "llama3.1:8b",
                       "http://localhost:11434/v1/chat/completions", tier=0.35,
                       lane="fast"),
    # Paid, never used unless you add a key.
    "anthropic": Provider("anthropic", "ANTHROPIC_API_KEY", "claude-haiku-4-5",
                          "https://api.anthropic.com/v1/messages",
                          style="anthropic", tier=0.85),
}

# Default fallback order inside each lane. Override with LLM_SMART_ORDER /
# LLM_FAST_ORDER in .env. Ordered by latency measured on 2026-09-24
# (Groq ~1s, OpenRouter/Cloudflare ~3s, NVIDIA ~30-40s, Gemini often 503).
SMART_ORDER = ["groq", "openrouter", "cloudflare", "cohere", "gemini", "mistral",
               "nvidia", "anthropic"]
FAST_ORDER = ["groq-fast", "openrouter-fast", "cloudflare-fast", "cohere-fast",
              "gemini-fast", "mistral-fast", "nvidia-fast", "ollama"]


# Cloudflare puts the account id in the path, not a header.
def local_only(env: dict[str, str]) -> bool:
    """LLM_LOCAL_ONLY=1: use only models served from this machine (Ollama); nothing
    leaves the computer, including image descriptions."""
    return env.get("LLM_LOCAL_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def is_local(p: "Provider", env: dict[str, str]) -> bool:
    return resolve_url(p, env).startswith(("http://localhost", "http://127.0.0.1"))


def usable(p: "Provider", env: dict[str, str]) -> bool:
    return p.available(env) and (not local_only(env) or is_local(p, env))


def resolve_url(p: Provider, env: dict[str, str]) -> str:
    if "{account_id}" not in p.url:
        return p.url
    # Accept a pasted dashboard URL as well as the bare 32-hex id.
    m = re.search(r"[0-9a-f]{32}", env.get("CLOUDFLARE_ACCOUNT_ID", ""))
    acct = m.group(0) if m else ""
    if not acct:
        raise ProviderError("CLOUDFLARE_ACCOUNT_ID is not set in .env "
                            "(find it on your Cloudflare dashboard URL)")
    return p.url.replace("{account_id}", acct)

# groq -> GROQ_MODEL, groq-fast -> GROQ_FAST_MODEL
MODEL_ENV_OVERRIDE = {n: n.upper().replace("-", "_") + "_MODEL" for n in PROVIDERS}


class ProviderError(RuntimeError):
    pass


# Groq, Cerebras and Together all sit behind Cloudflare, which blocks urllib's
# default "Python-urllib/3.x" signature outright - every call comes back as
# HTTP 403 "error code: 1010" no matter how valid the key is. Sending an ordinary
# browser User-Agent is what gets past that filter.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def _post(url: str, payload: dict[str, Any], headers: dict[str, str],
          timeout: int) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          **BROWSER_HEADERS, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read(600).decode("utf-8", "replace")
        raise ProviderError(f"HTTP {e.code}: {detail[:300]}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network: {e.reason}") from None
    # A slow provider times out mid-read as a bare TimeoutError/OSError, not a
    # URLError. Uncaught, it would end the turn instead of falling back.
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"network: {type(e).__name__}: {e}") from None


# Bump this whenever live.py changes in a way you need to confirm reached the
# user's machine. It is printed by --check.
BUILD = "2026-09-24-streaming-chat"

_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Qwen and several other open models emit their chain of thought inside
    <think> tags. It is not part of the answer and must not reach the user or
    the context store."""
    out = _THINK.sub("", text or "")
    # An unclosed opening tag means the model was cut off mid-thought.
    if "<think>" in out.lower():
        out = re.split(r"<think>", out, flags=re.IGNORECASE)[0]
    return out.strip()


def complete(provider: Provider, system: str, user: str, env: dict[str, str], *,
             max_tokens: int = 700, temperature: float = 0.0,
             timeout: int = 60) -> tuple[str, int]:
    """Return (text, prompt_tokens). Never logs or returns the key."""
    key = env.get(provider.key_env)
    if not key:
        raise ProviderError(f"{provider.key_env} is not set")
    model = env.get(MODEL_ENV_OVERRIDE.get(provider.name, ""), provider.model)

    if provider.style == "openai":
        data = _post(resolve_url(provider, env), {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature, **provider.extra,
        }, {"Authorization": f"Bearer {key}"}, timeout)
        text = data["choices"][0]["message"]["content"] or ""
        used = (data.get("usage") or {}).get("prompt_tokens", 0)

    elif provider.style == "anthropic":
        data = _post(resolve_url(provider, env), {
            "model": model, "system": system, "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user}],
        }, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        used = (data.get("usage") or {}).get("input_tokens", 0)

    else:  # gemini
        url = f"{provider.url}/{model}:generateContent?key={key}"
        data = _post(url, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens},
        }, {}, timeout)
        cands = data.get("candidates") or []
        text = "".join(p.get("text", "")
                       for p in (cands[0]["content"]["parts"] if cands else []))
        used = (data.get("usageMetadata") or {}).get("promptTokenCount", 0)

    return strip_reasoning(text), int(used or count_tokens(system + user))


def _sse_lines(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: int) -> Iterator[str]:
    """Yield the payload of each `data:` line of a server-sent-event stream."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream",
                                          **{k: v for k, v in BROWSER_HEADERS.items()
                                             if k != "Accept"}, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    if data:
                        yield data
    except urllib.error.HTTPError as e:
        detail = e.read(600).decode("utf-8", "replace")
        raise ProviderError(f"HTTP {e.code}: {detail[:300]}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"network: {type(e).__name__}: {e}") from None


def stream_events(provider: Provider, system: str, user: str, env: dict[str, str], *,
                  max_tokens: int = 4096, temperature: float = 0.7,
                  timeout: int = 60) -> Iterator[tuple[str, str]]:
    """Yield ("text", chunk) for the answer and ("think", chunk) for reasoning the
    provider streams separately, as the provider produces them.

    Text is unfiltered: <think> and <context> blocks inside it are the caller's
    to hide. Any failure, before or mid-stream, is a ProviderError.
    """
    key = env.get(provider.key_env)
    if not key:
        raise ProviderError(f"{provider.key_env} is not set")
    model = env.get(MODEL_ENV_OVERRIDE.get(provider.name, ""), provider.model)

    if provider.style == "gemini":
        url = f"{provider.url}/{model}:streamGenerateContent?alt=sse&key={key}"
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens}}
        headers: dict[str, str] = {}
    elif provider.style == "anthropic":
        url = resolve_url(provider, env)
        payload = {"model": model, "system": system, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": True,
                   "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url = resolve_url(provider, env)
        # No stream_options: Cohere's compatibility API rejects the field.
        payload = {"model": model, "stream": True,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "max_tokens": max_tokens, "temperature": temperature,
                   **provider.extra}
        headers = {"Authorization": f"Bearer {key}"}

    finish = None
    thought = texted = 0
    for data in _sse_lines(url, payload, headers, timeout):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if obj.get("error") or obj.get("type") == "error":
            err = obj.get("error") or obj
            raise ProviderError(f"stream error: {json.dumps(err)[:300]}")
        if provider.style == "gemini":
            for cand in obj.get("candidates") or []:
                finish = cand.get("finishReason") or finish
                for part in (cand.get("content") or {}).get("parts") or []:
                    if part.get("text"):
                        kind = "think" if part.get("thought") else "text"
                        thought += kind == "think"
                        texted += kind == "text"
                        yield kind, part["text"]
        elif provider.style == "anthropic":
            delta = obj.get("delta") or {}
            if obj.get("type") == "content_block_delta":
                if delta.get("text"):
                    texted += 1
                    yield "text", delta["text"]
                elif delta.get("thinking"):
                    thought += 1
                    yield "think", delta["thinking"]
            finish = delta.get("stop_reason") or finish
        else:
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                finish = ch.get("finish_reason") or finish
                think = delta.get("reasoning") or delta.get("reasoning_content")
                if think:
                    thought += 1
                    yield "think", think
                if delta.get("content"):
                    texted += 1
                    yield "text", delta["content"]
    # Hit the cap after answering is a long answer; hitting it before any answer
    # means the whole budget went on thinking - worth falling back over.
    if finish in ("length", "MAX_TOKENS", "max_tokens") and thought and not texted:
        raise ProviderError("ran out of output tokens while still reasoning")


def _get(url: str, headers: dict[str, str], timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={**BROWSER_HEADERS, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace")
        raise ProviderError(f"HTTP {e.code}: {detail[:300]}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network: {e.reason}") from None
    # A slow provider times out mid-read as a bare TimeoutError/OSError, not a
    # URLError. Uncaught, it would end the turn instead of falling back.
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"network: {type(e).__name__}: {e}") from None


def models_url(p: Provider, env: dict[str, str]) -> str:
    url = resolve_url(p, env)
    if p.style == "gemini":
        return url                       # already the models collection
    if p.style == "anthropic":
        return url.replace("/messages", "/models")
    return url.replace("/chat/completions", "/models")


# Names that are clearly not chat models, so they only add noise to the listing.
_NOT_CHAT = ("embed", "whisper", "tts", "rerank", "moderation", "guard",
             "speech", "transcribe", "image", "veo", "imagen")


def list_models(provider_name: str, env: dict[str, str],
                timeout: int = 30, chat_only: bool = True) -> list[str]:
    """Ask the provider which models this key can actually use.

    Model IDs drift constantly and no documentation keeps up. The provider's own
    /models endpoint is the only answer that is true right now, for this key.
    """
    p = PROVIDERS[provider_name]
    key = env.get(p.key_env)
    if not key:
        raise ProviderError(f"{p.key_env} is not set")

    ids: list[str] = []
    if p.style == "gemini":
        data = _get(f"{resolve_url(p, env)}?key={key}&pageSize=200", {}, timeout)
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods") or []
            if not methods or "generateContent" in methods:
                ids.append(str(m.get("name", "")).replace("models/", ""))
    else:
        headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
                   if p.style == "anthropic" else {"Authorization": f"Bearer {key}"})
        data = _get(models_url(p, env), headers, timeout)
        items = data.get("data") or data.get("models") or [] \
            if isinstance(data, dict) else data
        for m in items or []:
            mid = m.get("id") or m.get("name") if isinstance(m, dict) else m
            if mid:
                ids.append(str(mid))

    if chat_only:
        ids = [m for m in ids if not any(w in m.lower() for w in _NOT_CHAT)]
    return sorted(set(ids))


def check(env: dict[str, str], timeout: int = 90) -> list[dict[str, Any]]:
    """Make one real, tiny call to every configured provider.

    Model names drift - a provider renames or retires a model and every call
    starts returning 404. Better to find that out in two seconds here than at
    the first message of a demo.
    """
    rows: list[dict[str, Any]] = []
    for name, p in PROVIDERS.items():
        if not p.available(env):
            rows.append({"provider": name, "status": "no key", "detail": "",
                         "model": ""})
            continue
        model = env.get(MODEL_ENV_OVERRIDE.get(name, ""), p.model)
        t0 = time.time()
        try:
            text, _ = complete(p, "Reply with the single word: ok",
                               "Say ok.", env, max_tokens=2000, timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            rows.append({"provider": name, "status": "OK", "model": model,
                         "detail": f"{ms} ms, replied {text[:40]!r}"})
        except ProviderError as exc:
            msg = str(exc)
            hint = ""
            # Distinguish "the provider answered and said no" from "we never
            # reached the provider". A network 403 from a proxy is not a bad key,
            # and saying so would send you debugging the wrong thing.
            if "Timeout" in msg or "timed out" in msg:
                hint = "  <- reached the provider but it was too slow; usually "                        "busy, try again later"
            elif msg.startswith("network:"):
                hint = "  <- could not reach the provider at all (no internet, " \
                       "firewall, VPN, or a proxy blocking it)"
            elif "1010" in msg:
                # Cloudflare's "browser signature banned". Nothing to do with the key.
                hint = "  <- Cloudflare blocked the client signature, NOT a bad key. " \
                       "This build sends a browser User-Agent to get past it; if you " \
                       "still see 1010, a VPN or the network is being filtered."
            elif "404" in msg or "model_not_found" in msg or "does not exist" in msg:
                hint = f"  <- model name {model!r} looks wrong; set " \
                       f"{MODEL_ENV_OVERRIDE.get(name, 'the model')} in .env"
            elif msg.startswith("HTTP 401") or msg.startswith("HTTP 403"):
                hint = "  <- the provider rejected this key"
            elif msg.startswith("HTTP 429"):
                hint = "  <- key is valid, the free tier is just busy or spent"
            elif msg.startswith("HTTP 503") or msg.startswith("HTTP 502"):
                hint = "  <- key is valid, the provider is overloaded right now"
            # A 429 means the request authenticated and was accepted, then throttled.
            # Calling that a failure would send you replacing a key that works.
            busy = ("Timeout" in msg or "timed out" in msg
                    or msg.startswith(("HTTP 503", "HTTP 502")))
            status = ("LIMITED" if msg.startswith("HTTP 429")
                      else "BUSY" if busy else "FAIL")
            rows.append({"provider": name, "status": status, "model": model,
                         "detail": msg[:200] + hint})
    return rows


# --------------------------------------------------------------------------
# tasks - the answer changes if a constraint is lost
# --------------------------------------------------------------------------
@dataclass
class LiveTask:
    name: str
    brief: str
    steps: list[str]                    # what model A is asked to do, in order
    constraints: list[tuple[str, str]]  # (address, text) - established during phase A
    facts: list[tuple[str, str]]
    final_question: str
    answer: str                         # correct only if the constraints survived
    naive_answer: str                   # what a model that lost them produces
    difficulty: float = 0.7

    def check(self, text: str) -> tuple[bool, str]:
        got = extract_final(text)
        if got is None:
            return False, "no FINAL: line"
        norm = re.sub(r"[\s,]", "", got.lower())
        if re.sub(r"[\s,]", "", self.answer.lower()) in norm:
            return True, got
        return False, got


def extract_final(text: str) -> Optional[str]:
    m = re.findall(r"FINAL\s*:\s*(.+)", text or "", re.IGNORECASE)
    return m[-1].strip() if m else None


def default_live_tasks() -> list[LiveTask]:
    return [
        LiveTask(
            name="invoice-total",
            brief="Compute the total the customer owes on invoice INV-2213.",
            steps=[
                "Read the invoice lines and note the subtotal.",
                "Record the billing rules that apply to this account.",
            ],
            facts=[("/project/invoice/lines",
                    "INV-2213 lines: 12 units at 250.00 each. Subtotal 3000.00.")],
            constraints=[
                ("/project/constraints/discount",
                 "This account is on the ENTERPRISE plan: a 15% discount applies to the "
                 "subtotal before tax."),
                ("/project/constraints/tax",
                 "Tax is 8% and is applied AFTER the discount, never before."),
            ],
            final_question=("Compute the final amount owed on INV-2213. "
                            "Answer with the number only, two decimal places."),
            answer="2754.00",     # 3000 * 0.85 = 2550, * 1.08 = 2754.00
            naive_answer="3240.00",  # 3000 * 1.08, discount lost
        ),
        LiveTask(
            name="capacity-plan",
            brief="Work out how many worker nodes the batch job needs.",
            steps=[
                "Note the job's measured throughput per node.",
                "Record the operational constraints for this cluster.",
            ],
            facts=[("/project/perf/throughput",
                    "Measured: one worker node processes 400 records per minute. "
                    "The batch is 96000 records and must finish within 60 minutes.")],
            constraints=[
                ("/project/constraints/headroom",
                 "Capacity planning at this company always provisions 25% headroom "
                 "above the computed minimum, rounded up to a whole node."),
                ("/project/constraints/reserved",
                 "One additional node is always reserved as a hot standby and is never "
                 "counted toward throughput."),
            ],
            final_question=("How many worker nodes must be provisioned in total? "
                            "Answer with the integer only."),
            # 96000/60 = 1600/min; /400 = 4 nodes; +25% = 5; +1 standby = 6
            answer="6",
            naive_answer="4",
        ),
        LiveTask(
            name="release-date",
            brief="Determine the earliest possible release date for build 4.2.",
            steps=[
                "Note when the code freeze completes.",
                "Record the release policy constraints.",
            ],
            facts=[("/project/schedule/freeze",
                    "Code freeze for 4.2 completes on Monday 2 March 2026.")],
            constraints=[
                ("/project/constraints/soak",
                 "Every build must soak in staging for 5 full working days after code "
                 "freeze before it may ship. Weekends do not count."),
                ("/project/constraints/no-friday",
                 "This team never releases on a Friday; if the computed date is a "
                 "Friday, the release moves to the following Monday."),
            ],
            final_question=("What is the earliest release date for 4.2? "
                            "Answer as YYYY-MM-DD."),
            # freeze Mon 2 Mar; 5 working days soak -> Mon 9 Mar (Tue3,Wed4,Thu5,Fri6,Mon9)
            answer="2026-03-09",
            naive_answer="2026-03-07",
        ),
    ]


# --------------------------------------------------------------------------
# handoff interfaces
# --------------------------------------------------------------------------
SYS_A = (
    "You are an engineering agent working a task. Work carefully and think out loud. "
    "You are NOT being asked for the final answer yet - another agent will finish this."
)
SYS_B = (
    "You are continuing a task another agent started. Use ONLY the context you are "
    "given; do not invent facts. Apply every constraint you were given, in the order "
    "they specify. End your reply with a single line:\nFINAL: <answer>"
)


def phase_a_transcript(task: LiveTask, provider: Provider, env: dict[str, str],
                       *, live: bool) -> tuple[str, ContextOS]:
    """Model A works, and commits state as it goes (D2, write-time commit)."""
    ctx = ContextOS()
    ctx.put("/task/goal", task.brief, kind="goal", importance=1.0, pinned=True)
    for addr, val in task.facts:
        ctx.put(addr, val, kind="fact", source=provider.name, importance=0.85)
    for addr, val in task.constraints:
        ctx.put(addr, val, kind="constraint", source=provider.name, importance=0.95)

    chunks = [f"Task: {task.brief}"]
    for i, step in enumerate(task.steps, 1):
        known = "\n".join(v for _, v in task.facts + task.constraints)
        if live:
            try:
                out, _ = complete(provider, SYS_A,
                                  f"Task: {task.brief}\n\nWhat we know:\n{known}\n\n"
                                  f"Step {i}: {step}\nWrite your working notes.",
                                  env, max_tokens=400)
            except ProviderError as exc:
                out = f"(model A unavailable: {exc})"
        else:
            # Offline stand-in for model A. It quotes what it was told, which is what
            # a real agent's notes do - otherwise `raw` would fail for the wrong
            # reason and the dry run would flatter every other interface.
            out = (f"Working notes for step {i}: {step}\n" + known +
                   "\nProceeding on that basis.")
        chunks.append(f"\n--- step {i}: {step} ---\n{out}")
        ctx.put(f"/agent/{provider.name}/step{i}", out, kind="tool_result",
                source=provider.name, importance=0.15, lifetime="ephemeral")

    # The noise a real trajectory accumulates.
    for i in range(12):
        ctx.put(f"/tool/search/n{i}",
                "Search result: " + " ".join(
                    ["retry", "timeout", "handler", "cursor", "socket", "index"] * 6),
                kind="tool_result", source=provider.name,
                importance=0.1, lifetime="ephemeral")
        chunks.append(f"\n[tool] search result {i}: retry timeout handler cursor "
                      "socket index " * 3)

    return "\n".join(chunks), ctx


def iface_raw(task: LiveTask, transcript: str, ctx: ContextOS, budget: int) -> str:
    return transcript


def iface_traj_drop(task: LiveTask, transcript: str, ctx: ContextOS, budget: int) -> str:
    """The paper's best escalation interface: no trajectory, durable artifacts only.
    Here that means the goal and the files - which is exactly the failure this
    project exists to fix, since constraints are not files."""
    return f"Task: {task.brief}\n(The previous agent's trajectory was discarded.)"


def iface_summary(task: LiveTask, transcript: str, ctx: ContextOS, budget: int) -> str:
    keep, used = [], 0
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        head = line[:120]
        t = count_tokens(head)
        if used + t > budget:
            break
        keep.append(head)
        used += t
    return "Summary of prior work:\n" + "\n".join(keep)


def iface_contextos(task: LiveTask, transcript: str, ctx: ContextOS, budget: int) -> str:
    return ctx.handoff(direction="escalate", budget_tokens=budget,
                       difficulty=task.difficulty).render()


INTERFACES: dict[str, Callable[[LiveTask, str, ContextOS, int], str]] = {
    "raw": iface_raw,
    "traj_drop": iface_traj_drop,
    "summary": iface_summary,
    "contextos": iface_contextos,
}


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
@dataclass
class Result:
    task: str
    interface: str
    ok: bool
    got: str
    naive: bool
    transfer_tokens: int
    prompt_tokens: int
    error: str = ""


def run_one(task: LiveTask, iface: str, a: Provider, b: Provider,
            env: dict[str, str], budget: int, live: bool) -> Result:
    transcript, ctx = phase_a_transcript(task, a, env, live=live)
    transfer = INTERFACES[iface](task, transcript, ctx, budget)
    prompt = f"{transfer}\n\n## Your task now\n{task.final_question}"
    if live:
        try:
            out, used = complete(b, SYS_B, prompt, env, max_tokens=600)
        except ProviderError as exc:
            ctx.close()
            return Result(task.name, iface, False, "", False,
                          count_tokens(transfer), 0, str(exc))
    else:  # offline dry run: answer correctly iff every constraint text survived
        have = all(c[1][:40] in transfer for c in task.constraints)
        out = f"FINAL: {task.answer if have else task.naive_answer}"
        used = count_tokens(prompt)
    ctx.close()
    ok, got = task.check(out)
    naive = (got or "").strip().lower().startswith(task.naive_answer.lower())
    return Result(task.name, iface, ok, got or "", naive,
                  count_tokens(transfer), used)


def run(a_name: str, b_name: str, *, repeats: int = 1, budget: int = 1200,
        live: bool = True, env: Optional[dict[str, str]] = None,
        tasks: Optional[list[LiveTask]] = None) -> list[Result]:
    env = env if env is not None else load_env()
    a, b = PROVIDERS[a_name], PROVIDERS[b_name]
    if live:
        for p in (a, b):
            if not p.available(env):
                raise ProviderError(f"{p.name}: {p.key_env} missing from .env")
    results: list[Result] = []
    for task in (tasks or default_live_tasks()):
        for iface in INTERFACES:
            for _ in range(repeats):
                results.append(run_one(task, iface, a, b, env, budget, live))
                if live:
                    time.sleep(0.6)   # be polite to free tiers
    return results


def summarise(results: list[Result], a: str, b: str) -> str:
    order = ["raw", "traj_drop", "summary", "contextos"]
    lines = [
        f"Live cross-model handoff   {a} -> {b}   n={len(results)}",
        "",
        f"{'interface':<12}{'solved':>9}{'naive-wrong':>13}{'errors':>8}"
        f"{'transfer tok':>14}{'prompt tok':>12}",
        "-" * 68,
    ]
    for iface in order:
        rs = [r for r in results if r.interface == iface]
        if not rs:
            continue
        errs = [r for r in rs if r.error]
        lines.append(
            f"{iface:<12}{sum(r.ok for r in rs) / len(rs):>8.0%}"
            f"{sum(r.naive for r in rs) / len(rs):>13.0%}"
            f"{len(errs):>8}"
            f"{statistics.mean(r.transfer_tokens for r in rs):>14.0f}"
            f"{statistics.mean(r.prompt_tokens for r in rs):>12.0f}")
    bad = [r for r in results if r.error]
    if bad:
        lines += ["", "errors:"] + sorted({f"  {r.interface}/{r.task}: {r.error}"
                                           for r in bad})
    lines += [
        "",
        "solved       final answer correct - every constraint survived the handoff",
        "naive-wrong  the specific wrong answer a model gives when a constraint was lost",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live cross-model handoff evaluation")
    ap.add_argument("--a", default="groq", help="provider that starts the task")
    ap.add_argument("--b", default="gemini", help="provider that finishes it")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--budget", type=int, default=1200)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--dry-run", action="store_true",
                    help="no network: verifies the harness end to end")
    ap.add_argument("--list-providers", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="make one real call to every provider and report what works")
    ap.add_argument("--models", nargs="?", const="ALL", metavar="PROVIDER",
                    help="ask the providers which model IDs your keys can actually use")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    env = load_env(args.env)

    if args.models:
        names = ([args.models] if args.models != "ALL"
                 else [n for n in PROVIDERS if PROVIDERS[n].available(env)])
        if not names:
            print("No keys found in .env", file=sys.stderr)
            return 1
        for name in names:
            if name not in PROVIDERS:
                print(f"unknown provider {name!r}", file=sys.stderr)
                return 2
            current = env.get(MODEL_ENV_OVERRIDE.get(name, ""), PROVIDERS[name].model)
            print(f"\n=== {name} ===  currently configured: {current}")
            try:
                ids = list_models(name, env)
            except ProviderError as exc:
                print(f"  could not list models: {exc}")
                continue
            if not ids:
                print("  (the provider returned no chat models for this key)")
                continue
            for m in ids:
                print(f"  {'* ' if m == current else '  '}{m}")
            if current not in ids:
                env_var = MODEL_ENV_OVERRIDE.get(name, "")
                print(f"  --> {current!r} is NOT in this list. Pick one above and put "
                      f"it in .env as:  {env_var}=<model id>")
        return 0

    if args.check:
        # Print exactly which file is running. When output looks identical to a
        # previous run despite a fix, it is almost always a second copy of the
        # project being executed - this makes that visible in one line.
        from . import __version__ as _v
        print(f"ContextOS {_v}   build {BUILD}")
        print(f"running:  {pathlib.Path(__file__).resolve()}")
        print(f"env file: {pathlib.Path(args.env).resolve()} "
              f"({'found' if pathlib.Path(args.env).exists() else 'MISSING'})\n")
        print("Calling each provider once. This uses your real keys.\n")
        rows = check(env)
        width = max(len(r["provider"]) for r in rows)
        for r in rows:
            print(f"{r['provider']:<{width}}  {r['status']:<7} {r['model']}")
            if r["detail"]:
                print(f"{'':<{width}}  {r['detail']}")
        ok = [r["provider"] for r in rows if r["status"] == "OK"]
        limited = [r["provider"] for r in rows if r["status"] in ("LIMITED", "BUSY")]
        print(f"\n{len(ok)} working now: {', '.join(ok) if ok else 'none'}")
        if limited:
            print(f"{len(limited)} valid but rate-limited right now: "
                  f"{', '.join(limited)}  (these will work again later)")
        if len(ok) >= 2:
            print(f"\nEnough for a real handoff demo:\n"
                  f"  python -m contextos.live --a {ok[0]} --b {ok[1]}")
        elif len(ok) == 1:
            print("\nOnly one provider is working. The dashboard runs, but it has "
                  "nowhere to switch to when that one fails.")
        return 0
    if args.list_providers:
        print(f"{'provider':<17}{'key in .env':<13}{'lane':<7}model")
        for name, p in PROVIDERS.items():
            model = env.get(MODEL_ENV_OVERRIDE.get(name, ""), p.model)
            print(f"{name:<17}{'yes' if p.available(env) else 'no':<13}{p.lane:<7}{model}")
        return 0

    for n in (args.a, args.b):
        if n not in PROVIDERS:
            print(f"unknown provider {n!r}; choose from {', '.join(PROVIDERS)}",
                  file=sys.stderr)
            return 2
    try:
        results = run(args.a, args.b, repeats=args.repeats, budget=args.budget,
                      live=not args.dry_run, env=env)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print(summarise(results, args.a, args.b))
        if args.dry_run:
            print("\n(dry run: no API calls were made - model B was simulated as a "
                  "model that applies exactly the constraints it received)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
