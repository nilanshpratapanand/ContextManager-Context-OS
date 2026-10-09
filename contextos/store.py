"""Addressable, bi-temporal context store on SQLite + FTS5.

Design notes (see PLAN.md):
  D2  write-time commit  - agents put() as they work; packets are assembled, not reconstructed
  D3  retrieval not compression - values are stored verbatim, never pre-summarised
  D6  bi-temporal provenance + conflict detection (after Zep, arXiv 2501.13956)
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from .units import COLUMNS, Unit, count_tokens, normalize_address

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  address    TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  kind       TEXT NOT NULL,
  source     TEXT NOT NULL,
  lifetime   TEXT NOT NULL,
  importance REAL NOT NULL,
  confidence REAL NOT NULL,
  valid_from REAL NOT NULL,
  valid_to   REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  version    INTEGER NOT NULL,
  tokens     INTEGER NOT NULL,
  pinned     INTEGER NOT NULL DEFAULT 0,
  meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_units_kind     ON units(kind);
CREATE INDEX IF NOT EXISTS idx_units_valid_to ON units(valid_to);
CREATE INDEX IF NOT EXISTS idx_units_updated  ON units(updated_at);

CREATE TABLE IF NOT EXISTS unit_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  address TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL,
  source TEXT NOT NULL, version INTEGER NOT NULL,
  valid_from REAL NOT NULL, valid_to REAL NOT NULL, archived_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_address ON unit_history(address);

CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  address TEXT NOT NULL,
  old_value TEXT NOT NULL, old_source TEXT NOT NULL,
  new_value TEXT NOT NULL, new_source TEXT NOT NULL,
  detected_at REAL NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0,
  resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_conflicts_open ON conflicts(resolved);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(address, value);
"""

_WORD = re.compile(r"[A-Za-z0-9_]+")


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query. Returns '' when nothing usable."""
    words = [w.lower() for w in _WORD.findall(text or "") if len(w) > 1]
    if not words:
        return ""
    uniq = list(dict.fromkeys(words))[:32]
    return " OR ".join(f'"{w}"' for w in uniq)


class _Result:
    """Rows already fetched, so nothing is lazily read outside the lock."""

    __slots__ = ("rows", "rowcount", "lastrowid", "_i")

    def __init__(self, rows: list, rowcount: int, lastrowid: Any) -> None:
        self.rows, self.rowcount, self.lastrowid, self._i = rows, rowcount, lastrowid, 0

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list:
        return list(self.rows)


class _SafeConn:
    """Serialises every statement behind one lock.

    The dashboard serves requests on multiple threads, and a bare sqlite3
    connection refuses to be touched from a thread other than the one that made
    it. Rather than open a connection per thread (which loses the shared
    in-memory store used by tests), the connection is shared with
    check_same_thread=False and every access is serialised here.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)

    def execute(self, sql: str, args: Any = ()) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall() if cur.description else []
            return _Result(rows, cur.rowcount, cur.lastrowid)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class ContextStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.db = _SafeConn(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ write
    def put(
        self,
        address: str,
        value: str,
        *,
        kind: str = "fact",
        source: str = "unknown",
        lifetime: str = "task",
        importance: float = 0.5,
        confidence: float = 1.0,
        pinned: bool = False,
        meta: Optional[dict[str, Any]] = None,
        supersede: bool = False,
    ) -> Unit:
        """Write a unit. Mem0-style NOOP on identical value; conflict on a
        contradicting write from a *different* source unless supersede=True."""
        address = normalize_address(address)
        now = time.time()
        prev = self.get(address, include_superseded=False)

        if prev is not None and prev.value.strip() == value.strip():
            self.db.execute("UPDATE units SET updated_at=? WHERE address=?", (now, address))
            self.db.commit()
            self._event("noop", {"address": address})
            prev.updated_at = now
            return prev

        version = 1
        created = now
        if prev is not None:
            version = prev.version + 1
            created = prev.created_at
            self.db.execute(
                "INSERT INTO unit_history(address,value,kind,source,version,valid_from,valid_to,archived_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (prev.address, prev.value, prev.kind, prev.source, prev.version,
                 prev.valid_from, now, now),
            )
            if prev.source != source and not supersede:
                self.db.execute(
                    "INSERT INTO conflicts(address,old_value,old_source,new_value,new_source,detected_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (address, prev.value, prev.source, value, source, now),
                )
                self._event("conflict", {"address": address,
                                         "sources": [prev.source, source]})

        unit = Unit(
            address=address, value=value, kind=kind, source=source, lifetime=lifetime,
            importance=importance, confidence=confidence, valid_from=now, valid_to=None,
            created_at=created, updated_at=now, version=version, tokens=0,
            pinned=pinned, meta=meta or {},
        )
        self.db.execute(
            f"INSERT OR REPLACE INTO units ({COLUMNS}) VALUES ({','.join('?' * 15)})",
            unit.to_row(),
        )
        self.db.execute("DELETE FROM units_fts WHERE address=?", (address,))
        self.db.execute("INSERT INTO units_fts(address,value) VALUES(?,?)",
                        (address.replace("/", " "), value))
        self.db.commit()
        self._event("put", {"address": address, "version": version, "tokens": unit.tokens})
        return unit

    def put_artifact(self, address: str, path: str, content: str, **kw) -> Unit:
        """Artifacts are first-class (D4): traj-drop only works because durable
        work products survive the handoff."""
        from .units import sha256

        meta = dict(kw.pop("meta", {}) or {})
        meta.update({"path": path, "sha256": sha256(content), "bytes": len(content)})
        kw.setdefault("kind", "artifact")
        kw.setdefault("importance", 0.8)
        return self.put(address, content, meta=meta, **kw)

    def supersede(self, address: str, when: Optional[float] = None) -> bool:
        """Mark a unit no longer valid without deleting its provenance."""
        address = normalize_address(address)
        cur = self.db.execute("UPDATE units SET valid_to=? WHERE address=? AND valid_to IS NULL",
                              (when or time.time(), address))
        self.db.commit()
        return cur.rowcount > 0

    def delete(self, address: str) -> bool:
        address = normalize_address(address)
        cur = self.db.execute("DELETE FROM units WHERE address=?", (address,))
        self.db.execute("DELETE FROM units_fts WHERE address=?", (address.replace("/", " "),))
        self.db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------- read
    def get(self, address: str, include_superseded: bool = False) -> Optional[Unit]:
        address = normalize_address(address)
        sql = f"SELECT {COLUMNS} FROM units WHERE address=?"
        if not include_superseded:
            sql += " AND valid_to IS NULL"
        row = self.db.execute(sql, (address,)).fetchone()
        return Unit.from_row(row) if row else None

    def list(self, prefix: str = "", *, live_only: bool = True,
             kinds: Optional[Iterable[str]] = None, limit: int = 10_000) -> list[Unit]:
        sql = f"SELECT {COLUMNS} FROM units WHERE 1=1"
        args: list[Any] = []
        if prefix:
            p = "/" + prefix.strip("/").lower()
            sql += " AND (address = ? OR address LIKE ?)"
            args += [p, p + "/%"]
        if live_only:
            sql += " AND valid_to IS NULL"
        if kinds:
            ks = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(ks))})"
            args += ks
        sql += " ORDER BY address LIMIT ?"
        args.append(limit)
        return [Unit.from_row(r) for r in self.db.execute(sql, args)]

    def history(self, address: str) -> list[dict[str, Any]]:
        address = normalize_address(address)
        rows = self.db.execute(
            "SELECT value,kind,source,version,valid_from,valid_to FROM unit_history"
            " WHERE address=? ORDER BY version", (address,))
        keys = ("value", "kind", "source", "version", "valid_from", "valid_to")
        return [dict(zip(keys, r)) for r in rows]

    def conflicts(self, open_only: bool = True) -> list[dict[str, Any]]:
        sql = ("SELECT id,address,old_value,old_source,new_value,new_source,detected_at,resolved"
               " FROM conflicts")
        if open_only:
            sql += " WHERE resolved=0"
        sql += " ORDER BY detected_at DESC"
        keys = ("id", "address", "old_value", "old_source", "new_value",
                "new_source", "detected_at", "resolved")
        return [dict(zip(keys, r)) for r in self.db.execute(sql)]

    def resolve_conflict(self, conflict_id: int, resolution: str) -> bool:
        cur = self.db.execute("UPDATE conflicts SET resolved=1, resolution=? WHERE id=?",
                              (resolution, conflict_id))
        self.db.commit()
        return cur.rowcount > 0

    # -------------------------------------------------------------- lifecycle
    def expire(self, *, now: Optional[float] = None, task_done: bool = False) -> int:
        """Ephemeral units die on demand; task units die when the task closes.
        Permanent units never expire. Returns count superseded."""
        now = now or time.time()
        kinds = ["ephemeral"] + (["task"] if task_done else [])
        cur = self.db.execute(
            f"UPDATE units SET valid_to=? WHERE valid_to IS NULL AND pinned=0"
            f" AND lifetime IN ({','.join('?' * len(kinds))})", [now] + kinds)
        self.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM units WHERE valid_to IS NULL"
        ).fetchone()
        total = self.db.execute("SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM units").fetchone()
        by_kind = dict(self.db.execute(
            "SELECT kind, COUNT(*) FROM units WHERE valid_to IS NULL GROUP BY kind"))
        return {
            "live_units": row[0], "live_tokens": row[1],
            "all_units": total[0], "all_tokens": total[1],
            "by_kind": by_kind,
            "open_conflicts": self.db.execute(
                "SELECT COUNT(*) FROM conflicts WHERE resolved=0").fetchone()[0],
        }

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO events(ts,kind,payload) VALUES(?,?,?)",
                        (time.time(), kind, json.dumps(payload, separators=(",", ":"))))

    def close(self) -> None:
        self.db.commit()
        self.db.close()
