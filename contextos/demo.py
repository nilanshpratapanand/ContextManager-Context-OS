"""End-to-end demo: python -m contextos.demo"""
from __future__ import annotations

import random

from . import ContextOS
from .bench import format_summary, run
from .handoff import should_migrate

BAR = "=" * 78


def hdr(n: int, title: str) -> None:
    print(f"\n{BAR}\n{n}. {title}\n{BAR}")


def main() -> int:
    rng = random.Random(7)
    ctx = ContextOS()

    # ---------------------------------------------------------------- setup
    hdr(1, "An agent works for a while and commits state as it goes")
    ctx.put("/task/goal", "Replace cookie sessions with OAuth2 in the billing API",
            kind="goal", importance=1.0, pinned=True)
    ctx.put("/project/constraints/no-new-deps",
            "No new runtime dependencies without architecture review",
            kind="constraint", importance=0.95)
    ctx.put("/project/constraints/zero-downtime",
            "Migration must not log existing users out",
            kind="constraint", importance=0.95)
    ctx.put("/project/architecture/auth-current",
            "Cookie sessions signed with HMAC, stored in Redis, 30-day TTL",
            kind="fact", source="researcher", importance=0.8)
    ctx.put("/project/decisions/oauth-lib",
            "Use authlib, not oauthlib - authlib is already vendored in the monorepo",
            kind="decision", source="architect", importance=0.9)
    ctx.put_artifact("/artifact/auth/token-endpoint", "src/auth/token.py",
                     "async def token(request):\n    grant = request.form['grant_type']\n    ...",
                     source="coder", importance=0.85)
    ctx.put("/task/blockers/redis-acl",
            "Redis in staging has no ACL configured; the revocation list cannot be written",
            kind="blocker", importance=0.8)
    ctx.put("/user/preferences/style",
            "Prefers explicit error types over bare exceptions",
            kind="preference", lifetime="permanent", importance=0.6)

    # ...and a great deal of tool chatter, which is what actually fills a window
    words = ["retry", "timeout", "handler", "cursor", "socket", "index", "schema",
             "worker", "cache", "token", "grant", "scope", "nonce", "claim"]
    for i in range(120):
        ctx.put(f"/tool/search/hit{i}",
                "Search result: " + " ".join(rng.choice(words) for _ in range(60)),
                kind="tool_result", source="researcher", importance=0.1,
                lifetime="ephemeral")

    st = ctx.stats()
    print(f"   {st['live_units']} units, {st['live_tokens']} tokens stored")
    print(f"   by kind: {st['by_kind']}")

    # ------------------------------------------------------------ retrieval
    hdr(2, "Addressed retrieval: ask a question, get the unit, not the history")
    for q in ("which oauth library did we decide on",
              "what is blocking us right now"):
        hit = ctx.search(q, k=1)[0]
        print(f"   Q: {q}")
        print(f"   -> {hit.unit.address}  (via {hit.why})")
        print(f"      {hit.unit.value}\n")

    # -------------------------------------------------------------- handoff
    hdr(3, "Model migration: the same task, three directions, three packets")
    print("   Handoff interfaces are not symmetric. arXiv 2608.24358 measured, over")
    print("   58k agent runs, that dropping the trajectory recovers 64-84% of the")
    print("   quality gap when escalating, but only 28-53% when downshifting.\n")
    for direction, pair in (("escalate", ("haiku-4.5", "opus-4.7")),
                            ("downshift", ("opus-4.7", "haiku-4.5")),
                            ("lateral", ("gpt-5.6", "opus-4.7"))):
        p = ctx.handoff(direction=direction, budget_tokens=1200,
                        from_model=pair[0], to_model=pair[1], difficulty=0.75)
        print(f"   {direction:<10} {pair[0]:>10} -> {pair[1]:<10} "
              f"full replay {p.tokens_stored:>6} tok  ->  packet {p.tokens_selected:>5} tok "
              f"({p.reduction:5.1%} smaller)   {len(p.units)} units, "
              f"{len(p.omitted)} listed as omitted")

    hdr(4, "What the receiving model actually reads (escalate)")
    print(ctx.handoff(direction="escalate", budget_tokens=1200,
                      from_model="haiku-4.5", to_model="opus-4.7",
                      difficulty=0.75).render())

    # ------------------------------------------------------------- conflict
    hdr(5, "Two agents disagree - the store refuses to pretend otherwise")
    ctx.put("/project/decisions/oauth-lib", "Use oauthlib - authlib's API is unstable",
            kind="decision", source="second-opinion-agent", importance=0.9)
    for c in ctx.conflicts():
        print(f"   CONFLICT at {c['address']}")
        print(f"     {c['old_source']:>22}: {c['old_value']}")
        print(f"     {c['new_source']:>22}: {c['new_value']}")
    print("\n   The packet carries the warning rather than silently shipping one value:")
    p = ctx.handoff(direction="escalate", budget_tokens=1200, difficulty=0.75)
    for n in p.notes:
        print(f"     - {n}")

    # --------------------------------------------------------- routing gate
    hdr(6, "Refusing a migration that would cost more than it returns")
    for d in (0.9, 0.2):
        ok, why = should_migrate("escalate", d)
        print(f"   difficulty {d}: {'MIGRATE' if ok else 'REFUSE'} - {why}")

    # ------------------------------------------------------------ benchmark
    hdr(7, "Benchmark: does the packet actually carry what the next steps need?")
    print(format_summary(run(direction="lateral")))

    ctx.close()
    print(f"\n{BAR}\nThe model changed. The task did not.\n{BAR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
