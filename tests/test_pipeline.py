"""Pipeline mode tests: scripted models, no network."""
import json, pathlib, sys, threading, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from contextos import pipeline as P, router


def scripted(plan_json, calls=None):
    lock = threading.Lock()
    def ask(lane, system, user):
        with lock:
            if calls is not None:
                calls.append((lane, system[:12], user))
        if system == P.PLANNER_SYSTEM:
            return plan_json, "planner"
        if system == P.MERGE_SYSTEM:
            return "FINAL", "merger"
        return "out:" + user.split("## Your subtask\n")[-1][:20], f"w-{lane}"
    return ask


class PlanTests(unittest.TestCase):
    def test_validate_drops_bad_deps_and_unknown_kind(self):
        s = P.validate([{"id": "a", "task": "x", "kind": "weird", "needs": ["zzz", "b"]},
                        {"id": "b", "task": "y", "kind": "code", "needs": ["a"]}])
        self.assertEqual(s[0].kind, "reason"); self.assertEqual(s[0].needs, [])
        self.assertEqual(s[1].needs, ["a"]); self.assertEqual(s[1].lane, router.SMART)

    def test_validate_caps_steps_and_rejects_empty(self):
        s = P.validate([{"task": f"t{i}"} for i in range(20)])
        self.assertEqual(len(s), P.MAX_STEPS)
        with self.assertRaises(ValueError):
            P.validate([{"task": "  "}, "junk"])

    def test_fallback_numbered_parts(self):
        s = P.fallback_plan("Do:\n1. summarize the notes\n2. design a database schema")
        self.assertEqual(len(s), 2); self.assertEqual(s[0].lane, router.FAST)
        self.assertEqual(s[1].lane, router.SMART)

    def test_bad_planner_reply_falls_back(self):
        steps, how = P.plan("hello there", scripted("not json"))
        self.assertEqual(how, "fallback"); self.assertEqual(len(steps), 1)

    def test_waves_respect_dependencies(self):
        s = P.validate([{"id": "a", "task": "1"}, {"id": "b", "task": "2"},
                        {"id": "c", "task": "3", "needs": ["a", "b"]}])
        w = P.waves(s)
        self.assertEqual([[x.id for x in v] for v in w], [["a", "b"], ["c"]])


class RunTests(unittest.TestCase):
    PLAN = json.dumps({"steps": [
        {"id": "a", "task": "extract numbers", "kind": "extract"},
        {"id": "b", "task": "prove the bound", "kind": "reason"},
        {"id": "c", "task": "write answer", "kind": "write", "needs": ["a", "b"]}]})

    def test_routes_by_kind_and_feeds_only_dependencies(self):
        calls = []
        evs = list(P.run("big request", scripted(self.PLAN, calls)))
        lanes = {u.split("## Your subtask\n")[-1][:12]: l for l, s, u in calls
                 if s.startswith("You complete")}
        self.assertEqual(lanes["extract numb"], router.FAST)
        self.assertEqual(lanes["prove the bo"], router.SMART)
        c_prompt = [u for l, s, u in calls if "write answer" in u and s.startswith("You complete")][0]
        self.assertIn("Input from a", c_prompt); self.assertIn("Input from b", c_prompt)
        a_prompt = [u for l, s, u in calls if "extract numbers" in u and s.startswith("You complete")][0]
        self.assertNotIn("Input from", a_prompt)
        self.assertEqual(evs[-1]["type"], "pipeline_result"); self.assertEqual(evs[-1]["text"], "FINAL")

    def test_failed_step_does_not_sink_run(self):
        base = scripted(self.PLAN)
        def ask(lane, system, user):
            if "extract numbers" in user and system.startswith("You complete"):
                raise RuntimeError("429")
            return base(lane, system, user)
        evs = list(P.run("req", ask))
        done = {e["id"]: e["ok"] for e in evs if e["type"] == "step_done"}
        self.assertFalse(done["a"]); self.assertTrue(done["b"])
        self.assertEqual(evs[-1]["type"], "pipeline_result")

    def test_all_fail_reports_error(self):
        def ask(lane, system, user):
            if system == P.PLANNER_SYSTEM:
                return self.PLAN, "p"
            raise RuntimeError("down")
        self.assertEqual(list(P.run("req", ask))[-1]["type"], "pipeline_error")

    def test_single_step_skips_merge(self):
        evs = list(P.run("hi", scripted('{"steps":[{"task":"say hi","kind":"format"}]}')))
        self.assertFalse(any(e["type"] == "merge_start" for e in evs))

    def test_merge_failure_falls_back_to_parts(self):
        base = scripted(self.PLAN)
        def ask(lane, system, user):
            if system == P.MERGE_SYSTEM:
                raise RuntimeError("x")
            return base(lane, system, user)
        self.assertIn("out:", list(P.run("req", ask))[-1]["text"])


class EngineTests(unittest.TestCase):
    def test_pipeline_turn_end_to_end(self):
        import tempfile
        from contextos.server import Engine
        from contextos.agent import ModelPool
        e = Engine(tempfile.mkdtemp(), {}, offline=True)
        e._test_pool = ModelPool({}, scripted=lambda lane, sys_, user: (
            RunTests.PLAN if sys_ == P.PLANNER_SYSTEM else
            "FINAL" if sys_ == P.MERGE_SYSTEM else f"done-{lane}"))
        evs = list(e.chat_stream(None, "/pipeline do the big thing"))
        types = [x["type"] for x in evs]
        self.assertIn("pipeline_plan", types); self.assertEqual(types.count("step_done"), 3)
        done = evs[-1]; self.assertEqual(done["type"], "done")
        self.assertEqual(done["message"]["content"], "FINAL")
        self.assertEqual(done["message"]["meta"]["lane_used"], "pipeline")
        self.assertTrue(done["message"]["meta"]["written"][0]["address"].startswith("/task/pipeline/"))

    def test_pipeline_without_models_errors_cleanly(self):
        import tempfile
        from contextos.server import Engine
        e = Engine(tempfile.mkdtemp(), {}, offline=True)
        evs = list(e.chat_stream(None, "x", lane="pipeline"))
        self.assertEqual(evs[-1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
