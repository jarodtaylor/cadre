---
title: "Normalize a str-Enum field at the construction boundary"
date: "2026-06-29"
category: design-patterns
module: "fleet_engine (FleetResult.status)"
problem_type: design_pattern
component: service_object
severity: medium
applies_when:
  - "A dataclass or model field holds a str-Enum (or int-Enum)"
  - "That field is compared with identity (`is` / `is not`) anywhere — properties, helpers, callers"
  - "The field can be constructed from a raw/serialized value (manifest, JSON/YAML, CLI, DB, wire)"
tags:
  - "str-enum"
  - "identity-vs-equality"
  - "enum-coercion"
  - "normalize-at-boundary"
  - "dataclass-post-init"
  - "cross-model-review"
related_components:
  - "testing_framework"
---

# Normalize a str-Enum field at the construction boundary

## Context

Cadre's `FleetResult` dataclass carries an explicit `FleetStatus(str, Enum)` tri-state (`SUCCESS` / `DEGRADED` / `FAILED`) introduced in issue #22. Because `FleetStatus` subclasses `str`, the enum members compare equal to their string equivalents by value (`FleetStatus.FAILED == "failed"` is `True`). However, the derived reads in the codebase — `ok` (a `@property` returning `status is FleetStatus.SUCCESS`) and `has_usable_output()` (returning `status is not FleetStatus.FAILED`) — use identity comparison (`is`), not equality (`==`).

This creates a latent footgun: if a `FleetResult` is ever constructed with `status` holding a raw string rather than the enum member — for example, a result reconstructed from the manifest's serialized `"success"/"degraded"/"failed"` form — both derived reads silently misfire. At the time of the refactor, nothing in production reads the manifest back into a `FleetResult`, so the bug was latent, not live. But the manifest explicitly documents `status` as a serialized bare string, making any future consumer the reachable trigger.

The footgun survived an advisor pass, an 8-persona `ce-code-review` run, and a dedicated `cadre-invariant-reviewer` — all Claude-based. It was caught only by a cross-model adversarial review (`/codex:adversarial-review`, Codex) after the PR was otherwise ready to ship.

## Guidance

**Never compare a `str`-Enum field with `is` unless you can guarantee the field always holds an enum member, not a raw string.** The safe options are:

**(a) Use `==`/`!=` for comparisons.** Because `FleetStatus` subclasses `str`, value equality works correctly whether the field holds the enum member or a raw string. This is fine for one-off comparisons in non-load-bearing code.

**(b, preferred) Normalize the field to the enum at the construction boundary.** This is the approach Cadre uses. Define `__post_init__` on the dataclass to coerce the field to the enum member on every construction:

```python
def __post_init__(self) -> None:
    # Normalize status to the FleetStatus enum so the identity checks (`is`)
    # in ok / has_usable_output() / render / capture stay correct even when
    # status arrives as a raw string — e.g. the manifest's serialized
    # "success"/"degraded"/"failed" form round-tripped back into a result.
    # Idempotent on enum members; raises ValueError on an unknown value.
    self.status = FleetStatus(self.status)
```

This coercion has three useful properties:

- **Idempotent on enum members.** `FleetStatus(FleetStatus.FAILED)` returns the same singleton; all existing tests that pass enum members directly are unchanged.
- **Coerces raw strings.** `FleetStatus("failed")` returns `FleetStatus.FAILED`; a manifest-reconstructed result is normalized before any `is` check runs.
- **Fail-fast on garbage.** `FleetStatus("bogus")` raises `ValueError` immediately at construction; invalid state never reaches downstream code.

One boundary coercion hardens every `is` check at once — `ok`, `has_usable_output()`, and every identity comparison in `render.py` and `capture.py` — without touching those call sites individually.

This generalizes to any `str`-Enum (or `int`-Enum) field on a dataclass or model that (1) is compared with `is`, or (2) can be constructed from a serialized or raw value (JSON, YAML, CLI args, database reads, wire format, etc.).

## Why This Matters

**The silent-misread failure mode is asymmetric and easy to miss in tests.** On a string-valued `status`:

- `"failed" is not FleetStatus.FAILED` evaluates `True` → `has_usable_output()` wrongly returns `True` (a fully-failed run claims usable output).
- `"success" is FleetStatus.SUCCESS` evaluates `False` → `ok` wrongly returns `False` (a successful run reports failure).
- `"degraded"` happens to read correctly on both. A regression test that only checks the `"degraded"` case passes and hides both bugs.

Python emits `SyntaxWarning: "is" with a literal` when you write `status is "failed"` literally in source, but the warning doesn't fire when a variable merely happens to hold a string at runtime — so the trap is invisible at review time.

**The manifest round-trip makes this reachable in principle.** The Cadre manifest serializes `status` as a bare string (`result.status.value`). Any future consumer reconstructing a `FleetResult` from the manifest hits the bug on every non-degraded result.

**Normalize at the boundary — one fix hardens many call sites.** The `__post_init__` coercion matches Cadre's existing posture: `config.py` normalizes YAML `null` → `[]` for toolsets so downstream code never branches on the looser form. Fixing the construction boundary is strictly better than auditing every `is` check in render, capture, and tests individually.

**The cross-model review earns its keep on foundational code.** This bug survived a full same-model review cycle — advisor (Claude reviewing Claude), 8 `ce-code-review` personas (all Claude), and a repo-invariant reviewer (also Claude). The structural reason: the misread only fires on a string-valued field, which none of the Claude-based reviewers constructed in their mental model — they read the `is` checks as correct given the enum-member assumption. Codex, reasoning from a different training distribution, surfaced the round-trip construction path and caught the trap. For foundational or load-bearing code (type semantics, result contracts, serialization round-trips), same-model review alone is not sufficient; the cross-model pass is the discriminating check.

## When to Apply

Apply this pattern whenever **all three** of the following are true:

1. A dataclass or model field holds a `str` (or `int`) Enum.
2. The field is compared using identity (`is` / `is not`) anywhere — including `@property` implementations, serialization helpers, and callers.
3. The field can be constructed from a raw value — serialized manifest, JSON/YAML config, CLI argument, API response, database read, or any source outside the enum's own constructor.

In Cadre specifically:

- **`FleetStatus` on `FleetResult`** — addressed by PR #32 (this learning).
- **`ConvergenceMode` (deferred fast-follow)** — a `str`-Enum mirroring `FleetStatus` for the `convergence` field is a listed fast-follow. When it lands, apply the same `__post_init__` coercion on any dataclass that carries it. Don't wait for a round-trip bug to surface first.

More broadly: if you are writing a `@property` that uses `is` to compare an enum field, pause and ask whether any constructor path could supply a raw string. If the answer is "possibly, via serialization," add the coercion.

## Examples

### The broken reads (before normalization)

When `status` holds a raw string — e.g. a `FleetResult` reconstructed from the manifest's serialized form — the identity checks misfire. Tracing the actual comparisons:

```python
"failed"   is not FleetStatus.FAILED   # True  → has_usable_output() returns True   (WRONG)
"success"  is     FleetStatus.SUCCESS  # False → ok returns False                   (WRONG)
"degraded" is not FleetStatus.FAILED   # True  → has_usable_output() returns True    (correct, by coincidence)
```

Both reads break, on *different* values — and `"degraded"` reads correctly on both, so a test that only parametrizes `"degraded"` sees no failure. (`==` would be correct in every case; `is` is the trap.)

### The fix (`__post_init__` normalization)

```python
@dataclass
class FleetResult:
    status: FleetStatus
    # ... other fields ...

    def __post_init__(self) -> None:
        self.status = FleetStatus(self.status)   # idempotent on members; coerces strings; ValueError on garbage

    @property
    def ok(self) -> bool:
        return self.status is FleetStatus.SUCCESS

    def has_usable_output(self) -> bool:
        return self.status is not FleetStatus.FAILED
```

After normalization, `FleetResult(status="failed")` and `FleetResult(status=FleetStatus.FAILED)` are equivalent — both hold the `FleetStatus.FAILED` singleton, so every `is` check is safe.

### The regression test — parametrize over all three values

This repo uses stdlib `unittest` (no `pytest`/`parameterized`), so the parametrization is a `subTest` loop:

```python
def test_status_string_is_coerced_to_enum(self):
    for value in ("success", "degraded", "failed"):
        with self.subTest(value=value):
            from_str  = FleetResult(fleet="f", task="t", specialists=[], status=value)
            from_enum = FleetResult(fleet="f", task="t", specialists=[], status=FleetStatus(value))
            self.assertIs(from_str.status, FleetStatus(value))
            self.assertEqual(from_str.ok, from_enum.ok)
            self.assertEqual(from_str.has_usable_output(), from_enum.has_usable_output())

def test_status_invalid_string_raises(self):
    with self.assertRaises(ValueError):
        FleetResult(fleet="f", task="t", specialists=[], status="bogus")
```

The all-three-values shape is load-bearing: a single-value test on `"degraded"` passes even against the unfixed code, and `"success"` or `"failed"` alone catches one broken read while masking the other. Only all three together catch both misreads.

## Related

- [[enumerate-consumers-when-a-new-value-aliases-a-load-bearing-state]] — closest sibling, same `FleetResult` region. Complementary failure mode: that doc is about a *new value aliasing* an existing state (`ok=True, synthesis=None`); this one is about *identity-vs-equality* on the enum field. Read together when touching `FleetResult` state.
- [[populate-derived-field-eagerly-not-only-in-resolver]] — same "normalize eagerly at the boundary, not lazily downstream" principle, applied to a different field.
- [[empty-toolset-collapsed-to-all-tools]] — sibling instance of "a Python coercion at a boundary silently produces the wrong semantic" (`[] or None` collapsing to fail-open).
- [[fail-closed-allowlist-for-capability-gates]] — same fail-fast-on-unrecognized-value posture that `FleetStatus(...)`'s `ValueError` provides.
- [[coupling-test-for-cross-module-format-contracts]] — same PR lineage; the `_derive_status` shim-fidelity coupling test in this work follows that pattern.
- [[validate-and-read-the-same-fd-to-defang-special-files]] and [[lens-decomposition-vs-model-diversity-in-review-fleets]] — further evidence for the cross-model-review-catches-what-same-model-misses thesis (both, like this fix, were Codex cross-model catches or diversity data points).
- Issues: **#22** (the result-status enum — origin of this learning; closed by **PR #32**, squash `c154ce1`). **#33** (a deferred `capture` correctness bug filed in the same review pass). The `ConvergenceMode` type-guard is an un-filed fast-follow that will need this same coercion.
