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

This module returns raw typed data only. Sanitizing provider/model strings
for display is a later unit's CLI-sink job — nothing here touches
``cadre.text_safety``.
"""

from __future__ import annotations

from dataclasses import dataclass

from cadre.capture import resolved_hermes_home

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
