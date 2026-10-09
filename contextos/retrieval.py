"""Hybrid retrieval: address routing + BM25, fused with Reciprocal Rank Fusion.

Why RRF and not a weighted sum of raw scores: BM25 scores and address-overlap
scores live on incompatible scales, and FTS5's bm25() is an unbounded negative.
RRF fuses by *rank*, so no calibration is needed. Zep (arXiv 2501.13956) uses the
same family of rerankers over a cosine + BM25 + graph-traversal ensemble.

Embeddings are deliberately out of scope for v1 (PLAN §10): address routing gives
us the exact-match path that a vector index is bad at, and BM25 covers the rest.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Optional

from .store import ContextStore, fts_query
from .units import ROOTS, Unit, address_segments

RRF_K = 60
_WORD = re.compile(r"[A-Za-z0-9_]+")

# Query words that reliably indicate an address root or a kind.
ROOT_HINTS = {
    "user": "user", "preference": "user", "preferences": "user", "profile": "user",
    "project": "project", "architecture": "project", "requirement": "project",
    "requirements": "project", "decision": "project", "decided": "project",
    "task": "task", "goal": "task", "progress": "task", "blocker": "task",
    "blocked": "task", "next": "task",
    "tool": "tool", "search": "tool", "query": "tool",
    "file": "artifact", "code": "artifact", "artifact": "artifact",
    "diff": "artifact", "patch": "artifact", "implementation": "artifact",
    "agent": "agent",
}

# Query intent -> unit kind. "what is blocking us" should surface blockers even
# though no blocker's text contains the word "blocking".
KIND_HINTS = {
    "block": "blocker", "blocker": "blocker", "stuck": "blocker", "problem": "blocker",
    "decid": "decision", "decision": "decision", "chose": "decision", "choose": "decision",
    "pick": "decision", "why": "decision",
    "goal": "goal", "objective": "goal", "trying": "goal",
    "constraint": "constraint", "requir": "constraint", "must": "constraint",
    "rule": "constraint", "allow": "constraint",
    "file": "artifact", "code": "artifact", "wrote": "artifact", "implement": "artifact",
    "prefer": "preference",
}


def _stem(word: str) -> str:
    """Crude suffix stripping. Enough to bridge blocking/blocked/blocker and
    decide/decided/decision without pulling in a stemming dependency."""
    w = word.lower()
    for suf in ("ations", "ation", "ing", "ers", "er", "ed", "es", "s"):
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _hinted_kinds(query: str) -> set[str]:
    kinds: set[str] = set()
    for w in _WORD.findall(query or ""):
        for probe in (w.lower(), _stem(w)):
            if probe in KIND_HINTS:
                kinds.add(KIND_HINTS[probe])
    return kinds


@dataclass
class Hit:
    unit: Unit
    score: float
    why: str  # which rankers contributed - keeps retrieval auditable

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Hit {self.unit.address} {self.score:.4f} {self.why}>"


def route(query: str) -> list[str]:
    """Deterministic address routing: which roots does this query plausibly touch?

    This is the 'fast path' of the design - a question naming a root gets that
    root's subtree searched first, with no model call and no embedding.
    """
    words = [w.lower() for w in _WORD.findall(query or "")]
    roots: list[str] = []
    for w in words:
        s = _stem(w)
        r = ROOT_HINTS.get(w) or ROOT_HINTS.get(s) or (w if w in ROOTS else None)
        if r and r not in roots:
            roots.append(r)
    return roots


def _address_overlap(query_words: set[str], unit: Unit) -> float:
    segs = set(address_segments(unit.address))
    segs |= {_stem(s) for s in segs}
    if not segs:
        return 0.0
    hits = len(query_words & segs)
    if not hits:
        return 0.0
    # Deeper matches are more specific, so weight by depth of the matched address.
    return hits / len(segs) * (1.0 + 0.1 * len(segs))


def _prior(unit: Unit, now: float) -> float:
    """Importance x confidence x recency. Half-life of 7 days on updated_at."""
    age_days = max(0.0, (now - unit.updated_at) / 86400.0)
    recency = math.exp(-age_days / 7.0)
    return unit.importance * unit.confidence * (0.5 + 0.5 * recency)


def _rrf(ranked: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for lst in ranked:
        for i, addr in enumerate(lst):
            scores[addr] = scores.get(addr, 0.0) + 1.0 / (k + i + 1)
    return scores


def search(
    store: ContextStore,
    query: str,
    *,
    k: int = 20,
    prefix: str = "",
    kinds: Optional[list[str]] = None,
    include_superseded: bool = False,
) -> list[Hit]:
    now = time.time()
    raw_words = _WORD.findall(query or "")
    query_words = {w.lower() for w in raw_words} | {_stem(w) for w in raw_words}
    want_kinds = _hinted_kinds(query)

    candidates: dict[str, Unit] = {}

    # -- ranker 1: address routing ----------------------------------------
    routed_roots = route(query)
    scan_prefixes = [prefix] if prefix else (routed_roots or [""])
    for p in scan_prefixes:
        for u in store.list(p, live_only=not include_superseded, kinds=kinds):
            candidates[u.address] = u
    if not prefix and routed_roots:
        # Routing narrows, it must not blind us: keep a global pool too.
        for u in store.list("", live_only=not include_superseded, kinds=kinds):
            candidates.setdefault(u.address, u)

    # -- ranker 2: BM25 over FTS5 -----------------------------------------
    bm25_rank: list[str] = []
    q = fts_query(query)
    if q:
        try:
            rows = store.db.execute(
                "SELECT address, bm25(units_fts) FROM units_fts WHERE units_fts MATCH ?"
                " ORDER BY bm25(units_fts) LIMIT ?", (q, k * 5),
            ).fetchall()
        except Exception:
            rows = []
        for addr_spaced, _score in rows:
            addr = "/" + "/".join(addr_spaced.split())
            u = candidates.get(addr) or store.get(addr, include_superseded)
            if u is None:
                continue
            if kinds and u.kind not in kinds:
                continue
            if not include_superseded and not u.live:
                continue
            candidates.setdefault(addr, u)
            bm25_rank.append(addr)

    if prefix:
        # A prefix is a hard scope, not a hint: BM25 can surface anything in the
        # store, so the filter has to apply after every ranker has contributed.
        p = "/" + prefix.strip("/").lower()
        candidates = {a: u for a, u in candidates.items()
                      if a == p or a.startswith(p + "/")}
        bm25_rank = [a for a in bm25_rank if a in candidates]

    if not candidates:
        return []

    addr_rank = sorted(
        (a for a in candidates),
        key=lambda a: -_address_overlap(query_words, candidates[a]),
    )
    addr_rank = [a for a in addr_rank if _address_overlap(query_words, candidates[a]) > 0]

    prior_rank = sorted(candidates, key=lambda a: -_prior(candidates[a], now))

    # Intent ranker: the query asked about a kind of thing, so rank that kind first.
    kind_rank: list[str] = []
    if want_kinds:
        kind_rank = sorted(
            (a for a in candidates if candidates[a].kind in want_kinds),
            key=lambda a: -_prior(candidates[a], now),
        )

    fused = _rrf([r for r in (addr_rank, bm25_rank, kind_rank, prior_rank) if r])

    hits: list[Hit] = []
    for addr, score in fused.items():
        why = []
        if addr in addr_rank:
            why.append("addr")
        if addr in bm25_rank:
            why.append("bm25")
        if addr in kind_rank:
            why.append("kind")
        if addr in prior_rank[: max(10, k)]:
            why.append("prior")
        hits.append(Hit(candidates[addr], score, "+".join(why) or "prior"))

    hits.sort(key=lambda h: (-h.score, h.unit.address))
    return hits[:k]


def get_or_search(store: ContextStore, query: str, **kw) -> list[Hit]:
    """If the query *is* an address, that is the answer - no ranking needed."""
    try:
        u = store.get(query)
    except Exception:
        u = None
    if u is not None:
        return [Hit(u, 1.0, "exact")]
    return search(store, query, **kw)
