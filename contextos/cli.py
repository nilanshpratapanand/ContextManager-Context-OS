"""ContextOS command line.

    contextos put /project/decisions/db "PostgreSQL 16" --kind decision --importance .9
    contextos search "what database did we pick"
    contextos handoff --direction escalate --budget 2000
    contextos bench
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import ContextOS
from .budget import render


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="contextos",
                                 description="Addressable context store with model-handoff packets")
    ap.add_argument("--db", default="contextos.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("put", help="commit a unit")
    p.add_argument("address")
    p.add_argument("value")
    p.add_argument("--kind", default="fact")
    p.add_argument("--source", default="cli")
    p.add_argument("--importance", type=float, default=0.5)
    p.add_argument("--lifetime", default="task")
    p.add_argument("--pinned", action="store_true")
    p.add_argument("--path", help="file path (implies kind=artifact)")

    g = sub.add_parser("get", help="read one address")
    g.add_argument("address")
    g.add_argument("--history", action="store_true")

    ls = sub.add_parser("ls", help="list a subtree")
    ls.add_argument("prefix", nargs="?", default="")

    s = sub.add_parser("search", help="hybrid search")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=10)

    sel = sub.add_parser("select", help="pack context under a budget")
    sel.add_argument("query")
    sel.add_argument("--budget", type=int, default=2000)

    h = sub.add_parser("handoff", help="build a handoff packet")
    h.add_argument("--direction", default="lateral",
                   choices=["escalate", "downshift", "lateral"])
    h.add_argument("--budget", type=int, default=3000)
    h.add_argument("--from-model", default="")
    h.add_argument("--to-model", default="")
    h.add_argument("--difficulty", type=float, default=1.0)
    h.add_argument("--json", action="store_true")

    sub.add_parser("stats", help="store statistics")
    sub.add_parser("conflicts", help="unresolved conflicts")

    b = sub.add_parser("bench", help="run the handoff-sufficiency benchmark")
    b.add_argument("--direction", default="escalate",
                   choices=["escalate", "downshift", "lateral"])
    b.add_argument("--budgets", default="800,1500,3000")
    b.add_argument("--json", action="store_true")

    sub.add_parser("demo", help="run the end-to-end demo")

    a = ap.parse_args(argv)

    if a.cmd == "bench":
        from .bench import format_summary, run
        res = run(budgets=tuple(int(x) for x in a.budgets.split(",")), direction=a.direction)
        print(json.dumps(res, indent=2) if a.json else format_summary(res))
        return 0

    if a.cmd == "demo":
        from .demo import main as demo_main
        return demo_main()

    ctx = ContextOS(a.db)
    try:
        if a.cmd == "put":
            kw = dict(kind=a.kind, source=a.source, importance=a.importance,
                      lifetime=a.lifetime, pinned=a.pinned)
            u = (ctx.put_artifact(a.address, a.path, a.value, **{**kw, "kind": "artifact"})
                 if a.path else ctx.put(a.address, a.value, **kw))
            print(f"{u.address}  v{u.version}  {u.tokens} tokens")

        elif a.cmd == "get":
            u = ctx.get(a.address)
            if u is None:
                print(f"no live unit at {a.address}", file=sys.stderr)
                return 1
            print(u.render())
            if a.history:
                for hrow in ctx.history(a.address):
                    print(f"  v{hrow['version']} by {hrow['source']}: {hrow['value'][:100]}")

        elif a.cmd == "ls":
            for u in ctx.list(a.prefix):
                flag = "*" if u.pinned else " "
                print(f"{flag} {u.address:<48} {u.kind:<12} {u.tokens:>5}t  imp={u.importance:.2f}")

        elif a.cmd == "search":
            for hit in ctx.search(a.query, k=a.k):
                print(f"{hit.score:.4f}  {hit.unit.address}  [{hit.why}]")
                print(f"        {hit.unit.value[:160]}")

        elif a.cmd == "select":
            s = ctx.select(a.query, a.budget)
            print(render(s.units))
            print(f"\n-- {s.tokens_selected} tokens selected, {len(s.omitted)} units omitted")

        elif a.cmd == "handoff":
            p = ctx.handoff(direction=a.direction, budget_tokens=a.budget,
                            from_model=a.from_model, to_model=a.to_model,
                            difficulty=a.difficulty)
            print(json.dumps(p.to_dict(), indent=2) if a.json else p.render())
            if not a.json:
                # On a nearly empty store the packet's own scaffolding costs more
                # than replaying everything. Say so rather than printing a negative
                # number next to the word "smaller".
                word = "smaller" if p.reduction >= 0 else "LARGER - too little context to be worth packing"
                print(f"\n-- full replay {p.tokens_stored} tok -> packet "
                      f"{p.tokens_selected} tok ({abs(p.reduction):.1%} {word})")

        elif a.cmd == "stats":
            print(json.dumps(ctx.stats(), indent=2))

        elif a.cmd == "conflicts":
            cs = ctx.conflicts()
            if not cs:
                print("no unresolved conflicts")
            for c in cs:
                print(f"#{c['id']} {c['address']}")
                print(f"   {c['old_source']}: {c['old_value'][:100]}")
                print(f"   {c['new_source']}: {c['new_value'][:100]}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
