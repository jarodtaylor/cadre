"""cadre/palette_fleet.py — generate a runnable smoke-test fleet from a
successful ``cadre verify-palette`` cycle (#61, R7/R8).

Caller-layer module (KTD4), same posture as ``cadre.verify_palette`` and
``cadre.discover``: NEVER imported by ``cadre.engine`` or
``cadre.model_client``, and this module must not import them either
(``tests/test_personas.py``'s ``TestEngineIsolation`` guards both directions).

Hooked into ``verify_palette.main()`` (``cadre/verify_palette.py``), called
AFTER ``write_palette`` succeeds: ``write_palette_fleet(records)``. Filters
``records`` to the verified (``ok=True``) ones itself — the call site stays a
one-line hook.

## What it generates

``~/.cadre/fleets/palette-fleet.yaml`` — a tool-less ``collect`` fleet with one
lane per verified provider (that provider's FIRST verified model, in
``records`` order), capped at ``_MAX_LANES``. This is the first fleet an
operator (or an agent driving Cadre) can run with zero manual editing: no
persona files to author, no tools to provision, no synthesizer to pick — just
"does every verified pair answer end to end."

## Ownership — regenerated, not seeded

This file is DISCOVERY-OWNED: every successful verify cycle overwrites it in
place (R8). That is a deliberately different ownership model from the curated
starter fleets ``cadre.provision.seed_starter_fleets`` copies once and then
preserves (``_STARTER_FLEETS`` in ``cadre/provision.py``) — this file is never
added to that allowlist, and it is never treated as an operator-editable
starting point the way ``code-review.example.yaml`` is. Hand edits do not
survive the next verify cycle; the generated header says so.

## Failure posture

``write_palette_fleet`` NEVER raises. Fewer than ``_MIN_LANES`` distinct
verified providers skips generation entirely (a one-lane "fleet" is not a
fleet) — loudly, to stderr, naming any prior file left in place. A write
failure (a symlinked target, an unsafe parent directory, a permission error)
is caught and warned to stderr rather than propagated: ``palette.yaml``
(written by the caller just before this hook runs) is the primary
deliverable of a verify cycle; this generated fleet is a bonus artifact, not
allowed to flip a successful verify to a nonzero exit. Mirrors
``cadre.provision``'s seeding functions' never-raises contract.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from cadre.approval import _write_owner_only
from cadre.capture import resolved_hermes_home
from cadre.text_safety import sanitize as _sanitize

if TYPE_CHECKING:
    from cadre.verify_palette import VerifyRecord

# Default destination — a sibling of the curated starter fleets seeded by
# cadre.provision.seed_starter_fleets (dest_dir=cadre_home / "fleets"), so this
# lands in the same directory an operator already looks in. Kept as its own
# private constant (mirroring verify_palette.py's _DEFAULT_CANDIDATES_PATH /
# _DEFAULT_PALETTE_PATH) rather than calling cadre.provision.ensure_cadre_dirs():
# that function also scaffolds personas/, an unrelated coupling this hook does
# not need — write_palette_fleet creates its own parent directory (below).
# Left un-expanded at module scope (no filesystem touch merely from import);
# expansion happens only inside _default_fleet_path() at call time.
_DEFAULT_FLEET_PATH = Path("~/.cadre/fleets/palette-fleet.yaml")

# A smoke-test fleet, not a do-everything one (KTD5): one lane per distinct
# verified provider, capped small so the generated file stays a quick,
# obviously-cheap first run. Minimum 2 — a single lane is not a "fleet"
# (nothing to compare across), so generation is skipped below this floor.
_MAX_LANES = 5
_MIN_LANES = 2

# No tools, no persona file (none is guaranteed to exist on a fresh install) —
# just a short, honest connectivity check every verified pair should answer.
_FOCUS_TEXT = (
    "You have no tools available -- do not emit tool calls, fetch, or run "
    "anything. Answer directly from your own knowledge, in one or two short "
    "sentences: name one thing large language models are commonly used for. "
    "This is a connectivity smoke test confirming this model answers end to "
    "end -- keep the answer brief."
)


def _default_fleet_path() -> Path:
    """The default palette-fleet destination, expanded at call time."""
    return _DEFAULT_FLEET_PATH.expanduser()


def _first_verified_per_provider(records: list[VerifyRecord]) -> list[VerifyRecord]:
    """Return the FIRST ``ok=True`` record per distinct provider, in ``records``
    order, capped at ``_MAX_LANES`` distinct providers.

    Pure — no I/O. A provider with multiple verified models contributes only
    its first (the one that appears earliest in ``records``); later ones are
    skipped so the generated fleet has exactly one lane per provider.

    Args:
        records: Verification records from ``verify_candidates`` (or a test fake).

    Returns:
        Up to ``_MAX_LANES`` records, one per distinct provider, in the order
        each provider first appears among the ``ok=True`` records.
    """
    seen: set[str] = set()
    chosen: list[VerifyRecord] = []
    for r in records:
        if not r.ok or r.provider in seen:
            continue
        seen.add(r.provider)
        chosen.append(r)
        if len(chosen) >= _MAX_LANES:
            break
    return chosen


def _header(profile: str, generated_at: str) -> str:
    """The do-not-hand-edit comment header: purpose, regeneration contract,
    resolved profile, and generation timestamp — all as YAML comments, never
    as fleet-schema fields (this keeps the generated dict clean of anything
    ``FleetConfig.from_dict`` would have to ignore)."""
    return (
        "# Cadre palette fleet — GENERATED by `cadre verify-palette`; do not hand-edit.\n"
        "# Purpose: a connectivity smoke test and your first runnable fleet — one\n"
        "# lane per verified provider from the last verify cycle, no tools, no\n"
        "# synthesis. Confirms every verified pair actually answers end to end.\n"
        "#\n"
        "# This file is REGENERATED (overwritten) on every discover-then-verify\n"
        "# cycle — hand edits do not survive the next run. Copy it to a new file\n"
        "# first if you want to keep a customized version.\n"
        "#\n"
        f"# profile:      {profile}\n"
        f"# generated_at: {generated_at}\n"
        "\n"
    )


def build_fleet_yaml(lanes: list[VerifyRecord]) -> str:
    """Render the palette-fleet YAML text for the given (already capped and
    filtered) lanes. Pure — no I/O; ``write_palette_fleet`` handles writing.

    The YAML content is data: provider/model strings go in verbatim (via
    ``yaml.safe_dump``, which handles any YAML-special characters correctly) —
    never routed through ``cadre.text_safety.sanitize``, which is reserved for
    strings rendered on a *display/terminal* surface, not for a file's own
    data payload (KTD9).

    Args:
        lanes: One VerifyRecord per distinct provider (see
            ``_first_verified_per_provider``), already capped at ``_MAX_LANES``
            and known non-empty by the caller.

    Returns:
        The full file text: the comment header, then a valid ``collect``
        fleet YAML document (no ``synthesis``/``judge`` block — collect mode
        needs neither).
    """
    fleet_dict = {
        "name": "palette-fleet",
        "description": (
            "Auto-generated connectivity smoke-test fleet: one lane per "
            "verified provider from the last `cadre verify-palette` run, no "
            "tools, no synthesis (collect). Regenerated every verify cycle."
        ),
        "convergence": "collect",
        "specialists": [
            {
                "role": lane.provider,
                "provider": lane.provider,
                "model": lane.model,
                "toolset": [],
                "focus": _FOCUS_TEXT,
            }
            for lane in lanes
        ],
    }
    header = _header(resolved_hermes_home(), datetime.now().isoformat())
    return header + yaml.safe_dump(fleet_dict, sort_keys=False, allow_unicode=True)


def _notice_insufficient(n_verified: int, target: Path) -> None:
    """Loud stderr notice when generation is skipped for having too few
    verified providers (R8). Names ``target`` ONLY when a prior file actually
    exists there — a from-scratch host with nothing to name gets a shorter,
    accurate message."""
    lines = [
        f"[cadre] palette fleet not (re)generated: only {n_verified} provider(s) "
        f"verified (need at least {_MIN_LANES} for a fleet).",
    ]
    if target.exists():
        lines.append(
            f"  A previous {target} is left in place — it may now reference "
            "pairs no longer in your palette; a run against it will be "
            "correctly refused by preflight as off-palette."
        )
    lines.append(
        "  Re-run `cadre discover` + `cadre verify-palette` once two or more "
        "providers verify."
    )
    print("\n".join(lines), file=sys.stderr)


def write_palette_fleet(records: list[VerifyRecord], path: str | Path | None = None) -> None:
    """Generate the palette smoke-test fleet from verified records (R7/R8).

    Filters ``records`` to ``ok=True`` itself (the hook's call site in
    ``verify_palette.main`` stays a simple one-liner), picks the first
    verified model per distinct provider (``_first_verified_per_provider``),
    and writes it to ``path`` (or the default ``~/.cadre/fleets/palette-fleet.yaml``).

    NEVER raises. Two independent conditions degrade to a stderr warning
    instead of an exception, and in both cases any prior file at ``path`` is
    left completely untouched — no truncation, no partial write:

    * Fewer than ``_MIN_LANES`` distinct verified providers: generation is
      skipped entirely (see ``_notice_insufficient``).
    * A write failure (unsafe parent, symlinked target, permission error):
      caught and warned, never propagated — ``palette.yaml`` is the primary
      deliverable of a verify cycle, and this bonus artifact must not be able
      to turn a successful verify into a nonzero exit.

    Args:
        records: VerifyRecord list from ``verify_candidates`` (or a test fake).
        path: Destination path. Defaults to ``~/.cadre/fleets/palette-fleet.yaml``.
    """
    target = Path(path).expanduser() if path is not None else _default_fleet_path()
    lanes = _first_verified_per_provider(records)

    if len(lanes) < _MIN_LANES:
        _notice_insufficient(len(lanes), target)
        return

    content = build_fleet_yaml(lanes)
    try:
        # Canonical owner-only posture (approval._write_owner_only, KTD6):
        # tightened umask, 0o700 parent, _parent_is_safe, O_NOFOLLOW, 0600.
        _write_owner_only(target, content)
    except OSError as exc:
        print(f"[cadre] warning: could not write palette fleet to {target}: {exc}", file=sys.stderr)
        return

    # Discovery-sourced provider strings are untrusted display input (KTD9,
    # same rule verify_candidates/_verifying_banner already follow) — sanitize
    # each one before joining into this terminal notice.
    providers = ", ".join(_sanitize(lane.provider) for lane in lanes)
    print(f"[cadre] palette fleet ({len(lanes)} lane(s): {providers}) → {target}", file=sys.stderr)
