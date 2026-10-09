"""MCP client: connect to MCP servers over stdio and use their tools.

Servers are listed in mcp.json, in the same shape Claude Desktop uses:

    {"mcpServers": {
        "filesystem": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/work"]},
        "contextos":  {"command": "python", "args": ["-m", "contextos.mcp_server"],
                       "trusted": true}
    }}

Each server's tools become agent tools named mcp__<server>__<tool>. Calls need
the user's approval unless the server is marked "trusted": true. A server can be
switched off with "disabled": true.

Transport: newline-delimited JSON-RPC 2.0 on the child's stdin/stdout.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from .mcp_server import PROTOCOL_VERSION
from .tools import Tool, ToolError


class MCPServer:
    def __init__(self, name: str, command: str, args: Optional[list[str]] = None,
                 env: Optional[dict[str, str]] = None, trusted: bool = False,
                 timeout: float = 60, cwd: Optional[str] = None) -> None:
        self.name, self.trusted, self.timeout = name, trusted, timeout
        self.tools: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self._id = 0
        self._pending: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []
        # Resolve "npx" -> npx.cmd on Windows; "python" -> this interpreter.
        exe = sys.executable if command in ("python", "python3", "py") else (
            shutil.which(command) or command)
        child_env = {**os.environ, **(env or {})}
        try:
            self.proc = subprocess.Popen(
                [exe, *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=child_env, cwd=cwd, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
        except OSError as e:
            raise ToolError(f"MCP server '{name}' could not start ({command}): {e}") from None
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # ------------------------------------------------------------ transport
    def _read(self) -> None:
        for line in self.proc.stdout:            # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                         # servers sometimes log to stdout
            q = self._pending.get(msg.get("id")) if isinstance(msg, dict) else None
            if q is not None:
                q.put(msg)
        for q in list(self._pending.values()):  # process ended: unblock callers
            q.put({"error": {"message": "server exited"}})

    def _read_err(self) -> None:
        for line in self.proc.stderr:            # type: ignore[union-attr]
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

    def _send(self, msg: dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise ToolError(f"MCP server '{self.name}' is not running. "
                            + " | ".join(self.stderr_tail[-3:]))
        self.proc.stdin.write(json.dumps(msg) + "\n")   # type: ignore[union-attr]
        self.proc.stdin.flush()                           # type: ignore[union-attr]

    def request(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
        q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._pending[rid] = q
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params or {}})
            try:
                msg = q.get(timeout=self.timeout)
            except queue.Empty:
                raise ToolError(f"MCP server '{self.name}' did not answer {method} "
                                f"within {self.timeout:.0f}s") from None
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise ToolError(f"MCP {self.name}/{method}: {msg['error'].get('message')}")
        return msg.get("result")

    # ------------------------------------------------------------- protocol
    def start(self) -> "MCPServer":
        res = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "ContextOS", "version": "0.2"}})
        self.info = (res or {}).get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        cursor: Optional[str] = None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            self.tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return self

    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        res = self.request("tools/call", {"name": tool, "arguments": arguments}) or {}
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(json.dumps(c.get("resource"))[:2000])
            else:
                parts.append(f"[{c.get('type')} content]")
        text = "\n".join(parts) or json.dumps(res.get("structuredContent", res))[:4000]
        if res.get("isError"):
            raise ToolError(f"{self.name}.{tool} failed: {text}")
        return text

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()               # type: ignore[union-attr]
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()

    # --------------------------------------------------------- agent tools
    def as_tools(self) -> list[Tool]:
        out = []
        for t in self.tools:
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            req = set(schema.get("required") or [])
            params = {(k if k in req else k + "?"):
                      (v.get("description") or v.get("type") or "value")[:120]
                      for k, v in props.items()}

            def fn(_t=t["name"], _types={k: v.get("type") for k, v in props.items()},
                   **kw: str) -> str:
                return self.call(_t, {k: _coerce(v, _types.get(k)) for k, v in kw.items()})
            out.append(Tool(f"mcp__{self.name}__{t['name']}",
                            f"[{self.name}] {(t.get('description') or '').strip()[:300]}",
                            params, fn, "read" if self.trusted else "external"))
        return out


def _coerce(v: str, typ: Optional[str]) -> Any:
    """Agent arguments arrive as text; MCP servers validate JSON types."""
    try:
        if typ == "integer":
            return int(v)
        if typ == "number":
            return float(v)
        if typ == "boolean":
            return str(v).strip().lower() in ("true", "1", "yes")
        if typ in ("object", "array"):
            return json.loads(v)
    except (ValueError, json.JSONDecodeError):
        pass
    return v


def load_config(path: str = "mcp.json") -> dict[str, dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ToolError(f"{path} is not valid JSON: {e}") from None
    return {k: v for k, v in (data.get("mcpServers") or {}).items()
            if isinstance(v, dict) and v.get("command")}


class Connectors:
    """Every configured MCP server, started on demand, with status for the UI."""

    def __init__(self, path: str = "mcp.json") -> None:
        self.path = path
        self.servers: dict[str, MCPServer] = {}
        self.errors: dict[str, str] = {}

    def start(self) -> None:
        for name, cfg in load_config(self.path).items():
            if cfg.get("disabled") or name in self.servers:
                continue
            try:
                self.servers[name] = MCPServer(
                    name, cfg["command"], cfg.get("args"), cfg.get("env"),
                    bool(cfg.get("trusted")), float(cfg.get("timeout", 60)),
                    cfg.get("cwd")).start()
                self.errors.pop(name, None)
            except ToolError as e:
                self.errors[name] = str(e)

    def tools(self) -> list[Tool]:
        return [t for s in self.servers.values() for t in s.as_tools()]

    def status(self) -> list[dict[str, Any]]:
        cfg = load_config(self.path)
        rows = []
        for name, c in cfg.items():
            s = self.servers.get(name)
            rows.append({"name": name, "command": " ".join([c["command"], *c.get("args", [])])[:120],
                         "trusted": bool(c.get("trusted")), "disabled": bool(c.get("disabled")),
                         "running": bool(s and s.proc.poll() is None),
                         "tools": [t["name"] for t in s.tools] if s else [],
                         "error": self.errors.get(name)})
        return rows

    def close(self) -> None:
        for s in self.servers.values():
            s.close()
        self.servers.clear()
