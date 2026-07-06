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
``None`` and the preflight gate sees no palette (degrade OPEN -> proceed) across
the whole suite, keeping it hermetic regardless of the host. The path is a
subdirectory of THIS package directory that the suite never creates, so the file
cannot exist. It is deliberately ``__file__``-relative rather than under
``tempfile.gettempdir()`` — the latter is resolved at import time and can raise
in a constrained sandbox with no usable temp dir, which would fail the whole
suite to import. Tests that exercise the preflight gate set ``CADRE_PALETTE`` to
a real palette explicitly via ``patch.dict`` (scoped, and restored to this absent
default afterwards).
"""

import os

os.environ["CADRE_PALETTE"] = os.path.join(
    os.path.dirname(__file__), "__nonexistent_hermetic_palette__", "palette.yaml"
)
