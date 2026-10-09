"""ContextOS chat - a local chat app with a context store behind every conversation.

For every message the server:

  1. scores its difficulty and picks the smart or fast lane (router.py)
  2. selects only the relevant context from that conversation's store
  3. streams the reply from the first working model in the lane
  4. if a model fails - even mid-reply - builds a direction-aware handoff packet
     and continues on the next one
  5. commits the durable state the reply declared back to the store

The transcript is for display. The store is the memory.

    python -m contextos.server                # uses .env keys
    python -m contextos.server --offline      # no keys, simulated replies

Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, Optional

from . import ContextOS, __version__
from . import router
from .budget import render
from .handoff import classify, should_migrate
from .chats import ChatStore, title_from
from . import attachments as attach
from .tools import ToolError
from .live import (usable, local_only, FAST_ORDER, MODEL_ENV_OVERRIDE, PROVIDERS, SMART_ORDER,
                   ProviderError, load_env, stream_events, strip_reasoning)
from .units import KINDS, count_tokens

HERE = pathlib.Path(__file__).parent
# UI assets (bundled fonts, icons). Only plain file names under static/, no traversal.
_STATIC = re.compile(r"^/static/((?:fonts/)?[\w.-]+\.(?:woff2|svg|png))$")
_STATIC_TYPES = {".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png"}

# The model is asked to start its reply with a block like:
#   <context>
#   decision | /project/decisions/db | PostgreSQL 16, chosen for JSONB
#   constraint | /project/constraints/no-deps | no new runtime dependencies
#   </context>
SYSTEM = """You are a capable engineering assistant working on an ongoing task.

You are given only the RELEVANT context for this turn, not the whole conversation.
Trust it. If something you need is missing, say so plainly rather than inventing it.

A different model may take over this task at any moment and will see ONLY what you
save. So you MUST START your reply with a <context> block that saves every input
the user gave this turn (numbers, names, requirements, rules, choices) and every
result you will give. Then write your answer below it. One line per item:

kind | /address/with-hyphens | value in one line

kind is one of: decision, constraint, blocker, fact, goal.
Example - user asked "a shirt costs 800, 10% off, then 18% tax; final price?":

<context>
fact | /task/inputs/shirt-price | shirt base price is 800
constraint | /task/rules/discount | 10% discount applied first
constraint | /task/rules/tax | 18% tax applied on the discounted price
fact | /task/results/final-price | discounted 720, final price 849.60
</context>

Addresses are lowercase; the first segment must be user, project, task, agent, tool
or artifact. Reuse an existing address when updating the same item. Skip the block
only for pure small talk (greetings, thanks)."""

# Small models sometimes stop right after the <context> block, as if it were the
# reply (1 in 3 on gpt-oss-20b). Asking the same model to go on is one fast call;
# falling back to another model is a handoff and several seconds.
CONTINUE = ("\n\n## Note\nYour context block for this turn is already saved. Reply to "
            "the user now - write only the answer, with no <context> block.")

CONTEXT_RE = re.compile(r"<context>(.*?)</context>", re.DOTALL | re.IGNORECASE)

_RUN = re.compile(r"(\S)\1{29,}")


def is_degenerate(text: str) -> bool:
    """Free endpoints occasionally return a collapsed sample like '!!!!!!...'.
    Accepting it would put junk in front of the user instead of falling back."""
    body = CONTEXT_RE.sub("", text or "").strip()
    if not body:
        return False
    if _RUN.search(body) and len(_RUN.sub("", body).strip()) < len(body) * 0.5:
        return True
    return sum(c.isalnum() for c in body) < len(body) * 0.2


_PARTIAL_TAGS = ("<context>", "<think>", "<thinking>", "<reasoning>")


def visible_text(raw: str) -> str:
    """What the user should see of a reply that is still streaming: no <context>
    block, no reasoning, and no half-arrived tag that might become either."""
    t = strip_reasoning(CONTEXT_RE.sub("", raw or ""))
    cut = t.lower().find("<context")
    if cut >= 0:
        t = t[:cut]
    low = t.lower()
    for tag in _PARTIAL_TAGS:
        for k in range(len(tag) - 1, 0, -1):
            if low.endswith(tag[:k]):
                t = t[:-k]
                break
    return t.strip()


class Engine:
    """All the automatic behaviour lives here; the HTTP layer is a thin shell.

    Each conversation has its own ContextOS store (data/ctx/<id>.db) - its memory.
    The transcript in data/chats.db is only for display and editing.
    """

    OFFLINE_TIERS = {"offline-a": 0.85, "offline-b": 0.55, "offline-c": 0.5}

    def __init__(self, data_dir: str, env: dict[str, str], offline: bool,
                 budget: int = 1500) -> None:
        self.data = pathlib.Path(data_dir)
        (self.data / "ctx").mkdir(parents=True, exist_ok=True)
        self.chats = ChatStore(str(self.data / "chats.db"))
        self._ctxs: dict[str, ContextOS] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.env = env
        self.offline = offline
        self._test_pool = None
        self._test_describe = None
        self.budget = budget
        self.events: list[dict[str, Any]] = []
        self.forced_failures: set[str] = set()   # demo switch in the Providers panel
        self.cooldown = router.Cooldown()
        self.mode = env.get("LLM_ROUTING", "auto").strip().lower()
        self.threshold = float(env.get("LLM_ROUTE_THRESHOLD", router.DEFAULT_THRESHOLD))
        self.smart, self.fast = self._lanes()
        self.current = self.order[0] if self.order else None
        self._default: Optional[str] = None
        self.builds: dict[str, Any] = {}
        self._load_builds()
        self.connectors: Any = None
        self.env_path = ".env"
        self.auto_offline = False

    # ------------------------------------------------------------- providers
    def _lanes(self) -> tuple[list[str], list[str]]:
        if self.offline:
            return ["offline-a"], ["offline-b", "offline-c"]

        def lane(var: str, default: list[str]) -> list[str]:
            raw = self.env.get(var, "")
            names = [n.strip() for n in raw.split(",") if n.strip()] or default
            return [n for n in names if n in PROVIDERS and usable(PROVIDERS[n], self.env)]
        return lane("LLM_SMART_ORDER", SMART_ORDER), lane("LLM_FAST_ORDER", FAST_ORDER)

    @property
    def order(self) -> list[str]:
        return self.smart + [n for n in self.fast if n not in self.smart]

    def _model(self, name: str) -> str:
        if self.offline:
            return "simulated"
        return self.env.get(MODEL_ENV_OVERRIDE.get(name, ""), PROVIDERS[name].model)

    def provider_info(self) -> list[dict[str, Any]]:
        return [{"name": n, "lane": "smart" if n in self.smart else "fast",
                 "tier": self._tier(n), "model": self._model(n),
                 "current": n == self.current, "failed": n in self.forced_failures,
                 "cooldown": self.cooldown.remaining(n)} for n in self.order]

    def _tier(self, name: str) -> float:
        return self.OFFLINE_TIERS.get(name, 0.5) if self.offline else PROVIDERS[name].tier

    def _stream(self, name: str, system: str, user: str) -> Iterator[Any]:
        """Yields answer text as str, or ("think", text) for streamed reasoning."""
        if name in self.forced_failures:
            raise ProviderError("429 rate limit exceeded (simulated for demo)")
        if self.offline:
            for word in re.split(r"(\s+)", self._offline_reply(name, user)):
                time.sleep(0.004)
                yield word
            return
        for kind, chunk in stream_events(PROVIDERS[name], system, user, self.env):
            yield chunk if kind == "text" else ("think", chunk)

    def _offline_reply(self, name: str, user: str) -> str:
        """Canned but context-aware, so the loop is demonstrable with no keys."""
        asked = user.rsplit("## Now", 1)[-1].strip() or user.strip()
        seen = re.findall(r"(/(?:user|project|task|agent|tool|artifact)/[a-z0-9/_-]+)",
                          user)[:6]
        slug = re.sub(r"[^a-z0-9]+", "-", asked.lower()).strip("-")[:24] or "note"
        return (f"<context>\nfact | /project/notes/{slug} | user asked: {asked[:120]}\n"
                f"</context>\n**[{name}]** simulated reply - no model was called.\n\n"
                f"You asked: *{asked[:300]}*\n\n"
                f"Context I was given covers: {', '.join(seen) if seen else 'nothing yet'}"
                "\n\nStart the server without `--offline` to use real providers.")

    # ---------------------------------------------------------- conversations
    def ctx_for(self, cid: str) -> ContextOS:
        with self._guard:
            if cid not in self._ctxs:
                self._ctxs[cid] = ContextOS(str(self.data / "ctx" / f"{cid}.db"))
                self._locks[cid] = threading.Lock()
            return self._ctxs[cid]

    def delete_conversation(self, cid: str) -> bool:
        with self._guard:
            ctx = self._ctxs.pop(cid, None)
            self._locks.pop(cid, None)
        if ctx:
            ctx.close()
        for suffix in ("", "-wal", "-shm"):
            (self.data / "ctx" / f"{cid}.db{suffix}").unlink(missing_ok=True)
        return self.chats.delete(cid)

    def memory(self, cid: str) -> dict[str, Any]:
        ctx = self.ctx_for(cid)
        units = [{"address": u.address, "kind": u.kind, "value": u.value,
                  "source": u.source, "tokens": u.tokens, "pinned": u.pinned,
                  "version": u.version} for u in ctx.list("", live_only=True)]
        units.sort(key=lambda d: (d["kind"], d["address"]))
        return {"units": units, "stats": ctx.stats(), "conflicts": ctx.conflicts()}

    def forget(self, cid: str, address: str) -> bool:
        return self.ctx_for(cid).delete(address)

    def export(self, cid: str) -> str:
        conv = self.chats.get(cid) or {"title": "Chat", "messages": []}
        out = [f"# {conv['title']}", ""]
        for m in conv["messages"]:
            who = "You" if m["role"] == "user" else m["meta"].get("provider", "Assistant")
            out += [f"**{who}:**", "", m["content"], ""]
        return "\n".join(out)

    # ------------------------------------------------ portable export
    # Pasting into ChatGPT past ~5,000 characters turns the paste into a file
    # attachment the model reads less closely, so Compact is held under that.
    PORTABLE = {"compact": {"budget": 700, "chars": 4800, "recent": 1, "clip": 500},
                "standard": {"budget": 2500, "chars": None, "recent": 3, "clip": 1500},
                "full": {"budget": 1_000_000, "chars": None, "recent": None, "clip": None}}
    _SECTIONS = (("constraint", "Rules that must hold"), ("decision", "Decisions already made"),
                 ("blocker", "Open problems"), ("fact", "Key facts"),
                 ("preference", "My preferences"), ("artifact", "Files involved"),
                 ("tool_result", "Tool results"))

    def portable(self, cid: str, size: str = "compact") -> dict[str, Any]:
        """The chat's memory as one Markdown message to paste into any other AI.

        It reuses the handoff selection (budgeted, most important first) but is
        written for a chat box, not a ContextOS model: no store addresses and no
        "fetch by address", which another tool cannot do.
        """
        spec = self.PORTABLE.get(size, self.PORTABLE["compact"])
        conv = self.chats.get(cid) or {"title": "Chat", "messages": []}
        ctx = self.ctx_for(cid)
        budget = spec["budget"]
        while True:
            packet = ctx.handoff(direction="lateral", budget_tokens=budget)
            text = self._render_portable(conv, packet, spec)
            if not spec["chars"] or len(text) <= spec["chars"] or budget <= 100:
                break
            budget = int(budget * 0.8)
        return {"text": text, "chars": len(text), "tokens": count_tokens(text),
                "items": len(packet.units), "left_out": len(packet.omitted),
                "size": size, "title": conv["title"]}

    @staticmethod
    def _labelled(u: Any) -> str:
        """Models often save bare values ("10:00 AM") whose meaning lives in the
        address (/project/hostel/checkout-time). Addresses are dropped from the
        export, so the last segment comes along as a readable label."""
        label = u.address.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
        words = [w for w in label.lower().split() if len(w) > 2]
        generic = {"step", "item", "note", "notes", "value", "info", "detail", "misc"}
        if (not words or set(words) <= generic or re.search(r"\d", label)
                or all(w in u.value.lower() for w in words)):
            return u.value
        return f"{label[:1].upper()}{label[1:]}: {u.value}"

    def _render_portable(self, conv: dict[str, Any], packet: Any,
                         spec: dict[str, Any]) -> str:
        msgs = [m for m in conv["messages"] if m["content"].strip()]
        L = [f"# Context: {conv['title']}", "",
             "I'm continuing a conversation I started in another AI tool. Below is what "
             "we established there. Treat it as already agreed - don't repeat it back. "
             "Reply with one line saying what we're working on, then wait for my next "
             "message.", "", "## Goal", packet.goal or conv["title"]]
        for kind, title in self._SECTIONS:
            vals = [self._labelled(u) for u in packet.units
                    if u.kind == kind and u.value != packet.goal]
            if vals:
                L += ["", f"## {title}"] + [f"- {v}" for v in vals]

        take = msgs if spec["recent"] is None else msgs[-2 * spec["recent"]:]
        if take:
            L += ["", "## Conversation so far" if spec["recent"] is None
                  else "## Where we left off"]
            for m in take:
                body = m["content"].strip()
                if spec["clip"] and len(body) > spec["clip"]:
                    body = body[:spec["clip"]].rstrip() + " …"
                who = "Me" if m["role"] == "user" else "AI"
                L += ["", f"**{who}:** {body}"]
        if packet.omitted:
            L += ["", f"_{len(packet.omitted)} less important saved items were left out "
                      "to keep this short. Ask me if something seems missing._"]
        return "\n".join(L).strip() + "\n"

    # --------------------------------------------------------------- extract
    def _commit(self, text: str, source: str,
                ctx: Optional[ContextOS] = None) -> list[dict[str, Any]]:
        """Write the reply's <context> lines to the store (D2, write-time commit).
        Each item records whether it created the address, so regenerating or
        editing can undo exactly what this reply added."""
        ctx = ctx or self.ctx
        # Every block, not just the first: some models "think aloud" and mention
        # <context> before writing the real one. Malformed lines are dropped below.
        lines = [ln for blk in CONTEXT_RE.findall(text or "") for ln in blk.splitlines()]
        written = []
        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            kind, address, value = parts[0].lower(), parts[1], "|".join(parts[2:]).strip()
            if kind not in KINDS or not value:
                continue
            importance = {"goal": 1.0, "constraint": 0.95, "blocker": 0.85,
                          "decision": 0.9, "fact": 0.7}.get(kind, 0.5)
            try:
                existed = ctx.get(address) is not None
                u = ctx.put(address, value, kind=kind, source=source,
                            importance=importance, pinned=(kind == "goal"))
                written.append({"address": u.address, "kind": u.kind, "value": u.value,
                                "created": not existed})
            except Exception:
                continue          # a malformed address must never break the turn
        return written

    @staticmethod
    def _route_info(d: router.Decision) -> dict[str, Any]:
        return {"lane": d.lane, "difficulty": d.score, "reasons": d.reasons,
                "forced": d.forced}

    def _undo(self, cid: str, removed: list[dict[str, Any]]) -> None:
        ctx = self.ctx_for(cid)
        for m in removed:
            for w in m["meta"].get("written", []):
                if w.get("created"):
                    ctx.delete(w["address"])

    # ------------------------------------------------------------------ turn
    def chat_stream(self, cid: Optional[str], prompt: str = "", *,
                    lane: Optional[str] = None, route: Optional[str] = None,
                    regenerate: Optional[str] = None,
                    edit: Optional[str] = None,
                    attachments: Optional[list] = None) -> Iterator[dict[str, Any]]:
        """One turn as a stream of events for the UI.

        regenerate=<assistant message id> replaces that reply;
        edit=<user message id> replaces that message (and everything after it)
        with `prompt`. Either way the store writes of the dropped replies are
        undone first, so memory matches the visible conversation.
        """
        if not self.order:
            yield {"type": "error", "error": "No providers available. Add a key to "
                                             ".env, or start the server with --offline."}
            return
        if not cid or not self.chats.get(cid):
            cid = self.chats.create()["id"]
        ctx = self.ctx_for(cid)
        with self._locks[cid]:
            yield from self._turn(cid, ctx, prompt, lane, route, regenerate, edit, attachments)

    def _turn(self, cid: str, ctx: ContextOS, prompt: str, lane: Optional[str],
              route: Optional[str], regenerate: Optional[str],
              edit: Optional[str], attachments: Optional[list] = None
              ) -> Iterator[dict[str, Any]]:
        atts: list[attach.Attachment] = []
        if attachments and not regenerate:
            try:
                atts = attach.process(attachments, self._describer())
            except attach.AttachmentError as e:
                yield {"type": "error", "error": str(e)}
                return
        if regenerate:
            self._undo(cid, self.chats.truncate_from(cid, regenerate))
            users = [m for m in self.chats.messages(cid) if m["role"] == "user"]
            if not users:
                yield {"type": "error", "error": "Nothing to regenerate."}
                return
            user_msg_rec = users[-1]
            prompt = user_msg_rec["content"]
        else:
            prompt = (prompt or "").strip()
            if not prompt and atts:
                prompt = "Summarize the attached file(s) and point out anything important."
            if not prompt:
                yield {"type": "error", "error": "Empty message."}
                return
            if edit:
                removed = self.chats.truncate_from(cid, edit)
                self._undo(cid, removed)
                # Editing the opening message changes what the chat is about: an
                # auto title and the goal both came from it, so both follow the edit.
                if removed and not self.chats.messages(cid):
                    old = router.parse_override(removed[0]["content"])[1]
                    if self.chats.get(cid)["title"] == title_from(old):
                        self.chats.rename(cid, "New chat")
                    ctx.delete("/task/goal")
            user_msg_rec = self.chats.add(
                cid, "user", prompt,
                {"attachments": [a.public() for a in atts]} if atts else None)
            for a in atts:        # durable, addressable, carried across model handoffs
                ctx.put_artifact(a.address, a.name, a.text, source="user", importance=0.85)

        pipe = lane == "pipeline" or prompt.lstrip().lower().startswith("/pipeline")
        if pipe:
            prompt = re.sub(r"^\s*/pipeline\b\s*", "", prompt, flags=re.I) or prompt
            prompt = prompt.strip()
        forced, text_in = router.parse_override(prompt)
        conv = self.chats.get(cid)
        if conv["title"] == "New chat":
            self.chats.rename(cid, title_from(text_in))
            conv = self.chats.get(cid)
        yield {"type": "start", "conversation": {k: conv[k] for k in
                                                ("id", "title", "created", "updated")},
               "user_message": user_msg_rec}

        if pipe:
            yield from self._pipeline_turn(cid, ctx, text_in, user_msg_rec, attach.render(atts))
            return

        wanted = forced or (lane if lane in (router.SMART, router.FAST) else self.mode)
        decision = router.decide(text_in, wanted, self.threshold)
        yield {"type": "route", **self._route_info(decision)}

        if not ctx.get("/task/goal"):
            ctx.put("/task/goal", text_in.strip()[:400], kind="goal",
                    importance=1.0, pinned=True, source="user")

        history = [m for m in self.chats.messages(cid) if m["seq"] < user_msg_rec["seq"]]
        recent = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in history[-2:])
        selection = ctx.select(text_in, budget_tokens=self.budget)
        context_text = render(selection.units) or "(nothing on file yet)"
        attach_text = attach.render(atts)
        user_msg = (f"## Context on file\n{context_text}\n\n"
                    + (f"{attach_text}\n\n" if attach_text else "")
                    + (f"## Last exchange\n{recent}\n\n" if recent else "")
                    + f"## Now\n{text_in}")

        chain = router.build_chain(decision.lane, self.smart, self.fast,
                                   self.cooldown.active)
        if route in self.order:
            chain = [route] + [n for n in chain if n != route]
        attempts: list[dict[str, Any]] = []
        switched: list[dict[str, Any]] = []
        raw, shown, used, name = "", "", None, chain[0]
        t0 = time.time()

        try:
            for i, name in enumerate(chain):
                yield {"type": "model", "provider": name, "model": self._model(name),
                       "lane": "smart" if name in self.smart else "fast"}
                raw, shown, thinking, t_think = "", "", 0, None
                block_only = ""
                try:
                    for attempt in (0, 1):
                        ask = user_msg + (CONTINUE if attempt else "")
                        for chunk in self._stream(name, SYSTEM, ask):
                            if isinstance(chunk, tuple):
                                thinking += len(chunk[1])
                                t_think = t_think or time.time()
                                yield {"type": "thinking", "text": chunk[1]}
                                continue
                            raw += chunk
                            vis = visible_text(raw)
                            if len(vis) >= 30 and is_degenerate(vis):
                                raise ProviderError("garbled reply (repeated characters)")
                            # Update `shown` before yielding: a Stop lands at the
                            # yield, and the partial reply saved must include this piece.
                            prev, shown = shown, vis
                            if vis.startswith(prev):
                                if len(vis) > len(prev):
                                    yield {"type": "delta", "text": vis[len(prev):]}
                            else:
                                yield {"type": "replace", "text": vis}
                        if shown.strip() or attempt or not CONTEXT_RE.search(raw):
                            break
                        block_only, raw = raw, ""       # keep the block, ask again
                    if not shown.strip():
                        raise ProviderError("empty reply (the model may have spent its "
                                            "whole output budget on reasoning)")
                    raw = block_only + raw
                    used = name
                    break
                except ProviderError as exc:
                    benched = self.cooldown.hit(name, str(exc))
                    attempts.append({"provider": name, "error": str(exc)[:200],
                                     "cooldown": benched})
                    if shown:
                        yield {"type": "reset"}
                        shown = ""
                    nxt = chain[i + 1] if i + 1 < len(chain) else None
                    if nxt is None:
                        break
                    # The point of the project: rebuild the context for the model we
                    # are moving TO, in the direction we are moving.
                    direction = classify(self._tier(name), self._tier(nxt))
                    packet = ctx.handoff(direction=direction, budget_tokens=self.budget,
                                         from_model=name, to_model=nxt,
                                         difficulty=decision.score)
                    ok, why = should_migrate(direction, decision.score)
                    # The packet carries the store; the last exchange is what the
                    # failed model was also given, so the new one must not lose it.
                    user_msg = (f"{packet.render()}\n\n"
                                + (f"{attach_text}\n\n" if attach_text else "")
                                + (f"## Last exchange\n{recent}\n\n" if recent else "")
                                + f"## Now\n{text_in}")
                    sw = {"from": name, "to": nxt, "direction": direction,
                          "reason": str(exc)[:160], "packet_tokens": packet.tokens_selected,
                          "full_replay_tokens": packet.tokens_stored,
                          "omitted": len(packet.omitted), "gate": None if ok else why}
                    switched.append(sw)
                    self.events.append({"ts": time.time(), "kind": "switch", **sw})
                    yield {"type": "switch", **sw}
        except GeneratorExit:
            # The user pressed Stop. Keep what they saw, but commit nothing: a
            # half-written <context> block is not trustworthy state.
            if shown.strip():
                self.chats.add(cid, "assistant", shown,
                               {"provider": name, "model": self._model(name),
                                "stopped": True, **self._route_info(decision)})
            raise

        if used is None:
            self.events.append({"ts": time.time(), "kind": "all_failed",
                                "detail": attempts})
            yield {"type": "error", "error": "Every provider failed.",
                   "attempts": attempts, "switched": switched}
            return

        self.current = used
        self.cooldown.ok(used)
        written = self._commit(raw, used, ctx)
        if not written and decision.score > 0.05:
            # The model saved nothing. Keep the user's own words so a later
            # handoff still carries this turn's inputs.
            addr = f"/task/inputs/turn-{user_msg_rec['seq']}"
            existed = ctx.get(addr) is not None
            u = ctx.put(addr, text_in.strip()[:500], kind="fact", source="user",
                        importance=0.75)
            written = [{"address": u.address, "kind": u.kind, "value": u.value,
                        "created": not existed}]
        stats = ctx.stats()
        meta = {"provider": used, "model": self._model(used),
                "lane_used": "smart" if used in self.smart else "fast",
                **self._route_info(decision), "attempts": attempts,
                "switched": switched, "written": written,
                "context_sent": selection.addresses(),
                "thought_ms": int((time.time() - t_think) * 1000) if t_think and thinking else 0,
                "tokens": {"stored": stats["live_tokens"],
                           "sent": count_tokens(context_text),
                           "omitted_units": len(selection.omitted)},
                "ms": int((time.time() - t0) * 1000)}
        msg = self.chats.add(cid, "assistant", visible_text(raw), meta)
        yield {"type": "done", "message": msg}

    # ------------------------------------------------------------- pipeline
    def _describer(self):
        if self._test_describe:
            return self._test_describe
        if self.offline:
            return lambda mime, raw: f"[simulated description of a {mime} image, {len(raw)} bytes]"
        from .live import _post
        return None if local_only(self.env) else attach.gemini_describer(self.env, _post)

    def _pool(self):
        from .agent import ModelPool
        pool = self._test_pool or ModelPool(self.env)
        pool.cooldown = self.cooldown                  # share rested-model state
        return pool

    def _pipeline_turn(self, cid: str, ctx: ContextOS, text_in: str,
                       user_msg_rec: dict[str, Any], extra: str = ""
                       ) -> Iterator[dict[str, Any]]:
        """Split the prompt into subtasks, route each to a suitable free model, merge."""
        from . import pipeline
        pool = self._pool()
        if not pool.ready():
            yield {"type": "error", "error": "Pipeline mode needs at least one real model key."}
            return
        if not ctx.get("/task/goal"):
            ctx.put("/task/goal", text_in[:400], kind="goal", importance=1.0,
                    pinned=True, source="user")
        context_text = render(ctx.select(text_in, budget_tokens=self.budget).units)
        if extra:
            context_text = f"{context_text}\n\n{extra}".strip()
        yield {"type": "route", "lane": "pipeline", "difficulty": router.score(text_in)[0],
               "reasons": ["pipeline: split into subtasks"], "forced": True}
        t0, final, meta_steps, who = time.time(), "", [], ""
        ask = lambda lane, system, user: pool.ask(lane, system, user, max_tokens=2048)
        for ev in pipeline.run(text_in, ask, context_text if context_text.strip() else ""):
            if ev["type"] == "pipeline_error":
                yield {"type": "error", "error": ev["error"]}
                return
            if ev["type"] == "pipeline_result":
                final, who, meta_steps = ev["text"], ev["provider"], ev["steps"]
                continue
            yield ev
        # Commit each subtask's conclusion as addressable state so a later model
        # (or handoff) can fetch it by address instead of replaying the transcript.
        written = []
        addr = f"/task/pipeline/turn-{user_msg_rec['seq']}"
        existed = ctx.get(addr) is not None
        u = ctx.put(addr, final[:1500], kind="fact", source="pipeline", importance=0.7)
        written.append({"address": u.address, "kind": u.kind, "value": u.value,
                        "created": not existed})
        meta = {"provider": who, "model": self._model(who) if who in PROVIDERS else who,
                "lane_used": "pipeline", "lane": "pipeline", "difficulty": 0.0,
                "reasons": ["pipeline"], "forced": True, "attempts": [], "switched": [],
                "written": written, "pipeline": meta_steps, "context_sent": [],
                "tokens": {"stored": ctx.stats()["live_tokens"],
                           "sent": count_tokens(context_text), "omitted_units": 0},
                "ms": int((time.time() - t0) * 1000)}
        msg = self.chats.add(cid, "assistant", final, meta)
        yield {"type": "delta", "text": final}
        yield {"type": "done", "message": msg}

    # ------------------------------------------------ non-streaming wrapper
    @property
    def ctx(self) -> ContextOS:
        """The default conversation's store, for callers without a chat id."""
        if self._default is None:
            self._default = self.chats.create()["id"]
        return self.ctx_for(self._default)

    def chat(self, prompt: str, cid: Optional[str] = None, **kw) -> dict[str, Any]:
        self.ctx                                       # ensure the default exists
        out: dict[str, Any] = {"switched": [], "attempts": []}
        for ev in self.chat_stream(cid or self._default, prompt, **kw):
            if ev["type"] == "route":
                out["route"] = {k: ev[k] for k in ("lane", "difficulty", "reasons",
                                                   "forced")}
            elif ev["type"] == "switch":
                out["switched"].append(ev)
            elif ev["type"] == "error":
                out.update(error=ev["error"], attempts=ev.get("attempts", []))
            elif ev["type"] == "done":
                m = ev["message"]
                out.update(reply=m["content"], provider=m["meta"]["provider"],
                           written=m["meta"]["written"], attempts=m["meta"]["attempts"],
                           tokens=m["meta"]["tokens"], message=m)
        return out

    # ----------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        return {"version": __version__, "offline": self.offline,
                "providers": self.provider_info(), "events": self.events[-30:],
                "budget": self.budget,
                "routing": {"mode": self.mode, "threshold": self.threshold},
                "needs_setup": not any(p.available(self.env) for p in PROVIDERS.values()),
                "auto_offline": self.auto_offline}

    # --------------------------------------------------------------- API keys
    def keys_status(self) -> dict[str, Any]:
        from .keys import status
        return {"providers": status(self.env), "offline": self.offline,
                "auto_offline": self.auto_offline, "env_file": str(pathlib.Path(self.env_path).resolve())}

    def save_keys(self, values: dict[str, Any]) -> dict[str, Any]:
        from .keys import KeySetupError, clean, warn, write_env
        try:
            updates = {str(k): clean(str(k), str(v or "")) for k, v in values.items()}
        except KeySetupError as e:
            raise ToolError(str(e)) from None
        if not updates:
            raise ToolError("nothing to save")
        write_env(self.env_path, updates)
        warnings = [w for k, v in updates.items() if (w := warn(k, v))]
        self.reload_env()
        return {**self.keys_status(), "warnings": warnings}

    def test_key(self, pid: str, values: dict[str, Any]) -> list[dict[str, Any]]:
        from .keys import KeySetupError, test_provider
        try:
            return test_provider(pid, {str(k): str(v or "") for k, v in values.items()},
                                 self.env)
        except KeySetupError as e:
            raise ToolError(str(e)) from None

    def reload_env(self) -> None:
        """Pick up .env changes without a restart."""
        old = self.env
        self.env = load_env(self.env_path)
        changed = [n for n, p in PROVIDERS.items()
                   if old.get(p.key_env) != self.env.get(p.key_env)]
        self.cooldown.clear(changed)                 # a new key deserves a fresh try
        if self.offline and self.auto_offline and                 any(p.available(self.env) for p in PROVIDERS.values()):
            self._go_live()
        self.smart, self.fast = self._lanes()
        self.current = self.order[0] if self.order else None
        if self.connectors:
            self.connectors.close()
            self.connectors = None

    def _go_live(self) -> None:
        """Offline only because there were no keys, and now there are: switch to
        real models and to the real chat folder (simulated chats stay apart)."""
        for c in self._ctxs.values():
            c.close()
        self._ctxs.clear()
        self._locks.clear()
        self.chats.close()
        if self.data.name == "offline":
            self.data = self.data.parent
        (self.data / "ctx").mkdir(parents=True, exist_ok=True)
        self.chats = ChatStore(str(self.data / "chats.db"))
        self._default = None
        self.offline = self.auto_offline = False

    def toggle_failure(self, name: str) -> bool:
        if name in self.forced_failures:
            self.forced_failures.discard(name)
            self.cooldown.clear([name])      # un-failing a provider restores it at once
            return False
        self.forced_failures.add(name)
        return True

    def preview_handoff(self, cid: str, direction: str) -> dict[str, Any]:
        p = self.ctx_for(cid).handoff(direction=direction, budget_tokens=self.budget,
                                      difficulty=0.7)
        return {"markdown": p.render(), "tokens": p.tokens_selected,
                "full_replay": p.tokens_stored, "reduction": p.reduction,
                "omitted": p.omitted[:40], "notes": p.notes}

    # ------------------------------------------------------------ build mode
    def _connectors(self) -> Any:
        if getattr(self, "connectors", None) is None:
            from .mcp_client import Connectors
            self.connectors = Connectors("mcp.json")
            self.connectors.start()
        return self.connectors

    def start_build(self, body: dict[str, Any]) -> dict[str, Any]:
        from .builder import BuildRun
        if self.offline:
            raise ToolError("builds need real models - start ContextOS without --offline "
                            "(and add at least one API key)")
        goal = str(body.get("goal", "")).strip()
        if len(goal) < 10:
            raise ToolError("describe what to build in a sentence or two")
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:32] or "project"
        ws = str(body.get("workspace") or "").strip() or \
            str(self.data / "builds" / f"{slug}-{int(time.time()) % 100000}")
        run = BuildRun(goal, ws, self.env, connectors=self._connectors(),
                       auto_approve_tests=bool(body.get("auto_approve_tests", True)),
                       research=bool(body.get("research", True)),
                       review_plan=bool(body.get("review_plan", True)),
                       max_features=int(body.get("max_features") or 6),
                       app_root=str(HERE.parent))
        self.builds[run.id] = run
        run.on_finish = lambda r: self._save_builds()
        self._save_builds()
        run.start()
        return run.summary()

    # A small index so builds - and the way to their files - survive a restart.
    def _index(self) -> pathlib.Path:
        return self.data / "builds.json"

    def _load_builds(self) -> None:
        from .builder import ArchivedBuild
        try:
            recs = json.loads(self._index().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            recs = []                  # no index yet: older builds are adopted below
        for rec in recs if isinstance(recs, list) else []:
            try:
                if pathlib.Path(rec["workspace"]).is_dir():
                    self.builds[rec["id"]] = ArchivedBuild(rec)
            except (KeyError, TypeError, OSError):
                continue
        self._adopt_unindexed()

    def _adopt_unindexed(self) -> None:
        """Builds made before the index existed: rebuild a record from their report."""
        import hashlib
        from .builder import ArchivedBuild
        known = {str(pathlib.Path(b.ws.root).resolve()) for b in self.builds.values()}
        for rep in sorted((self.data / "builds").glob("*/BUILD_REPORT.md")):
            ws = rep.parent.resolve()
            if str(ws) in known:
                continue
            text = rep.read_text(encoding="utf-8", errors="replace")
            goal = re.search(r"^\*\*Goal:\*\*\s*(.+)$", text, re.M)
            block = lambda h: (re.search(rf"^## {h}\s*\n+```\n?(.*?)\n?```", text, re.M | re.S)
                               or [None, ""])[1].strip()
            bid = hashlib.sha1(str(ws).encode()).hexdigest()[:10]
            self.builds[bid] = ArchivedBuild({
                "id": bid, "goal": goal.group(1).strip() if goal else ws.name,
                "workspace": str(ws), "status": "done", "started": rep.stat().st_mtime,
                "plan": {"test_all": block("How to test"), "run_command": block("How to run"),
                         "features": []},
                "results": [{"name": m.group(2), "passed": m.group(1) == "PASS", "rounds": 1}
                            for m in re.finditer(r"^- (PASS|FAIL) \*\*(.+?)\*\*", text, re.M)]})

    def _save_builds(self) -> None:
        recs = [b.record() for b in self.builds.values()][-200:]
        tmp = self._index().with_suffix(".tmp")
        tmp.write_text(json.dumps(recs, indent=1), encoding="utf-8")
        tmp.replace(self._index())

    def change_build(self, bid: str, request: str) -> dict[str, Any]:
        """A follow-up change to a finished build, in the same project and memory."""
        from .builder import BuildRun
        if self.offline:
            raise ToolError("changes need real models - add an API key first")
        run = self.build(bid)
        if run.archived:                          # loaded after a restart: bring it back
            run = BuildRun.resume(run.record(), self.env, connectors=self._connectors(),
                                  app_root=str(HERE.parent))
            self.builds[bid] = run
        run.on_finish = lambda r: self._save_builds()
        run.change(request)
        self._save_builds()
        return run.summary()

    def open_folder(self, bid: str) -> None:
        """Show the project in Explorer / Finder / the file manager. Only ever a
        build's own workspace, never a path from the request."""
        import subprocess
        import sys as _sys
        path = str(self.build(bid).ws.root)
        if _sys.platform == "win32":
            os.startfile(path)                                   # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if _sys.platform == "darwin" else "xdg-open", path])

    def build(self, bid: str) -> Any:
        run = self.builds.get(bid)
        if not run:
            raise ToolError("no such build")
        return run

    def skills_list(self) -> list[dict[str, str]]:
        from .skills import discover
        return [{"name": s.name, "description": s.description, "path": str(s.path)}
                for s in discover("skills").values()]

    def close(self) -> None:
        for run in getattr(self, "builds", {}).values():
            run.stop.set()
        if getattr(self, "connectors", None):
            self.connectors.close()
        for c in self._ctxs.values():
            c.close()
        self.chats.close()


_CONV = re.compile(r"^/api/conversations/([0-9a-f]{12})(?:/([a-z]+))?$")
_BUILD = re.compile(r"^/api/builds/([0-9a-f]{10})(?:/([a-z]+))?$")
_SITE = re.compile(r"^/api/builds/([0-9a-f]{10})/site/(.*)$")
# Generated pages run with an opaque origin: they can load their own files but
# can't call this API (which holds your keys) or read its responses.
_SITE_CSP = ("sandbox allow-scripts allow-forms allow-popups allow-modals; "
             "default-src 'self' 'unsafe-inline' data: blob: https:; connect-src https:")


class Handler(BaseHTTPRequestHandler):
    engine: Engine = None            # set in serve()
    port: int = 8000
    server_version = "ContextOS"

    def log_message(self, fmt, *args):    # keep the console clean
        pass

    # ------------------------------------------------------------- plumbing
    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > 32 * 1024 * 1024:              # uploads are capped per file; this caps the request
            self.rfile.read(0)
            return {"_too_large": True}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _trusted(self) -> bool:
        """The server holds your API keys, so only this page may drive it.
        Host blocks DNS rebinding; Origin blocks other websites posting to
        localhost; requiring JSON forces a CORS preflight we never answer."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if self.command == "GET" and _SITE.match(self.path.partition("?")[0]):
            return True           # a sandboxed preview's own assets (Origin: null)
        if origin and origin not in (f"http://127.0.0.1:{self.port}",
                                     f"http://localhost:{self.port}"):
            return False
        if self.command == "POST":
            return (self.headers.get("Content-Type") or "").startswith("application/json")
        return True

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        if not self._trusted():
            self._send(403, b"forbidden", "text/plain")
            return
        path, _, query = self.path.partition("?")
        eng = self.engine
        try:
            if path in ("/", "/index.html"):
                page = HERE / "dashboard.html"
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            elif m := _STATIC.match(path):
                f = (HERE / "static" / m.group(1)).resolve()
                if not f.is_file() or (HERE / "static").resolve() not in f.parents:
                    self._send(404, b"not found", "text/plain")
                else:
                    self._send(200, f.read_bytes(), _STATIC_TYPES[f.suffix],
                               {"Cache-Control": "public, max-age=86400"})
            elif path == "/api/state":
                self._json(eng.state())
            elif path == "/api/conversations":
                q = urllib.parse.parse_qs(query).get("q", [""])[0]
                self._json({"conversations": eng.chats.list(q)})
            elif path == "/api/builds":
                self._json({"builds": [r.summary() for r in eng.builds.values()]})
            elif path == "/api/connectors":
                self._json({"servers": eng._connectors().status(),
                            "builtin": ["web_search (DuckDuckGo)", "fetch_url", "wikipedia",
                                        "arxiv_search", "arxiv_read"]})
            elif path == "/api/skills":
                self._json({"skills": eng.skills_list()})
            elif path == "/api/keys":
                self._json(eng.keys_status())
            elif m := _SITE.match(path):
                self._serve_site(eng.build(m.group(1)), urllib.parse.unquote(m.group(2)))
            elif m := _BUILD.match(path):
                bid, action = m.groups()
                run = eng.build(bid)
                args = urllib.parse.parse_qs(query)
                if action is None:
                    self._json({**run.summary(), "plan": run.plan, "results": run.results,
                                "changes": getattr(run, "changes", [])})
                elif action == "events":
                    self._stream_build(run, int(args.get("after", ["0"])[0] or 0))
                elif action == "download":
                    from .builder import export_zip
                    name = re.sub(r"[^\w.-]+", "-", run.ws.root.name)[:60] or "project"
                    self._send(200, export_zip(run.ws), "application/zip",
                               {"Content-Disposition": f'attachment; filename="{name}.zip"'})
                elif action == "files":
                    self._json({"files": run.ws.list_files(".", "4")})
                elif action == "file":
                    self._json({"text": run.ws.read_file(args.get("path", [""])[0], "1", "400")})
                else:
                    self._json({"error": "not found"}, 404)
            elif m := _CONV.match(path):
                cid, action = m.groups()
                if not eng.chats.get(cid):
                    self._json({"error": "no such conversation"}, 404)
                elif action is None:
                    self._json(eng.chats.get(cid))
                elif action == "memory":
                    self._json(eng.memory(cid))
                elif action == "portable":
                    args = urllib.parse.parse_qs(query)
                    out = eng.portable(cid, args.get("size", ["compact"])[0])
                    if args.get("download", [""])[0]:
                        name = re.sub(r"[^\w -]", "", out["title"])[:40] or "chat"
                        self._send(200, out["text"].encode(), "text/markdown; charset=utf-8",
                                   {"Content-Disposition":
                                    f'attachment; filename="{name} - context.md"'})
                    else:
                        self._json(out)
                elif action == "export":
                    title = re.sub(r"[^\w -]", "", eng.chats.get(cid)["title"])[:40]
                    self._send(200, eng.export(cid).encode(), "text/markdown; charset=utf-8",
                               {"Content-Disposition":
                                f'attachment; filename="{title or "chat"}.md"'})
                else:
                    self._json({"error": "not found"}, 404)
            else:
                self._send(404, b"not found", "text/plain")
        except ToolError as exc:                  # the caller's mistake, said plainly
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:
        if not self._trusted():
            self._json({"error": "forbidden"}, 403)
            return
        eng = self.engine
        try:
            body = self._body()
            if self.path == "/api/chat":
                self._stream_chat(body)
            elif self.path == "/api/builds":
                self._json(eng.start_build(body))
            elif self.path == "/api/keys/save":
                self._json(eng.save_keys(body.get("values") or {}))
            elif self.path == "/api/keys/test":
                self._json({"rows": eng.test_key(str(body.get("provider", "")),
                                                 body.get("values") or {})})
            elif self.path == "/api/connectors/reload":
                if eng.connectors:
                    eng.connectors.close()
                    eng.connectors = None
                self._json({"servers": eng._connectors().status()})
            elif m := _BUILD.match(self.path):
                bid, action = m.groups()
                run = eng.build(bid)
                if action == "answer":
                    self._json({"ok": run.answer(str(body.get("id", "")), body)})
                elif action == "stop":
                    run.stop.set()
                    self._json({"ok": True})
                elif action == "open":
                    eng.open_folder(bid)
                    self._json({"ok": True})
                elif action == "change":
                    self._json(eng.change_build(bid, str(body.get("request", ""))))
                else:
                    self._json({"error": "not found"}, 404)
            elif self.path == "/api/conversations":
                self._json(eng.chats.create())
            elif m := _CONV.match(self.path):
                cid, action = m.groups()
                if not eng.chats.get(cid):
                    self._json({"error": "no such conversation"}, 404)
                elif action == "rename":
                    eng.chats.rename(cid, str(body.get("title", "")))
                    self._json(eng.chats.get(cid))
                elif action == "delete":
                    self._json({"deleted": eng.delete_conversation(cid)})
                elif action == "forget":
                    self._json({"forgotten": eng.forget(cid, str(body.get("address", "")))})
                elif action == "handoff":
                    self._json(eng.preview_handoff(cid, body.get("direction", "escalate")))
                else:
                    self._json({"error": "not found"}, 404)
            elif self.path == "/api/fail":
                self._json({"failing": eng.toggle_failure(str(body.get("provider", "")))})
            elif self.path == "/api/check":
                from .live import check as live_check
                self._json({"offline": eng.offline,
                            "rows": [] if eng.offline else live_check(eng.env)})
            else:
                self._json({"error": "not found"}, 404)
        except ToolError as exc:                  # the caller's mistake, said plainly
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _serve_site(self, run: Any, rel: str) -> None:
        import mimetypes
        p = run.ws.path(rel or "index.html")          # workspace-bound, protected paths refused
        if p.is_dir():
            p = p / "index.html"
        if not p.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype,
                   {"Content-Security-Policy": _SITE_CSP, "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer"})

    def _stream_build(self, run: Any, after: int) -> None:
        """Tail a build's events as NDJSON. Closing the connection doesn't stop the
        build; the page reconnects with ?after=<last seq + 1>."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        n = after
        try:
            while True:
                evs = run.events_after(n, 15)
                for ev in evs:
                    self.wfile.write((json.dumps(ev) + "\n").encode())
                n += len(evs)
                if not evs:                        # keep-alive so proxies don't cut us off
                    self.wfile.write(b'{"type":"ping"}\n')
                self.wfile.flush()
                if run.status in ("done", "failed", "stopped") and n >= len(run.events):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _stream_chat(self, body: dict[str, Any]) -> None:
        """One JSON event per line. The connection closes when the turn ends
        (HTTP/1.0), so no chunked encoding is needed. If the browser goes away -
        the user pressed Stop - the write fails and the turn is closed, which
        saves the partial reply."""
        gen = self.engine.chat_stream(
            body.get("conversation_id") or None, str(body.get("prompt") or ""),
            lane=body.get("lane"), route=body.get("route"),
            regenerate=body.get("regenerate"), edit=body.get("edit"),
            attachments=body.get("attachments"))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for ev in gen:
                self.wfile.write((json.dumps(ev) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            gen.close()


def serve(data: str, port: int, offline: bool, budget: int, env_path: str,
          open_browser: bool = True) -> None:
    env = load_env(env_path)
    auto_offline = False
    if not offline and not any(p.available(env) for p in PROVIDERS.values()):
        print("No provider keys found in .env - starting in offline mode. Add a key in the "
              "app (Set up models) and it switches to real models without a restart.")
        offline = auto_offline = True
    # Simulated chats never mix with real ones.
    data_dir = str(pathlib.Path(data) / "offline") if offline else data
    engine = Engine(data_dir, env, offline, budget)
    engine.env_path, engine.auto_offline = env_path, auto_offline
    Handler.engine, Handler.port = engine, port

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}"
    print("=" * 62)
    print(f"  ContextOS chat  {url}")
    if engine.offline:
        print("  Mode: OFFLINE (simulated replies)")
    else:
        print(f"  Smart lane: {', '.join(engine.smart) or '(none)'}")
        print(f"  Fast lane:  {', '.join(engine.fast) or '(none)'}")
        print(f"  Routing: {engine.mode}, threshold {engine.threshold}")
    print(f"  Chats saved in: {pathlib.Path(data_dir).resolve()}")
    print("=" * 62)
    print("  Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        engine.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ContextOS chat")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default="chat_data",
                    help="folder for conversations and their context stores")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--budget", type=int, default=1500,
                    help="tokens of context sent per turn")
    ap.add_argument("--offline", action="store_true",
                    help="no API calls - simulated replies, full UI")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    serve(a.data, a.port, a.offline, a.budget, a.env, not a.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
