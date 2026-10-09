"""Handoff-sufficiency benchmark.

What this measures, and what it does not.

The handoff-tax paper (arXiv 2608.24358) measured end-task quality across 58,000
agent runs. Replicating that needs SWE-bench, API keys and a five-figure budget.
This harness measures the *necessary condition* underneath it, offline and
deterministically:

    State sufficiency @ budget
      After a handoff at step k, does the transferred context contain every unit
      the remaining steps provably depend on?

If a required unit is missing, the receiving model must re-derive or guess it -
which is the mechanism behind the measured quality drop. Sufficiency is therefore
an upper bound on handoff quality, and it can be computed with no model calls.

Baselines
  full_replay  send everything, truncate when the budget runs out (the naive
               "never lose context" approach the original ContextOS pitch described)
  recency      keep the most recently written units until the budget runs out
  summary      lossy compression - first sentence of every unit
  contextos    direction-aware packet + loss manifest

Two recall numbers are reported. `strict` counts only what is in the packet.
`assisted` also counts units named in the loss manifest, since the receiving model
can fetch those by address - that is what the manifest is for. Strict is the
headline; assisted shows what the manifest buys.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .budget import render
from .handoff import build as build_handoff, full_replay_tokens
from .store import ContextStore
from .units import Unit, count_tokens


@dataclass
class Step:
    description: str
    writes: list[dict[str, Any]] = field(default_factory=list)   # units produced here
    requires: list[str] = field(default_factory=list)            # addresses needed to do it


@dataclass
class Task:
    name: str
    goal: str
    steps: list[Step]
    difficulty: float = 0.6

    def required_after(self, k: int) -> set[str]:
        req: set[str] = set()
        for s in self.steps[k:]:
            req.update(s.requires)
        return req


# ---------------------------------------------------------------- task suite
def _noise(n: int, tag: str, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        out.append({
            "address": f"/tool/{tag}/n{i}",
            "value": ("Search result: " + " ".join(
                rng.choice(["retry", "timeout", "handler", "cursor", "socket", "index",
                            "schema", "worker", "cache", "token"]) for _ in range(40))),
            "kind": "tool_result", "importance": 0.1, "source": "researcher",
        })
    return out


def default_tasks() -> list[Task]:
    """Three tasks with explicit, auditable dependency ground truth."""
    t1 = Task(
        name="oauth-migration",
        goal="Replace session-cookie auth with OAuth2 authorization-code flow in the billing API",
        difficulty=0.75,
        steps=[
            Step("Survey existing auth", writes=[
                {"address": "/project/architecture/auth-current", "kind": "fact",
                 "value": "Auth is cookie sessions signed with HMAC, stored in Redis, 30-day TTL",
                 "importance": 0.8},
                *_noise(12, "auth", 1),
            ]),
            Step("Record the decision", writes=[
                {"address": "/project/decisions/oauth-lib", "kind": "decision",
                 "value": "Use authlib, not oauthlib: authlib already vendored in the monorepo",
                 "importance": 0.9, "source": "architect"},
                {"address": "/project/constraints/no-new-deps", "kind": "constraint",
                 "value": "No new runtime dependencies without architecture review",
                 "importance": 0.95},
                *_noise(15, "libs", 2),
            ], requires=["/project/architecture/auth-current"]),
            Step("Implement the token endpoint", writes=[
                {"address": "/artifact/auth/token-endpoint", "kind": "artifact",
                 "value": "async def token(request):\n    grant = request.form['grant_type']\n    ...",
                 "importance": 0.85, "meta": {"path": "src/auth/token.py"}},
                *_noise(20, "impl", 3),
            ], requires=["/project/decisions/oauth-lib", "/project/constraints/no-new-deps"]),
            Step("Handle the Redis blocker", writes=[
                {"address": "/task/blockers/redis-staging", "kind": "blocker",
                 "value": "Redis in staging has no ACL configured; token revocation list cannot be written",
                 "importance": 0.8},
                *_noise(18, "infra", 4),
            ], requires=["/project/architecture/auth-current", "/artifact/auth/token-endpoint"]),
            Step("Migrate existing sessions", requires=[
                "/project/architecture/auth-current", "/artifact/auth/token-endpoint",
                "/task/blockers/redis-staging", "/project/decisions/oauth-lib",
            ]),
            Step("Write the migration runbook", requires=[
                "/project/constraints/no-new-deps", "/task/blockers/redis-staging",
                "/artifact/auth/token-endpoint",
            ]),
        ],
    )

    t2 = Task(
        name="query-perf",
        goal="Cut p95 latency on the reporting endpoint from 4.2s to under 800ms",
        difficulty=0.65,
        steps=[
            Step("Profile the endpoint", writes=[
                {"address": "/project/perf/baseline", "kind": "fact",
                 "value": "p95 4.2s; 3.6s is one N+1 query over invoice_lines (11k rows)",
                 "importance": 0.9},
                *_noise(25, "profile", 5),
            ]),
            Step("Decide the fix", writes=[
                {"address": "/project/decisions/join-strategy", "kind": "decision",
                 "value": "Single JOIN with a covering index, not caching: data must stay live",
                 "importance": 0.9, "source": "architect"},
                {"address": "/project/constraints/live-data", "kind": "constraint",
                 "value": "Reporting must reflect writes within 1 second; no cache layer permitted",
                 "importance": 1.0},
                *_noise(20, "options", 6),
            ], requires=["/project/perf/baseline"]),
            Step("Write the migration", writes=[
                {"address": "/artifact/db/idx-invoice-lines", "kind": "artifact",
                 "value": "CREATE INDEX CONCURRENTLY idx_il_invoice_id ON invoice_lines(invoice_id) INCLUDE (amount, tax);",
                 "importance": 0.85, "meta": {"path": "migrations/0042_idx.sql"}},
                *_noise(22, "sql", 7),
            ], requires=["/project/decisions/join-strategy", "/project/constraints/live-data"]),
            Step("Verify against the baseline", requires=[
                "/project/perf/baseline", "/artifact/db/idx-invoice-lines",
                "/project/constraints/live-data",
            ]),
            Step("Roll out behind a flag", requires=[
                "/artifact/db/idx-invoice-lines", "/project/decisions/join-strategy",
            ]),
        ],
    )

    t3 = Task(
        name="flaky-tests",
        goal="Find and fix the flaky integration tests blocking the release branch",
        difficulty=0.55,
        steps=[
            Step("Collect failures", writes=[
                {"address": "/project/tests/flaky-list", "kind": "fact",
                 "value": "4 flaky: test_webhook_retry, test_tz_rollover, test_bulk_import, test_ws_reconnect",
                 "importance": 0.85},
                *_noise(30, "ci", 8),
            ]),
            Step("Find the shared cause", writes=[
                {"address": "/project/tests/root-cause", "kind": "decision",
                 "value": "All four share a module-scoped fixture that freezes time at import; ordering decides pass/fail",
                 "importance": 0.95, "source": "debugger"},
                *_noise(24, "traces", 9),
            ], requires=["/project/tests/flaky-list"]),
            Step("Patch the fixture", writes=[
                {"address": "/artifact/tests/conftest", "kind": "artifact",
                 "value": "@pytest.fixture(scope='function')\ndef frozen_clock():\n    ...",
                 "importance": 0.8, "meta": {"path": "tests/conftest.py"}},
                *_noise(20, "patch", 10),
            ], requires=["/project/tests/root-cause"]),
            Step("Re-run and confirm", requires=[
                "/project/tests/flaky-list", "/artifact/tests/conftest",
                "/project/tests/root-cause",
            ]),
        ],
    )
    return [t1, t2, t3]


# ------------------------------------------------------------- store building
def build_store(task: Task, upto_step: int, path: str = ":memory:") -> ContextStore:
    store = ContextStore(path)
    store.put("/task/goal", task.goal, kind="goal", importance=1.0, pinned=True)
    for s in task.steps[:upto_step]:
        for w in s.writes:
            w = dict(w)
            addr = w.pop("address")
            val = w.pop("value")
            store.put(addr, val, **w)
    return store


# ------------------------------------------------------------------ baselines
def _fit(units: list[Unit], budget: int) -> list[Unit]:
    out, used = [], 0
    for u in units:
        if used + u.tokens > budget:
            break
        out.append(u)
        used += u.tokens
    return out


@dataclass
class Transfer:
    """What actually crosses the handoff boundary."""
    text: str                                    # what the receiving model reads
    tokens: int
    manifest: list[str] = field(default_factory=list)  # addresses it can fetch

    def delivers(self, unit: Unit) -> bool:
        """A unit is delivered only if its content survives verbatim.

        Counting a mention of the address would let a lossy summariser score full
        recall while having thrown the fact away - the exact failure the
        portability paper found (compression gave no universal benefit).
        """
        if unit.kind == "artifact":
            # An artifact is delivered by reference, not by value: traj-drop works
            # precisely because the working tree survives the handoff. A path the
            # next agent can open counts; a path it was never told about does not.
            path = unit.meta.get("path")
            return bool((path and path in self.text) or unit.address in self.text)
        return unit.value.strip() in self.text


def strat_full_replay(store: ContextStore, budget: int, task: Task) -> Transfer:
    units = store.list("", live_only=True)
    units.sort(key=lambda u: u.created_at)   # oldest first, truncate the tail
    kept = _fit(units, budget)
    txt = render(kept)
    return Transfer(txt, count_tokens(txt))


def strat_recency(store: ContextStore, budget: int, task: Task) -> Transfer:
    units = store.list("", live_only=True)
    units.sort(key=lambda u: -u.updated_at)
    kept = _fit(units, budget)
    txt = render(kept)
    return Transfer(txt, count_tokens(txt))


def strat_summary(store: ContextStore, budget: int, task: Task) -> Transfer:
    """Lossy compression: first sentence of every unit, highest importance first.
    Keeps every address, so it looks good on any address-presence metric - and
    loses the content, which is why the metric above checks the content."""
    units = store.list("", live_only=True)
    units.sort(key=lambda u: (-u.importance, u.address))
    parts, used = [], 0
    for u in units:
        head = u.value.split(".")[0][:160]
        line = f"{u.address}: {head}"
        t = count_tokens(line)
        if used + t > budget:
            continue
        parts.append(line)
        used += t
    txt = "\n".join(parts)
    return Transfer(txt, count_tokens(txt))


def strat_contextos(store: ContextStore, budget: int, task: Task,
                    direction: str = "escalate") -> Transfer:
    p = build_handoff(store, direction=direction, budget_tokens=budget,
                      difficulty=task.difficulty)
    return Transfer(p.render(), p.tokens_selected, [o["address"] for o in p.omitted])


STRATEGIES: dict[str, Callable[..., Transfer]] = {
    "full_replay": strat_full_replay,
    "recency": strat_recency,
    "summary": strat_summary,
    "contextos": strat_contextos,
}


# --------------------------------------------------------------------- runner
def run(
    tasks: Optional[list[Task]] = None,
    budgets: tuple[int, ...] = (800, 1500, 3000),
    direction: str = "escalate",
) -> dict[str, Any]:
    tasks = tasks or default_tasks()
    rows: list[dict[str, Any]] = []

    for task in tasks:
        # Hand off at every interior step - the paper injected handoffs across the
        # 5th-50th percentile of trajectory length, not at one fixed point.
        for k in range(1, len(task.steps)):
            required = task.required_after(k)
            if not required:
                continue
            store = build_store(task, k)
            live = store.list("", live_only=True)
            by_addr = {u.address: u for u in live}
            required_units = [by_addr[a] for a in required if a in by_addr]
            if not required_units:
                store.close()
                continue
            for budget in budgets:
                for name, fn in STRATEGIES.items():
                    kw = {"direction": direction} if name == "contextos" else {}
                    tr: Transfer = fn(store, budget, task, **kw)
                    man = set(tr.manifest)
                    delivered = [u for u in required_units if tr.delivers(u)]
                    fetchable = [u for u in required_units
                                 if u not in delivered and u.address in man]
                    strict = len(delivered) / len(required_units)
                    assisted = (len(delivered) + len(fetchable)) / len(required_units)
                    # precision: of everything that crossed, how much was needed
                    carried = [u for u in live if tr.delivers(u)]
                    precision = (len(delivered) / len(carried)) if carried else 0.0
                    rows.append({
                        "task": task.name, "handoff_step": k, "budget": budget,
                        "strategy": name, "tokens": tr.tokens,
                        "over_budget": tr.tokens > budget,
                        "recall_strict": round(strict, 4),
                        "recall_assisted": round(assisted, 4),
                        "precision": round(precision, 4),
                        "sufficient": strict >= 1.0,
                        "sufficient_assisted": assisted >= 1.0,
                        "required": len(required_units), "sent": len(carried),
                    })
            store.close()

    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        a = agg.setdefault(r["strategy"], {
            "n": 0, "recall_strict": 0.0, "recall_assisted": 0.0, "precision": 0.0,
            "tokens": 0, "sufficient": 0, "sufficient_assisted": 0, "over_budget": 0,
        })
        a["n"] += 1
        a["recall_strict"] += r["recall_strict"]
        a["recall_assisted"] += r["recall_assisted"]
        a["precision"] += r["precision"]
        a["tokens"] += r["tokens"]
        a["sufficient"] += int(r["sufficient"])
        a["sufficient_assisted"] += int(r["sufficient_assisted"])
        a["over_budget"] += int(r["over_budget"])
    for name, a in agg.items():
        n = max(1, a["n"])
        a["recall_strict"] = round(a["recall_strict"] / n, 4)
        a["recall_assisted"] = round(a["recall_assisted"] / n, 4)
        a["precision"] = round(a["precision"] / n, 4)
        a["mean_tokens"] = round(a["tokens"] / n, 1)
        a["sufficiency_rate"] = round(a["sufficient"] / n, 4)
        a["sufficiency_rate_assisted"] = round(a["sufficient_assisted"] / n, 4)
        a["over_budget_rate"] = round(a["over_budget"] / n, 4)
        del a["tokens"]
    return {"rows": rows, "summary": agg, "direction": direction, "budgets": list(budgets)}


def format_summary(result: dict[str, Any]) -> str:
    order = ["full_replay", "recency", "summary", "contextos"]
    w = ("strategy", "recall", "assisted", "prec", "suff", "suff+asst", "mean tok", "over budget")
    lines = [
        f"Handoff-sufficiency benchmark   direction={result['direction']}  "
        f"budgets={result['budgets']}  n={result['summary'][order[0]]['n']} per strategy",
        "",
        f"{w[0]:<13}{w[1]:>8}{w[2]:>10}{w[3]:>8}{w[4]:>8}{w[5]:>11}{w[6]:>10}{w[7]:>13}",
        "-" * 81,
    ]
    for name in order:
        a = result["summary"].get(name)
        if not a:
            continue
        lines.append(
            f"{name:<13}{a['recall_strict']:>8.1%}{a['recall_assisted']:>10.1%}"
            f"{a['precision']:>8.1%}{a['sufficiency_rate']:>8.1%}"
            f"{a['sufficiency_rate_assisted']:>11.1%}{a['mean_tokens']:>10.0f}"
            f"{a['over_budget_rate']:>13.1%}"
        )
    lines += [
        "",
        "recall     fraction of provably-required units present in the transferred context",
        "assisted   also counting units named in the loss manifest (fetchable by address)",
        "suff       runs where every required unit made it (recall = 100%)",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="ContextOS handoff-sufficiency benchmark")
    ap.add_argument("--direction", default="escalate",
                    choices=["escalate", "downshift", "lateral"])
    ap.add_argument("--budgets", default="800,1500,3000")
    ap.add_argument("--json", action="store_true", help="emit raw rows as JSON")
    args = ap.parse_args(argv)

    res = run(budgets=tuple(int(b) for b in args.budgets.split(",")),
              direction=args.direction)
    print(json.dumps(res, indent=2) if args.json else format_summary(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
