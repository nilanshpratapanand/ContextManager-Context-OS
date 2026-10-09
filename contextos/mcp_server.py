"""ContextOS as an MCP stdio server (spec 2026-07-28).

Distribution matters more than novelty here: exposed over MCP, ContextOS works
inside any MCP client without that client knowing anything about it. Agents commit
state as they work (D2, write-time commit) and a handoff packet is one tool call.

Deliberately dependency-free - raw JSON-RPC over stdio, no SDK - so it runs
anywhere Python 3.9+ runs.

    python -m contextos.mcp_server --db ./project.db
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from . import ContextOS, __version__
from .handoff import POLICY

PROTOCOL_VERSION = "2026-07-28"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "context_put",
        "title": "Commit a unit of context",
        "description": (
            "Commit durable state to the addressable context store so it survives a "
            "model handoff. Call this as you work - when you make a decision, learn a "
            "constraint, hit a blocker, or produce a file - not at the end. Addresses "
            "look like /project/decisions/auth-library or /task/blockers/redis. "
            "Roots: user, project, task, agent, tool, artifact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "e.g. /project/decisions/db"},
                "value": {"type": "string", "description": "The content, verbatim. Do not pre-summarise."},
                "kind": {"type": "string", "enum": [
                    "goal", "constraint", "decision", "fact", "blocker",
                    "artifact", "tool_result", "preference"]},
                "source": {"type": "string", "description": "Your agent or model id"},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "lifetime": {"type": "string", "enum": ["permanent", "task", "ephemeral"]},
                "pinned": {"type": "boolean", "description": "Never evict from a packet"},
                "path": {"type": "string", "description": "For kind=artifact: the file path on disk"},
            },
            "required": ["address", "value"],
        },
    },
    {
        "name": "context_get",
        "title": "Read one address",
        "description": "Fetch a single unit by exact address. Use this to pull anything "
                       "a handoff packet listed as omitted.",
        "inputSchema": {
            "type": "object",
            "properties": {"address": {"type": "string"}},
            "required": ["address"],
        },
    },
    {
        "name": "context_search",
        "title": "Search context",
        "description": "Hybrid search over the context store: deterministic address "
                       "routing fused with BM25. Returns ranked units with their addresses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 100},
                "prefix": {"type": "string", "description": "Restrict to an address subtree"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "context_select",
        "title": "Select context under a token budget",
        "description": "Return the most relevant context for a query, packed into a "
                       "token budget. Use instead of pasting whole history into a prompt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "budget_tokens": {"type": "integer", "minimum": 100},
            },
            "required": ["query"],
        },
    },
    {
        "name": "context_handoff",
        "title": "Build a model-handoff packet",
        "description": (
            "Produce the context packet for continuing this task on a different model. "
            "direction=escalate (weak->strong) drops the narrative and low-value residue; "
            "downshift (strong->weak) keeps the guidance the weaker model cannot re-derive; "
            "lateral is a peer transfer. The packet names what it excluded so the receiving "
            "model can fetch it by address rather than guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": list(POLICY)},
                "budget_tokens": {"type": "integer", "minimum": 200},
                "from_model": {"type": "string"},
                "to_model": {"type": "string"},
                "difficulty": {"type": "number", "minimum": 0, "maximum": 1,
                               "description": "Escalating an easy task is refused: on easy "
                                              "tasks every escalation interface underperforms."},
                "format": {"type": "string", "enum": ["markdown", "json"]},
            },
        },
    },
    {
        "name": "context_stats",
        "title": "Store statistics",
        "description": "Unit and token counts, breakdown by kind, and open conflict count.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "context_conflicts",
        "title": "List unresolved conflicts",
        "description": "Contradicting writes to the same address from different sources. "
                       "Neither value should be treated as authoritative until resolved.",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
]


class Server:
    def __init__(self, db: str) -> None:
        self.ctx = ContextOS(db)
        self.client_protocol = PROTOCOL_VERSION

    # ------------------------------------------------------------- dispatch
    def handle(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            self.client_protocol = params.get("protocolVersion", PROTOCOL_VERSION)
            return self._ok(mid, {
                "protocolVersion": self.client_protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "contextos", "version": __version__,
                               "title": "ContextOS"},
                "instructions": (
                    "Commit durable state with context_put as you work. Before switching "
                    "models, call context_handoff. Anything a packet lists as omitted can "
                    "be pulled with context_get."),
            })

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"resultType": "complete", "tools": TOOLS})
        if method == "tools/call":
            return self._call(mid, params)

        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def _call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        try:
            text, structured = fn(args)
        except Exception as exc:  # tool execution error - the model can self-correct
            return self._ok(mid, {
                "resultType": "complete", "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            })
        result: dict[str, Any] = {"resultType": "complete", "isError": False,
                                  "content": [{"type": "text", "text": text}]}
        if structured is not None:
            result["structuredContent"] = structured
        return self._ok(mid, result)

    @staticmethod
    def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    # ---------------------------------------------------------------- tools
    def _t_context_put(self, a: dict[str, Any]):
        path = a.pop("path", None)
        addr, val = a.pop("address"), a.pop("value")
        if path:
            u = self.ctx.put_artifact(addr, path, val, **a)
        else:
            u = self.ctx.put(addr, val, **a)
        return (f"Committed {u.address} (v{u.version}, {u.tokens} tokens)",
                {"address": u.address, "version": u.version, "tokens": u.tokens})

    def _t_context_get(self, a: dict[str, Any]):
        u = self.ctx.get(a["address"])
        if u is None:
            return f"No live unit at {a['address']}", {"found": False}
        return u.render(), {"found": True, **u.to_dict()}

    def _t_context_search(self, a: dict[str, Any]):
        hits = self.ctx.search(a["query"], k=int(a.get("k", 10)),
                               prefix=a.get("prefix", ""))
        if not hits:
            return "No matches.", {"hits": []}
        lines = [f"{h.unit.address}  [{h.unit.kind}] score={h.score:.4f} via {h.why}\n"
                 f"  {h.unit.value[:300]}" for h in hits]
        return "\n".join(lines), {"hits": [
            {"address": h.unit.address, "kind": h.unit.kind,
             "score": h.score, "why": h.why} for h in hits]}

    def _t_context_select(self, a: dict[str, Any]):
        sel = self.ctx.select(a["query"], int(a.get("budget_tokens", 4000)))
        from .budget import render
        return (render(sel.units),
                {"addresses": sel.addresses(), "tokens": sel.tokens_selected,
                 "omitted": sel.omitted})

    def _t_context_handoff(self, a: dict[str, Any]):
        fmt = a.pop("format", "markdown")
        p = self.ctx.handoff(**a)
        return (json.dumps(p.to_dict(), indent=2) if fmt == "json" else p.render(),
                p.to_dict())

    def _t_context_stats(self, a: dict[str, Any]):
        s = self.ctx.stats()
        return json.dumps(s, indent=2), s

    def _t_context_conflicts(self, a: dict[str, Any]):
        c = self.ctx.conflicts()
        if not c:
            return "No unresolved conflicts.", {"conflicts": []}
        lines = [f"{x['address']}: {x['old_source']} said {x['old_value'][:80]!r}; "
                 f"{x['new_source']} said {x['new_value'][:80]!r}" for x in c]
        return "\n".join(lines), {"conflicts": c}

    # ----------------------------------------------------------------- loop
    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}}) + "\n")
                sys.stdout.flush()
                continue
            resp = self.handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ContextOS MCP server (stdio)")
    ap.add_argument("--db", default="contextos.db")
    args = ap.parse_args(argv)
    Server(args.db).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
