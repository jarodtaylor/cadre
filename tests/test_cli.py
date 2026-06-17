import os
import tempfile
import unittest

from fleet_engine.cli import run_command, validate_command
from fleet_engine.model_client import AgentResult

EXAMPLE = "fleets/research-swarm.example.yaml"


def _tmp_yaml(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class FakeClient:
    def __init__(self, behavior=None):
        self.behavior = behavior or {}

    def run(self, *, role, provider, model, prompt, toolset=()):
        kind, payload = self.behavior.get(role, ("ok", f"{role}-output"))
        ok = kind == "ok"
        return AgentResult(role=role, provider=provider, model=model, ok=ok,
                           text=payload if ok else None, error=None if ok else payload)


class TestValidate(unittest.TestCase):
    def test_valid_example_passes(self):
        code, out = validate_command(EXAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("OK: research-swarm", out)
        self.assertIn("synthesis:", out)

    def test_invalid_spec_fails(self):
        path = _tmp_yaml("name: broken\nspecialists: []\n")  # missing synthesis, empty specialists
        self.addCleanup(os.unlink, path)
        code, out = validate_command(path)
        self.assertEqual(code, 1)
        self.assertIn("Invalid fleet config", out)
        self.assertIn("synthesis", out)


class TestRun(unittest.TestCase):
    def test_run_renders_synthesis_and_provenance(self):
        client = FakeClient({"synthesizer": ("ok", "THE REPORT")})
        code, out = run_command(EXAMPLE, "what's new?", client=client)
        self.assertEqual(code, 0)
        self.assertIn("THE REPORT", out)
        self.assertIn("--- provenance ---", out)
        for role in ("social", "web", "analysis"):
            self.assertIn(role, out)

    def test_run_renders_failures(self):
        client = FakeClient({"social": ("fail", "auth error"), "synthesizer": ("ok", "PARTIAL")})
        code, out = run_command(EXAMPLE, "task", client=client)
        self.assertEqual(code, 0)
        self.assertIn("[FAIL] social", out)
        self.assertIn("auth error", out)
        self.assertIn("PARTIAL", out)

    def test_run_total_failure_exits_nonzero(self):
        client = FakeClient({r: ("fail", "down") for r in ("social", "web", "analysis")})
        code, out = run_command(EXAMPLE, "task", client=client)
        self.assertEqual(code, 1)
        self.assertIn("partial result (no synthesis)", out)


if __name__ == "__main__":
    unittest.main()
