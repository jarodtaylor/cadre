import sys
import types
import unittest
from pathlib import Path

from cadre.failure import FailureReason
from cadre.model_client import AgentResult, ModelClient


def success_result(text, **extra):
    """The banked run_conversation success shape (#76 KTD5): completed, not
    failed, text present. ``extra`` lets a test add usage keys or override flags.
    """
    result = {"final_response": text, "completed": True, "failed": False}
    result.update(extra)
    return result


class FakeAgent:
    """Fake for the factory contract: exposes ``run_conversation(prompt) -> dict``.

    ``result`` is returned verbatim (a dict, None, or any drifted shape a test
    wants to probe); ``raises`` raises instead — the construction/call error path.
    """

    def __init__(self, *, result=None, raises=None):
        self._result = result
        self._raises = raises

    def run_conversation(self, prompt):
        if self._raises is not None:
            raise self._raises
        return self._result


def factory_returning(agent, captured=None):
    def factory(provider, model, toolset):
        if captured is not None:
            captured.append({"provider": provider, "model": model, "toolset": toolset})
        return agent
    return factory


def run_with(result=None, raises=None, **run_kwargs):
    """One adapter call against a FakeAgent — the shared harness for the
    classification matrix below."""
    client = ModelClient(agent_factory=factory_returning(FakeAgent(result=result, raises=raises)))
    kwargs = {"role": "web", "provider": "prov-a", "model": "model-x", "prompt": "go"}
    kwargs.update(run_kwargs)
    return client.run(**kwargs)


class TestSuccess(unittest.TestCase):
    def test_returns_labeled_text(self):
        client = ModelClient(agent_factory=factory_returning(FakeAgent(result=success_result("findings"))))
        r = client.run(role="web", provider="openrouter", model="google/gemini-3-flash",
                       prompt="go", toolset=["web"])
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "findings")
        self.assertEqual((r.role, r.provider, r.model), ("web", "openrouter", "google/gemini-3-flash"))
        self.assertIsNone(r.error)

    def test_successful_call_has_no_reason(self):
        # The case the __post_init__ not-None guard protects: a successful lane's
        # reason stays at its None default, and construction must not raise.
        r = run_with(result=success_result("findings"))
        self.assertIsNone(r.reason)

    def test_factory_receives_provider_model_toolset(self):
        captured = []
        client = ModelClient(agent_factory=factory_returning(FakeAgent(result=success_result("ok")), captured))
        client.run(role="social", provider="xai", model="grok-4.3", prompt="go", toolset=["x_search"])
        self.assertEqual(captured[0], {"provider": "xai", "model": "grok-4.3", "toolset": ["x_search"]})


class TestFailure(unittest.TestCase):
    def test_agent_exception_becomes_typed_failure(self):
        r = run_with(raises=RuntimeError("auth blew up"))
        self.assertFalse(r.ok)
        self.assertIsNone(r.text)
        self.assertIn("RuntimeError", r.error)
        self.assertIn("auth blew up", r.error)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)

    def test_non_runtimeerror_exception_also_caught(self):
        # The catch-all must handle exception types other than RuntimeError —
        # the boundary stays broad (agent construction, signature drift, timeouts).
        r = run_with(raises=ValueError("bad model"))
        self.assertFalse(r.ok)
        self.assertIn("ValueError", r.error)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)

    def test_empty_final_response_is_failure(self):
        for reply in ("", "   ", None):
            r = run_with(result=success_result(reply))
            self.assertFalse(r.ok, f"final_response={reply!r} should be a failure")
            self.assertIn("empty", r.error)
            self.assertIs(r.reason, FailureReason.EMPTY_OUTPUT, f"final_response={reply!r}")


class TestStructuredFlagClassification(unittest.TestCase):
    """#76 KTD3: classification reads AIAgent's structured turn-outcome flags —
    ``failed`` / non-empty ``error`` / ``interrupted`` — and NEVER the response
    text. The U9 false-verify shape (an SDK error quoted back as a normal
    assistant message) must fail; a model legitimately QUOTING an error message
    with clean flags must pass.
    """

    def test_failed_flag_dict_is_model_error_never_the_text(self):
        # The exact observed #76 shape: the post-loop fall-through's error prose
        # as final_response, with the structured failure markers set.
        r = run_with(result={
            "final_response": "I apologize, but I encountered repeated errors: auth boom",
            "completed": False,
            "failed": True,
            "error": "auth boom",
            "turn_exit_reason": "repeated_errors",
        })
        self.assertFalse(r.ok)
        self.assertIsNone(r.text)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)
        self.assertIn("auth boom", r.error)
        self.assertIn("turn_exit_reason", r.error)
        # The structured detail must never be the response text itself.
        self.assertNotIn("I apologize", r.error)

    def test_error_text_with_clean_flags_is_success(self):
        # MANDATORY anti-content-sniff regression: a response QUOTING an error
        # message, with clean structured flags, is a legitimate answer.
        quoted = 'The log line you asked about reads: "I apologize, but I encountered repeated errors: quota exceeded"'
        r = run_with(result=success_result(quoted))
        self.assertTrue(r.ok)
        self.assertEqual(r.text, quoted)
        self.assertIsNone(r.error)
        self.assertIsNone(r.reason)

    def test_interrupted_flag_is_model_error(self):
        r = run_with(result={"final_response": "partial text", "interrupted": True})
        self.assertFalse(r.ok)
        self.assertIsNone(r.text)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)
        self.assertIn("interrupted=True", r.error)

    def test_error_key_without_failed_is_model_error(self):
        # The banked mid-loop terminal shape can set error without failed being
        # present at all — a non-empty error alone is a failure marker.
        r = run_with(result={"final_response": "x", "error": "socket closed"})
        self.assertFalse(r.ok)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)
        self.assertIn("socket closed", r.error)

    def test_empty_error_key_is_not_a_failure_marker(self):
        # error present-but-empty must not trip the classifier (KTD3: "present
        # and non-empty").
        r = run_with(result=success_result("fine", error=""))
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "fine")

    def test_failure_flags_take_precedence_over_empty_text(self):
        # failed + no final_response is MODEL_ERROR, not EMPTY_OUTPUT — the
        # structured marker is the more specific signal.
        r = run_with(result={"failed": True, "error": "boom"})
        self.assertFalse(r.ok)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)
        self.assertIn("boom", r.error)

    def test_partial_alone_with_text_is_success(self):
        # The banked probe assigns `partial` no failure semantics (not pinned;
        # docs/reference/hermes/README.md fact 11): failure is keyed on
        # failed/error/interrupted ONLY, so partial with clean flags passes.
        r = run_with(result={"final_response": "what I have so far",
                             "completed": False, "failed": False, "partial": True})
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "what I have so far")

    def test_turn_exit_reason_alone_is_not_a_failure_marker(self):
        # Successful turns carry an exit reason too — it is detail on a flagged
        # failure, never a trigger by itself.
        r = run_with(result=success_result("done", turn_exit_reason="completed"))
        self.assertTrue(r.ok)

    def test_non_dict_result_is_empty_output(self):
        # None (the known dead-model shape) and any drifted non-dict return carry
        # no flags and no text — byte-compatible with the pre-#76 None path.
        for result in (None, "bare string", 42, ["x"]):
            r = run_with(result=result)
            self.assertFalse(r.ok, f"result={result!r} should be a failure")
            self.assertIsNone(r.text)
            self.assertEqual(r.error, "empty response from model")
            self.assertIs(r.reason, FailureReason.EMPTY_OUTPUT, f"result={result!r}")

    def test_missing_final_response_is_empty_output(self):
        r = run_with(result={"completed": True, "failed": False})
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "empty response from model")
        self.assertIs(r.reason, FailureReason.EMPTY_OUTPUT)


class TestUsageReceipt(unittest.TestCase):
    """#76 KTD4: the usage/cost receipt is a whitelist-copied, type-gated subset
    of the result dict — captured on success AND flagged failure, never gating
    classification. Token counters match structurally (``*_tokens`` int keys);
    cost keys are exact (banked 2026-07-09 probe names).
    """

    _FULL = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 5,
        "reasoning_tokens": 7,
        "estimated_cost_usd": 0.0042,
        "cost_status": "estimated",
        "cost_source": "pricing-table",
    }

    def test_usage_holds_exactly_the_whitelist_subset(self):
        r = run_with(result=success_result(
            "hi",
            session_id="abc",          # not usage — must not be copied
            api_calls=3,               # not usage — must not be copied
            messages=[{"role": "assistant"}],
            **self._FULL,
        ))
        self.assertTrue(r.ok)
        self.assertEqual(r.usage, self._FULL)

    def test_no_usage_keys_yields_none(self):
        r = run_with(result=success_result("hi"))
        self.assertTrue(r.ok)
        self.assertIsNone(r.usage)

    def test_zero_values_preserved_not_dropped(self):
        # OAuth/quota-billed rows may honestly report 0 — 0 is a receipt, not
        # an absence.
        r = run_with(result=success_result(
            "hi", input_tokens=0, output_tokens=0, estimated_cost_usd=0,
        ))
        self.assertEqual(
            r.usage,
            {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0},
        )

    def test_flagged_failure_still_carries_usage(self):
        # A failed call may still have burned tokens — record it.
        r = run_with(result={
            "failed": True, "error": "auth boom", "input_tokens": 64,
        })
        self.assertFalse(r.ok)
        self.assertIs(r.reason, FailureReason.MODEL_ERROR)
        self.assertEqual(r.usage, {"input_tokens": 64})

    def test_empty_output_dict_still_carries_usage(self):
        r = run_with(result={"final_response": "", "completed": True,
                             "failed": False, "input_tokens": 12})
        self.assertFalse(r.ok)
        self.assertIs(r.reason, FailureReason.EMPTY_OUTPUT)
        self.assertEqual(r.usage, {"input_tokens": 12})

    def test_exception_path_has_no_usage(self):
        r = run_with(raises=RuntimeError("boom"))
        self.assertIsNone(r.usage)

    def test_values_are_type_gated(self):
        # A drifted upstream can't smuggle arbitrary payloads under known keys:
        # counters must be non-bool ints, cost_usd a number, status/source strings.
        r = run_with(result=success_result(
            "hi",
            input_tokens="100",         # str counter — dropped
            cache_tokens=True,          # bool is not a counter — dropped
            estimated_cost_usd="free",  # str cost — dropped
            cost_status=123,            # non-str status — dropped
            output_tokens=20,           # valid — kept
        ))
        self.assertEqual(r.usage, {"output_tokens": 20})


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
        r = run_with(result=success_result("text"), toolset=["web"])
        self.assertIsNone(r.elapsed_s)

    def test_run_does_not_set_toolset_from_argument(self):
        # toolset defaults to [] and run() must leave it there, even when a toolset is passed
        r = run_with(result=success_result("text"), toolset=["web"])
        self.assertEqual(r.toolset, [])

    def test_run_does_not_set_timed_out(self):
        r = run_with(result=success_result("text"))
        self.assertFalse(r.timed_out)

    def test_error_path_also_leaves_capture_fields_at_defaults(self):
        r = run_with(raises=RuntimeError("boom"))
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

            def run_conversation(self, prompt):
                return success_result("ok")

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


class TestNoChatFakesRemain(unittest.TestCase):
    """#76 sweep guard: the adapter consumes ``run_conversation``, so a fake
    exposing only ``.chat`` would exercise a dead code path — its test would fail
    loud today, but this pins the sweep so a future fake can't quietly reintroduce
    the pattern (the false-verify bug lived exactly in a bare-``.chat`` seam).
    """

    # Needles are split so this file's own source never matches them.
    _DEF_NEEDLE = "def " + "chat("
    _CALL_NEEDLE = "." + "chat("

    def test_no_test_fake_defines_chat(self):
        tests_dir = Path(__file__).resolve().parent
        offenders = sorted(
            p.name for p in tests_dir.glob("*.py")
            if self._DEF_NEEDLE in p.read_text(encoding="utf-8")
        )
        self.assertEqual(offenders, [])

    def test_no_cadre_module_calls_chat(self):
        import cadre
        package_dir = Path(cadre.__file__).resolve().parent
        offenders = sorted(
            p.name for p in package_dir.glob("*.py")
            if self._CALL_NEEDLE in p.read_text(encoding="utf-8")
        )
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
