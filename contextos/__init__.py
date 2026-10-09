"""ContextOS - the state layer that makes trajectory-drop handoffs work.

Not a memory system. Memory systems (MemGPT/Letta, Mem0, Zep) solve conversational
recall and are benchmarked on it. ContextOS solves a different problem: when a task
moves from one model to another, what state must travel, and what must not.

Quick start:

    from contextos import ContextOS

    ctx = ContextOS("project.db")
    ctx.put("/task/goal", "Add OAuth2 login to the API", kind="goal")
    ctx.put("/project/architecture/database", "PostgreSQL 16",
            kind="decision", source="architect", importance=0.9)

    packet = ctx.handoff(direction="escalate", budget_tokens=2000,
                         from_model="haiku", to_model="opus")
    print(packet.render())
"""
from __future__ import annotations

from typing import Any, Optional

from .budget import Selection, pack, render
from .handoff import HandoffPacket, build as _build_handoff, classify, should_migrate
from .retrieval import Hit, get_or_search, search
from .store import ContextStore
from .units import Unit, count_tokens, normalize_address, tokens_exact

__version__ = "0.3.0"
__all__ = [
    "ContextOS", "ContextStore", "Unit", "Hit", "Selection", "HandoffPacket",
    "search", "pack", "render", "classify", "should_migrate", "count_tokens",
    "normalize_address", "tokens_exact", "__version__",
]


class ContextOS:
    """Facade over store + retrieval + budgeting + handoff."""

    def __init__(self, path: str = ":memory:") -> None:
        self.store = ContextStore(path)

    # -- write -------------------------------------------------------------
    def put(self, address: str, value: str, **kw) -> Unit:
        return self.store.put(address, value, **kw)

    def put_artifact(self, address: str, path: str, content: str, **kw) -> Unit:
        return self.store.put_artifact(address, path, content, **kw)

    def supersede(self, address: str) -> bool:
        return self.store.supersede(address)

    def delete(self, address: str) -> bool:
        return self.store.delete(address)

    # -- read --------------------------------------------------------------
    def get(self, address: str, include_superseded: bool = False) -> Optional[Unit]:
        return self.store.get(address, include_superseded)

    def list(self, prefix: str = "", **kw) -> list[Unit]:
        return self.store.list(prefix, **kw)

    def search(self, query: str, **kw) -> list[Hit]:
        return get_or_search(self.store, query, **kw)

    def history(self, address: str) -> list[dict[str, Any]]:
        return self.store.history(address)

    def conflicts(self, open_only: bool = True) -> list[dict[str, Any]]:
        return self.store.conflicts(open_only)

    def resolve_conflict(self, conflict_id: int, resolution: str) -> bool:
        return self.store.resolve_conflict(conflict_id, resolution)

    # -- select ------------------------------------------------------------
    def select(self, query: str, budget_tokens: int = 4000, **kw) -> Selection:
        hits = self.search(query, k=200, **kw)
        return pack(hits, budget_tokens, tokens_stored=self.stats()["live_tokens"])

    def context_for(self, query: str, budget_tokens: int = 4000, **kw) -> str:
        return render(self.select(query, budget_tokens, **kw).units)

    # -- migrate -----------------------------------------------------------
    def handoff(self, **kw) -> HandoffPacket:
        return _build_handoff(self.store, **kw)

    # -- lifecycle ---------------------------------------------------------
    def expire(self, task_done: bool = False) -> int:
        return self.store.expire(task_done=task_done)

    def stats(self) -> dict[str, Any]:
        return self.store.stats()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "ContextOS":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
