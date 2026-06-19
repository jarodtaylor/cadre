#!/usr/bin/env python
"""U1 verification spike — confirm AIAgent provider/model resolution + failure mode,
then write the confirmed pairs to ~/.cadre/palette.yaml.

RUNS ON THE HERMES HOST with the Hermes venv Python:

    ~/.hermes/hermes-agent/venv/bin/python spikes/verify_aiagent_providers.py

---

## Two cleanly separated pieces

**(1) Live verification** — not testable on the dev machine (no hermes-agent).
    `verify_candidates(candidates)` calls AIAgent on the Hermes host and returns
    a list of VerifyRecord. Never call this here; the test suite patches `_agent`.

**(2) Pure writer** — fully testable here with fake records.
    `write_palette(records, toolsets, path)` takes a record list + declared toolsets,
    filters to ok pairs + safe toolsets, and writes owner-only 0o600 YAML.

---

## One-time host workflow

1. Edit CANDIDATES (or ~/.cadre/palette-candidates.yaml) with your real strings.
2. Run this spike via the Hermes venv — verifies + writes ~/.cadre/palette.yaml.
3. The cadre-fleet skill reads palette.yaml; compose only from its contents.

---

## Format notes

* OAuth providers (xai, openai-codex, nous): provider + bare model id.
* OpenRouter: provider="openrouter" + full "vendor/model" slug.
* NEVER put API keys here — credentials live in Hermes auth/env.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

# Default candidates file path (operator edits once after install seeds it).
_DEFAULT_CANDIDATES_PATH = Path("~/.cadre/palette-candidates.yaml")

# Default palette output path.
_DEFAULT_PALETTE_PATH = Path("~/.cadre/palette.yaml")

# ---------------------------------------------------------------------------
# Candidate pairs to verify on the host.
# Edit these (or use ~/.cadre/palette-candidates.yaml) before running.
# ---------------------------------------------------------------------------

PROVIDERS: list[tuple[str, str]] = [
    # ("xai", "grok-4.3"),
    # ("openrouter", "google/gemini-3-flash"),
]

# One tool-enabled check: (provider, model, toolset, prompt-that-needs-the-tool).
TOOL_CHECK: tuple[str, str, list[str], str] | None = None
# e.g. ("openrouter", "google/gemini-3-flash", ["web"], "Search the web for today's top AI story.")

# Deliberate failure: a bad (provider, model) to capture the raised exception type.
FAILURE_CASE: tuple[str, str] = ("openrouter", "this/model-does-not-exist-xyz")


# ---------------------------------------------------------------------------
# Record type
# ---------------------------------------------------------------------------


@dataclass
class VerifyRecord:
    """One verification outcome for a (provider, model) pair."""

    provider: str
    model: str
    ok: bool
    detail: str = field(default="")


# ---------------------------------------------------------------------------
# Live verification (NOT testable on dev; runs on Hermes host)
# ---------------------------------------------------------------------------


def _agent(provider: str, model: str, toolset: list[str] | None = None):
    """Build an AIAgent for verification. Import is lazy — never at module level.

    [] not None: in Hermes, enabled_toolsets=None enables EVERY toolset.
    An empty list (fail-closed zero tools) is the safe default for verification.
    """
    from run_agent import AIAgent  # noqa: PLC0415 — intentionally lazy

    return AIAgent(
        provider=provider,
        model=model,
        # [] not None: in Hermes, enabled_toolsets=None enables EVERY toolset.
        enabled_toolsets=list(toolset) if toolset else [],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
    )


def verify_candidates(candidates: list[tuple[str, str]]) -> list[VerifyRecord]:
    """Verify each (provider, model) pair via AIAgent and return structured records.

    Each pair gets a VerifyRecord(ok=True/False, detail=...). Empty response
    (None or blank) is treated as ok=False. Exceptions are caught and recorded.

    Args:
        candidates: List of (provider, model) pairs to verify.

    Returns:
        List of VerifyRecord, one per candidate, in input order.
    """
    records: list[VerifyRecord] = []
    for provider, model in candidates:
        try:
            text = _agent(provider, model).chat("Reply with the single word: ok")
            ok = bool(text and str(text).strip())
            detail = str(text)[:60] if ok else "empty response"
            print(f"[{'OK' if ok else 'EMPTY'}] {provider} / {model}: {str(text)[:60]!r}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
            print(f"[FAIL] {provider} / {model}: {detail}")
        records.append(VerifyRecord(provider=provider, model=model, ok=ok, detail=detail))
    return records


# ---------------------------------------------------------------------------
# Pure writer (fully testable with fake records)
# ---------------------------------------------------------------------------


def write_palette(
    records: list[VerifyRecord],
    toolsets: list[str],
    path: Path | str,
) -> None:
    """Write the verified palette to ``path`` as owner-only YAML (0o600).

    Filters ``records`` to only ``ok=True`` pairs. Intersects ``toolsets`` with
    ``fleet_engine.config.SAFE_TOOLSETS``, preserving input order (list
    comprehension, not set intersection — order is locked by the downstream
    schema). Raises ValueError (before any filesystem side-effects) if there are
    zero ok records.

    The written YAML has the locked schema::

        generated_at: "2026-06-18T14:30:00.123456"
        models:
          - provider: xai
            model: grok-4.3
          - provider: openrouter
            model: google/gemini-3-flash
        toolsets: [web, search, x_search, vision]

    Args:
        records: Verification records from ``verify_candidates`` (or test fakes).
        toolsets: Declared toolsets for this profile; non-safe names are dropped.
        path: Destination path for the palette YAML (parent is created if missing).

    Raises:
        ValueError: If there are no ``ok=True`` records (nothing to write).
    """
    # Import here to keep the lazy-import discipline consistent with _agent,
    # and so this module stays importable on the dev box without fleet_engine.
    from fleet_engine.config import SAFE_TOOLSETS  # noqa: PLC0415

    # Validate BEFORE any side effects — zero ok records → don't write anything.
    ok_records = [r for r in records if r.ok]
    if not ok_records:
        raise ValueError(
            "no verified providers — palette not written; "
            "check candidates and host auth"
        )

    # Order-preserving safe-toolset filter (NOT set intersection).
    safe_toolsets = [t for t in toolsets if t in SAFE_TOOLSETS]

    # Build the palette dict in the locked field order.
    palette = {
        "generated_at": datetime.now().isoformat(),
        "models": [{"provider": r.provider, "model": r.model} for r in ok_records],
        "toolsets": safe_toolsets,
    }

    # Honesty header at the point of use: models are live-verified, but toolsets
    # are only DECLARED + safe-filtered, never tool-probed (verify_candidates runs
    # a no-tool chat). A model can resolve while its search tool is unprovisioned,
    # so a lane reading that toolset can run silently ungrounded. The full fix is
    # host-side per-toolset probing (see RUNBOOK); until then this warns the reader.
    header = (
        "# Cadre verified palette — generated by the verify step; do not hand-edit.\n"
        "# models:   provider/model pairs confirmed by a live chat call.\n"
        "# toolsets: DECLARED safe toolsets, NOT tool-probed — provision each in the\n"
        "#           Hermes profile (see RUNBOOK) or a lane using it runs silently\n"
        "#           ungrounded (answers from training knowledge, with no error).\n"
    )
    content = header + yaml.safe_dump(palette, sort_keys=False, allow_unicode=True)

    path = Path(path)

    # Create parent dir owner-only (mirror capture.py's prepare_run_dir umask discipline).
    old_umask = os.umask(0o077)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Write owner-only at creation — never the momentary 0o644 a write-then-chmod
        # leaves under a default umask (mirrors capture._write exactly).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        path.chmod(0o600)
    finally:
        os.umask(old_umask)


# ---------------------------------------------------------------------------
# Main — glues verify → write on the Hermes host
# ---------------------------------------------------------------------------


def _load_candidates(candidates_path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Read candidate pairs and declared toolsets from the seed YAML.

    Returns (candidates, toolsets). Falls back to the module-level PROVIDERS
    if the seed file doesn't exist or has no candidates block.
    """
    if candidates_path.exists():
        data = yaml.safe_load(candidates_path.read_text(encoding="utf-8")) or {}
        raw_candidates = data.get("candidates", [])
        candidates = [(c["provider"], c["model"]) for c in raw_candidates if c]
        toolsets = data.get("toolsets", [])
        return candidates, toolsets
    # Fallback: module-level PROVIDERS (edit before running).
    return PROVIDERS, []


def main() -> int:
    candidates_path = _DEFAULT_CANDIDATES_PATH.expanduser()
    palette_path = _DEFAULT_PALETTE_PATH.expanduser()

    candidates, declared_toolsets = _load_candidates(candidates_path)

    if not candidates:
        print(
            "No candidates found. Either:\n"
            f"  • Edit PROVIDERS in this file, or\n"
            f"  • Populate {candidates_path} with a 'candidates' list\n"
            f"  (Run the install script to seed it from fleets/palette.example.yaml)"
        )
        return 1

    print(f"=== verifying {len(candidates)} candidate(s) ===")
    records = verify_candidates(candidates)

    if TOOL_CHECK:
        provider, model, toolset, prompt = TOOL_CHECK
        print("\n=== tool invocation ===")
        try:
            text = _agent(provider, model, toolset).chat(prompt)
            print(f"[tool {toolset}] {provider} / {model}: {str(text)[:120]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL tool] {type(exc).__name__}: {exc}")

    print(f"\n=== deliberate failure (records exception type for U2's catch) ===")
    fp, fm = FAILURE_CASE
    try:
        _agent(fp, fm).chat("hello")
        print("UNEXPECTED: bad model did not raise")
    except Exception as exc:  # noqa: BLE001
        print(f"AIAgent raised: {type(exc).__module__}.{type(exc).__name__}: {exc}")

    print(f"\n=== writing palette to {palette_path} ===")
    try:
        write_palette(records, declared_toolsets, palette_path)
        n_ok = sum(1 for r in records if r.ok)
        print(f"[OK] wrote {n_ok} verified provider(s) to {palette_path}")
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
