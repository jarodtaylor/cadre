---
module: fleet_engine
date: 2026-06-27
problem_type: design_pattern
component: testing_framework
severity: medium
related_components:
  - development_workflow
applies_when:
  - "Two modules must agree on a text-format/protocol across a boundary that forbids a shared constant (engine-purity, layering, separate processes)"
  - "One side emits a structured text format and the other parses it, with no compiler-enforced contract between them"
  - "The live integration is paid, remote, or slow, so format drift costs real money or time to discover"
  - "The format is human-readable structured text, not a binary or schema-validated format with its own enforcement"
tags:
  - coupling-test
  - cross-module-contract
  - text-format-contract
  - format-drift
  - engine-purity
  - test-as-contract
  - build-time-validation
  - end-to-end-smoke
---

# Test-as-Contract for Cross-Isolation Format Coupling

## Context

When two modules must agree on a text protocol across an isolation boundary that forbids sharing a constant, the contract lives in two separate source files with no compiler enforcement. Any typo — a renamed keyword, a changed delimiter, a shifted casing — silently breaks the parser at runtime. If the only integration test is a live (paid/remote) run, format drift surfaces there.

In Cadre's judge-convergence feature, `fleet_engine/engine.py`'s `_judge_prompt` function emits a per-lane judge response format:

```
=== LANE: <exact role string> ===
Grade: <score / letter / PASS|FAIL>
Rationale: <free text>
```

The caller-layer parser `fleet_engine/judge_grade.py::parse_grades(judge_text, surviving_lanes)` reads exactly that format. It matches each `=== LANE: <label> ===` marker to a surviving lane on the exact `role` key, then extracts `Grade:` and `Rationale:` fields per block.

The isolation rule — engine purity — forbids either module from importing the other. `judge_grade.py` must not import `engine`; `engine.py` must not import `judge_grade`. This is enforced by `tests/test_personas.py::TestEngineIsolation`, which source-scans both modules for forbidden import substrings. That isolation rule is correct and load-bearing. But it has a price: there is no shared format constant. The format is duplicated by necessity across the two modules. Without a test pinning the two sides together, a one-character edit to the marker in either file is invisible until the paid host run.

The plan for this feature acknowledged the risk and deferred "does a real model emit the format the parser expects?" to the host dogfood. But the *self-consistency* class of failures — "does our emit side match our parse side?" — does not require a real model. It can be proven at build time.

## Guidance

Pin the cross-isolation format contract with two cheap, laptop-runnable tests:

**1. Coupling test** (`tests/test_judge_grade.py::TestCouplingWithEngine`)

Import the real `_judge_prompt` (the test file is explicitly allowed to cross the boundary; only `judge_grade.py` itself must not). Assert that all three marker tokens the parser keys on appear in the prompt's emitted text:

```python
from fleet_engine.engine import _judge_prompt
prompt = _judge_prompt(cfg, "task", successes)
self.assertIn("=== LANE:", prompt)
self.assertIn("Grade:", prompt)
self.assertIn("Rationale:", prompt)
```

Then hand-build a response in exactly the format the prompt instructs for and round-trip it through `parse_grades`. Assert full structured extraction — correct entries per lane, `ungraded` empty, `parsed_ok` true, model resolved from the lane tuple (not judge text). If either side's format drifts, this test fails before any code reaches the remote host.

**2. End-to-end smoke** (`tests/test_capture.py::TestJudgeEndToEnd`)

Build a judge `FleetConfig`, run `run_fleet` with a `FakeClient` whose judge call returns a canonical-format response, then assert both render and capture paths:

- `render_result` shows the grade content (e.g., `"Grade: A"` appears in the rendered output)
- `save_run` writes a `synthesis.md` with the `# Judge grade` header and a `manifest.json` with structured `grades` entries and an empty `ungraded` list

This smoke catches edge-wiring breaks — render or capture calling `parse_grades` with wrong arguments, a missing field on the `FleetResult`, a manifest serialization gap — that per-unit tests cannot see because they test each unit in isolation.

The key nuance: the coupling test proves "our emit side and our parse side agree." The live run proves "a real model actually complies with the format we both expect." These are different questions. The test closes the first; the live run narrows to only the second.

## Why This Matters

Without this pattern, format drift is invisible until it reaches a paid/remote run. Worse, it may not fail loudly there: `parse_grades` fails toward false-partial (a lane lands in `ungraded`) rather than raising, so a delimiter typo between the emit and parse sides would produce zero structured grades in the manifest — a silent mismatch that a human eyeballing the terminal output might miss if the raw judge text still looks coherent.

The isolation rule is correct — engine purity prevents the engine from importing arbitrary caller-layer code, limiting the blast radius of caller changes and keeping the engine testable against fakes. Abandoning it to allow a shared constant would be the wrong fix. The test is the alternative to the shared definition: a test-enforced contract achieves the same safety guarantee without coupling the modules at import time.

When Cadre ran the judge feature on the paid host (5 real models, an Opus-4.8 judge), the parser needed no loosening — real model output matched the format, all 4 specialist lanes parsed into the manifest. The local coupling test had already de-risked the self-consistency class. The live run only had to answer the remaining unknown: does a real model comply? It did. That is the correct division of labor.

## When to Apply

Use this pattern when all of the following are true:

- Two modules share a text-format protocol (a prompt template on one side, a regex parser on the other; a serializer and a deserializer; a log emitter and a log reader)
- An isolation rule (purity, layering, or dependency direction) forbids them from sharing a constant
- The live integration test is expensive, paid, remote, or slow — so the cost of discovering format drift at that boundary is high
- The format is human-readable structured text, not a binary or schema-validated format (those have other enforcement mechanisms)

This pattern is NOT a substitute for the live integration test. It narrows the live test's scope so it only has to answer "does a real external system comply?" — not "do our own two sides agree?" Both questions must be answered; the coupling test just answers one of them cheaply.

## Examples

### The format contract (duplicated by necessity)

`fleet_engine/engine.py::_judge_prompt` (emit side, line ~124):
```python
f"=== LANE: {example_role} ===\n"
"Grade: <a score, letter grade, or PASS/FAIL — your choice of scale>\n"
"Rationale: <your justification; may span multiple lines>\n\n"
"Copy each lane's exact role string verbatim into the === LANE: <role> === marker — "
```

`fleet_engine/judge_grade.py::parse_grades` (parse side, line ~17 docstring, regex ~76):
```python
# === LANE: <exact role string> ===
# Grade: <grade value — judge's choice of scale>
# Rationale: <free-text justification; may span multiple lines>

_GRADE_RE = re.compile(r"^Grade:[^\S\n]*(.+)$", re.IGNORECASE | re.MULTILINE)
_RATIONALE_RE = re.compile(r"^Rationale:\s*(.*)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
```

### Coupling test — three token assertions + round-trip

`tests/test_judge_grade.py::TestCouplingWithEngine` (lines 460–574):

```python
# Token assertions: the three keywords the parser keys on must appear in the prompt
def test_judge_prompt_contains_lane_marker_token(self):
    from fleet_engine.engine import _judge_prompt
    prompt = _judge_prompt(self._judge_cfg(), "test task", self._make_successes())
    self.assertIn("=== LANE:", prompt)

def test_judge_prompt_contains_grade_token(self):
    ...
    self.assertIn("Grade:", prompt)

def test_judge_prompt_contains_rationale_token(self):
    ...
    self.assertIn("Rationale:", prompt)

# Round-trip: canonical format → parse_grades → full structured extraction
def test_canonical_format_response_round_trips_correctly(self):
    response = (
        "=== LANE: web ===\nGrade: PASS\nRationale: Strong citations.\n\n"
        "=== LANE: social ===\nGrade: B+\nRationale: Minor gaps.\n"
    )
    result = parse_grades(response, [("web", "web/model"), ("social", "grok")])
    self.assertTrue(result.parsed_ok)
    self.assertEqual(len(result.entries), 2)
    self.assertEqual(result.ungraded, [])
```

### End-to-end smoke — run_fleet → render + manifest

`tests/test_capture.py::TestJudgeEndToEnd` (lines 1621–1700):

```python
_JUDGE_RESPONSE = (
    "=== LANE: web ===\nGrade: A\nRationale: Strong grounding.\n\n"
    "=== LANE: social ===\nGrade: B\nRationale: Adequate social coverage."
)

def _run(self):
    cfg = _build_cfg()
    client = _FakeClient({
        "web": ("ok", "web-specialist-output"),
        "social": ("ok", "social-specialist-output"),
        "judge": ("ok", self._JUDGE_RESPONSE),
    })
    return cfg, run_fleet(cfg, "smoke task", client)

def test_e2e_manifest_grades_fully_structured(self):
    cfg, result = self._run()
    save_run(cfg, result, self.run_dir)
    manifest = json.load(open(self.run_dir / "manifest.json"))
    self.assertEqual(len(manifest["grades"]), 2)
    self.assertEqual(manifest["ungraded"], [])
    self.assertEqual({g["role"] for g in manifest["grades"]}, {"web", "social"})
```

The smoke verifies the full wiring: `run_fleet` (engine) → `parse_grades` (caller parser) → `render_result` (render) → `save_run` (capture) → on-disk `manifest.json`. A per-unit test for any one of those four cannot catch an argument-passing break at the seams between them.
