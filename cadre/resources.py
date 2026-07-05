"""cadre/resources.py — resolve cadre's packaged data (``cadre/data/``).

Starter fleets, personas, and the palette example ship as package data under
``cadre/data/`` and must resolve identically in dev (running from the repo
root, no install) and once installed (a wheel extracted into site-packages).
``importlib.resources.files("cadre")`` is the single source for both: for any
regular on-disk package loaded via the standard ``SourceFileLoader`` — which
is what ``cadre`` is in *both* contexts, since pip always extracts a wheel
into loose files rather than importing it as a zipapp — CPython's own
``importlib.resources.readers.FileReader.files()`` returns
``pathlib.Path(loader.path).parent``: a real filesystem ``Path``, not merely
a duck-typed ``Traversable`` (verified against the 3.11 stdlib source, not
just the docs). That is what lets ``_seed_files`` do ``src_dir / name`` and
``.read_text()`` on the result. This assumption would only break under a
zipimport-style install (e.g. ``zip_safe`` execution straight from a wheel
zip), which cadre does not use and does not support. No dev/installed
branch — this module is the one place that resolves the data root.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def data_dir() -> Path:
    """Root of the packaged data directory (``cadre/data/``)."""
    return files("cadre") / "data"


def fleets_dir() -> Path:
    """Directory containing the starter fleet ``.example.yaml`` files + README.md."""
    return data_dir() / "fleets"


def personas_dir() -> Path:
    """Directory containing the starter persona ``.md`` files."""
    return data_dir() / "personas"


def palette_example_path() -> Path:
    """Path to ``palette.example.yaml`` — a sibling of ``fleets/``/``personas/`` (KTD3)."""
    return data_dir() / "palette.example.yaml"


def skill_dir() -> Path:
    """Directory containing the shipped Hermes skill (``SKILL.md`` + ``run.py``).

    ``cadre install-skill`` (``cadre/install_skill.py``) symlinks a Hermes
    skills directory entry to this path (R15/R16) — the same dev/installed
    parity every other ``cadre/data/`` reader gets from this module.
    """
    return data_dir() / "skill"
