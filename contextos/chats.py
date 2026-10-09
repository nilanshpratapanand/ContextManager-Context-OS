"""Conversation transcripts for the chat UI.

This is what the user sees, not what the model is given. The model gets the
conversation's ContextOS store plus the last exchange; the transcript exists so
the UI can show and edit the conversation.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv_id, seq);
"""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def title_from(prompt: str, words: int = 7) -> str:
    """First few words of the opening message. No model call: titling every chat
    with an LLM would spend free quota on something the user rarely reads."""
    parts = " ".join(prompt.split()).split(" ")
    title = " ".join(parts[:words])
    return (title + ("…" if len(parts) > words else ""))[:80] or "New chat"


class ChatStore:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ------------------------------------------------------- conversations
    def create(self, title: str = "New chat") -> dict[str, Any]:
        now = time.time()
        cid = _new_id()
        with self.lock, self.db:
            self.db.execute("INSERT INTO conversations VALUES (?,?,?,?)",
                            (cid, title, now, now))
        return {"id": cid, "title": title, "created": now, "updated": now}

    def list(self, query: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM conversations"
        args: tuple = ()
        if query:
            sql += (" WHERE title LIKE ? OR id IN (SELECT conv_id FROM messages"
                    " WHERE content LIKE ?)")
            args = (f"%{query}%", f"%{query}%")
        rows = self.db.execute(sql + " ORDER BY updated DESC", args).fetchall()
        return [dict(r) for r in rows]

    def get(self, cid: str) -> Optional[dict[str, Any]]:
        row = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        return {**dict(row), "messages": self.messages(cid)}

    def rename(self, cid: str, title: str) -> bool:
        title = title.strip()[:80] or "New chat"
        with self.lock, self.db:
            cur = self.db.execute("UPDATE conversations SET title=? WHERE id=?",
                                  (title, cid))
        return cur.rowcount > 0

    def delete(self, cid: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        return cur.rowcount > 0

    # ------------------------------------------------------------ messages
    def messages(self, cid: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM messages WHERE conv_id=? ORDER BY seq",
                               (cid,)).fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"])} for r in rows]

    def add(self, cid: str, role: str, content: str,
            meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        now = time.time()
        mid = _new_id()
        with self.lock, self.db:
            seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM messages "
                                  "WHERE conv_id=?", (cid,)).fetchone()[0]
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                            (mid, cid, seq, role, content, json.dumps(meta or {}), now))
            self.db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, cid))
        return {"id": mid, "conv_id": cid, "seq": seq, "role": role,
                "content": content, "meta": meta or {}, "created": now}

    def truncate_from(self, cid: str, mid: str) -> list[dict[str, Any]]:
        """Delete message `mid` and everything after it; return what was removed
        so the caller can undo the store writes those replies made."""
        row = self.db.execute("SELECT seq FROM messages WHERE id=? AND conv_id=?",
                              (mid, cid)).fetchone()
        if not row:
            return []
        removed = [m for m in self.messages(cid) if m["seq"] >= row["seq"]]
        with self.lock, self.db:
            self.db.execute("DELETE FROM messages WHERE conv_id=? AND seq>=?",
                            (cid, row["seq"]))
        return removed

    def close(self) -> None:
        self.db.close()
