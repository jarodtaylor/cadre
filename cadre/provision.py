"""cadre/provision.py — provision ``~/.cadre`` from the installed cadre package.

Caller-layer module (KTD4): all I/O — scaffolding directories, seeding starter
data, writing config, and verifying an interpreter can import ``cadre`` — lives
here, never in ``cadre.engine``/``cadre.model_client``. Moved unchanged from
``scripts/resolve_venv.py`` during the U4 packaging pass
(docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md), which already
sourced starter data via ``cadre.resources`` (package data under
``cadre/data/``) rather than a repo-relative path — so the move needed no
signature changes to the seed functions.

Exported functions:
  ensure_cadre_dirs(home)                 — owner-only dir scaffolding
  write_config(python_path, config_path)  — owner-only config writer (C2 contract)
  seed_starter_fleets(cadre_home)         — idempotent fleet seeding, never raises
  seed_personas(cadre_home)               — idempotent persona seeding, never raises
  seed_palette_candidates(cadre_home)     — idempotent palette-candidates seeding, never raises
  verify_importable(python_path)          — KTD11: subprocess check, `import cadre` under python_path

``cadre setup`` (cadre/cli.py) orchestrates these: verify_importable() gates
(fail-closed, before any write) the rest — ensure_cadre_dirs() then the three
seed_* functions then write_config().
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from cadre.resources import fleets_dir, palette_example_path, personas_dir

# Explicit allowlist — NEVER glob fleets/*.yaml: palette.example.yaml lives there
# and is not a fleet (it would break FleetConfig.load if seeded as a fleet).
_STARTER_FLEETS = (
    "research-swarm.example.yaml",
    "code-review.example.yaml",
    "doc-review.example.yaml",
    "review-scoring.example.yaml",
    "research-brief.example.yaml",
    "debate.example.yaml",
    "critique-revise.example.yaml",
)

# Explicit allowlist of persona files to seed. Personas have no .example infix —
# names copy unchanged (coherence-reviewer.md → coherence-reviewer.md).
_STARTER_PERSONAS = (
    "coherence-reviewer.md",
    "feasibility-reviewer.md",
    "scope-guardian-reviewer.md",
    "product-reviewer.md",
    "adversarial-document-reviewer.md",
)

# KTD11: bound the subprocess importability check so a hung/misbehaving
# recorded interpreter can't wedge `cadre setup` indefinitely.
_IMPORT_CHECK_TIMEOUT_S = 30.0


def _seed_files(
    src_dir: Path,
    dest_dir: Path,
    names: tuple[str, ...],
    dest_name_fn: Callable[[str], str],
) -> None:
    """Seed a set of files from src_dir into dest_dir, owner-only and idempotent.

    Shared implementation for seed_starter_fleets, seed_personas, and
    seed_palette_candidates. Callers differ only in their source dir, dest dir,
    file list, and the dest-name transform (fleet strips ``.example`` infix;
    personas and the palette-candidates source copy/rename as given).

    Idempotent: existing destination files are NOT overwritten (operator edits preserved).
    Owner-only: dest_dir is created 0o700 and each file written 0o600 under umask 0o077.
    Never raises: all I/O errors are caught, warned to stderr, and skipped.
    Never writes to stdout: all messages go to sys.stderr.

    Args:
        src_dir: Directory containing the source files.
        dest_dir: Destination directory (created 0o700 if absent).
        names: Ordered tuple of source filenames to seed.
        dest_name_fn: Maps a source filename to its destination filename.
    """
    old_umask = os.umask(0o077)
    try:
        # Directory-level symlink guard (#5, R7): the per-file O_NOFOLLOW below
        # refuses a symlinked *file*, but mkdir(exist_ok=True) silently succeeds
        # through a symlinked *directory* — an attacker-planted ~/.cadre/fleets ->
        # /elsewhere would then receive our seeds. Reject a symlinked dest_dir
        # before writing (lstat does not follow the link). Degrade like the
        # create-failure branch: warn and skip, never crash the install.
        if dest_dir.is_symlink():
            print(
                f"[cadre] warning: refusing to seed into {dest_dir}: it is a symlink "
                "(possible tampering); remove it and re-run to seed.",
                file=sys.stderr,
            )
            return
        try:
            dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[cadre] warning: could not create {dest_dir}: {exc}", file=sys.stderr)
            return

        for name in names:
            dest_name = dest_name_fn(name)
            src = src_dir / name
            dest = dest_dir / dest_name
            try:
                content = src.read_text(encoding="utf-8")
                # Atomic preserve-existing: O_EXCL fails if the destination already exists,
                # closing the TOCTOU window a separate exists()-then-open had (which could
                # truncate an operator-edited file); O_NOFOLLOW refuses to follow a symlink
                # planted at the destination. Mirrors capture.prepare_run_dir's atomic
                # reservation idiom.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(str(dest), flags, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                dest.chmod(0o600)
                print(f"[cadre] seeded {dest_name}", file=sys.stderr)
            except FileExistsError:
                # Destination already exists (a regular file or a symlink) — preserve it.
                print(f"[cadre] {dest_name}: exists — preserved", file=sys.stderr)
            except (OSError, UnicodeDecodeError) as exc:
                # UnicodeDecodeError (a non-UTF-8 source) is not an OSError subclass; catch it
                # too so the never-raises contract holds. ELOOP from O_NOFOLLOW on a symlink
                # also lands here — warned and skipped, never followed.
                # Best-effort remove a partially-written destination so the next install
                # does not preserve a corrupt file (the write failed mid-stream).
                try:
                    os.unlink(dest)
                except OSError:
                    pass
                print(f"[cadre] warning: could not seed {dest_name}: {exc}", file=sys.stderr)
                continue
    finally:
        os.umask(old_umask)


def seed_starter_fleets(cadre_home: Path) -> None:
    """Copy starter fleets from the installed cadre package to <cadre_home>/fleets/.

    Source data lives at ``cadre/data/fleets/`` (package data, resolved via
    ``cadre.resources.fleets_dir()`` — the same helper in dev/repo-root and
    installed/wheel contexts). Each source file has a ``.example`` infix that
    is stripped on copy (e.g. ``research-swarm.example.yaml`` → ``research-swarm.yaml``).

    Idempotent: existing destination files are NOT overwritten (operator edits preserved).
    Owner-only: the fleets dir is created 0o700 and each file written 0o600 under umask 0o077.
    Never raises: all I/O errors are caught, warned to stderr, and skipped.
    Never writes to stdout: all messages go to sys.stderr.

    Args:
        cadre_home: Resolved ~/.cadre home directory (returned by ensure_cadre_dirs).
    """
    _seed_files(
        src_dir=fleets_dir(),
        dest_dir=cadre_home / "fleets",
        names=_STARTER_FLEETS,
        dest_name_fn=lambda name: name.replace(".example.yaml", ".yaml"),
    )


def seed_personas(cadre_home: Path) -> None:
    """Copy persona files from the installed cadre package to <cadre_home>/personas/.

    Source data lives at ``cadre/data/personas/`` (package data, resolved via
    ``cadre.resources.personas_dir()``). Personas have no ``.example`` infix —
    names copy unchanged (e.g. ``coherence-reviewer.md`` → ``coherence-reviewer.md``).

    Idempotent: existing destination files are NOT overwritten (operator edits preserved).
    Owner-only: the personas dir is created 0o700 and each file written 0o600 under umask 0o077.
    Never raises: all I/O errors are caught, warned to stderr, and skipped.
    Never writes to stdout: all messages go to sys.stderr.

    Args:
        cadre_home: Resolved ~/.cadre home directory (returned by ensure_cadre_dirs).
    """
    _seed_files(
        src_dir=personas_dir(),
        dest_dir=cadre_home / "personas",
        names=_STARTER_PERSONAS,
        dest_name_fn=lambda name: name,
    )


def seed_palette_candidates(cadre_home: Path) -> None:
    """Seed <cadre_home>/palette-candidates.yaml from the packaged palette example.

    Source: ``cadre/data/palette.example.yaml`` (a sibling of fleets/personas,
    KTD3 — resolved via ``cadre.resources.palette_example_path()``).
    Destination: directly under cadre_home (NOT a subdirectory) — replaces
    install.sh's old ``cp fleets/palette.example.yaml ~/.cadre/palette-candidates.yaml``
    line, broken since U3 moved the file out of the repo-root fleets/ dir.

    Reuses _seed_files with cadre_home itself as dest_dir, so the same
    directory-symlink refusal that guards ~/.cadre/fleets and ~/.cadre/personas
    also covers a pre-planted ~/.cadre symlink for this seed.

    Idempotent: an existing palette-candidates.yaml (operator-edited, or seeded
    by a prior run) is NOT overwritten.
    Owner-only: written 0o600 under umask 0o077.
    Never raises: all I/O errors are caught, warned to stderr, and skipped.
    Never writes to stdout: all messages go to sys.stderr.

    Args:
        cadre_home: Resolved ~/.cadre home directory (returned by ensure_cadre_dirs).
    """
    example = palette_example_path()
    _seed_files(
        src_dir=example.parent,
        dest_dir=cadre_home,
        names=(example.name,),
        dest_name_fn=lambda _name: "palette-candidates.yaml",
    )


def ensure_cadre_dirs(home: str = "~/.cadre") -> Path:
    """Create <home> and <home>/fleets owner-only (0o700) under umask(0o077).

    Mirrors capture.prepare_run_dir's umask discipline so every created
    directory component is owner-only from creation — no momentary 0o755.
    Idempotent: safe to call multiple times.

    Args:
        home: Root of the ~/.cadre control dir. Defaults to "~/.cadre".

    Returns:
        The resolved home Path (expanduser'd).
    """
    home_path = Path(home).expanduser()
    old_umask = os.umask(0o077)
    try:
        home_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (home_path / "fleets").mkdir(mode=0o700, parents=True, exist_ok=True)
        (home_path / "personas").mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    return home_path


def write_config(
    python_path: str,
    config_path: str = "~/.cadre/config",
) -> None:
    """Write CADRE_HERMES_PYTHON=<python_path> to config_path, owner-only 0o600.

    Writes a single line: ``CADRE_HERMES_PYTHON=<python_path>\\n``

    Uses the capture._write idiom (os.open with O_CREAT/O_TRUNC at mode 0o600,
    then explicit chmod) so the file is owner-only at creation — never the
    momentary 0o644 a write-then-chmod leaves under a default umask.

    Idempotent: overwrites on re-run (single-key file, O_TRUNC).

    The locked config contract (C2): the skill reads this via:
        PYBIN="${CADRE_HERMES_PYTHON:-$(grep -E '^CADRE_HERMES_PYTHON=' ~/.cadre/config | cut -d= -f2-)}"
    so the line MUST match exactly:
        CADRE_HERMES_PYTHON=<path>\\n
    No quotes around the path, no trailing spaces.

    Args:
        python_path: The resolved Hermes venv Python path to record.
        config_path: Destination config file path. Defaults to "~/.cadre/config".
    """
    path = Path(config_path).expanduser()
    content = f"CADRE_HERMES_PYTHON={python_path}\n"

    old_umask = os.umask(0o077)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        path.chmod(0o600)
    finally:
        os.umask(old_umask)


def verify_importable(python_path: str, *, timeout: float = _IMPORT_CHECK_TIMEOUT_S) -> bool:
    """Return True iff ``import cadre`` resolves when python_path is run fresh.

    KTD11: ``cadre setup`` must not record a Hermes Python that cannot import
    cadre — e.g. the pipx-standalone-CLI case, where the operator points
    ``--venv-python`` at a Hermes venv that hasn't had ``pip install cadre``
    run against it yet. A subprocess is required (not a sys.modules /
    importlib introspection): python_path may name a wholly different
    interpreter/venv than the one currently running ``cadre setup``.

    The child inherits the caller's cwd (not forced to a neutral directory):
    when cadre truly is pip-installed into python_path's site-packages, the
    result is correct regardless of cwd; the one case this doesn't catch — an
    unrelated interpreter "rescued" by a cadre checkout sitting in the caller's
    cwd — only arises when an operator points --venv-python at a foreign venv
    while running from inside a cadre repo clone, and is the same repo-root
    convenience R9 already embraces for dev (running the test suite/CLI from
    the repo root needs no install). Real target hosts (KTD2's load-bearing
    pip-into-Hermes-venv deployment) have no repo clone present at all.

    Never raises: a missing/non-executable python_path, a crash, or a timeout
    all resolve to False (fail closed) — same posture as the seeding helpers.

    Args:
        python_path: Path to the Python interpreter to check (already expanduser'd
                     by the caller — this function does no path resolution).
        timeout: Seconds to wait before treating the check as failed.

    Returns:
        True if the subprocess exits 0 (import succeeded); False otherwise,
        including on any failure to even launch python_path.
    """
    try:
        result = subprocess.run(
            [python_path, "-c", "import cadre"],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
