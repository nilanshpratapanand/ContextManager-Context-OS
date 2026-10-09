import pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from contextos import router as R


class Sampling(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(R.temperature_for("write a python function to reverse a linked list"), R.TEMP_PRECISE)
        self.assertEqual(R.temperature_for("a shirt costs 800, 10% off, then 18% tax - final price?"), R.TEMP_PRECISE)
        self.assertEqual(R.temperature_for("translate this sentence to Hindi"), R.TEMP_PRECISE)
        self.assertEqual(R.temperature_for("write a short story about a lighthouse"), R.TEMP_CREATIVE)
        self.assertEqual(R.temperature_for("brainstorm names for my cafe"), R.TEMP_CREATIVE)
        self.assertEqual(R.temperature_for("why is the sky blue"), R.TEMP_DEFAULT)

    def test_empty_and_range(self):
        self.assertEqual(R.temperature_for(""), R.TEMP_DEFAULT)
        for t in (R.TEMP_PRECISE, R.TEMP_DEFAULT, R.TEMP_CREATIVE):
            self.assertTrue(0.0 <= t <= 1.0)

    def test_creative_beats_code_words(self):
        self.assertEqual(R.temperature_for("write a poem about python programming"), R.TEMP_CREATIVE)

    def test_route_event_reports_temperature(self):
        import tempfile
        from contextos.server import Engine
        e = Engine(tempfile.mkdtemp(), {}, offline=True)
        evs = list(e.chat_stream(None, "write a story about a fox"))
        route = [x for x in evs if x["type"] == "route"][0]
        self.assertEqual(route["temperature"], R.TEMP_CREATIVE)


if __name__ == "__main__":
    unittest.main()
