"""cadre/install_skill.py — materialize the Hermes cadre-fleet skill (R11).

Caller-layer module (KTD6): all I/O here — never in ``cadre.engine`` /
``cadre.model_client``. ``cadre install-skill`` (``cadre/cli.py``) points a
Hermes skills directory entry at the installed package's skill data
(``cadre.resources.skill_dir()`` — ``SKILL.md`` + the de-hacked ``run.py``,
U6 of docs/plans/2026-07-04-003-feat-package-as-cadre-plan.md), so the skill
survives the repo moving or being deleted (R14) and a same-Python in-place
``cadre`` upgrade (R16 — ``pip install --upgrade`` replaces the site-packages
skill dir in place; a symlink to that path keeps resolving after the swap).

Default mode: an ATOMIC SYMLINK SWAP. ``<skills_dir>/cadre-fleet`` is created
(or replaced) as a symlink to ``cadre.resources.skill_dir()`` via a
create-at-temp-name-then-``os.replace`` idiom — POSIX-atomic, so a concurrent
reader never observes a half-written entry, and a re-run is idempotent (no
partial state). Guards reused, not invented (KTD6):
  - refuse a symlinked ``skills_dir`` itself (mirrors ``provision._seed_files``'s
    directory-symlink refusal) — checked BEFORE ``realpath`` collapses it (see
    the comment at the check for why the order matters).
  - ``expanduser`` before ``realpath`` (docs/solutions/design-patterns/
    expanduser-before-realpath-for-confined-paths.md).
  - parent-directory ownership + mode-bits (``cadre.approval._parent_is_safe``)
    — the directory we write the ``cadre-fleet`` entry into must be owned by
    the caller and not group/other-writable.
  - overwrite disclosure BEFORE the swap (``os.path.lexists`` — catches a
    dangling symlink ``.exists()`` would miss).

Fallback mode (``copy=True``): for a filesystem where symlinking is
unworkable, materializes ``SKILL.md`` + ``run.py`` as real files under
``<skills_dir>/cadre-fleet/`` via ``provision._seed_files``'s
``O_EXCL | O_NOFOLLOW``-per-file idiom — the SAME idempotent, preserve-
existing seeding contract the starter fleets/personas already use. This
regresses R16 (a package upgrade does not refresh an already-copied file —
the operator must clear the stale copy and re-run), so symlink stays the
default; copy is a documented contingency, not auto-selected on a symlink
failure. Not currently wired to the CLI (``install-skill`` has no ``--copy``
flag yet) — call ``install_skill(..., copy=True)`` directly if a host's
filesystem cannot symlink into the skills directory.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

from cadre.approval import _parent_is_safe
from cadre.provision import _seed_files
from cadre.resources import skill_dir

# The two files that make up the shipped skill — the symlink path's source
# ships both (skill_dir() names their containing directory); the copy
# fallback seeds them by this same pair.
_SKILL_FILES = ("SKILL.md", "run.py")


def install_skill(
    skills_dir: str | None = None,
    *,
    env: dict[str, str] | None = None,
    copy: bool = False,
) -> tuple[int, str]:
    """Materialize the ``cadre-fleet`` skill into ``skills_dir`` (R11).

    Resolution precedence for the target directory (mirrors
    ``cli.setup_command``'s ``venv_python`` precedence):
      1. ``skills_dir`` (e.g. ``--skills-dir``), if given.
      2. ``env['HERMES_SKILLS_DIR']`` (``env`` defaults to ``os.environ``).
      3. Neither given: a clear ``(1, message)`` refusal — never crashes.

    Never raises: every ordinary refusal (no target given, a symlinked
    target, an unsafe parent, a symlink-swap OSError) returns ``(1,
    message)`` instead.

    Args:
        skills_dir: Target Hermes skills directory. ``~`` is expanded.
        env: Mapping to resolve ``HERMES_SKILLS_DIR`` from. Defaults to
            ``os.environ``.
        copy: Use the copy-plus-re-run fallback instead of the default
            atomic symlink swap (KTD6). See the module docstring.

    Returns:
        (exit_code, message) — 0 and a confirmation naming the installed
        path on success; 1 and a clear, actionable error otherwise.
    """
    if env is None:
        env = os.environ

    raw = skills_dir if skills_dir else env.get("HERMES_SKILLS_DIR")
    if not raw:
        return (
            1,
            "error: no skills directory given — pass --skills-dir or set "
            "HERMES_SKILLS_DIR.",
        )

    # expanduser FIRST — os.path.realpath does NOT expand `~` (the same trap
    # docs/solutions/design-patterns/expanduser-before-realpath-for-confined-paths.md
    # documents for the persona pool / approval path).
    expanded = os.path.expanduser(raw)

    # Refuse a symlinked target dir — BEFORE realpath. os.path.realpath fully
    # dereferences symlinks (even a dangling one resolves to its nonexistent
    # target, not the link itself), so checking is_symlink() AFTER realpath
    # can never be True and would silently defeat this guard. Mirrors
    # provision.py's _seed_files directory-symlink refusal, checked the same
    # way: on the expanduser'd-only path, before anything is created.
    if Path(expanded).is_symlink():
        return (
            1,
            f"refusing to install into {expanded}: it is a symlink "
            "(possible tampering) — remove it and re-run.",
        )

    old_umask = os.umask(0o077)
    try:
        Path(expanded).mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return 1, f"could not create skills directory {expanded}: {exc}"
    finally:
        os.umask(old_umask)

    # Canonicalize AFTER the symlink refusal + creation, so every subsequent
    # step (the parent-safety stat, the entry path we write) operates on the
    # real directory — mirrors personas.resolve's expanduser-then-realpath
    # ordering for the persona pool root.
    real_dir = os.path.realpath(expanded)

    if not _parent_is_safe(real_dir):
        return (
            1,
            f"skills directory {real_dir!r} is unsafe — it must be owned by you "
            "and not group/other-writable (the entry written there has no other "
            "integrity check). Fix it, then re-run.",
        )

    entry = Path(real_dir) / "cadre-fleet"
    return _install_copy(entry) if copy else _install_symlink(entry)


def _install_symlink(entry: Path) -> tuple[int, str]:
    """Atomic symlink swap: ``entry`` -> ``skill_dir()`` (KTD6 default)."""
    src = skill_dir()

    # Disclose BEFORE the swap. os.path.lexists uses lstat (does not follow),
    # so it catches a dangling symlink `.exists()` would miss.
    if os.path.lexists(entry):
        print(f"[cadre] replacing existing {entry}", file=sys.stderr)

    # A fresh temp name each call; the pid keeps concurrent installers from
    # colliding, and a same-pid re-run's stray leftover (from a prior call
    # that crashed between symlink() and replace()) is cleaned up below
    # before we recreate it — os.symlink fails FileExistsError otherwise.
    tmp = entry.parent / f".cadre-fleet.tmp.{os.getpid()}"
    with contextlib.suppress(OSError):
        tmp.unlink()

    try:
        os.symlink(src, tmp)
        os.replace(tmp, entry)  # POSIX-atomic — no TOCTOU window
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return (
            1,
            f"could not install the skill at {entry}: {exc}\n"
            "If a real directory already exists there (e.g. from a prior "
            "copy-based install), remove it first and re-run.",
        )

    return 0, f"installed cadre-fleet skill -> {entry} (symlink to {src})"


def _install_copy(entry: Path) -> tuple[int, str]:
    """Copy-plus-re-run fallback: materialize the skill files as real copies.

    Reuses provision._seed_files verbatim — the SAME O_EXCL|O_NOFOLLOW,
    preserve-existing, never-raises seeding contract as the starter fleets/
    personas. A package upgrade will NOT refresh an already-copied file (the
    accepted R16 cost of this fallback — see the module docstring);
    re-materializing after an upgrade requires clearing the stale copy first.
    """
    _seed_files(skill_dir(), entry, _SKILL_FILES, lambda name: name)
    return 0, f"installed cadre-fleet skill (copy) -> {entry}"
