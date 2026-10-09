"""API key setup from the web UI: provider signup info, safe .env writing, key tests.

Keys travel only from the page to this local server (same-origin checked), are
written to .env, and are never sent back - the page only ever sees a masked form.
Only known variable names can be written, and values must be one clean line, so a
pasted value can't inject extra settings into .env.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from .live import MODEL_ENV_OVERRIDE, PROVIDERS, ProviderError, complete

# Signup pages and limits checked September 2026 (see docs/CONFIGURATION.md).
INFO: list[dict[str, Any]] = [
    {"id": "groq", "label": "Groq", "env": "GROQ_API_KEY", "recommended": True,
     "url": "https://console.groq.com/keys", "prefix": "gsk_",
     "free": "Fastest (about 1 s replies). 30 requests/min, 1,000/day per model.",
     "steps": ["Sign in with Google or GitHub.", "Click Create API Key.", "Copy the key (starts with gsk_)."]},
    {"id": "openrouter", "label": "OpenRouter", "env": "OPENROUTER_API_KEY", "recommended": True,
     "url": "https://openrouter.ai/settings/keys", "prefix": "sk-or-",
     "free": "One key for 20 free models. 20 requests/min, 50/day.",
     "steps": ["Sign in.", "Click Create Key (no credit needed).", "Copy the key (starts with sk-or-)."]},
    {"id": "cloudflare", "label": "Cloudflare Workers AI", "env": "CLOUDFLARE_API_KEY",
     "recommended": True, "url": "https://dash.cloudflare.com/profile/api-tokens", "prefix": "",
     "free": "10,000 neurons/day. Needs a token and your account ID.",
     "steps": ["Create Token, then use the Workers AI template.",
               "Copy the token.",
               "Account ID: the long code in your dashboard's address bar (you can paste the whole address)."],
     "extra": {"env": "CLOUDFLARE_ACCOUNT_ID", "label": "Account ID",
               "placeholder": "32-character ID or your dashboard URL"}},
    {"id": "cohere", "label": "Cohere", "env": "COHERE_API_KEY", "recommended": True,
     "url": "https://dashboard.cohere.com/api-keys", "prefix": "",
     "free": "1,000 calls/month. Non-commercial use only.",
     "steps": ["Sign in.", "Copy the Trial key shown on the API Keys page."]},
    {"id": "gemini", "label": "Google Gemini", "env": "GEMINI_API_KEY",
     "url": "https://aistudio.google.com/app/apikey", "prefix": "AIza",
     "free": "Strong Flash models; often busy at peak hours. Pro is paid.",
     "steps": ["Sign in with your Google account.", "Click Create API key.", "Copy it (starts with AIza)."]},
    {"id": "nvidia", "label": "NVIDIA", "env": "NVIDIA_API_KEY",
     "url": "https://build.nvidia.com/settings/api-keys", "prefix": "nvapi-",
     "free": "80+ open models, 40 requests/min, free credits. Slow, so used late.",
     "steps": ["Sign in (free developer account).", "Click Generate API Key.",
               "Copy it (starts with nvapi-)."]},
    {"id": "mistral", "label": "Mistral", "env": "MISTRAL_API_KEY",
     "url": "https://console.mistral.ai/api-keys", "prefix": "",
     "free": "Free Experiment plan; often rate-limited.",
     "steps": ["Sign in and choose the free Experiment plan.", "Create a new key and copy it."]},
    {"id": "ollama", "label": "Ollama (this computer)", "env": "OLLAMA_API_KEY",
     "url": "https://ollama.com/download", "prefix": "", "local": True,
     "free": "Runs on your own PC: no key, no limits, works offline.",
     "steps": ["Install Ollama.", "Run: ollama pull llama3.1:8b",
               "Type anything below (it's an on/off switch, not a secret)."]},
]
_BY_ID = {p["id"]: p for p in INFO}
ALLOWED = {p["env"] for p in INFO} | {p["extra"]["env"] for p in INFO if "extra" in p}
_VALUE = re.compile(r"^[\x21-\x7e]{1,400}$")          # one line, printable, no spaces


class KeySetupError(ValueError):
    pass


def mask(value: str) -> str:
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) >= 12 else "set"


def clean(env_name: str, value: str) -> str:
    """Validate one value for .env. Raises KeySetupError with a plain message."""
    if env_name not in ALLOWED:
        raise KeySetupError(f"{env_name} is not a setting this screen can change")
    v = (value or "").strip().strip('"').strip("'")
    if env_name == "CLOUDFLARE_ACCOUNT_ID" and v:
        m = re.search(r"[0-9a-f]{32}", v)
        if not m:
            raise KeySetupError("Cloudflare account ID should be 32 characters of 0-9 and a-f "
                            "(or paste your dashboard address)")
        return m.group(0)
    if v and not _VALUE.match(v):
        raise KeySetupError(f"{env_name}: paste just the key - one line, no spaces")
    return v


def warn(env_name: str, value: str) -> Optional[str]:
    """A gentle hint when a key doesn't look like that provider's keys."""
    p = next((x for x in INFO if x["env"] == env_name), None)
    if p and p["prefix"] and value and not value.startswith(p["prefix"]):
        return f"{p['label']} keys usually start with {p['prefix']} - check you copied the right one"
    return None


def status(env: dict[str, str]) -> list[dict[str, Any]]:
    """What the page may know: which providers are set, never the values."""
    out = []
    for p in INFO:
        row = {k: p[k] for k in ("id", "label", "env", "url", "free", "steps")}
        row.update(recommended=bool(p.get("recommended")), local=bool(p.get("local")),
                   set=bool(env.get(p["env"])), masked=mask(env.get(p["env"], "")))
        if "extra" in p:
            e = p["extra"]
            row["extra"] = {**e, "set": bool(env.get(e["env"])),
                            "masked": mask(env.get(e["env"], ""))}
        out.append(row)
    return out


def write_env(path: str, updates: dict[str, str]) -> None:
    """Set or clear variables in .env, keeping every other line and comment.
    Written to a temp file and swapped in, so a crash never leaves half a file."""
    p = Path(path)
    if not p.exists():
        example = p.with_name(".env.example")
        lines = example.read_text(encoding="utf-8").splitlines() if example.exists() else []
    else:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    todo = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in todo:
            lines[i] = f"{m.group(1)}={todo.pop(m.group(1))}"
    for k, v in todo.items():
        lines.append(f"{k}={v}")
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(p.parent.resolve()))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        if os.name != "nt":
            os.chmod(tmp, 0o600)                     # readable by you only
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def test_provider(pid: str, values: dict[str, str], env: dict[str, str],
                  timeout: int = 60) -> list[dict[str, Any]]:
    """One tiny real call per model of this provider, using the pasted values
    (falling back to the saved ones). Nothing is saved."""
    p = _BY_ID.get(pid)
    if not p:
        raise KeySetupError("unknown provider")
    trial = dict(env)
    for k, v in values.items():
        if v:
            trial[k] = clean(k, v)
    secrets = [v for k, v in trial.items() if k in ALLOWED and v]
    rows = []
    for name in (pid, f"{pid}-fast"):
        route = PROVIDERS.get(name)
        if not route:
            continue
        model = trial.get(MODEL_ENV_OVERRIDE.get(name, ""), route.model)
        if not route.available(trial):
            rows.append({"route": name, "model": model, "status": "no key",
                         "detail": "paste a key first"})
            continue
        try:
            complete(route, "Reply with the single word: ok", "Say ok.", trial,
                     max_tokens=1500, timeout=timeout)
            rows.append({"route": name, "model": model, "status": "OK", "detail": "working"})
        except ProviderError as e:
            msg = str(e)
            for s in secrets:                         # never echo a key back
                msg = msg.replace(s, "…")
            st = ("LIMITED" if "429" in msg else "BUSY" if re.search(r"50[23]|imeout", msg)
                  else "REJECTED" if re.search(r"\b40[13]\b", msg) else "FAIL")
            hint = {"LIMITED": "the key works; this free tier is busy or used up for now",
                    "BUSY": "the key works; the provider is overloaded right now",
                    "REJECTED": "the provider rejected this key - copy it again"}.get(st, "")
            rows.append({"route": name, "model": model, "status": st,
                         "detail": (hint + " - " if hint else "") + msg[:220]})
    return rows
