"""cadre/discover.py — read Hermes's authenticated-provider inventory into
typed candidates, at zero cost (#61).

Caller-layer only: intended for ``cadre/cli.py`` (the ``cadre discover`` verb)
and ``cadre/provision.py`` (setup's candidate-seeding step) — both later
units. NEVER imported by ``engine.py`` or ``model_client.py`` — the engine
stays a pure, Hermes-free computation (``tests/test_personas.py``'s
``TestEngineIsolation`` guards this in both directions, mirroring
``approval.py``/``preflight.py``'s posture).

``discover_candidates()`` enumerates every authenticated provider Hermes knows
about and its curated model list — no model call, no spend (R1). It reads
``hermes_cli.inventory`` in-process via a lazy-import adapter
(``_fetch_inventory``, mirroring ``verify_palette._agent`` — see
docs/solutions/architecture-patterns/lazy-import-adapter-for-volatile-dependencies.md):
the import happens inside the function body, never at module import time, and
every failure (Hermes absent, or its internal surface having drifted) becomes
a typed ``DiscoveryError`` naming the manual hand-edit fallback (R10) rather
than a raw traceback. ``discover_candidates(payload=...)`` also accepts an
injected payload so tests never touch Hermes or the network.

Hermes's own aggregator row (the virtual "mixture of agents" provider,
``auth_type: "virtual"``) cannot back a fleet lane and is excluded; every
other authenticated row is included regardless of auth style — OAuth and
API-key providers are indistinguishable in the payload, and both count (R2).
A row that claims to be authenticated but does not fully parse into a
provider slug plus a non-empty model list is never silently dropped: it
raises ``DiscoveryError`` naming the row, and so does a payload with zero
authenticated providers after filtering — discovery never returns a partial
or empty result (R2/R10/KTD2).

``discover_candidates`` returns raw typed data only. ``write_candidates`` (U2)
turns a completed ``DiscoveryResult`` into the on-disk candidates file
``verify_palette._load_candidates`` already reads — same ``candidates:``/
``toolsets:`` schema, no change to that contract. Its write posture is the
canonical owner-only, symlink-refusing, parent-checked one
(``approval._write_owner_only`` — R13); regenerating an existing file prints
a loud stderr notice first (R4), since the file is discovery-owned and hand
edits are
about to be discarded. ``main`` is the ``cadre discover`` CLI entrypoint:
fetch, write, report — or degrade to one legible failure line (R6/R10); no
paid model call anywhere in this module. Discovered provider/model strings
are untrusted DISPLAY input only on the surfaces that print them to a
terminal (``main``'s summary line, routed through ``cadre.text_safety.sanitize``);
the YAML file content itself is written verbatim so it round-trips exactly.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from cadre.approval import _write_owner_only
from cadre.capture import resolved_hermes_home
from cadre.policy import Policy, PolicyError, Violation, load_policy, resolve_policy_path
from cadre.resources import palette_example_path
from cadre.text_safety import sanitize as _sanitize

# Every DiscoveryError ends with this so a failure reads as a next step, not a
# dead end (R10). Test scenarios assert on "palette-candidates"/"verify-palette".
_MANUAL_FALLBACK = (
    "Edit ~/.cadre/palette-candidates.yaml by hand with your authenticated "
    "providers and models, then run `cadre verify-palette`."
)


class DiscoveryError(Exception):
    """Raised whenever discovery cannot produce a full candidate set.

    Hermes not importable, its internal surface having drifted, or the
    payload shaped unexpectedly all land here — every message names the
    manual fallback. Discovery never returns a partial result: any row that
    cannot be fully parsed raises before ``providers`` is populated.
    """


@dataclass
class DiscoveredProvider:
    """One authenticated provider's curated models, in the inventory's own order."""

    provider: str
    models: list[str]


@dataclass
class DiscoveryResult:
    """A completed discovery pass: every authenticated, non-virtual provider
    found, plus the resolved Hermes profile the inventory came from."""

    providers: list[DiscoveredProvider]
    hermes_home: str


def _fetch_inventory() -> dict:
    """Lazy-import ``hermes_cli.inventory`` and fetch the raw payload.

    The import — and both calls — happen only here, never at module import
    time (KTD1), mirroring ``verify_palette._agent``. Hermes's internal
    surface is out of cadre's control, so ANY failure (ImportError,
    AttributeError, a drifted call signature, anything) becomes one typed
    ``DiscoveryError`` naming the manual fallback rather than an uncaught
    exception. ``from None``: the original exception's type/message is
    already folded into the DiscoveryError text, so chaining would only add
    a redundant second traceback (mirrors ``personas.resolve``'s same choice).
    """
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context  # noqa: PLC0415 — intentionally lazy

        ctx = load_picker_context()
        return build_models_payload(ctx, probe_custom_providers=False, picker_hints=True)
    except Exception as exc:  # noqa: BLE001 — any failure becomes one legible refusal
        raise DiscoveryError(
            f"could not read Hermes's provider inventory ({type(exc).__name__}: {exc}). "
            f"{_MANUAL_FALLBACK}"
        ) from None


def discover_candidates(payload: dict | None = None) -> DiscoveryResult:
    """Turn a Hermes inventory payload into typed candidates, or raise DiscoveryError.

    Args:
        payload: An already-fetched inventory payload (tests inject this so
            they never touch Hermes). ``None`` (the default) fetches a fresh
            payload via ``_fetch_inventory``.

    Returns:
        A ``DiscoveryResult`` whose ``providers`` preserves the payload's own
        provider order and, within each provider, its own model order.

    Raises:
        DiscoveryError: Hermes was not importable; the payload (or a row
            within it) is not shaped as expected; an authenticated row does
            not parse into a provider slug plus a non-empty model list; or
            zero authenticated, non-virtual providers remain after filtering.
    """
    if payload is None:
        payload = _fetch_inventory()

    if not isinstance(payload, dict):
        raise DiscoveryError(
            "Hermes's inventory payload is not a mapping "
            f"(got {type(payload).__name__}). {_MANUAL_FALLBACK}"
        )

    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raise DiscoveryError(
            "Hermes's inventory payload has no 'providers' list "
            f"(the internal surface may have drifted). {_MANUAL_FALLBACK}"
        )

    providers: list[DiscoveredProvider] = []
    for index, row in enumerate(raw_providers):
        if not isinstance(row, dict):
            raise DiscoveryError(
                f"Hermes provider row at index {index} is not a mapping "
                f"(got {type(row).__name__}). {_MANUAL_FALLBACK}"
            )

        if not row.get("authenticated"):
            continue  # not authenticated -- skipped, not an error (R2)
        if row.get("auth_type") == "virtual":
            continue  # Hermes's own aggregator row -- excluded, not an error (R2)

        slug = row.get("slug")
        if not isinstance(slug, str) or not slug:
            name = row.get("name")
            label = repr(name) if isinstance(name, str) and name else f"index {index}"
            raise DiscoveryError(
                f"an authenticated Hermes provider row ({label}) has no usable slug. "
                f"{_MANUAL_FALLBACK}"
            )

        raw_models = row.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise DiscoveryError(
                f"authenticated provider {slug!r} has no models list. {_MANUAL_FALLBACK}"
            )

        models: list[str] = []
        for entry in raw_models:
            if isinstance(entry, str) and entry:
                models.append(entry)
                continue
            if isinstance(entry, dict):
                model_id = entry.get("id")
                if isinstance(model_id, str) and model_id:
                    models.append(model_id)
                    continue
            raise DiscoveryError(
                f"authenticated provider {slug!r} has an unrecognized model entry "
                f"({entry!r}). {_MANUAL_FALLBACK}"
            )

        providers.append(DiscoveredProvider(provider=slug, models=models))

    if not providers:
        raise DiscoveryError(
            f"no authenticated providers were found on this Hermes host. {_MANUAL_FALLBACK}"
        )

    return DiscoveryResult(providers=providers, hermes_home=resolved_hermes_home())


# ---------------------------------------------------------------------------
# Candidates writer (U2) — turns a DiscoveryResult into the on-disk file
# verify_palette._load_candidates already reads. No change to that schema.
# ---------------------------------------------------------------------------

# The single home of the candidates-file location: verify_palette (the
# reader) imports this constant, so the writer and reader can't drift apart.
_DEFAULT_CANDIDATES_PATH = Path("~/.cadre/palette-candidates.yaml")


def default_candidates_path() -> Path:
    """Public accessor for the candidates-file location (un-expanded).

    For consumers OUTSIDE the candidates read/write pair (e.g. preflight's
    remedy probe) — importing the private constant across that boundary
    couples them to an internal detail (CodeRabbit #74).
    """
    return _DEFAULT_CANDIDATES_PATH

# Absolute last-resort toolsets if the packaged palette.example.yaml is ever
# unreadable (should not happen — it ships with the package). Kept separate
# from _default_toolsets so a broken package resource degrades to *something*
# usable rather than raising out of write_candidates.
_FALLBACK_TOOLSETS = ["web", "search", "x_search", "vision"]


def _existing_toolsets(path: Path) -> list[str] | None:
    """Read the ``toolsets:`` list out of an existing candidates file, or None.

    None means "nothing readable to carry over" — the file is absent,
    unreadable, not UTF-8, not valid YAML, not a mapping, or its ``toolsets``
    key is absent/null/a non-list scalar (guards the same char-iteration
    footgun ``verify_palette._load_candidates`` guards). A *present, readable*
    ``toolsets:`` list is returned as-is, including an explicitly empty
    ``[]`` — that is a meaningful operator choice, not a reason to fall back
    to the packaged default (KTD3: carry over "the old file's toolsets list
    if readable", not "if non-empty"). Mirrors
    ``verify_palette._load_candidates``'s tolerant-parse posture without
    importing that concurrently-developed sibling module.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    raw_toolsets = data.get("toolsets")
    if not isinstance(raw_toolsets, list):
        return None  # absent, null, or a scalar (e.g. `toolsets: web`) -- never char-iterated
    return [t for t in raw_toolsets if isinstance(t, str)]


def _default_toolsets() -> list[str]:
    """The packaged default toolsets list (``cadre/data/palette.example.yaml``).

    Used only on first generation — no prior candidates file to carry a
    declared toolsets list over from. Declared, not tool-probed (KTD3
    unchanged): a starting point the verify step still treats as unverified.
    """
    try:
        data = yaml.safe_load(palette_example_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return list(_FALLBACK_TOOLSETS)
    raw = data.get("toolsets") if isinstance(data, dict) else None
    if isinstance(raw, list):
        toolsets = [t for t in raw if isinstance(t, str)]
        if toolsets:
            return toolsets
    return list(_FALLBACK_TOOLSETS)


def write_candidates(
    result: DiscoveryResult,
    path: Path | str = _DEFAULT_CANDIDATES_PATH,
    *,
    create_only: bool = False,
) -> None:
    """Write ``result`` to ``path`` as the discovery-owned candidates file.

    Every discovered (provider, model) pair becomes one ``candidates:``
    entry, grouped per provider in the inventory's own curated order
    (KTD3) — under the EXISTING ``candidates:``/``toolsets:`` schema
    ``verify_palette._load_candidates`` already reads; this changes nothing
    about that schema, only how the file gets populated. ``toolsets:``
    carries over from a prior candidates file at ``path`` when one exists
    and parses (``_existing_toolsets``); otherwise it's seeded from the
    packaged default (``_default_toolsets``) — except in ``create_only``
    mode, where there is never a prior file to carry over from (see below).

    Default mode (``create_only=False``, the ``cadre discover`` CLI verb):
    regenerating an existing file prints a loud ``[cadre]``-prefixed stderr
    notice BEFORE overwriting (R4): the file is discovery-owned and hand
    edits are about to be discarded. A first-time write (no prior file)
    prints nothing extra — there is nothing to warn about losing yet.

    ``create_only=True`` (``cadre setup``'s discovery-seeding step, KTD6):
    creates with ``O_EXCL`` instead of ``O_TRUNC`` — if ``path`` already
    exists (a regular file, or a symlink; either way ``O_EXCL`` sees a
    dirent already there), the write raises ``FileExistsError`` instead of
    overwriting, and prints no notice (there is nothing to warn about
    losing when nothing is overwritten). This is a no-check-then-write
    race: the existence check IS the ``open()`` call, same atomicity as
    ``provision._seed_files``'s per-file guard. Callers that need to tell
    "preserved" apart from other write failures should catch
    ``FileExistsError`` before the broader ``OSError``.

    The write posture is otherwise identical between both modes, mirroring
    ``verify_palette.write_palette`` / ``approval.write_approval`` /
    ``provision.write_config``: owner-only parent (0o700, tightened
    umask), the ``_parent_is_safe`` guard against a foreign-owned or
    group/other-writable parent, ``O_NOFOLLOW`` so a symlink planted at
    ``path`` is refused rather than followed, and a 0o600 leaf written at
    creation (never a momentary default-umask mode). No new weaker write
    path (R13).

    The written YAML content is verbatim, UN-sanitized data (KTD9) — it
    must round-trip exactly through ``verify_palette._load_candidates``.
    Provider/model strings are untrusted DISPLAY input only where a caller
    (e.g. ``main``) prints them to a terminal; nothing in this function
    prints a provider/model string.

    Args:
        result: A completed discovery pass (``discover_candidates()``).
        path: Destination path for the candidates YAML (parent is created
            if missing). Defaults to ``~/.cadre/palette-candidates.yaml``.
        create_only: When True, never overwrite an existing ``path`` —
            raise ``FileExistsError`` instead (KTD6). Defaults to False
            (the ``cadre discover`` CLI verb's loud-overwrite posture).

    Raises:
        OSError: ``path``'s parent directory is a symlink, is not owned by
            the current user, or is group/other-writable
            (``_parent_is_safe``); or ``path`` itself is a symlink
            (``O_NOFOLLOW``) — in ``create_only`` mode a symlink at
            ``path`` instead raises ``FileExistsError`` (below), since
            ``O_EXCL`` sees the dirent before ``O_NOFOLLOW`` would matter.
        FileExistsError: ``create_only`` is True and ``path`` already
            exists (a subclass of ``OSError`` — catch it first if the
            distinction matters to the caller).
    """
    path = Path(path).expanduser()

    if create_only:
        # No prior file to carry a toolsets list over from: O_EXCL below
        # raises FileExistsError before this content is ever written if one
        # is already there, so there is nothing here to preserve or warn
        # about losing.
        toolsets = _default_toolsets()
    else:
        existed = path.exists()
        toolsets = _existing_toolsets(path)
        if toolsets is None:
            toolsets = _default_toolsets()

        if existed:
            print(
                f"[cadre] {path} already exists — regenerating it now. This file "
                "is discovery-owned: hand edits are discarded on every "
                "`cadre discover` run. To keep a hand-curated file instead, edit "
                "it directly and do not run `cadre discover` again.",
                file=sys.stderr,
            )

    candidates_data = [
        {"provider": provider.provider, "model": model}
        for provider in result.providers
        for model in provider.models
    ]

    header = (
        "# Generated by `cadre discover` — DO NOT hand-edit.\n"
        "# Re-running `cadre discover` OVERWRITES this file and discards any\n"
        "# changes made here by hand. To maintain a hand-curated candidates\n"
        "# file instead, edit it directly and do not run `cadre discover`\n"
        "# again (the pre-#61 manual workflow still works).\n"
        "#\n"
        f"# generated_at: {datetime.now().isoformat()}\n"
        f"# hermes_home:  {result.hermes_home}\n"
        "#\n"
        "# candidates: every authenticated (provider, model) pair Hermes\n"
        "#             reported, grouped per provider in its own curated\n"
        "#             order. Composed fleets should still only use pairs\n"
        "#             `cadre verify-palette` confirms.\n"
        "# toolsets:   declared safe toolsets for this profile, NOT\n"
        "#             tool-probed — see the generated palette.yaml's own\n"
        "#             header for the same caveat.\n"
    )
    body = yaml.safe_dump(
        {"candidates": candidates_data, "toolsets": toolsets},
        sort_keys=False,
        allow_unicode=True,
    )
    content = header + body

    # Create parent dir owner-only (mirrors write_palette / write_approval /
    # write_config's identical umask discipline).
    # Canonical owner-only posture (approval._write_owner_only): tightened
    # umask, 0o700 parent, _parent_is_safe, O_NOFOLLOW, 0600 leaf. create_only
    # swaps O_TRUNC for O_EXCL (KTD6): the open() itself is the atomic
    # existence check, raising FileExistsError rather than truncating a file
    # (or following a symlink) already there.
    _write_owner_only(path, content, exclusive=create_only)


# ---------------------------------------------------------------------------
# `cadre discover` CLI entrypoint (U2) + the #78 policy chokepoint
# ---------------------------------------------------------------------------


def _apply_policy(result: DiscoveryResult, policy: Policy) -> tuple[DiscoveryResult, list[Violation]]:
    """Split a discovery result into (policy-allowed, banned) — pure, no I/O.

    Preserves each surviving provider's own model order; a provider left with
    zero allowed models is dropped entirely, so ``main``'s summary line
    (provider count) stays accurate rather than counting a fully-denied
    provider that contributed nothing. Operates on the provider-grouped
    ``DiscoveredProvider`` shape directly (rather than round-tripping through
    ``cadre.policy.filter_pairs``' flat-pair-list shape, which
    ``cadre.verify_palette`` uses instead) since that shape is what this
    module already has and what it needs to reconstruct; both routes share
    the same underlying rule logic, ``Policy.check``.
    """
    kept_providers: list[DiscoveredProvider] = []
    violations: list[Violation] = []
    for provider in result.providers:
        kept_models: list[str] = []
        for model in provider.models:
            violation = policy.check(provider.provider, model)
            if violation is None:
                kept_models.append(model)
            else:
                violations.append(violation)
        if kept_models:
            kept_providers.append(DiscoveredProvider(provider=provider.provider, models=kept_models))
    return DiscoveryResult(providers=kept_providers, hermes_home=result.hermes_home), violations


def main() -> int:
    """``cadre discover``: fetch, filter by policy, write, report — or fail legibly.

    Fetches a fresh inventory from Hermes (no injected payload — this is the
    real CLI path), filters it against the local policy gate (#78,
    ``cadre.policy`` — a banned pair never reaches the candidates file), then
    writes what's left via ``write_candidates`` (which owns the loud
    discovery-owned overwrite notice, KTD6). A ``DiscoveryError`` — Hermes
    absent, unauthenticated, or its internal surface having drifted —
    degrades to its own legible message and exit 1, never a raw traceback
    (KTD10); a write-posture failure (an unsafe parent, a planted symlink)
    degrades the same way. A malformed policy file (``PolicyError``) ALSO
    degrades to exit 1, checked BEFORE the (free, but still worth skipping on
    a broken safety file) discovery fetch — a broken safety file must never
    silently mean no safety. If every discovered candidate is excluded by an
    otherwise-valid policy (zero survive ``_apply_policy``), ``main`` also
    degrades to exit 1 — refusing before ``write_candidates`` is ever called,
    so a previously-curated or discovery-owned candidates file already on
    disk is never clobbered with an empty result (mirrors
    ``verify_palette.main()``'s identical all-excluded guard). No new
    ``ExitCode``/``FailureReason`` member for
    discovery's own outcomes: returns the same integer values as
    ``cadre.exit_codes.ExitCode.SUCCESS`` (0) / ``.ERROR`` (1) as bare ints —
    mirroring ``verify_palette.main()``'s established convention of not
    importing the enum for a single-verb entrypoint (the
    ``FailureReason.POLICY_BLOCKED`` token lives on the PREFLIGHT refusal
    surface, not here).

    Transient progress goes to the ``[cadre]``-prefixed stderr stream (R9
    discipline, matching ``provision.py``'s seeding messages) — including
    each policy exclusion (pair + rule) and a trailing count; zero exclusions
    means zero extra noise, so a host with no policy file (or one that
    excludes nothing) sees byte-identical output to before #78. The final
    result — success summary or failure message — prints to stdout, matching
    every other verb's eventual stdout destination (``cli.py``'s shared
    ``print(out)``) even though this function, like
    ``verify_palette.main()``, owns its own printing rather than returning a
    tuple for ``cli.py`` to print. Discovered provider strings are untrusted
    display input (KTD9): the summary line sanitizes each one individually.
    """
    print("[cadre] discovering authenticated providers from Hermes...", file=sys.stderr)

    try:
        policy_path = resolve_policy_path()
        policy = load_policy(policy_path)
    except PolicyError as exc:
        print(_sanitize(str(exc)))
        return 1

    try:
        result = discover_candidates()
    except DiscoveryError as exc:
        # DiscoveryError text can embed payload-derived strings (slugs, entry
        # reprs, hermes exception text) — untrusted display input (KTD9).
        print(_sanitize(str(exc)))
        return 1

    result, violations = _apply_policy(result, policy)
    for v in violations:
        # v.provider/v.model come from the untrusted Hermes discovery payload
        # (KTD9 display input, same as elsewhere in this module); only
        # v.rule is derived from the local policy file. All three go through
        # the same sanitize() chokepoint regardless — one auditable call per
        # sink, not a source-dependent branch.
        print(
            f"[cadre] policy: excluded {_sanitize(v.provider)}/{_sanitize(v.model)} "
            f"({_sanitize(v.rule)})",
            file=sys.stderr,
        )
    if violations:
        print(f"[cadre] policy: {len(violations)} pair(s) excluded", file=sys.stderr)

    if violations and not result.providers:
        print(
            f"All {len(violations)} candidate(s) excluded by policy "
            f"({_sanitize(str(policy_path))}) — candidates file NOT written."
        )
        return 1

    path = _DEFAULT_CANDIDATES_PATH.expanduser()
    try:
        write_candidates(result, path)
    except OSError as exc:
        print(_sanitize(f"Cannot write {path}: {exc}"))
        return 1

    total_pairs = sum(len(p.models) for p in result.providers)
    provider_list = ", ".join(_sanitize(p.provider) for p in result.providers)
    print(
        f"Discovered {total_pairs} candidate(s) across {len(result.providers)} "
        f"provider(s) ({provider_list}) -> {path}\n"
        "Next: run `cadre verify-palette` to confirm which candidates "
        "actually resolve on this host."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
