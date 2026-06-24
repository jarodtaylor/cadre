"""Caller-layer persona resolver.

Caller-layer only: imported by ``fleet_engine/cli.py`` and
``skills/cadre-fleet/run.py``. NEVER imported by ``engine.py`` or
``model_client.py`` — the engine receives only the resolved
``effective_instruction`` string and gains no file I/O or fleet-domain
knowledge (KTD1).

Resolution populates ``spec.effective_instruction`` in-place for every
specialist before the engine and any preview/lint render runs (KTD8).
Focus-only fleets resolve trivially with no file I/O (R4).

Confinement (R10 / KTD4 defense-in-depth):
  realpath-confine to pool root → open original candidate with O_NOFOLLOW
  (so a symlink planted after the realpath check is refused at open time).
  Opening the original ``candidate`` path with O_NOFOLLOW — not the
  canonicalized ``real`` — is what closes the TOCTOU window.
"""

from __future__ import annotations

import os

from fleet_engine.config import ConfigError, FleetConfig


def resolve(config: FleetConfig, pool_dir: str | os.PathLike) -> FleetConfig:
    """Populate ``spec.effective_instruction`` for every specialist.

    For focus-only specs: sets ``effective_instruction = focus``, no I/O.
    For persona specs: reads the named file from ``pool_dir`` and sets
    ``effective_instruction`` to its raw text (markdown preserved — the
    preview/render layer sanitizes; we do not).

    ``pool_dir`` is only required to exist when at least one specialist
    references a persona (R4). A focus-only fleet resolves cleanly even
    when no personas pool exists on the host.

    Accumulates every error and raises a single ``ConfigError`` at the end
    (mirrors the config loader pattern). Never tracebacks — every failure
    mode produces a clear error string.

    Args:
        config: Loaded and validated ``FleetConfig``.
        pool_dir: Directory containing ``<name>.md`` persona files.

    Returns:
        ``config`` (the same object, mutated in-place — returned for
        the one-line ``config = resolve(config, pool_dir)`` calling
        convention).

    Raises:
        ConfigError: One or more specialists could not be resolved.
    """
    errors: list[str] = []

    # Pass 1: focus-only specs are set unconditionally, no I/O.
    # Collect persona specs for the second pass.
    persona_specs = []
    for spec in config.specialists:
        if not spec.persona:
            # Focus-only specialist (persona XOR focus validated at parse time,
            # so focus is guaranteed non-empty here).
            spec.effective_instruction = spec.focus
        else:
            persona_specs.append(spec)

    # If no specialist references a persona, we are done — zero I/O (R4).
    if not persona_specs:
        return config

    # At least one persona is referenced: the pool must exist.
    # os.path.realpath does NOT raise on a missing path — it returns the
    # canonicalized nonexistent path — so we must check explicitly.
    pool_real = os.path.realpath(pool_dir)
    if not os.path.isdir(pool_real):
        errors.append(
            f"persona pool directory not found or not a directory: {pool_dir!r}"
        )
        # Cannot proceed — raise now rather than accumulating per-specialist
        # errors that all stem from the same missing root.
        raise ConfigError(errors)

    # Pass 2: resolve each persona spec.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for spec in persona_specs:
        name = spec.persona  # config-validated: [A-Za-z0-9][A-Za-z0-9._-]*; no separators

        # Build candidate path inside the canonical pool root.
        candidate = os.path.join(pool_real, name + ".md")

        # Realpath-confine: canonicalize the target and assert it stays inside
        # the pool.  os.path.commonpath raises ValueError on mixed absolute/
        # relative paths or (on Windows) different drives — treat as out-of-pool.
        real = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath([real, pool_real]) == pool_real
        except ValueError:
            inside = False

        if not inside:
            errors.append(
                f"persona {name!r}: resolved path is outside the pool directory "
                f"(possible path traversal) — not loaded"
            )
            continue

        # Open the ORIGINAL candidate path (not ``real``) with O_NOFOLLOW.
        # A symlink swapped in after the realpath check is refused at open time
        # (ELOOP on POSIX), closing the TOCTOU window.  Wrapping open + read
        # in ONE try/except catches O_NOFOLLOW rejection (ELOOP), missing files
        # (ENOENT), permission errors (EACCES), directory targets (EISDIR),
        # I/O errors, and non-UTF-8 content — all produce a clear error, never
        # a traceback (R8 / KTD5 / KTD6).
        try:
            fd = os.open(candidate, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(
                f"persona {name!r}: could not read persona file: {exc}"
            )
            continue

        # Empty or whitespace-only persona is a loud error (R5 / KTD5).
        if not text.strip():
            errors.append(
                f"persona {name!r}: persona file is empty or contains only whitespace "
                "— a valid persona must contain at least one non-whitespace character"
            )
            continue

        # Store raw text; the preview/render layer sanitizes at render time (KTD6).
        spec.effective_instruction = text

    if errors:
        raise ConfigError(errors)

    return config
