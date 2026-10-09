import pathlib, sys, tempfile, unittest, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from contextos import ContextOS, reflect


class Reflect(unittest.TestCase):
    def setUp(self):
        self.c = ContextOS(":memory:")

    def test_merges_duplicates_keeps_most_important_and_history(self):
        self.c.put("/project/notes/a", "Use PostgreSQL 16 for storage", kind="fact", importance=0.4)
        self.c.put("/project/notes/b", "use  postgresql 16 for STORAGE", kind="fact", importance=0.8)
        out = reflect.run(self.c.store)
        self.assertEqual(out["merged"], 1)
        self.assertIsNotNone(self.c.get("/project/notes/b"))
        self.assertIsNone(self.c.get("/project/notes/a"))
        self.assertIsNotNone(self.c.store.get("/project/notes/a", include_superseded=True))

    def test_never_touches_protected_kinds(self):
        for i, k in enumerate(["goal", "constraint", "blocker", "decision"]):
            self.c.put(f"/task/x{i}", "identical protected text here", kind=k, importance=0.5)
        self.assertEqual(reflect.run(self.c.store)["merged"], 0)
        self.assertEqual(len(self.c.store.list()), 4)

    def test_prunes_old_tool_results_only(self):
        for i in range(10):
            self.c.put(f"/tool/search/h{i}", f"result number {i} with distinct words {i*7}",
                       kind="tool_result", importance=0.1)
        self.c.put("/tool/search/keep", "important tool output worth keeping long", kind="tool_result", importance=0.9)
        out = reflect.run(self.c.store, keep_tool=3)
        self.assertEqual(out["pruned"], 7)
        self.assertIsNotNone(self.c.get("/tool/search/keep"))

    def test_dangling_links_dropped(self):
        self.c.put("/task/r", "result text", kind="fact", meta={"derived_from": ["/task/r/s1", "/task/r/gone"]})
        self.c.put("/task/r/s1", "step one output text", kind="tool_result", importance=0.9)
        out = reflect.run(self.c.store)
        self.assertEqual(out["unlinked"], 1)
        self.assertEqual(self.c.get("/task/r").meta["derived_from"], ["/task/r/s1"])

    def test_idempotent(self):
        self.c.put("/project/a", "same text same text", kind="fact"); self.c.put("/project/b", "same text same text", kind="fact")
        reflect.run(self.c.store)
        self.assertEqual(reflect.run(self.c.store), {"merged": 0, "pruned": 0, "unlinked": 0})

    def test_pipeline_stores_steps_and_links(self):
        from contextos.server import Engine
        from contextos.agent import ModelPool
        from contextos import pipeline as P
        import json
        plan = json.dumps({"steps": [{"id": "a", "task": "do a", "kind": "extract"},
                                     {"id": "b", "task": "do b", "kind": "reason"}]})
        e = Engine(tempfile.mkdtemp(), {}, offline=True)
        e._test_pool = ModelPool({}, scripted=lambda lane, s, u: (plan if s == P.PLANNER_SYSTEM
                                 else "MERGED" if s == P.MERGE_SYSTEM else f"out-{lane}"))
        evs = list(e.chat_stream(None, "/pipeline go"))
        ctx = e.ctx_for(evs[-1]["message"]["conv_id"])
        res = [u for u in ctx.store.list("/task/pipeline") if u.source == "pipeline" and u.kind == "fact"][0]
        self.assertEqual(len(res.meta["derived_from"]), 2)
        for a in res.meta["derived_from"]:
            self.assertIsNotNone(ctx.get(a))


if __name__ == "__main__":
    unittest.main()
