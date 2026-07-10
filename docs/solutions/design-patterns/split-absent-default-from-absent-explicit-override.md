---
title: "An absent safety config is opt-out only on the default path — an absent explicit override fails closed"
date: 2026-07-10
category: design-patterns
module: "cadre policy gate (#78: policy.load_policy absence semantics + CADRE_POLICY override); generalizes to any degrade-open gate whose config location can be overridden by env var or parameter"
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A gate degrades OPEN (proceeds permissively) when its config file is absent, as a deliberate opt-out"
  - "The config's location can be overridden — an env var, a CLI flag, a parameter"
  - "The gate resolves the location and classifies absence in separate reads of the same ambient state"
tags: [trust-safety, safety-gate, fail-open, config-loading, env-override, absence-semantics, cross-model-review]
---

# An absent safety config is opt-out only on the default path — an absent explicit override fails closed

## Context

`distinguish-absent-from-invalid-in-a-degrade-open-gate.md` established the first split: a degrade-open gate must not collapse *absent* (opt-out → proceed) with *present-but-invalid* (broken → fail closed). Cadre's policy gate (#78) shipped with that split — and a cross-model review pass showed **absent itself has two shapes** the gate was still collapsing:

- **Default path absent** — the operator never named a policy. This is the opt-out; permissive is correct (zero behavior change for hosts that never adopted the feature).
- **Explicit override absent** — an env var (`CADRE_POLICY`-style) or a passed parameter names a location, and nothing is there. The operator *declared* "policy lives HERE" and pointed at nothing: that is a misconfiguration — a stale wrapper env, a typo'd path, a deleted file — and treating it as opt-out silently ignores the real policy sitting at the default location. Every chokepoint sharing the loader fails open at once.

Two adjacent hazards surfaced in the same pass:

- **Split reads of ambient state.** Resolution ("which path?") and classification ("was this an override?") each read the env var separately; anything changing it between the reads lets an env-named missing file be classified as the absent default → permissive.
- **Unlink-to-absent.** If the config's *parent directory* is foreign-owned or group/other-writable, another local user can delete the file and convert an enforced policy into "absent."

## Guidance

Classify absence **by how the path was named**, not merely by whether the file exists:

1. **Default path absent → permissive** (the opt-out; keep the zero-behavior-change contract).
2. **Explicit override absent → fail closed**, with an error naming the override source (env vs parameter), the missing path, and the remedy. An explicit pointer at nothing is never an opt-out.
3. **Snapshot the ambient state once.** Read the env var one time and derive both the resolved path and the source classification from that single value — never two reads that can disagree.
4. **Check the parent directory before trusting absence** (owner + write bits, mirroring the file's own posture): a loose parent means "absent" may be attacker-chosen, so refuse rather than degrade open. A *nonexistent* parent stays opt-out — a fresh host with no config dir yet is the legitimate default-absent case.

Test-suite consequence: a hermetic suite can no longer point the override at a guaranteed-nonexistent path to force permissiveness — materialize a real, inert (fully-commented) config file in an owner-only temp dir and point the override there instead.

## Why This Matters

The override exists for tests and unusual layouts, which makes it exactly the thing a wrapper script, CI job, or leftover shell export sets and forgets. Under absent-means-permissive, that leftover doesn't error — it silently disables the gate while the operator's real config sits unread. The failure needs no attacker; ordinary environment drift produces it.

## When to Apply

- Any opt-in safety/spend/permission config with a location override and a permissive absent-default.
- The parent-directory check applies whenever "absent" triggers a materially weaker posture.
- Not applicable when absence already fails closed (mandatory config) — there is no permissive branch to protect.

## Examples

```python
if path is not None:
    source, resolved = "param", resolve(path)
else:
    env = os.environ.get("POLICY_ENV_VAR")       # ONE read
    source = "env" if env else None
    resolved = resolve(env or DEFAULT_PATH)
...
except FileNotFoundError:
    if source is None:
        return Policy.permissive()               # default absent = opt-out
    raise PolicyError(f"policy named by {source} does not exist: {resolved} …")
```

## Related

- `distinguish-absent-from-invalid-in-a-degrade-open-gate.md` — the prior split this extends; together: *absent-default* (open) / *absent-override* (closed) / *present-invalid* (closed).
- `validate-and-read-the-same-fd-to-defang-special-files.md` — the same single-observation principle applied to file contents.
- `cross-model-adversarial-review-on-trust-seams.md` — both absence findings came from premise-challenging cross-model passes.
