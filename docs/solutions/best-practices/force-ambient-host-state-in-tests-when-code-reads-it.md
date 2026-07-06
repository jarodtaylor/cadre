---
title: "Force ambient host state in tests when production code reads or writes it"
date: 2026-07-06
last_updated: 2026-07-06
category: best-practices
module: "cadre test suite (tests/__init__.py CADRE_PALETTE pin, read direction, from #62; the two TestSkillDocFlag CADRE_APPROVAL_PATH pins, write direction, from #61 U5); generalizes to any suite whose product code reads OR writes env vars, dotfiles, tokens, or host config"
problem_type: best_practice
component: testing
severity: high
applies_when:
  - "A change adds — or a pre-existing path already performs — production code that reads OR writes ambient host state: an env var, a ~/. dotfile, host config, a well-known path, or a minted token/lock file"
  - "The test suite exercises that code path without pinning the read source or write destination to a known, throwaway value"
  - "CI runs in a clean container (no dotfiles, no env, nothing pre-existing) so the suite stays green there regardless of which direction the leak runs"
  - "For a write-direction leak specifically: nothing in the suite asserts the real ambient location was left untouched, so the suite can stay green on every machine — including the one it silently mutates"
  - "A test class already pins ambient state in most of its tests — audit every test that reaches the same read/write path, not just the ones an existing pattern already covers"
tags: [testing, hermeticity, test-isolation, ci, ambient-state, developer-experience, write-isolation]
---

# Force ambient host state in tests when production code reads or writes it

## Context

Cadre's #62 preflight gate added a production **read** of the host palette (`CADRE_PALETTE` env -> `~/.cadre/palette.yaml`). The suite exercised the runners that call it but never pinned that ambient state. It stayed green on CI and on the dev laptop (neither has a `~/.cadre/palette.yaml`), so nothing looked wrong.

But on a **provisioned host** — exactly the dogfood machine where the operator has run `cadre setup` / `cadre verify-palette` and a real palette exists — the suite broke: 68 `test_cli` failures, because the example fleets' placeholder models are off that real palette, so the new gate refused them. The break was invisible to CI (a clean container) and would surface only on the machine the project actually dogfoods on. Caught by the testing lens in code review, reproduced with a poison palette.

The mirror bug showed up one build later, during #61 (palette auto-discovery), and it is the **write**-direction version of the same class. Two `TestSkillDocFlag` tests in `tests/test_cli.py` exercised `run.py --preview --doc <file>` — a doc-only preview that composes a task and, per the preview-bound-approval design (`cadre/approval.py`), **mints** a one-shot approval token to `CADRE_APPROVAL_PATH` (default `~/.cadre/approval`). Both tests patched `ModelClient` and `prepare_run_dir` but never pinned `CADRE_APPROVAL_PATH` — so running the suite on any machine wrote a fresh token to the operator's **real** `~/.cadre/approval`, clobbering whatever real one-shot token happened to be sitting there. Every other test in the same class already pinned it; these two were the ones that fell through.

Unlike the read-direction case, this one produces **no red test anywhere, on any machine — CI included.** Nothing in the suite asserts that `~/.cadre/approval` wasn't touched, so a fresh container has literally nothing to fail on: no pre-existing token to clobber, and no check that one wasn't created. The bug was caught only because a fresh `~/.cadre` directory turned up on a dev laptop that had never had one — an observed side effect, not a failing assertion — then bisected test-by-test to the two culprits (`test_preview_with_doc_shows_path_and_makes_no_model_call`, `test_preview_marks_oversize_doc_as_truncated`).

## Guidance

When a change adds production code that reads **ambient host state**, force that state to a known value **suite-wide**, at the suite root, so the suite is hermetic regardless of the host. Tests that specifically exercise the new read override the pinned value locally (scoped, and restored to the pinned default).

```python
# tests/__init__.py — runs once when the tests package is imported by `unittest discover`
import os
# Pin the palette to a guaranteed-absent path so the #62 preflight gate sees no
# palette (degrade open) regardless of the host's ~/.cadre. Tests that exercise
# the gate set CADRE_PALETTE explicitly via patch.dict.
os.environ["CADRE_PALETTE"] = os.path.join(
    os.path.dirname(__file__), "__nonexistent__", "palette.yaml"
)
```

Two gotchas this run hit:

- **Prove the pin actually loads under the *documented* test command.** `unittest discover -s tests` runs `tests/__init__.py` only because `tests` is a package — verify it empirically with a poison value (`CADRE_PALETTE=<a real off-palette file> python -m unittest discover -s tests` must stay green), don't assume it runs.
- **Don't build the pinned path from `tempfile.gettempdir()` at import time.** It can raise in a constrained sandbox with no usable temp dir and fail the whole suite to *import*. A `__file__`-relative path has no such dependency.

### The write-side mirror

The read-direction fix above pins ambient state your code *reads*, so the suite doesn't accidentally observe the host's real value. The write direction is the mirror obligation: pin the *destination* of anything your code *writes* — a minted token, a seeded file, an updated config — to a throwaway path, exactly like the write-target's own sibling tests already do:

```python
# tests/test_cli.py — TestSkillDocFlag: a --preview --doc composes a task and
# therefore MINTS an approval token (cadre/approval.py). Unpinned, this test
# wrote that token to the real ~/.cadre/approval on whatever host ran it.
env = {"CADRE_APPROVAL_PATH": str(self.tmp / "approval")}
with patch.dict(os.environ, env):
    with patch.object(self.run_mod, "ModelClient", fake_client_cls):
        with patch.object(self.run_mod, "prepare_run_dir", mock_prepare):
            with contextlib.redirect_stdout(buf):
                code = self.run_mod.main(
                    ["--fleet", _EXAMPLE_FLEET, "--preview", "--doc", doc]
                )
```

Two things make the write direction worse than the read direction, and both change what "covered" has to mean:

1. **No test goes red, anywhere, ever — that's the failure mode itself, not a side effect of it.** The read-direction leak eventually fails loudly the moment ambient state is present and differs from what the suite assumed (the provisioned host's 68 failures). The write-direction leak has no such tripwire: nothing downstream asserts on the mutated path, so a fresh CI container, a clean dev laptop, and a fully provisioned host all stay green while the write still lands on whichever one is real. Detection depends on someone happening to notice an artifact that shouldn't exist — here, a `~/.cadre` directory appearing on a machine that never had one.
2. **A single missed test in an otherwise-correct class is enough.** This wasn't a suite that never pinned the write target — every *other* test in `TestSkillDocFlag` already did. The lesson isn't "add the pin once"; it's "audit every test that reaches the write path, every time" — the established pattern already present in the file did not protect the two methods someone forgot to apply it to.

**Prevention, made concrete:** after any change to code that mints, seeds, or writes ambient host state, run the full suite and then check that the real location is untouched — `ls ~/.cadre` (or the equivalent for the state in question) should show nothing the suite didn't put there deliberately, ideally nothing at all on a machine that had nothing before. This repo's fix was verified exactly that way: full suite green, and `~/.cadre` is no longer created by running it.

## Why This Matters

CI's clean-container hermeticity is a false comfort: it hides a non-hermetic read behind a green check, and the break surfaces on the human's provisioned machine — the worst place for a suite to suddenly fail, because it reads as "your machine is broken," not "the tests aren't isolated." Pinning the ambient state at the suite root makes the suite hermetic by construction and protects every current *and future* test that hits the read, not just the ones the author remembered to patch.

The write direction raises the stakes further, because the false comfort has no expiration date. A read-direction leak is eventually betrayed by reality — some machine's ambient state will someday disagree with the suite's assumption, and a test goes red. A write-direction leak has no such reckoning built in: the suite can run for years, on every machine that ever exists, and never once fail because of it — while quietly overwriting real operator state (here, a one-shot approval token) every single time it runs. The harm isn't "wrong output somewhere"; it's silent destruction of state a human or agent was actually relying on, with zero error raised anywhere in the chain. That turns a test-isolation nuisance into an operational trust concern the moment the ambient state in question is something load-bearing, like an approval artifact, rather than merely a cache or a config the code degrades open around.

## When to Apply

- Any change that adds a read of an env var, a `~/.`-dotfile, `/etc` config, or a well-known host path to product code the suite exercises.
- Especially when the read behaves differently present vs absent (a gate, a feature flag, a credential lookup) — the provisioned-host case is exactly what CI can't see.
- Any change that adds — or any pre-existing code path that already performs — a **write** of ambient host state: minting a token, seeding a file, updating a config, writing a lock. This applies even when the write path itself is untouched by the current change, if a new test starts exercising it (a new test for `--doc` composing a task inherits the write-direction obligation of everything that composing calls into).
- When a test class already has an established pin-the-target convention for some of its tests, audit **every** test in the class/file that reaches the same write path — don't assume the pattern's presence elsewhere means every test follows it.

## Examples

**Read-direction tell:** "green everywhere, but only tested where the ambient state happens to be absent." The fix is a suite-root pin, verified with a poison value — not a per-test patch you have to remember to add to every new test that touches the read.

**Write-direction tell:** "green everywhere, full stop — because nothing checks the destination at all." The fix has the same shape (pin the destination to a throwaway path), but the audit is different: for a write, verify by *absence* — run the suite, then confirm the real ambient location wasn't created or modified, on a machine known to start clean.

## Related

- GitHub issue #62 (preflight-refuse) — the read-direction origin of this pattern.
- GitHub issue #61 (palette auto-discovery, U5) — the write-direction mirror; `cadre/approval.py`'s token-minting path is the concrete write this doc's second half is about.
