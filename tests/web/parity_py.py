"""Emit what the Python engine computes for tests/web/scenario.json, as JSON on stdout."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from contextos import ContextOS
from contextos import router
from contextos.units import normalize_address, AddressError
from contextos.server import visible_text, is_degenerate
from contextos.budget import render
from contextos import reflect
from contextos.live import PROVIDERS, SMART_ORDER, FAST_ORDER
from contextos.keys import INFO

sc = json.load(open(os.path.join(os.path.dirname(__file__), "scenario.json")))
ctx = ContextOS()
for addr, val, kind, src, imp, pinned in sc["units"]:
    if kind == "artifact":
        ctx.put_artifact(addr, "api/routes.py", val, source=src, importance=imp)
    else:
        ctx.put(addr, val, kind=kind, source=src, importance=imp, pinned=pinned)
out = {"select": {}, "handoff": {}, "route": {}, "addr": {}, "text": {}, "stats": ctx.stats()}
for q in sc["queries"]:
    for b in sc["budgets"]:
        s = ctx.select(q, budget_tokens=b)
        out["select"][f"{q}|{b}"] = {"addrs": [u.address for u in s.units], "tokens": s.tokens_selected,
                                      "omitted": sorted(o["address"] for o in s.omitted), "render": render(s.units)}
for d in ("escalate", "downshift", "lateral"):
    for b in sc["budgets"]:
        p = ctx.handoff(direction=d, budget_tokens=b, from_model="a", to_model="b", difficulty=0.7)
        out["handoff"][f"{d}|{b}"] = {"render": p.render(), "tokens": p.tokens_selected, "full": p.tokens_stored,
                                      "units": [u.address for u in p.units], "omitted": sorted(o["address"] for o in p.omitted)}
for t in sc["prompts"]:
    sco, reasons = router.score(t)
    out["route"][t] = {"score": sco, "reasons": reasons, "override": list(router.parse_override(t))}
for a in sc["addresses"]:
    try: out["addr"][a] = normalize_address(a)
    except AddressError as e: out["addr"][a] = "ERR"
for t in sc["texts"]:
    out["text"][t] = {"visible": visible_text(t), "degenerate": is_degenerate(t)}
out["providers"] = {n: {"key_env": p.key_env, "model": p.model, "url": p.url, "style": p.style,
                        "tier": p.tier, "lane": p.lane, "extra": p.extra} for n, p in PROVIDERS.items()}
out["orders"] = {"smart": SMART_ORDER, "fast": FAST_ORDER}
out["keys"] = [{k: v for k, v in i.items() if k != "extra"} | ({"extra": i["extra"]} if "extra" in i else {}) for i in INFO]
out["temp"] = {t: router.temperature_for(t) for t in sc["prompts"] + sc["creative"]}
r = ContextOS()
for a, v, k, imp, meta in sc["reflect"]:
    r.put(a, v, kind=k, importance=imp, **({"meta": meta} if meta else {}))
res = reflect.run(r.store, keep_tool=3)
out["reflect"] = {"counts": res, "live": sorted(u.address for u in r.store.list()),
                  "links": {u.address: u.meta.get("derived_from") for u in r.store.list() if u.meta.get("derived_from") is not None}}
print(json.dumps(out))
