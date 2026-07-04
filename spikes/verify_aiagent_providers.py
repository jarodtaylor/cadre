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

import contextlib
import io
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

# When run as a standalone script (`python spikes/verify_aiagent_providers.py` —
# how install.sh invokes it), Python puts spikes/ on sys.path[0], NOT the repo
# root, so write_palette's lazy `from fleet_engine.config import ...` fails with
# ModuleNotFoundError. Insert the repo root so it resolves whether run as a script
# or imported. Mirrors skills/cadre-fleet/run.py. (Dev tests never hit this: they
# run via `python -m unittest` from the repo root, which is already on sys.path.)
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
        ok, detail = _verify_one(provider, model)
        if ok:
            print(f"  ✓ {provider} / {model}")
        else:
            print(f"  ✗ {provider} / {model}  — skipped ({_short_reason(detail)})")
        records.append(VerifyRecord(provider=provider, model=model, ok=ok, detail=detail))
    return records


def _verify_one(provider: str, model: str) -> tuple[bool, str]:
    """Run one verification chat call, SILENCING the provider's own output.

    A candidate the host doesn't support (e.g. a model your provider doesn't
    offer) makes AIAgent dump a multi-line error to stdout/stderr — which reads
    like a crash even though a skipped candidate is a normal outcome. Capture
    that output (and mute logging) so verify_candidates can print one calm line
    instead. Set ``CADRE_VERIFY_VERBOSE=1`` to see the raw provider output.

    Returns ``(ok, detail)``; never raises.
    """
    def _call() -> object:
        return _agent(provider, model).chat("Reply with the single word: ok")

    # Verbose: stream the raw provider output (for debugging an unexpected result).
    if os.getenv("CADRE_VERIFY_VERBOSE"):
        try:
            text = _call()
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        ok = bool(text and str(text).strip())
        return ok, (str(text)[:60] if ok else "empty response")

    # Default: capture the provider's output + mute logging so a skip is one calm
    # line — then mine the capture for the real reason (AIAgent usually logs the
    # error and returns None rather than raising, so the bare result is just None).
    sink = io.StringIO()
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            text = _call()
        ok = bool(text and str(text).strip())
        if ok:
            return True, str(text)[:60]
        return False, _reason_from_capture(sink.getvalue()) or "empty response"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        logging.disable(logging.NOTSET)


def _short_reason(detail: str) -> str:
    """A terse, one-line skip reason for DISPLAY — the last ': '-delimited segment
    (strips noise like an emoji/'Error'/'HTTP 4xx' prefix). The full detail is
    kept in the record."""
    msg = detail.rsplit(": ", 1)[-1].strip()
    return (msg[:70] + "…") if len(msg) > 71 else msg


def _reason_from_capture(captured: str) -> str:
    """Mine a calm one-line reason from suppressed provider output (or '')."""
    lines = [ln.strip() for ln in captured.splitlines() if ln.strip()]
    # Prefer the most specific phrasing, then a generic error/status line.
    for needle in ("not supported", "unauthor", "forbidden", "denied",
                   "invalid", "quota", "rate limit"):
        for ln in lines:
            if needle in ln.lower():
                return ln
    for ln in lines:
        low = ln.lower()
        if "error:" in low or "http 4" in low or "http 5" in low:
            return _short_reason(ln)
    return ""


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
        if not isinstance(data, dict):
            # A non-mapping root (top-level list/scalar) has no candidates block;
            # `or {}` only catches None, so guard the type before calling .get().
            return PROVIDERS, []
        raw_candidates = data.get("candidates") or []
        candidates = [
            (c["provider"], c["model"])
            for c in raw_candidates
            if isinstance(c, dict) and c.get("provider") and c.get("model")
        ]
        raw_toolsets = data.get("toolsets") or []
        # Guard scalar toolsets: a bare string (e.g. `toolsets: web`) must NOT be
        # iterated char-by-char. Only accept a list; a scalar string is treated as
        # invalid and discarded.
        toolsets = [t for t in raw_toolsets if isinstance(t, str)] if isinstance(raw_toolsets, list) else []
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
            "  • Edit PROVIDERS in this file, or\n"
            f"  • Populate {candidates_path} with a 'candidates' list\n"
            "  (Run the install script to seed it from fleets/palette.example.yaml)"
        )
        return 1

    print(
        f"Verifying {len(candidates)} candidate(s) against this host — unsupported or\n"
        "unauthenticated ones are skipped (a skip is normal, not an error). Provider\n"
        "output is hidden; set CADRE_VERIFY_VERBOSE=1 to show it.\n"
    )
    records = verify_candidates(candidates)

    n_ok = sum(1 for r in records if r.ok)
    try:
        write_palette(records, declared_toolsets, palette_path)
    except ValueError as exc:
        print(f"\n✗ {exc}")
        return 1

    print(f"\n✓ {n_ok} of {len(records)} verified → {palette_path}")
    skipped = [f"{r.provider}/{r.model}" for r in records if not r.ok]
    if skipped:
        print(f"  skipped {len(skipped)}: {', '.join(skipped)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
