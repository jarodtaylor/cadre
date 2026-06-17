import unittest

from fleet_engine.model_client import ModelClient


class FakeAgent:
    def __init__(self, *, reply=None, raises=None):
        self._reply = reply
        self._raises = raises

    def chat(self, prompt):
        if self._raises is not None:
            raise self._raises
        return self._reply


def factory_returning(agent, captured=None):
    def factory(provider, model, toolset):
        if captured is not None:
            captured.append({"provider": provider, "model": model, "toolset": toolset})
        return agent
    return factory


class TestSuccess(unittest.TestCase):
    def test_returns_labeled_text(self):
        client = ModelClient(agent_factory=factory_returning(FakeAgent(reply="findings")))
        r = client.run(role="web", provider="openrouter", model="google/gemini-3-flash",
                       prompt="go", toolset=["web"])
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "findings")
        self.assertEqual((r.role, r.provider, r.model), ("web", "openrouter", "google/gemini-3-flash"))
        self.assertIsNone(r.error)

    def test_factory_receives_provider_model_toolset(self):
        captured = []
        client = ModelClient(agent_factory=factory_returning(FakeAgent(reply="ok"), captured))
        client.run(role="social", provider="xai", model="grok-4.3", prompt="go", toolset=["x_search"])
        self.assertEqual(captured[0], {"provider": "xai", "model": "grok-4.3", "toolset": ["x_search"]})


class TestFailure(unittest.TestCase):
    def test_agent_exception_becomes_typed_failure(self):
        client = ModelClient(agent_factory=factory_returning(FakeAgent(raises=RuntimeError("auth blew up"))))
        r = client.run(role="web", provider="p", model="m", prompt="go")
        self.assertFalse(r.ok)
        self.assertIsNone(r.text)
        self.assertIn("RuntimeError", r.error)
        self.assertIn("auth blew up", r.error)

    def test_non_runtimeerror_exception_also_caught(self):
        # The catch-all must handle exception types other than RuntimeError —
        # U1 has not yet pinned AIAgent's actual type, so the boundary stays broad.
        client = ModelClient(agent_factory=factory_returning(FakeAgent(raises=ValueError("bad model"))))
        r = client.run(role="web", provider="p", model="m", prompt="go")
        self.assertFalse(r.ok)
        self.assertIn("ValueError", r.error)

    def test_empty_response_is_failure(self):
        for reply in ("", "   ", None):
            client = ModelClient(agent_factory=factory_returning(FakeAgent(reply=reply)))
            r = client.run(role="web", provider="p", model="m", prompt="go")
            self.assertFalse(r.ok, f"reply={reply!r} should be a failure")
            self.assertIn("empty", r.error)


class TestImportIsolation(unittest.TestCase):
    def test_constructible_without_hermes_agent(self):
        # Proves the lazy-import seam: ModelClient with the default factory is
        # constructible even though hermes-agent is not installed on this machine.
        # The default factory would import run_agent only when .run is called live.
        self.assertIsNotNone(ModelClient())


if __name__ == "__main__":
    unittest.main()
