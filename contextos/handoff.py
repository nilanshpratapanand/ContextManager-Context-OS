"""Direction-aware handoff packets.

This module is the point of the project. Its shape is dictated by one empirical
result (arXiv 2608.24358, 58k agent runs on SWE-bench Verified):

    escalation (weak -> strong)
        raw full trajectory ....... 47% (Claude) / 36% (GPT) quality-gap recovery,
                                    at 4.0x / 6.1x cost
        traj-drop (no trajectory,
        working-tree edits kept) .. 64% (Claude) / 84% (GPT)

    downshift (strong -> weak)
        trajectory kept ........... 50-79% quality-gap recovery
        trajectory removed ........ 28% (Claude) / 53% (GPT)

So there is no single correct packet. Escalation wants the *state* and none of the
narrative; downshift wants the guidance too. A system that ships one format is
provably wrong in one direction. ContextOS ships both.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .budget import Selection, pack
from .retrieval import search
from .store import ContextStore
from .units import Unit, count_tokens

SCHEMA_VERSION = "contextos/handoff/1"

# What each direction carries. Derived directly from the table above.
"""What traj-drop actually drops is the *narrative* - messages, reasoning steps,
raw tool chatter. It is not "throw away everything the previous agent concluded":
the same paper found compact_pre (the departing model committing a summary) raised
recovery 47% -> 60%. Committed decisions are that summary, in structured form, so
they travel in every direction. What varies is tool chatter and low-value residue."""
POLICY: dict[str, dict[str, Any]] = {
    "escalate": {
        # No tool chatter, and no low-importance residue from the weaker model:
        # carrying it cost 2.2x per step with no quality gain.
        "kinds": ("goal", "constraint", "blocker", "artifact", "preference",
                  "decision", "fact"),
        "min_importance": 0.4,
        "budget_scale": 0.6,
        "rationale": ("traj-drop: state and committed decisions travel, the weak model's "
                      "narrative and low-value residue do not"),
    },
    "downshift": {
        # The weak model cannot re-derive the strong model's guidance; give it all.
        "kinds": ("goal", "constraint", "blocker", "artifact", "preference",
                  "decision", "fact", "tool_result"),
        "min_importance": 0.0,
        "budget_scale": 1.0,
        "rationale": ("trajectory-preserving: removing the strong model's guidance drops "
                      "recovery to 28-53%, so it is kept"),
    },
    "lateral": {
        "kinds": ("goal", "constraint", "blocker", "artifact", "preference",
                  "decision", "fact"),
        "min_importance": 0.2,
        "budget_scale": 0.8,
        "rationale": "peer transfer: state plus decisions, no raw tool chatter",
    },
}

DIFFICULTY_FLOOR = 0.35  # below this, escalation is not worth paying for


@dataclass
class HandoffPacket:
    direction: str
    goal: str
    units: list[Unit] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    from_model: str = ""
    to_model: str = ""
    created_at: float = field(default_factory=time.time)
    tokens_stored: int = 0        # every live unit, rendered
    tokens_selected: int = 0      # this packet, rendered - comparable to tokens_stored
    notes: list[str] = field(default_factory=list)

    @property
    def reduction(self) -> float:
        """Like-for-like: this packet vs. a full-replay packet of the same shape.

        Comparing a rendered packet against raw stored bytes would flatter the
        result by hiding the packet's own scaffolding, so both sides are rendered.
        """
        if not self.tokens_stored:
            return 0.0
        return 1.0 - (self.tokens_selected / self.tokens_stored)

    # ------------------------------------------------------------------ views
    def to_dict(self) -> dict[str, Any]:
        buckets: dict[str, list[dict[str, str]]] = {}
        for u in self.units:
            buckets.setdefault(u.kind, []).append(
                {"address": u.address, "value": u.value, **(
                    {"path": u.meta["path"]} if u.meta.get("path") else {})}
            )
        return {
            "schema": SCHEMA_VERSION,
            "direction": self.direction,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "created_at": self.created_at,
            "goal": self.goal,
            "context": buckets,
            "omitted": self.omitted,
            "fetch": "contextos.get(<address>) retrieves any omitted unit in full",
            "budget": {
                "full_replay_tokens": self.tokens_stored,
                "packet_tokens": self.tokens_selected,
                "reduction": round(self.reduction, 4),
            },
            "notes": self.notes,
        }

    def render(self) -> str:
        """Neutral Markdown. Deliberately not in any one agent's idiom: cross-agent
        specs transferred at Token F1 0.035 in the worst case (arXiv 2608.21208)."""
        L = [f"# Task handoff ({self.direction})"]
        if self.from_model or self.to_model:
            L.append(f"_{self.from_model or '?'} -> {self.to_model or '?'}_")
        L.append("\nYou are continuing work already in progress. The previous agent's "
                 "conversation is intentionally not included; its durable state is below.")
        L.append(f"\n## Goal\n{self.goal or '(not recorded)'}")

        order = ("constraint", "blocker", "decision", "artifact", "fact",
                 "tool_result", "preference")
        titles = {
            "constraint": "Constraints (must hold)",
            "blocker": "Open blockers",
            "decision": "Decisions already made",
            "artifact": "Work products on disk",
            "fact": "Established facts",
            "tool_result": "Relevant tool results",
            "preference": "User preferences",
        }
        for kind in order:
            us = [u for u in self.units if u.kind == kind]
            if not us:
                continue
            L.append(f"\n## {titles[kind]}")
            for u in us:
                if kind == "artifact" and u.meta.get("path"):
                    L.append(f"- `{u.meta['path']}` ({u.meta.get('bytes', 0)} bytes, "
                             f"sha256 {str(u.meta.get('sha256', ''))[:12]}) — `{u.address}`")
                else:
                    L.append(f"- {u.value}  \n  `{u.address}`")

        if self.omitted:
            L.append(f"\n## Not included ({len(self.omitted)} units, "
                     f"{sum(o['tokens'] for o in self.omitted)} tokens)")
            L.append("These exist in the context store but did not fit the budget. "
                     "If you need one, fetch it by address rather than guessing:")
            for o in self.omitted[:25]:
                L.append(f"- `{o['address']}` ({o['kind']}, {o['tokens']} tok)")
            if len(self.omitted) > 25:
                L.append(f"- ...and {len(self.omitted) - 25} more")

        if self.notes:
            L.append("\n## Notes\n" + "\n".join(f"- {n}" for n in self.notes))
        return "\n".join(L)


def classify(from_capability: float, to_capability: float) -> str:
    """Capability in [0,1]. Direction decides the packet (POLICY above)."""
    if to_capability - from_capability > 0.15:
        return "escalate"
    if from_capability - to_capability > 0.15:
        return "downshift"
    return "lateral"


def should_migrate(direction: str, difficulty: float) -> tuple[bool, str]:
    """Difficulty gate (PLAN D7).

    The paper found that on easy tasks *every* escalation interface underperformed,
    and that for Claude, restarting from scratch beat continuing an LC trajectory.
    So escalating a cheap task is a pure loss - refuse it.
    """
    if direction == "escalate" and difficulty < DIFFICULTY_FLOOR:
        return False, (f"task difficulty {difficulty:.2f} < {DIFFICULTY_FLOOR}: escalation "
                       "interfaces underperform on easy tasks; finish or restart instead")
    return True, "ok"


def build(
    store: ContextStore,
    *,
    direction: str = "lateral",
    budget_tokens: int = 4000,
    goal_address: str = "/task/goal",
    query: str = "",
    from_model: str = "",
    to_model: str = "",
    difficulty: float = 1.0,
) -> HandoffPacket:
    if direction not in POLICY:
        raise ValueError(f"direction must be one of {tuple(POLICY)}")
    policy = POLICY[direction]
    effective_budget = int(budget_tokens * policy["budget_scale"])

    goal_unit = store.get(goal_address)
    goal = goal_unit.value if goal_unit else ""

    must = [u for u in store.list("", live_only=True) if u.pinned]
    if goal_unit:
        must.append(goal_unit)
    must += store.list("", live_only=True, kinds=["constraint"])
    must += store.list("", live_only=True, kinds=["blocker"])

    q = query or goal or "current task state"
    hits = search(store, q, k=500, kinds=list(policy["kinds"]))
    floor = float(policy["min_importance"])
    hits = [h for h in hits if h.unit.importance >= floor or h.unit.pinned]

    all_live = store.list("", live_only=True)
    sel: Selection = pack(
        hits, effective_budget, must_include=must,
        allow_kinds=policy["kinds"],
    )

    # Anything live that did not make the packet is an omission, whatever the reason.
    # Without this the receiving model cannot tell "absent" from "does not exist".
    included = {u.address for u in sel.units}
    already = {o["address"] for o in sel.omitted}
    for u in all_live:
        if u.address in included or u.address in already:
            continue
        why = ("direction-policy" if u.kind not in policy["kinds"]
               else "below-importance-floor" if u.importance < floor else "budget")
        sel.omitted.append({"address": u.address, "kind": u.kind,
                            "tokens": u.tokens, "score": 0.0, "why": why})
    sel.omitted = [o for o in sel.omitted if o["address"] not in included]

    notes = [policy["rationale"]]
    ok, reason = should_migrate(direction, difficulty)
    if not ok:
        notes.append("WARNING: " + reason)
    conflicts = store.conflicts(open_only=True)
    if conflicts:
        notes.append(
            f"{len(conflicts)} unresolved context conflict(s); do not assume either "
            f"value is authoritative: " +
            ", ".join(c["address"] for c in conflicts[:5])
        )
    if sel.overflow:
        notes.append("Structural context alone exceeds the budget; the task statement "
                     "may be over-constrained.")

    packet = HandoffPacket(
        direction=direction, goal=goal, units=sel.units, omitted=sel.omitted,
        from_model=from_model, to_model=to_model,
        tokens_stored=0, tokens_selected=0, notes=notes,
    )
    _enforce_budget(packet, effective_budget, protected={u.address for u in must})
    packet.tokens_stored = full_replay_tokens(store, direction=direction, goal=goal,
                                              from_model=from_model, to_model=to_model)
    return packet


def _enforce_budget(packet: HandoffPacket, budget: int, protected: set[str],
                    max_rounds: int = 200) -> None:
    """Enforce the budget on the *rendered packet*, not on the sum of unit tokens.

    The packet's own scaffolding - headings, the loss manifest, the notes - is real
    context that the receiving model pays for. Budgeting the units alone and then
    rendering is how a system quietly ships 2x its stated budget.

    Structural units (goal, constraints, blockers, pinned) are never evicted; if
    they alone exceed the budget the packet reports overflow instead of lying.
    """
    packet.tokens_selected = count_tokens(packet.render())
    if packet.tokens_selected <= budget:
        return

    droppable = [u for u in packet.units if u.address not in protected]
    # Evict cheapest-value-first: lowest importance, then largest.
    droppable.sort(key=lambda u: (u.importance, -u.tokens))

    for _ in range(max_rounds):
        if packet.tokens_selected <= budget or not droppable:
            break
        victim = droppable.pop(0)
        packet.units = [u for u in packet.units if u.address != victim.address]
        packet.omitted.append({"address": victim.address, "kind": victim.kind,
                               "tokens": victim.tokens, "score": 0.0,
                               "why": "packet-budget"})
        packet.tokens_selected = count_tokens(packet.render())

    if packet.tokens_selected > budget:
        packet.notes.append(
            f"Structural context is {packet.tokens_selected} tokens against a "
            f"{budget}-token budget; nothing further can be dropped without losing "
            "the goal, a constraint or an open blocker.")
        packet.tokens_selected = count_tokens(packet.render())


def full_replay_tokens(store: ContextStore, *, direction: str = "lateral",
                       goal: str = "", from_model: str = "", to_model: str = "") -> int:
    """The honest baseline: every live unit, rendered through the same template.

    This is what "send the whole context" costs. Any reduction claim is measured
    against this number, not against raw stored bytes.
    """
    baseline = HandoffPacket(
        direction=direction, goal=goal, units=store.list("", live_only=True),
        omitted=[], from_model=from_model, to_model=to_model,
    )
    return count_tokens(baseline.render())


def to_json(packet: HandoffPacket) -> str:
    return json.dumps(packet.to_dict(), indent=2)
