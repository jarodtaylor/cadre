import unittest

from fleet_engine.config import FleetConfig
from fleet_engine.engine import run_fleet
from fleet_engine.model_client import AgentResult


def _config(**overrides):
    data = {
        "name": "t",
        "synthesis": {"provider": "openrouter", "model": "synth/model", "prompt": "SYNTH:"},
        "specialists": [
            {"role": "web", "provider": "openrouter", "model": "web/model", "toolset": ["web"]},
            {"role": "social", "provider": "xai", "model": "grok", "toolset": ["x_search"]},
            {"role": "analysis", "provider": "openrouter", "model": "ana/model", "toolset": ["web"]},
        ],
    }
    data.update(overrides)
    return FleetConfig.from_dict(data)


class FakeClient:
    """Duck-typed ModelClient: behavior keyed by role -> ('ok', text) | ('fail', error)."""

    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []  # (role, prompt)

    def run(self, *, role, provider, model, prompt, toolset=()):
        self.calls.append((role, prompt))
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(
            role=role, provider=provider, model=model, ok=ok,
            text=payload if ok else None, error=None if ok else payload,
        )

    def prompt_for(self, role):
        return next(p for (r, p) in self.calls if r == role)


class TestHappyPath(unittest.TestCase):
    def test_all_succeed_then_synthesize(self):
        client = FakeClient({"synthesizer": ("ok", "FINAL REPORT")})
        result = run_fleet(_config(), "what's new in agents?", client)
        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "FINAL REPORT")
        self.assertEqual(len(result.specialists), 3)
        self.assertEqual(len(result.successes), 3)
        self.assertEqual(result.failures, [])

    def test_synthesizer_receives_labeled_provenance(self):
        client = FakeClient({"synthesizer": ("ok", "FINAL")})
        run_fleet(_config(), "task", client)
        synth_prompt = client.prompt_for("synthesizer")
        for role in ("web", "social", "analysis"):
            self.assertIn(role, synth_prompt)
        for model in ("web/model", "grok", "ana/model"):
            self.assertIn(model, synth_prompt)

    def test_fan_out_one_call_per_specialist_then_synth(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        run_fleet(_config(), "task", client)
        roles = [r for (r, _) in client.calls]
        self.assertEqual(sorted(roles[:3]), ["analysis", "social", "web"])
        self.assertEqual(roles[3], "synthesizer")
        self.assertEqual(len(client.calls), 4)


class TestProvenance(unittest.TestCase):
    def test_each_specialist_labeled(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        result = run_fleet(_config(), "task", client)
        by_role = {r.role: r for r in result.specialists}
        self.assertEqual(by_role["social"].provider, "xai")
        self.assertEqual(by_role["social"].model, "grok")
        self.assertEqual(by_role["web"].provider, "openrouter")


class TestDegradation(unittest.TestCase):
    def test_partial_failure_synthesizes_survivors(self):
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "PARTIAL")})
        result = run_fleet(_config(), "task", client)
        self.assertTrue(result.ok)
        self.assertEqual(result.synthesis, "PARTIAL")
        self.assertEqual(len(result.failures), 1)
        self.assertTrue(any("social" in n and "failed" in n for n in result.notes))
        # the failed specialist's output is not fed to the synthesizer
        self.assertNotIn("social", client.prompt_for("synthesizer"))

    def test_total_failure_no_synthesis(self):
        behavior = {r: ("fail", "down") for r in ("web", "social", "analysis")}
        client = FakeClient(behavior)
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertTrue(any("all specialists failed" in n for n in result.notes))
        self.assertNotIn("synthesizer", [r for (r, _) in client.calls])

    def test_synthesizer_failure_keeps_labeled_outputs(self):
        client = FakeClient({"synthesizer": ("fail", "rate limited")})
        result = run_fleet(_config(), "task", client)
        self.assertFalse(result.ok)
        self.assertIsNone(result.synthesis)
        self.assertEqual(len(result.successes), 3)  # specialist outputs preserved
        self.assertTrue(any("synthesizer failed" in n for n in result.notes))

    def test_single_survivor_is_flagged(self):
        client = FakeClient({
            "web": ("fail", "x"), "analysis": ("fail", "y"), "synthesizer": ("ok", "SOLO"),
        })
        result = run_fleet(_config(), "task", client)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.successes), 1)
        self.assertTrue(any("single surviving" in n for n in result.notes))


class TestSynthesisPromptFallback(unittest.TestCase):
    def test_default_prompt_used_when_prompt_empty(self):
        client = FakeClient({"synthesizer": ("ok", "x")})
        cfg = _config(synthesis={"provider": "openrouter", "model": "synth/model", "prompt": ""})
        run_fleet(cfg, "task", client)
        self.assertIn("Synthesize the specialist findings", client.prompt_for("synthesizer"))


if __name__ == "__main__":
    unittest.main()
