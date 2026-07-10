"""Test package for cadre.

Hermeticity guard (added with the #62 preflight gate, PR for #62/#70): both
``cadre run`` (``cadre/cli.py``) and the agent runner
(``cadre/data/skill/run.py``) now call ``preflight.preflight_refusal(cfg)``,
which resolves the palette via ``CADRE_PALETTE`` -> ``~/.cadre/palette.yaml``.
Without isolation, a suite run on a host that has a provisioned
``~/.cadre/palette.yaml`` (or an exported ``CADRE_PALETTE``) would refuse every
example fleet whose placeholder models are not on that palette -- breaking
dozens of real-run tests on a provisioned/dogfood host while CI (a fresh
container with no ``~/.cadre``) stays green.

Force ``CADRE_PALETTE`` at a guaranteed-absent path so ``load_palette`` returns
``None`` and the preflight gate sees the same "no palette" state on every
machine (since the #61 flip that state REFUSES a run — every fleet-run test
therefore brings its own explicit palette via
``tests/palette_fixtures.matching_palette``; this pin keeps the ambient
default hermetic rather than open). The path is a
subdirectory of THIS package directory that the suite never creates, so the file
cannot exist. It is deliberately ``__file__``-relative rather than under
``tempfile.gettempdir()`` — the latter is resolved at import time and can raise
in a constrained sandbox with no usable temp dir, which would fail the whole
suite to import. Tests that exercise the preflight gate set ``CADRE_PALETTE`` to
a real palette explicitly via ``patch.dict`` (scoped, and restored to this absent
default afterwards).

Same guard, same reasoning, for the #78 policy gate: ``cadre.discover.main()``,
``cadre.verify_palette.main()``, and ``preflight.preflight_refusal`` all resolve
their policy file via ``cadre.policy.default_policy_path()`` (``CADRE_POLICY`` ->
``~/.cadre/policy.yaml``). An absent policy file loads as ``Policy.permissive()``
(blocks nothing) — the SAFE default, unlike the palette's absent-refuses
posture — so forcing it absent here does not itself require every test to
bring its own fixture; it only prevents a provisioned/dogfood host's real
policy.yaml from silently narrowing what the suite's fleets can reference.
Tests that exercise the policy gate set ``CADRE_POLICY`` to a real policy file
explicitly via ``patch.dict`` (scoped, restored to this absent default after).
"""

import os

os.environ["CADRE_PALETTE"] = os.path.join(
    os.path.dirname(__file__), "__nonexistent_hermetic_palette__", "palette.yaml"
)
os.environ["CADRE_POLICY"] = os.path.join(
    os.path.dirname(__file__), "__nonexistent_hermetic_policy__", "policy.yaml"
)
