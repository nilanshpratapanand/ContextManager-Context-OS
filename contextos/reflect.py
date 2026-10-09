"""Reflection pass: keep a long-lived store small and honest, without a model call.

Inspired by the memory design in NVIDIA's NOOA report (a background pass that merges
duplicates, links related records and prunes what is no longer relevant). Here every
step is deterministic and reversible, because a store that silently rewrites itself
with an LLM is exactly what ContextOS's verbatim-units rule (D3) avoids:

* merge   - the same text stored under several addresses is kept once (the most
            important copy); the others are superseded, not deleted, so history stays.
* prune   - old low-importance tool results beyond the newest ``keep_tool`` are
            superseded. Pinned units, goals, constraints, blockers, decisions and
            uploaded artifacts are never touched.
* link    - ``derived_from`` in a unit's meta names the units it was built from; a
            link to a unit that no longer exists is dropped.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

PROTECTED_KINDS = {"goal", "constraint", "blocker", "decision", "artifact", "preference"}


def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip().lower()


def run(store: Any, *, keep_tool: int = 40, tool_max_importance: float = 0.3,
        now: float | None = None) -> dict[str, int]:
    """Returns counts: merged, pruned, unlinked."""
    now = now or time.time()
    live = store.list()
    out = {"merged": 0, "pruned": 0, "unlinked": 0}

    # merge exact duplicates (ignoring case and spacing)
    groups: dict[str, list[Any]] = {}
    for u in live:
        if u.pinned or u.kind in PROTECTED_KINDS or len(u.value) < 12:
            continue
        groups.setdefault(_norm(u.value), []).append(u)
    for units in groups.values():
        if len(units) < 2:
            continue
        units.sort(key=lambda u: (-u.importance, -u.updated_at, u.address))
        for dup in units[1:]:
            if store.supersede(dup.address, now):
                out["merged"] += 1

    # prune old, low-value tool results
    tools = [u for u in store.list(kinds=["tool_result"]) if not u.pinned
             and u.importance <= tool_max_importance]
    tools.sort(key=lambda u: -u.updated_at)
    for u in tools[keep_tool:]:
        if store.supersede(u.address, now):
            out["pruned"] += 1

    # drop dangling derived_from links
    alive = {u.address for u in store.list()}
    for u in store.list():
        links = u.meta.get("derived_from") if isinstance(u.meta, dict) else None
        if links:
            kept = [a for a in links if a in alive]
            if kept != links:
                meta = {**u.meta, "derived_from": kept}
                # put() would treat identical text as a no-op and keep the old
                # meta, so the link list is updated in place.
                store.db.execute("UPDATE units SET meta=? WHERE address=? AND valid_to IS NULL",
                                 (json.dumps(meta), u.address))
                store.db.commit()
                out["unlinked"] += 1
    return out
