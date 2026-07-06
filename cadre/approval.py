"""Preview-bound approval — canonical surface digest (U1) + owner-only token store (U2).

Caller-layer only: intended for ``cadre/data/skill/run.py`` (and ``render.py``
for surfacing the digest inputs). NEVER imported by ``engine.py`` or
``model_client.py`` — approval / trust-boundary logic is not the engine's
concern (KTD5; ``tests/test_personas.py``'s ``TestEngineIsolation`` guards
this in both directions).

U1 — ``surface_digest``: a PURE function binding the full previewed run
surface (the whole resolved ``FleetConfig``, the composed task — which
already carries ``--task`` + ``--doc`` file contents — and the resolved
``HERMES_HOME`` profile string) into one stable sha256 hex digest. Hashing
ONE json structure (never a delimiter-joined concatenation) sidesteps the
whole delimiter-confusion class: the composed task can legally contain a NUL
byte (U+0000 is valid UTF-8 and survives Cadre's ``--doc`` decoder), so no
separator byte is safe, while JSON string-escaping is injective.

U2 — the approval token store: a create-or-replace, symlink-guarded,
owner-only (``~/.cadre/approval``, 0o600) file recording one minted digest.
``write_approval`` mints; ``consume_approval`` atomically reads-and-deletes
(``os.unlink`` BEFORE reading from the still-open fd) so a token authorizes
exactly one run — a racing second consumer gets ENOENT, and a
present-but-undeletable token is refused rather than honored (fail-closed).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cadre.config import FleetConfig


# ---------------------------------------------------------------------------
# U1 — canonical surface digest
# ---------------------------------------------------------------------------


def surface_digest(cfg: FleetConfig, composed_task: str, profile: str) -> str:
    """Return a stable sha256 hex binding the full previewed run surface.

    Binds the WHOLE resolved config (not a curated field list — fail-safe: a
    future execution-affecting field is bound by default), the composed task
    (which already carries --task + --doc file contents), and the resolved
    HERMES_HOME profile string.

    Hashes ONE json structure — NOT a concatenation/join of the three inputs
    with any delimiter (see module docstring for why). ``cfg`` is digested
    exactly as handed in: this function does not call ``personas.resolve()``
    itself, so the caller must resolve personas first if
    ``SpecialistSpec.effective_instruction`` should be bound by its resolved
    content.

    Every ``FleetConfig`` field (and its nested ``SpecialistSpec`` /
    ``SynthesisSpec`` / ``JudgeSpec`` dataclasses) is a JSON-serializable
    str / list[str] / bool / int / None — and none are sets — so
    ``sort_keys=True`` makes the output deterministic across processes with
    no ``PYTHONHASHSEED`` dependence.
    """
    payload = {
        "config": dataclasses.asdict(cfg),
        "task": composed_task,
        "profile": profile,
    }
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# U2 — approval token store
# ---------------------------------------------------------------------------

# Default approval-token location — mirrors the CADRE_PERSONAS_DIR / CADRE_RUN_DIR
# pattern (cadre/personas.py, cadre/capture.py). Override in tests via
# CADRE_APPROVAL_PATH to avoid touching the real ~/.cadre.
DEFAULT_APPROVAL_PATH = "~/.cadre/approval"


def default_approval_path() -> str:
    """CADRE_APPROVAL_PATH env override → DEFAULT_APPROVAL_PATH. No expanduser here."""
    return os.environ.get("CADRE_APPROVAL_PATH") or DEFAULT_APPROVAL_PATH


def _parent_is_safe(parent: str) -> bool:
    """True iff ``parent`` is owned by the current user and not group/other-writable.

    The token carries no MAC/secret (the digest is a hash of inputs the agent
    already holds), so the ONLY thing standing between an attacker and a forged
    approval is that the token file cannot be replaced by another user — which is
    guaranteed only when its *directory* is owner-owned and not group/other-
    writable. A group/world-writable parent lets a co-resident user unlink and
    replant the token even though the leaf is 0o600 + O_NOFOLLOW (Codex adversarial
    review). Mirrors the persona-pool ownership/permission check in
    ``personas.resolve`` (``cadre/personas.py``) — the same repo posture for
    a trust surface whose integrity rests on filesystem permissions, not crypto.
    Non-POSIX (no ``getuid``) skips the ownership half but still enforces the mode
    bits, exactly as the persona-pool check does.
    """
    try:
        st = os.stat(parent)
    except OSError:
        return False
    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        return False
    if st.st_mode & 0o022:  # group- or other-writable
        return False
    return True


def _write_owner_only(path: Path, content: str, *, exclusive: bool = False) -> None:
    """Write ``content`` to ``path`` with the repo's canonical owner-only posture.

    The shared write tail of the #61 writers (``discover.write_candidates``,
    ``palette_fleet``'s fleet writer): tightened umask, an owner-only-created
    parent, the ``_parent_is_safe`` refusal of a foreign-owned or
    group/other-writable pre-existing parent, and ``O_NOFOLLOW`` so a symlink
    planted at ``path`` is refused rather than written through. The earlier
    writers (``write_approval`` above, ``verify_palette.write_palette``,
    ``provision.write_config``/``_seed_files``) hand-roll the same sequence
    with per-site error types/messages and predate this helper; unifying them
    is a deliberate non-goal here (behavior-preserving passes don't rewrite
    shipped trust-surface code).

    ``exclusive=True`` uses ``O_EXCL``: the ``open()`` itself becomes the
    atomic existence check, raising ``FileExistsError`` rather than touching
    a file (or following a symlink) already there. ``exclusive=False``
    (overwrite) writes to a same-directory temp file and ``os.replace``s it
    into place, so a mid-write failure (disk full, I/O error) leaves any
    prior file byte-identical — never truncated, never partial (CodeRabbit:
    a plain ``O_TRUNC`` open destroys the prior content the instant it
    succeeds, breaking ``write_palette_fleet``'s no-truncation promise on
    any post-open failure). A symlink at ``path`` is refused up front in
    this mode (``os.replace`` would swap the symlink itself rather than
    refuse, so the pre-check preserves the O_NOFOLLOW refusal contract).

    Raises:
        OSError: the parent is unsafe, or ``path`` is a symlink;
            ``FileExistsError`` (a subclass) when ``exclusive`` and ``path``
            already exists.
    """
    old_umask = os.umask(0o077)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Check AFTER mkdir: a freshly-created dir is 0o700 by construction;
        # only a PRE-EXISTING loose parent is refused (mkdir(exist_ok=True)
        # will not tighten one).
        if not _parent_is_safe(str(path.parent)):
            raise OSError(
                f"{path.parent} is unsafe — it must be owned by you and not "
                "group/other-writable. Fix it, then re-run."
            )
        if exclusive:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(path), flags, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                path.chmod(0o600)
            except OSError:
                # O_EXCL succeeded, so WE created this file — a failed write
                # (disk full, I/O error) must not leave a partial file behind:
                # create-only callers would read it as "exists — preserved"
                # forever, silently defeating every future seeding attempt.
                # Best-effort; the original error is what the caller needs.
                with contextlib.suppress(OSError):
                    path.unlink()
                raise
        else:
            if path.is_symlink():
                raise OSError(f"{path} is a symlink — refusing to write through it.")
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, str(path))
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
    finally:
        os.umask(old_umask)


@dataclasses.dataclass
class ApprovalToken:
    """A minted, not-yet-consumed approval. TTL is off by default (R3)."""

    digest: str
    privileged: bool
    minted_at: float | None = None
    ttl_seconds: int | None = None

    def is_expired(self, now: float) -> bool:
        """False when ``ttl_seconds`` is None (TTL off by default); otherwise
        ``now > minted_at + ttl_seconds``. The caller (U4) invokes this after
        ``consume_approval`` — expiry is never checked inside consume itself.
        """
        if self.ttl_seconds is None:
            return False
        return now > (self.minted_at or 0.0) + self.ttl_seconds


def write_approval(
    digest: str,
    *,
    privileged: bool,
    ttl_seconds: int | None = None,
    now: float | None = None,
    path: str | None = None,
) -> None:
    """Mint an approval token: owner-only, symlink-guarded, create-or-replace.

    ``minted_at`` defaults to the injected ``now`` (deterministic for tests —
    never calls ``time.time()`` when ``now`` is provided). If ``now`` is None
    and a TTL is set, falls back to ``time.time()`` so the TTL has a real
    reference point; otherwise ``minted_at`` is written as None.

    Creates the parent directory (0o700) under a tightened umask if it does
    not yet exist — the bare ``capture._write`` idiom assumes the directory
    already exists, so this must do that step itself.

    Opens with ``O_NOFOLLOW`` on top of the ``capture._write`` create/truncate
    idiom (``capture._write`` itself omits ``O_NOFOLLOW``) — a symlinked token
    path is refused (OSError propagates to the caller) rather than followed,
    mirroring the Pass-1 seed-dir symlink-refusal posture. ``chmod`` after
    writing tightens a pre-existing regular file at this path to 0o600, since
    ``O_CREAT`` does not alter the mode of a file that already exists.
    """
    resolved = os.path.expanduser(path or default_approval_path())

    minted_at = now
    if minted_at is None and ttl_seconds is not None:
        minted_at = time.time()

    payload = {
        "digest": digest,
        "privileged": privileged,
        "minted_at": minted_at,
        "ttl_seconds": ttl_seconds,
    }
    blob = json.dumps(payload)

    parent = os.path.dirname(resolved) or "."
    old_umask = os.umask(0o077)
    try:
        Path(resolved).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Refuse to mint into a group/other-writable (or foreign-owned) directory.
        # mkdir(exist_ok=True) creates a fresh dir 0o700 but will NOT tighten a
        # PRE-EXISTING loose one — and a loose parent lets another user replant the
        # token (no MAC to stop them). Check AFTER mkdir so a just-created dir passes
        # by construction and only a pre-existing loose dir is refused (Codex).
        if not _parent_is_safe(parent):
            raise PermissionError(
                f"approval directory {parent!r} is unsafe — it must be owned by you "
                "and not group/other-writable (the token has no MAC, so its integrity "
                "rests on the directory's permissions). Fix it, then re-preview."
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(resolved, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.chmod(resolved, 0o600)
    finally:
        os.umask(old_umask)


def consume_approval(path: str | None = None) -> ApprovalToken | None:
    """Atomically read-and-delete the approval token; return None on any failure.

    Opens with ``O_NOFOLLOW`` — a missing file or a symlinked path (ELOOP)
    raises OSError, caught here and reported as None (fail-closed).

    Atomic one-shot: ``os.unlink`` runs BEFORE reading from the still-open fd
    (POSIX permits reading an unlinked fd), so a racing second consumer gets
    ENOENT on its own open. If the unlink itself fails (present but
    undeletable, e.g. a restrictive parent directory), the fd is closed and
    None is returned — never honor a token that could not be deleted.

    Any malformed / short / non-JSON / wrong-shaped content returns None,
    never a traceback. Expiry is NOT checked here — the caller calls
    ``token.is_expired(time.time())``.
    """
    resolved = os.path.expanduser(path or default_approval_path())

    # Refuse to HONOR a token from a group/other-writable (or foreign-owned)
    # directory — that is the actual exploit path: a co-resident user replants the
    # token in a loose dir and the run consumes it. Check BEFORE the read; fail
    # closed (return None → the run gate refuses), mirroring write_approval's mint
    # refusal and the persona-pool posture (Codex adversarial review).
    parent = os.path.dirname(resolved) or "."
    if not _parent_is_safe(parent):
        return None

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError:
        return None

    try:
        os.unlink(resolved)
    except OSError:
        # Present but undeletable — fail-closed, never honor it. Close the fd
        # ourselves: os.fdopen (which would otherwise own it) is never reached.
        os.close(fd)
        return None

    # From here os.fdopen owns fd; its `with` block closes it on exit — no
    # manual os.close past this point (that would double-close).
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            content = f.read()
        data = json.loads(content)
        digest = data["digest"]
        privileged = data["privileged"]
        if not isinstance(digest, str) or not isinstance(privileged, bool):
            raise ValueError("approval token: digest/privileged have the wrong type")
        # Type-check the TTL fields too: a token with a wrong-typed minted_at/ttl_seconds
        # (e.g. "ttl_seconds": "60") would otherwise round-trip and crash later in
        # ApprovalToken.is_expired (`now > str + int`), a traceback instead of a
        # fail-closed refusal (CodeRabbit). bool is an int subclass, so exclude it.
        minted_at = data.get("minted_at")
        ttl_seconds = data.get("ttl_seconds")
        if minted_at is not None and (isinstance(minted_at, bool) or not isinstance(minted_at, (int, float))):
            raise ValueError("approval token: minted_at has the wrong type")
        if ttl_seconds is not None and (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)):
            raise ValueError("approval token: ttl_seconds has the wrong type")
        return ApprovalToken(
            digest=digest,
            privileged=privileged,
            minted_at=minted_at,
            ttl_seconds=ttl_seconds,
        )
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
