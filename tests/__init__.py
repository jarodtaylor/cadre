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
``None`` and preflight degrades OPEN (no palette -> proceed) across the whole
suite, keeping it hermetic regardless of the host. The parent directory does not
exist, so the file cannot either. Tests that exercise the preflight gate set
``CADRE_PALETTE`` to a real palette explicitly via ``patch.dict`` (scoped, and
restored to this absent default afterwards).
"""

import os
import tempfile

os.environ["CADRE_PALETTE"] = os.path.join(
    tempfile.gettempdir(), "cadre-hermetic-tests-nonexistent-dir", "palette.yaml"
)
