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
``~/.cadre/policy.yaml``). REVISED for the #85 absence-semantics fold: an absent
file named by an EXPLICIT override (and ``CADRE_POLICY`` is one) now fails
CLOSED (a misconfiguration, not an opt-out — ``cadre.policy.load_policy``), so
we can no longer point ``CADRE_POLICY`` at a guaranteed-absent path (that would
refuse every fleet suite-wide). Instead we materialize a REAL, permissive policy
file — fully-commented, so ``yaml.safe_load`` -> ``None`` -> ``Policy.permissive()``
(blocks nothing, the SAFE ambient, unlike the palette's absent-refuses posture)
— in a session-scoped tempdir and point ``CADRE_POLICY`` there. This still keeps
a provisioned/dogfood host's real policy.yaml from silently narrowing the
suite's fleets, and still never reads or writes the real ``~/.cadre``. Tests
that exercise the policy gate set ``CADRE_POLICY`` (or pass ``policy_path=``)
explicitly via ``patch.dict`` (scoped, restored to this permissive default
after); tests of the absent-DEFAULT opt-out patch ``resolve_policy_path`` to a
controlled path so they never touch the real ``~/.cadre``.
"""

import os
import tempfile

os.environ["CADRE_PALETTE"] = os.path.join(
    os.path.dirname(__file__), "__nonexistent_hermetic_palette__", "palette.yaml"
)

# A present-but-inert policy file (see the #85 note above). mkdtemp yields a
# 0o700 owner-only parent for free (satisfying load_policy's parent-safety
# check); the leaf is written 0o600 via O_EXCL. The dir persists for the process
# lifetime (never cleaned) so every test sees the same permissive ambient.
# Unlike the palette force above (kept __file__-relative to dodge resolving a
# temp dir at import), the policy force MUST materialize a present file — but a
# sandbox with no usable temp dir would already fail every test's own mkdtemp in
# setUp, so this reintroduces no new import fragility.
_hermetic_policy_dir = tempfile.mkdtemp(prefix="cadre-hermetic-policy-")  # 0o700 by mkdtemp
_hermetic_policy_file = os.path.join(_hermetic_policy_dir, "policy.yaml")
with os.fdopen(
    os.open(_hermetic_policy_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
    "w",
    encoding="utf-8",
) as _f:
    _f.write("# cadre test hermetic policy — fully commented, so it loads as permissive.\n")
os.environ["CADRE_POLICY"] = _hermetic_policy_file
