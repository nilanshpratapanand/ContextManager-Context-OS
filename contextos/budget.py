"""Tiered budgeting.

The handoff-tax paper (arXiv 2608.24358) found that carrying a weak model's full
trajectory into a strong model made every post-handoff step cost 2.2x more with no
quality gain. So the budget is not "fill the window" - it is "spend as little as
possible while still carrying everything the next step provably needs".

Overflow is never silent. Anything that does not fit is recorded in a loss manifest
(PLAN D5) so the receiving model knows what it does *not* have and can fetch it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .retrieval import Hit
from .units import Unit, count_tokens

# Tier 0 kinds are structural: without them the task statement itself is incomplete.
TIER0_KINDS = ("goal", "constraint")


@dataclass
class Selection:
    units: list[Unit] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    tokens_selected: int = 0
    tokens_stored: int = 0
    overflow: bool = False

    @property
    def reduction(self) -> float:
        if not self.tokens_stored:
            return 0.0
        return 1.0 - (self.tokens_selected / self.tokens_stored)

    def addresses(self) -> list[str]:
        return [u.address for u in self.units]


def pack(
    candidates: Iterable[Hit] | Iterable[Unit],
    budget_tokens: int,
    *,
    must_include: Optional[Iterable[Unit]] = None,
    tokens_stored: int = 0,
    allow_kinds: Optional[tuple[str, ...]] = None,
) -> Selection:
    """Fill a token budget in tiers.

    Tier 0  pinned / goal / constraint            - always in, even on overflow
    Tier 1  artifacts                             - durable work products (D4)
    Tier 2  everything else, by score-per-token   - value density
    """
    scored: list[tuple[float, Unit]] = []
    for c in candidates:
        if isinstance(c, Hit):
            scored.append((c.score, c.unit))
        else:
            scored.append((c.importance, c))

    if allow_kinds is not None:
        scored = [(s, u) for s, u in scored if u.kind in allow_kinds]

    by_addr: dict[str, tuple[float, Unit]] = {}
    for s, u in scored:
        if u.address not in by_addr or s > by_addr[u.address][0]:
            by_addr[u.address] = (s, u)

    for u in must_include or []:
        by_addr.setdefault(u.address, (float("inf"), u))

    tier0, tier1, tier2 = [], [], []
    for s, u in by_addr.values():
        if u.pinned or u.kind in TIER0_KINDS or s == float("inf"):
            tier0.append((s, u))
        elif u.kind == "artifact":
            tier1.append((s, u))
        else:
            tier2.append((s, u))

    tier0.sort(key=lambda p: (-p[1].importance, p[1].address))
    tier1.sort(key=lambda p: (-p[0], p[1].address))
    tier2.sort(key=lambda p: (-(p[0] / max(1, p[1].tokens)), p[1].address))

    sel = Selection(tokens_stored=tokens_stored or sum(u.tokens for _, u in by_addr.values()))
    used = 0

    for _s, u in tier0:
        sel.units.append(u)
        used += u.tokens
    if used > budget_tokens:
        sel.overflow = True  # structural context alone exceeds the window

    for tier, why in ((tier1, "budget"), (tier2, "budget")):
        for s, u in tier:
            if used + u.tokens <= budget_tokens:
                sel.units.append(u)
                used += u.tokens
            else:
                sel.omitted.append({
                    "address": u.address, "kind": u.kind,
                    "tokens": u.tokens, "score": round(float(s), 5), "why": why,
                })

    sel.units.sort(key=lambda u: (u.kind, u.address))
    sel.tokens_selected = used
    return sel


def render(units: list[Unit], header: str = "") -> str:
    parts = [header] if header else []
    current_kind = None
    for u in units:
        if u.kind != current_kind:
            current_kind = u.kind
            parts.append(f"\n## {u.kind}")
        parts.append(u.render())
    return "\n".join(parts).strip()


def render_tokens(units: list[Unit], header: str = "") -> int:
    return count_tokens(render(units, header))
