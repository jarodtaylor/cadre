import sys
import types
import unittest

from cadre.failure import FailureReason
from cadre.model_client import AgentResult, ModelClient


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

    def test_successful_call_has_no_reason(self):
        # The case the __post_init__ not-None guard protects: a successful lane's
        # reason stays at its None default, and construction must not raise.
        client = ModelClient(agent_factory=factory_returning(FakeAgent(reply="findings")))
        r = client.run(role="web", provider="p", model="m", prompt="go")
        self.assertIsNone(r.reason)

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
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)

    def test_non_runtimeerror_exception_also_caught(self):
        # The catch-all must handle exception types other than RuntimeError —
        # U1 has not yet pinned AIAgent's actual type, so the boundary stays broad.
        client = ModelClient(agent_factory=factory_returning(FakeAgent(raises=ValueError("bad model"))))
        r = client.run(role="web", provider="p", model="m", prompt="go")
        self.assertFalse(r.ok)
        self.assertIn("ValueError", r.error)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)

    def test_empty_response_is_failure(self):
        for reply in ("", "   ", None):
            client = ModelClient(agent_factory=factory_returning(FakeAgent(reply=reply)))
            r = client.run(role="web", provider="p", model="m", prompt="go")
            self.assertFalse(r.ok, f"reply={reply!r} should be a failure")
            self.assertIn("empty", r.error)
            self.assertIs(r.reason, FailureReason.EMPTY_OUTPUT, f"reply={reply!r}")


class TestAgentResultReasonCoercion(unittest.TestCase):
    """AgentResult.__post_init__ coerces a raw-string reason, but ONLY when it is
    not None — unlike FleetStatus.status (never None), reason defaults to None on
    every successful lane, so an unconditional coerce would raise on every success.
    """

    def test_string_reason_coerces_to_enum_member(self):
        r = AgentResult(role="web", provider="p", model="m", ok=False, reason="model_error")
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)

    def test_none_reason_stays_none(self):
        r = AgentResult(role="web", provider="p", model="m", ok=True, reason=None)
        self.assertIsNone(r.reason)

    def test_default_reason_is_none_and_does_not_raise(self):
        # No reason passed at all — the default must stay None with no ValueError,
        # which is exactly the case an unconditional coerce would break.
        r = AgentResult(role="web", provider="p", model="m", ok=True)
        self.assertIsNone(r.reason)

    def test_enum_member_reason_is_idempotent(self):
        r = AgentResult(role="web", provider="p", model="m", ok=False, reason=FailureReason.TIMEOUT)
        self.assertIs(r.reason, FailureReason.TIMEOUT)


class TestImportIsolation(unittest.TestCase):
    def test_constructible_without_hermes_agent(self):
        # Proves the lazy-import seam: ModelClient with the default factory is
        # constructible even though hermes-agent is not installed on this machine.
        # The default factory would import run_agent only when .run is called live.
        self.assertIsNotNone(ModelClient())


class TestAgentResultCaptureFieldDefaults(unittest.TestCase):
    """Guards the boundary: ModelClient.run() must NOT populate the capture fields.

    elapsed_s, toolset, and timed_out are set by the engine at collection, never
    by ModelClient.run. This test documents and enforces that boundary — if a future
    edit 'helpfully' echoes the passed toolset into the result, the engine's uniform
    enrichment logic breaks (the spec toolset, not the runtime call's toolset, is
    what goes into the manifest).
    """

    def test_run_does_not_set_elapsed_s(self):
        client = ModelClient(agent_factory=factory_returning(FakeAgent(reply="text")))
        r = client.run(role="web", provider="p", model="m", prompt="go", toolset=["web"])
        self.assertIsNone(r.elapsed_s)

    def test_run_does_not_set_toolset_from_argument(self):
        # toolset defaults to [] and run() must leave it there, even when a toolset is passed
        client = ModelClient(agent_factory=factory_returning(FakeAgent(reply="text")))
        r = client.run(role="web", provider="p", model="m", prompt="go", toolset=["web"])
        self.assertEqual(r.toolset, [])

    def test_run_does_not_set_timed_out(self):
        client = ModelClient(agent_factory=factory_returning(FakeAgent(reply="text")))
        r = client.run(role="web", provider="p", model="m", prompt="go")
        self.assertFalse(r.timed_out)

    def test_error_path_also_leaves_capture_fields_at_defaults(self):
        client = ModelClient(agent_factory=factory_returning(FakeAgent(raises=RuntimeError("boom"))))
        r = client.run(role="web", provider="p", model="m", prompt="go")
        self.assertFalse(r.ok)
        self.assertIsNone(r.elapsed_s)
        self.assertEqual(r.toolset, [])
        self.assertFalse(r.timed_out)


class TestDefaultFactoryToolsetIsFailClosed(unittest.TestCase):
    """The real adapter factory must pass an explicit [] (zero tools), never None.

    In Hermes, enabled_toolsets=None enables EVERY toolset (terminal, file, browser,
    code_execution, ...); [] enables none. The old `list(toolset) or None` collapsed
    []->None, silently handing the synthesizer and empty-toolset specialists the full
    privileged surface over untrusted content. This guards the *real* factory — the
    synthesizer has no config allowlist behind it, so this test is its only gate.
    """

    def _stub_run_agent(self):
        captured = []

        class _StubAIAgent:
            def __init__(self, **kwargs):
                captured.append(kwargs)

            def chat(self, prompt):
                return "ok"

        module = types.ModuleType("run_agent")
        module.AIAgent = _StubAIAgent
        sys.modules["run_agent"] = module
        self.addCleanup(sys.modules.pop, "run_agent", None)
        return captured

    def test_no_toolset_sends_empty_list_not_none(self):
        captured = self._stub_run_agent()
        # synthesizer-style call: no toolset passed at all
        ModelClient().run(role="synthesizer", provider="openrouter", model="m", prompt="go")
        self.assertEqual(captured[0]["enabled_toolsets"], [])
        self.assertIsNotNone(captured[0]["enabled_toolsets"])  # None would enable ALL toolsets

    def test_empty_toolset_sends_empty_list_not_none(self):
        captured = self._stub_run_agent()
        ModelClient().run(role="web", provider="p", model="m", prompt="go", toolset=[])
        self.assertEqual(captured[0]["enabled_toolsets"], [])

    def test_toolset_passed_through_verbatim(self):
        captured = self._stub_run_agent()
        ModelClient().run(role="web", provider="p", model="m", prompt="go", toolset=["web"])
        self.assertEqual(captured[0]["enabled_toolsets"], ["web"])


if __name__ == "__main__":
    unittest.main()
