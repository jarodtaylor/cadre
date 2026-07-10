"""cadre/smoke.py — zero-spend pre-release host smoke gate (#79).

Caller-layer only: imported by ``cadre/cli.py`` (the ``cadre smoke`` verb).
NEVER imported by ``engine.py`` or ``model_client.py`` — this is a read-only
diagnostic over host state, not engine behavior (mirrors ``discover.py`` /
``preflight.py``'s posture; ``tests/test_personas.py``'s ``TestEngineIsolation``
guards both directions).

Three checks, each pure/read-only and NON-DESTRUCTIVE — none of them write
anything to ``~/.cadre`` or anywhere else:

1. **Inventory shape** (``check_inventory_shape``) — fetches Hermes's live
   provider inventory IN MEMORY, reusing ``discover._fetch_inventory`` (the
   exact same lazy ``hermes_cli.inventory`` import ``cadre discover`` uses —
   no separate import path to drift out of sync), and asserts the payload
   still matches the banked-facts contract
   (``docs/reference/hermes/README.md`` facts 7-8): top-level keys
   ``model``/``provider``/``providers``; each non-aggregator provider row
   carries ``{authenticated, is_current, is_user_defined, models, name, slug,
   source, total_models}`` with sane types (``auth_type`` is deliberately
   NOT in that set — a normal row never carries it); each ``models`` entry is
   a bare string or an ``{"id": ...}`` dict. Hermes's own aggregator row
   (``auth_type: "virtual"``) is exempted from the row-shape check the same
   way ``discover_candidates`` skips it. This NEVER calls
   ``discover.write_candidates`` — smoke reads the payload and stops; the
   candidates file is untouched. On a machine without ``hermes_cli``
   importable (e.g. a dev laptop), this degrades to a clear FAIL naming that,
   via ``discover.DiscoveryError`` — the validate/preflight checks below
   still run and can still pass.

2. **Fleet validation** (``check_fleet_validation``) — the same load-and-resolve
   pass ``cadre validate`` performs (``FleetConfig.load`` + ``personas.resolve``),
   in-process (never a subprocess), over every packaged starter fleet
   (``cadre/data/fleets/*.example.yaml``) — the release artifacts this package
   actually ships. Resolves personas against the PACKAGED pool
   (``cadre.resources.personas_dir()``), not whatever ``~/.cadre/personas``
   happens to hold on the invoking host: this check is package-integrity (are
   the fleets and personas we ship together mutually valid?), not host
   provisioning — checks 1 and 3 already cover live host state, and at least
   one packaged starter (``doc-review.example.yaml``) references personas by
   name, so validating it needs a pool that is guaranteed to exist regardless
   of whether this host ever ran ``cadre setup``.

3. **Preflight resolution** (``check_preflight``) — the host's generated
   ``~/.cadre/fleets/palette-fleet.yaml`` (written by a successful
   ``cadre verify-palette``, one lane per verified provider) is loaded and
   run through ``cadre.preflight.preflight_refusal`` against the LIVE host
   palette. Because that fleet is *derived* from the verified palette, it
   should always preflight clean; a refusal here is real drift signal, not a
   fixture gap. Reuses ``preflight_refusal``'s exact refusal text on a
   refusal — this module never writes its own off-palette prose. A missing
   ``palette-fleet.yaml`` fails with a distinct, factual message (it is a
   generated artifact, not a hand-authored one — see ``cadre.palette_fleet``)
   rather than being forced through ``preflight_refusal`` with a fabricated
   config, which would risk a misleading "off-palette model" reason naming a
   model that was never real.

``cadre smoke`` (``main``) runs all three, prints one ``[cadre] smoke: <name>
... PASS`` / ``FAIL (<reason>)`` line per check to stderr (R9 — stdout stays
clean except the one-line final verdict, matching ``discover``/
``verify-palette``'s own stdout/stderr split), and exits 0 iff every check
passed, else 1. Makes zero model calls and zero spend; the only I/O is
read-only (the live inventory fetch, the packaged fleet files, and
``~/.cadre/palette.yaml`` / ``~/.cadre/fleets/palette-fleet.yaml``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from cadre.config import ConfigError, FleetConfig
from cadre.discover import DiscoveryError, _fetch_inventory
from cadre.exit_codes import ExitCode
from cadre.palette_fleet import default_fleet_path
from cadre.personas import default_pool_dir, resolve
from cadre.preflight import preflight_refusal
from cadre.resources import fleets_dir, personas_dir
from cadre.text_safety import sanitize as _sanitize

# The exact 8 keys a normal (non-aggregator) provider row carries, per
# docs/reference/hermes/README.md fact 8 — in the SAME order that fact lists
# them, so a reader can diff this against the source fact directly.
# `auth_type` is deliberately absent: a normal row never carries it (only the
# aggregator does), so requiring it here would be a smoke-test-authored bug,
# not a drift detector.
_REQUIRED_ROW_KEYS: dict[str, type] = {
    "authenticated": bool,
    "is_current": bool,
    "is_user_defined": bool,
    "models": list,
    "name": str,
    "slug": str,
    "source": str,
    "total_models": int,
}

# The 3 top-level keys fact 8 documents. Presence-only (no type assertion) —
# fact 8 doesn't characterize their types beyond "the host's current
# selection", so asserting a type here would be this module inventing a
# constraint the banked fact never made.
_TOP_LEVEL_KEYS = ("model", "provider", "providers")


@dataclass
class SmokeCheck:
    """One check's outcome: a name, pass/fail, and (on fail) the reason."""

    name: str
    ok: bool
    detail: str = ""


def _is_sane_type(value: object, expected: type) -> bool:
    """``isinstance``, except ``bool`` is never accepted as a stand-in for
    ``int`` (``total_models``) — ``bool`` is an ``int`` subclass in Python, so
    a bare ``isinstance(x, int)`` would silently accept a wrong-shaped
    boolean there."""
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def check_inventory_shape(payload: dict | None = None) -> SmokeCheck:
    """Assert a Hermes inventory payload matches the banked-facts contract.

    Pure once ``payload`` is given (tests inject one — no Hermes needed).
    With no ``payload``, fetches a fresh one via ``discover._fetch_inventory``
    (the same lazy import ``cadre discover`` uses) — never
    ``discover.write_candidates``, so nothing is written regardless of
    outcome.

    Returns:
        A ``SmokeCheck`` — ``ok=True`` when every non-aggregator row has all
        8 required keys with sane types and every ``models`` entry is a
        recognized shape; ``ok=False`` naming exactly what drifted (a missing
        key, a wrong type, an unrecognized model entry, a non-list
        ``providers``, or Hermes simply not being importable on this host).
        Extra/unknown keys — top-level or per-row — are NOT a failure (benign
        growth, not drift): a stderr note lists them and the check still
        passes.
    """
    name = "inventory-shape"
    if payload is None:
        try:
            payload = _fetch_inventory()
        except DiscoveryError as exc:
            return SmokeCheck(
                name,
                False,
                f"hermes not importable (or its inventory unreadable) on this host: {exc}",
            )

    if not isinstance(payload, dict):
        return SmokeCheck(
            name, False, f"inventory payload is not a mapping (got {type(payload).__name__})"
        )

    missing_top = [k for k in _TOP_LEVEL_KEYS if k not in payload]
    if missing_top:
        return SmokeCheck(
            name, False, f"inventory payload missing top-level key(s): {', '.join(missing_top)}"
        )

    raw_providers = payload["providers"]
    if not isinstance(raw_providers, list):
        return SmokeCheck(
            name,
            False,
            f"inventory 'providers' is not a list (got {type(raw_providers).__name__})",
        )

    extra_keys: set[str] = set(payload) - set(_TOP_LEVEL_KEYS)

    for index, row in enumerate(raw_providers):
        if not isinstance(row, dict):
            return SmokeCheck(name, False, f"provider row at index {index} is not a mapping")

        slug = row.get("slug")
        label = slug if isinstance(slug, str) and slug else f"index {index}"

        if row.get("auth_type") == "virtual":
            continue  # Hermes's own aggregator row -- exempt, mirrors discover.py's own skip

        missing = [k for k in _REQUIRED_ROW_KEYS if k not in row]
        if missing:
            return SmokeCheck(
                name, False, f"provider {label!r} missing required key(s): {', '.join(missing)}"
            )

        wrong_type = [
            k for k, expected in _REQUIRED_ROW_KEYS.items() if not _is_sane_type(row[k], expected)
        ]
        if wrong_type:
            detail = ", ".join(f"{k} (got {type(row[k]).__name__})" for k in wrong_type)
            return SmokeCheck(name, False, f"provider {label!r} has wrong-typed key(s): {detail}")

        for entry in row["models"]:
            if isinstance(entry, str):
                continue
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                continue
            return SmokeCheck(
                name, False, f"provider {label!r} has an unrecognized model entry: {entry!r}"
            )

        extra_keys |= set(row) - set(_REQUIRED_ROW_KEYS)

    if extra_keys:
        shown = ", ".join(sorted(_sanitize(k) for k in extra_keys))
        print(
            f"[cadre] smoke: {name} note: extra key(s) observed beyond the documented "
            f"contract (benign growth, not drift): {shown}",
            file=sys.stderr,
        )

    return SmokeCheck(name, True)


def check_fleet_validation() -> SmokeCheck:
    """The same load-and-resolve pass ``cadre validate`` performs, in-process,
    over every packaged starter fleet.

    Iterates ``cadre/data/fleets/*.example.yaml`` — the release artifacts
    this package ships — calling ``FleetConfig.load`` + ``personas.resolve``
    exactly as ``cadre.cli.validate_command`` does (makes no model call).
    Persona references resolve against the PACKAGED pool
    (``cadre.resources.personas_dir()``), not ``~/.cadre/personas`` — see the
    module docstring for why. Read-only: never touches ``~/.cadre``.

    Globbing deliberately diverges from ``provision._STARTER_FLEETS``'s
    explicit allowlist: for a pre-release gate, a new fleet file failing loud
    beats a stale allowlist silently skipping it. (Only fleets live under
    ``data/fleets/`` today; the ``.example.yaml`` suffix scopes the glob.)

    Returns:
        A ``SmokeCheck`` — ``ok=True`` iff every packaged fleet loads and
        resolves clean; otherwise ``ok=False`` naming each failing file and
        its error.
    """
    name = "fleet-validate"
    paths = sorted(fleets_dir().glob("*.example.yaml"))
    if not paths:
        return SmokeCheck(name, False, f"no packaged starter fleets found under {fleets_dir()}")

    pool_dir = str(personas_dir())
    failures: list[str] = []
    for path in paths:
        try:
            cfg = FleetConfig.load(str(path))
            resolve(cfg, pool_dir)
        except ConfigError as exc:
            failures.append(f"{path.name}: {exc}")
        except FileNotFoundError:
            failures.append(f"{path.name}: not found")

    if failures:
        return SmokeCheck(name, False, "; ".join(failures))
    return SmokeCheck(name, True)


def check_preflight(fleet_path: str | Path | None = None) -> SmokeCheck:
    """Preflight the generated palette fleet against the live host palette.

    ``~/.cadre/fleets/palette-fleet.yaml`` (``fleet_path``'s default — the
    SAME path ``cadre.palette_fleet`` writes, via its public
    ``default_fleet_path()`` accessor, so the two can never drift apart) is
    DERIVED from a successful ``cadre
    verify-palette``: one lane per verified provider. It should therefore
    always preflight clean; a refusal here is real signal (the palette or the
    fleet went stale relative to each other), not a fixture gap.

    Read-only end to end: ``FleetConfig.load`` + ``resolve`` (a focus-only
    fleet — the palette fleet always is — resolves with zero I/O) +
    ``preflight_refusal`` (which itself only reads ``~/.cadre/palette.yaml``).
    Nothing is written.

    Returns:
        A ``SmokeCheck``. ``ok=True`` when the fleet loads and
        ``preflight_refusal`` returns ``None`` (would proceed). ``ok=False``
        with ``preflight_refusal``'s OWN refusal text verbatim when the fleet
        is off-palette or the live palette is absent/malformed (never
        invented prose — this module is not the source of that wording). A
        genuinely MISSING ``palette-fleet.yaml`` is a distinct, factual
        failure (it's a generated artifact — see ``cadre.palette_fleet``) --
        naming its own remedy (``cadre verify-palette``) rather than being
        forced through ``preflight_refusal`` with a fabricated config, which
        would risk misattributing the failure to a fake "off-palette model".
    """
    name = "preflight"
    path = Path(fleet_path).expanduser() if fleet_path is not None else default_fleet_path()

    if not path.exists():
        return SmokeCheck(
            name,
            False,
            f"{path} not found -- generated automatically by `cadre verify-palette` "
            "once 2+ providers verify (see docs/RUNBOOK.md); run it to generate this file",
        )

    try:
        cfg = FleetConfig.load(str(path))
        resolve(cfg, default_pool_dir())
    except ConfigError as exc:
        return SmokeCheck(name, False, f"{path} failed to load: {exc}")
    except FileNotFoundError:
        return SmokeCheck(name, False, f"{path} not found")

    refusal = preflight_refusal(cfg)
    if refusal is not None:
        return SmokeCheck(name, False, refusal)
    return SmokeCheck(name, True)


def main() -> int:
    """``cadre smoke``: run all three zero-spend checks and report.

    Prints one ``[cadre] smoke: <name> ... PASS`` / ``FAIL (<reason>)`` line
    per check to stderr as it goes (R9 — transient progress on stderr,
    mirroring ``discover.main`` / ``verify_palette.main``), then a single
    ``✓``/``✗`` verdict line to stdout. Returns (does not print via the
    ``cli.py`` ``(code, out)`` convention — like ``discover.main`` /
    ``verify_palette.main``, this owns its own printing since it has
    multiple per-check lines to emit, not one end-of-call message).

    Returns:
        ``ExitCode.SUCCESS`` (0) iff every check passed; ``ExitCode.ERROR``
        (1) if any failed. No new exit code — mirrors ``discover.main``'s
        convention of reusing the existing SUCCESS/ERROR pair for a
        single-verb entrypoint that has no third outcome.
    """
    checks = [
        check_inventory_shape(),
        check_fleet_validation(),
        check_preflight(),
    ]
    for check in checks:
        if check.ok:
            status = "PASS"
        else:
            # check.detail legitimately spans lines (preflight_refusal's
            # refusals and ConfigError's bullet list are both \n-joined), but
            # the per-check contract is ONE stderr line. Collapse whitespace
            # runs to single spaces BEFORE the single-line sanitize —
            # sanitize(multiline=False) DELETES newlines rather than spacing
            # them, which would fuse words across the join (e.g.
            # "checked.Fix:"). Safe to collapse: every untrusted fragment
            # inside those texts (role/provider/model/path) was already
            # individually sanitized at construction in preflight/config, so
            # the newlines reaching here are trusted structural ones.
            reason = " ".join(check.detail.split())
            status = f"FAIL ({_sanitize(reason)})"
        print(f"[cadre] smoke: {check.name} ... {status}", file=sys.stderr)

    passed = sum(1 for c in checks if c.ok)
    if passed == len(checks):
        print(f"✓ smoke: {passed}/{len(checks)} checks passed")
        return ExitCode.SUCCESS

    failed_names = ", ".join(c.name for c in checks if not c.ok)
    print(f"✗ smoke: {len(checks) - passed}/{len(checks)} check(s) failed: {failed_names}")
    return ExitCode.ERROR


if __name__ == "__main__":
    raise SystemExit(main())
