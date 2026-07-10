"""cadre/policy.py — local, owner-only allow/deny policy gate (#78).

Palette membership is the only control over what a fleet can spend on: a
re-verify re-admits, and the non-destructive always-keep preserves, a pair an
operator must never run. This module adds one more control layer, orthogonal
to the palette: a local ``~/.cadre/policy.yaml`` naming providers to deny
outright, or model patterns to restrict to specific providers (e.g. an
expensive family that must only run through the one billing route the
operator intends). It is enforced at three CALLER-LAYER chokepoints —
``cadre discover`` (exclude + disclose), ``cadre verify-palette`` (refuse
admission + prune in-palette violations), and preflight (defense-in-depth
refuse) — each importing from here; this module itself performs I/O only in
``load_policy`` and holds no chokepoint-specific behavior.

Two rule shapes::

    deny_providers:
      - <provider-slug>
    restrict_models:
      - match: <model or family pattern, fnmatch glob>
        allowed_providers: [<provider-slug>]

Tri-state load (KTD2): the file is ABSENT on a fresh or opted-out host —
``load_policy`` returns ``Policy.permissive()`` (blocks nothing), so existing
users see zero behavior change until they opt in. The file is PRESENT and
valid — ``load_policy`` returns the parsed ``Policy``. The file is PRESENT
but cannot be trusted (a symlink, foreign-owned, group/other-writable,
unreadable, not UTF-8, not valid YAML, wrong-shaped, or carrying an
unrecognized key) — ``load_policy`` raises ``PolicyError``. Every chokepoint
that calls it must treat that raise as FAIL CLOSED (refuse the operation
loudly), never as "no policy" — a broken safety file must never silently
mean no safety.

Out of scope (per the issue): spend metering, budgets, per-run dollar caps,
pricing awareness. This is allow/deny, not FinOps. Also out of scope: a
policy CLI editor, per-fleet policy overrides.

Engine purity (KTD7): this module is never imported by ``cadre.engine`` or
``cadre.model_client`` (``TestEngineIsolation`` in ``tests/test_personas.py``
guards both directions) — policy enforcement is entirely caller-layer,
mirroring ``preflight.py``/``approval.py``'s posture. No import-time I/O:
the only function that touches the filesystem is ``load_policy``.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Default policy file location — mirrors the CADRE_PALETTE / CADRE_PERSONAS_DIR
# env-override convention (cadre.preview_lint.DEFAULT_PALETTE_PATH,
# cadre.personas.DEFAULT_PERSONAS_DIR / default_pool_dir). Every chokepoint
# resolves its policy path via default_policy_path() below rather than
# hand-rolling its own env lookup, so ONE CADRE_POLICY override — and the test
# suite's hermetic forced-absent default (tests/__init__.py, mirroring its
# existing CADRE_PALETTE force) — reaches discover, verify-palette, and
# preflight uniformly, with no per-call-site plumbing.
DEFAULT_POLICY_PATH = "~/.cadre/policy.yaml"


def default_policy_path() -> str:
    """CADRE_POLICY env override -> DEFAULT_POLICY_PATH. No expanduser here."""
    return os.environ.get("CADRE_POLICY") or DEFAULT_POLICY_PATH


class PolicyError(Exception):
    """Raised when ``policy.yaml`` exists but cannot be trusted — fail CLOSED.

    A symlink, foreign ownership, group/other-writable permissions, an
    unreadable or non-UTF-8 file, invalid YAML, a non-mapping top level, an
    unrecognized key, or a wrong-shaped rule all raise this rather than
    degrading to permissive (KTD2). Every message names the resolved path
    and the remedy: fix or remove it.
    """


@dataclass(frozen=True)
class Violation:
    """One (provider, model) pair blocked by a policy rule, and which rule.

    ``rule`` is a human-readable description naming the exact rule that
    fired, e.g. ``"deny_providers: prov-a"`` or
    ``"restrict_models: 'family-*' allows [prov-b]"`` — every chokepoint's
    disclosure/refusal text is built by naming ``provider``/``model``/``rule``
    together, never just one.
    """

    provider: str
    model: str
    rule: str


@dataclass(frozen=True)
class _RestrictRule:
    """One parsed ``restrict_models`` entry."""

    match: str
    allowed_providers: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    """A loaded (or permissive-default) policy. Immutable; ``check`` is pure.

    Construct via ``load_policy`` (from disk) or ``Policy.permissive()`` (the
    absent-file default) — not directly, so every instance's ``source``
    honestly reflects where it came from.
    """

    deny_providers: frozenset[str] = field(default_factory=frozenset)
    restrict_models: tuple[_RestrictRule, ...] = ()
    source: str = "absent"  # "absent" (no file) | "file" (loaded from disk)

    @classmethod
    def permissive(cls) -> "Policy":
        """The absent-file default: blocks nothing."""
        return cls()

    def check(self, provider: str, model: str) -> "Violation | None":
        """Return a ``Violation`` if ``(provider, model)`` is blocked, else ``None``.

        Pure — no I/O. ``deny_providers`` is an absolute veto, checked before
        ``restrict_models``: a provider that is both denied outright and
        listed as an allowed provider for some ``restrict_models`` rule is
        still blocked. ``restrict_models`` rules are checked in file order;
        the FIRST rule whose ``match`` glob matches ``model`` decides the
        outcome for that model — a later rule that would also match is not
        additionally consulted (first-match-wins, not most-permissive-wins).
        A model matching no rule is unaffected by ``restrict_models``
        entirely. Provider slugs and model ids are host-defined identifiers,
        compared exactly as written: ``fnmatch.fnmatchcase`` is used (never
        the bare ``fnmatch.fnmatch``, whose case sensitivity silently
        depends on the host OS's path-casing convention).
        """
        if provider in self.deny_providers:
            return Violation(provider, model, f"deny_providers: {provider}")
        for rule in self.restrict_models:
            if fnmatch.fnmatchcase(model, rule.match):
                if provider in rule.allowed_providers:
                    return None
                allowed = ", ".join(rule.allowed_providers)
                return Violation(
                    provider,
                    model,
                    f"restrict_models: {rule.match!r} allows [{allowed}]",
                )
        return None


def filter_pairs(
    pairs: list[tuple[str, str]], policy: "Policy"
) -> tuple[list[tuple[str, str]], list[Violation]]:
    """Pure, order-preserving split of ``(provider, model)`` pairs.

    Returns ``(allowed, violations)``. The shared filter for a flat pair list
    — ``cadre.verify_palette`` uses this directly, since its candidates are
    already a flat list. ``cadre.discover`` filters its own provider-grouped
    ``DiscoveredProvider`` shape directly against ``Policy.check`` instead
    (its own module has the loop, to preserve per-provider model order and
    drop a now-empty provider) — both routes share the same underlying rule
    logic, ``Policy.check``. No I/O.
    """
    allowed: list[tuple[str, str]] = []
    violations: list[Violation] = []
    for provider, model in pairs:
        violation = policy.check(provider, model)
        if violation is None:
            allowed.append((provider, model))
        else:
            violations.append(violation)
    return allowed, violations


_KNOWN_TOP_KEYS = frozenset({"deny_providers", "restrict_models"})
_KNOWN_RESTRICT_KEYS = frozenset({"match", "allowed_providers"})


def load_policy(path: str | Path) -> Policy:
    """Load a policy file — tri-state (see module docstring for the contract).

    Read posture mirrors ``cadre.personas.resolve``'s confinement pattern —
    the repo's only precedent for reading a TRUSTED file (``provision.py`` /
    ``approval.py`` expose only WRITE-side owner-only helpers, none for
    reads): open with ``O_NOFOLLOW`` (a symlink at ``path`` is refused, never
    followed), then an ownership/mode check on the OPENED file via
    ``os.fstat`` on that same fd — not a separate ``os.stat`` call, which
    would reopen a TOCTOU window between check and read. A policy file is
    money-safety-relevant, the same trust class as the persona pool (trusted
    as model instructions) and the approval token (trusted as a run
    authorization), so it gets the same posture: refuse a foreign-owned or
    group/other-writable file outright, before its content is ever parsed or
    trusted. Non-POSIX (no ``os.getuid``) skips the ownership half but still
    enforces the mode bits, matching ``approval._parent_is_safe``'s same
    fallback.

    No import-time I/O (KTD1): this is the only function in the module that
    touches the filesystem.

    Args:
        path: The policy file location (already resolved by the caller —
            e.g. via ``default_policy_path()`` — this function does no env
            lookup of its own).

    Returns:
        ``Policy.permissive()`` if ``path`` does not exist, or a
        ``source="file"`` ``Policy`` if it exists and parses.

    Raises:
        PolicyError: ``path`` exists but is a symlink, foreign-owned,
            group/other-writable, unreadable, not UTF-8, not valid YAML, a
            non-mapping top level, carries an unrecognized top-level or
            ``restrict_models`` entry key, or a rule is wrong-shaped.
    """
    resolved = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(resolved), flags)
    except FileNotFoundError:
        return Policy.permissive()
    except OSError as exc:
        # Covers ELOOP (a symlink refused by O_NOFOLLOW) and any other open
        # failure (e.g. EACCES) — all fail CLOSED, never silently permissive.
        raise PolicyError(
            f"{resolved} could not be opened ({exc}). This is a "
            f"safety-relevant file — fix or remove {resolved}."
        ) from None

    # From here `fd` is open; every path below must close it exactly once —
    # the ownership/mode check runs before os.fdopen takes ownership of fd,
    # so a raise in that block must close it itself.
    try:
        st = os.fstat(fd)
        getuid = getattr(os, "getuid", None)
        if getuid is not None and st.st_uid != getuid():
            raise PolicyError(
                f"{resolved} is not owned by you — refusing to trust a "
                f"safety-relevant policy file with foreign ownership. "
                f"Fix or remove {resolved}."
            )
        if st.st_mode & 0o022:
            raise PolicyError(
                f"{resolved} is group- or other-writable — refusing to "
                f"trust a safety-relevant policy file with loose "
                f"permissions. Fix (chmod 600 {resolved}) or remove it."
            )
    except BaseException:
        os.close(fd)
        raise

    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:  # fdopen now owns fd
            raw = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(
            f"{resolved} could not be read ({exc}). Fix or remove {resolved}."
        ) from None

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(
            f"{resolved} is not valid YAML ({exc}). Fix or remove {resolved}."
        ) from None

    if data is None:
        # A fully-commented (or empty) file parses to None — the seeded
        # default (all-comments) must load as permissive, not an error.
        return Policy.permissive()

    if not isinstance(data, dict):
        raise PolicyError(
            f"{resolved} must be a YAML mapping at the top level (got "
            f"{type(data).__name__}). Fix or remove {resolved}."
        )

    unknown = set(data) - _KNOWN_TOP_KEYS
    if unknown:
        raise PolicyError(
            f"{resolved} has unrecognized key(s) {sorted(unknown)} — a "
            f"misspelled rule name would otherwise silently do nothing. "
            f"Fix or remove {resolved}."
        )

    # `is None` (never a truthy/falsy `or []`) so a wrong-shaped-but-falsy
    # value (e.g. `deny_providers: false`) is caught by the isinstance check
    # below rather than silently coerced to empty — only an explicit
    # absence or an explicit YAML null default to "no rules".
    deny_raw = data.get("deny_providers")
    if deny_raw is None:
        deny_raw = []
    if not isinstance(deny_raw, list) or not all(
        isinstance(p, str) and p for p in deny_raw
    ):
        raise PolicyError(
            f"{resolved}: `deny_providers` must be a list of non-empty "
            f"provider-slug strings. Fix or remove {resolved}."
        )

    restrict_raw = data.get("restrict_models")
    if restrict_raw is None:
        restrict_raw = []
    if not isinstance(restrict_raw, list):
        raise PolicyError(
            f"{resolved}: `restrict_models` must be a list of "
            f"{{match, allowed_providers}} mappings. Fix or remove {resolved}."
        )

    restrict_rules: list[_RestrictRule] = []
    for i, entry in enumerate(restrict_raw):
        label = f"restrict_models[{i}]"
        if not isinstance(entry, dict):
            raise PolicyError(
                f"{resolved}: {label} must be a mapping. Fix or remove {resolved}."
            )
        extra = set(entry) - _KNOWN_RESTRICT_KEYS
        if extra:
            raise PolicyError(
                f"{resolved}: {label} has unrecognized key(s) {sorted(extra)}. "
                f"Fix or remove {resolved}."
            )
        match = entry.get("match")
        if not isinstance(match, str) or not match:
            raise PolicyError(
                f"{resolved}: {label}.match must be a non-empty string. "
                f"Fix or remove {resolved}."
            )
        allowed_raw = entry.get("allowed_providers")
        if not isinstance(allowed_raw, list) or not allowed_raw or not all(
            isinstance(p, str) and p for p in allowed_raw
        ):
            raise PolicyError(
                f"{resolved}: {label}.allowed_providers must be a non-empty "
                f"list of provider-slug strings. Fix or remove {resolved}."
            )
        restrict_rules.append(
            _RestrictRule(match=match, allowed_providers=tuple(allowed_raw))
        )

    return Policy(
        deny_providers=frozenset(deny_raw),
        restrict_models=tuple(restrict_rules),
        source="file",
    )


# ---------------------------------------------------------------------------
# Seed content — a fully-commented, generic default (cadre/provision.py's
# seed_policy writes this verbatim). Co-located with the schema it documents
# so the two can never drift apart. No real provider or model names (they
# would read as an endorsement or a leaked host detail); every comment line
# starts with "# " so the seeded file parses as `None` (all-comments -> a
# permissive Policy) via load_policy above.
# ---------------------------------------------------------------------------

SEED_TEMPLATE = """\
# Cadre policy — local allow/deny gate for providers and models (#78).
#
# Absent or fully-commented (as seeded): permissive — blocks nothing. A
# malformed file (bad YAML, an unrecognized key, a wrong-shaped rule) fails
# CLOSED — every fleet is refused until this file is fixed or removed.
#
# Enforced at three points: `cadre discover` excludes banned pairs from the
# candidates file; `cadre verify-palette` refuses to admit a banned candidate
# (and prunes an already-admitted pair a tightened policy now bans); preflight
# refuses a fleet that references a banned pair before any spend.
#
# deny_providers: block a provider outright, e.g. one billed per-use when you
# only intend to use subscription-quota routes.
#
# deny_providers:
#   - example-provider
#
# restrict_models: pin an expensive model or family to the one provider whose
# billing you actually intend — `match` is a glob (fnmatch), so a `*` suffix
# covers a whole family. A model matching no rule here is unaffected.
#
# restrict_models:
#   - match: "example-family-*"
#     allowed_providers: [example-provider]
"""
