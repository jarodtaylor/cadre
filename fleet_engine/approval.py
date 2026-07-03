"""Preview-bound approval — canonical surface digest (U1) + owner-only token store (U2).

Caller-layer only: intended for ``skills/cadre-fleet/run.py`` (and ``render.py``
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

import dataclasses
import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fleet_engine.config import FleetConfig


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
# pattern (fleet_engine/personas.py, fleet_engine/capture.py). Override in tests via
# CADRE_APPROVAL_PATH to avoid touching the real ~/.cadre.
DEFAULT_APPROVAL_PATH = "~/.cadre/approval"


def default_approval_path() -> str:
    """CADRE_APPROVAL_PATH env override → DEFAULT_APPROVAL_PATH. No expanduser here."""
    return os.environ.get("CADRE_APPROVAL_PATH") or DEFAULT_APPROVAL_PATH


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

    old_umask = os.umask(0o077)
    try:
        Path(resolved).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        return ApprovalToken(
            digest=digest,
            privileged=privileged,
            minted_at=data.get("minted_at"),
            ttl_seconds=data.get("ttl_seconds"),
        )
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
