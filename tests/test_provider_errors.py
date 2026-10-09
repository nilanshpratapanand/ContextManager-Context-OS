import pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from contextos import live
from contextos.live import ProviderError, openai_text


class OpenAIText(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(openai_text({"choices": [{"message": {"content": "hi"}}]}), "hi")

    def test_null_content_is_empty_string(self):
        self.assertEqual(openai_text({"choices": [{"message": {"content": None}}]}), "")

    def test_error_object_with_http_200(self):
        with self.assertRaises(ProviderError) as cm:
            openai_text({"error": {"code": 429, "message": "rate limited upstream"}})
        self.assertIn("429", str(cm.exception))

    def test_missing_or_empty_choices(self):
        for bad in ({}, {"choices": []}, {"choices": None}, {"id": "x"}):
            with self.assertRaises(ProviderError):
                openai_text(bad)

    def test_not_a_dict(self):
        with self.assertRaises(ProviderError):
            openai_text("<html>gateway error</html>")


class CompleteDoesNotCrash(unittest.TestCase):
    def test_complete_turns_bad_body_into_provider_error(self):
        orig = live._post
        try:
            live._post = lambda *a, **k: {"error": {"message": "no capacity"}}
            p = live.PROVIDERS["openrouter"]
            with self.assertRaises(ProviderError):
                live.complete(p, "s", "u", {p.key_env: "k"})
            live._post = lambda *a, **k: {"candidates": [{"finishReason": "SAFETY"}]}
            g = live.PROVIDERS["gemini"]
            self.assertEqual(live.complete(g, "s", "u", {g.key_env: "k"})[0], "")
        finally:
            live._post = orig

    def test_model_pool_falls_through_on_bad_body(self):
        from contextos.agent import ModelPool
        orig = live.complete
        import contextos.agent as ag
        calls = []
        def fake(provider, *a, **k):
            calls.append(provider.name)
            if len(calls) == 1:
                raise ProviderError("200: no choices")
            return "ok", 1
        try:
            ag.complete = fake
            pool = ModelPool({"GROQ_API_KEY": "k", "GEMINI_API_KEY": "k"})
            text, who = pool.ask("smart", "s", "u")
            self.assertEqual(text, "ok"); self.assertEqual(len(calls), 2)
        finally:
            ag.complete = orig


if __name__ == "__main__":
    unittest.main()
