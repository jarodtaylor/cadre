#!/usr/bin/env python
"""U1 verification spike — confirm AIAgent provider/model resolution + failure mode,
then write the confirmed pairs to ~/.cadre/palette.yaml.

RUNS ON THE HERMES HOST with the Hermes venv Python:

    ~/.hermes/hermes-agent/venv/bin/python spikes/verify_aiagent_providers.py

---

## Two cleanly separated pieces

**(1) Live verification** — not testable on the dev machine (no hermes-agent).
    `verify_candidates(candidates)` calls AIAgent on the Hermes host and returns
    a list of VerifyRecord. `probe_toolsets(records, toolsets)` then live-probes
    each declared toolset with a strongly tool-forcing prompt via
    `run_conversation()` (never `chat()` — it returns only final text, which can
    never show a tool fire) and returns only the toolsets a tool call was actually
    observed for. Never call either here; the test suite patches `_agent`.

**(2) Pure writer** — fully testable here with fake records.
    `write_palette(records, proven_toolsets, path, declared_toolsets=...)` takes a
    record list + the toolsets `probe_toolsets` proved fire, filters to ok pairs +
    safe toolsets, warns by name on any declared toolset that didn't make it in,
    and writes owner-only 0o600 YAML.

---

## One-time host workflow

1. Edit CANDIDATES (or ~/.cadre/palette-candidates.yaml) with your real strings.
2. Run this spike via the Hermes venv — verifies models, live-probes declared
   toolsets, and writes ~/.cadre/palette.yaml.
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
# Live per-toolset probing (NOT testable on dev; runs on Hermes host)
#
# U7 (issue #5 Finding 3): a model resolving via verify_candidates() tells us
# NOTHING about whether its declared toolsets actually fire — Hermes tools are
# profile-scoped, so a model can answer fine while its web/search tool is
# unprovisioned, and a lane reading that toolset runs silently ungrounded (no
# error). This closes that gap with a live, forced-tool-call probe.
# ---------------------------------------------------------------------------

# Tailored tool-forcing prompts for the toolsets most relevant to the failure this
# probe exists to catch (a lane silently answering from training knowledge instead
# of grounding via a declared-but-unprovisioned web/search tool). Anything else
# falls back to _forcing_prompt's generic phrasing below — covers every other
# current SAFE_TOOLSETS member and degrades gracefully if that allowlist grows.
# Wording is best-effort; a live host dogfood (not a unit test) is the real
# reliability check.
_PROBE_PROMPT_OVERRIDES: dict[str, str] = {
    "web": (
        "Search the web right now for today's exact date and cite the source URL. "
        "You MUST call your web/search tool to do this; do not answer from memory."
    ),
    "search": (
        "Use your search tool right now to look up today's exact date and cite "
        "the source URL. You MUST call the tool; do not answer from memory."
    ),
    "x_search": (
        "Use your X (Twitter) search tool right now to find one real, recent post "
        "and quote it verbatim with its URL. You MUST call the tool; do not answer "
        "from memory."
    ),
}

# Small SAME-MODEL retry budget: guards the false-negative where a capable model
# just declines to call a tool on one attempt. This is NOT a cross-model sweep —
# see probe_toolsets() for why a single representative candidate is used per
# toolset (toolsets are profile-scoped, so one successful probe proves the tool
# is provisioned for the whole profile; KTD6).
_MAX_PROBE_ATTEMPTS = 2


def _forcing_prompt(toolset: str) -> str:
    """A strongly tool-forcing prompt for a single toolset probe.

    Uses the tailored override when one exists (_PROBE_PROMPT_OVERRIDES);
    otherwise falls back to a generic forcing phrasing that names the toolset.
    """
    if toolset in _PROBE_PROMPT_OVERRIDES:
        return _PROBE_PROMPT_OVERRIDES[toolset]
    return (
        f"Use your {toolset} tool right now and report concrete evidence that you "
        f"actually called it. You MUST call the {toolset} tool at least once; do "
        "not answer from memory or merely describe what you would do."
    )


def _has_tool_call_evidence(messages: object) -> bool:
    """Scan a run_conversation() ``messages`` history for a tool-call/tool-role entry.

    docs/reference/hermes/python-library.md confirms only the conversation-level
    shape: run_conversation() "returns a dictionary with the full response,
    message history, and metadata", whose ``messages`` key is "The complete
    message history (system, user, assistant, tool calls)". It does NOT document
    the per-message dict schema for a tool call — and a live call can't be run
    here to observe one — so this scans defensively for several known
    agent-framework shapes rather than assuming a single one: OpenAI-style (a
    "tool" role message, or an assistant message carrying a non-empty
    "tool_calls" list), the legacy single "function_call" dict, and
    Anthropic-style content-block items (a list "content" containing a dict whose
    "type" is "tool_use"/"tool_result"/"function_call"). Any one is accepted as
    fire evidence, including a bare tool-call *request* without yet seeing its
    result.

    That's deliberately not tightened to require a tool *result*: AGENTS.md
    verified-fact #1 records that Hermes runs a per-tool ``check_fn`` before a
    toolset ever reaches the model, so a model generally cannot even emit a call
    for a toolset it wasn't given — a bare request is already meaningful evidence,
    not a hallucinated intent. If a live dogfood ever shows a recorded-but-
    still-ungrounded toolset, tighten this to require a tool-result (role="tool")
    specifically.
    """
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in ("tool", "function"):
            return True
        if msg.get("tool_calls"):
            return True
        if msg.get("function_call"):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "tool_use",
                    "tool_result",
                    "function_call",
                ):
                    return True
    return False


def _probe_toolset(provider: str, model: str, toolset: str) -> bool:
    """Probe whether ``toolset`` actually fires for (provider, model) via a live,
    strongly tool-forcing call. Returns True only if a tool call was OBSERVED in
    the conversation history.

    Uses ``run_conversation()``, never ``chat()``: "chat() ... returns just the
    final text response" (python-library.md) — final text alone can never show a
    tool fired. ``run_conversation()``'s ``messages`` carries the full history
    including tool calls, which is the only place the fire signal can be
    observed. Call shape confirmed against the vendored guide's own example:
    ``agent.run_conversation(user_message=..., task_id=...)`` (python-library.md,
    "Full Conversation Control").

    Retries up to ``_MAX_PROBE_ATTEMPTS`` times — a fresh agent each attempt,
    mirroring the "one AIAgent per task, never shared" thread-safety guidance —
    before giving up. Fail-closed: any exception, or no tool-call evidence across
    the whole budget, returns False. This function never raises and never
    returns True on an error path.

    Output-suppressed exactly like _verify_one (redirect_stdout/stderr to a sink
    + logging.disable in a try/finally), so a multi-toolset sweep doesn't spam
    raw provider errors to the terminal.
    """
    prompt = _forcing_prompt(toolset)
    sink = io.StringIO()
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            for attempt in range(_MAX_PROBE_ATTEMPTS):
                try:
                    agent = _agent(provider, model, toolset=[toolset])
                    result = agent.run_conversation(
                        user_message=prompt,
                        task_id=f"cadre-probe-{toolset}-{attempt}",
                    )
                except Exception:  # noqa: BLE001 — fail-closed: try again, never crash the sweep
                    continue
                messages = result.get("messages") if isinstance(result, dict) else None
                if _has_tool_call_evidence(messages):
                    return True
    finally:
        logging.disable(logging.NOTSET)
    return False


def probe_toolsets(records: list[VerifyRecord], toolsets: list[str]) -> list[str]:
    """Live-probe each SAFE declared toolset and return only the ones PROVEN to
    fire (input order preserved).

    Probes against a single representative candidate — the first ok=True record
    — not every verified candidate. _probe_toolset's own retry budget already
    guards the single-model-declines false negative, and toolsets are
    profile-scoped (the palette's ``toolsets`` is one global list for the whole
    profile, not per-model — KTD6), so there is one fact to establish, not a
    per-model matrix. A toolset that never fires on that candidate is left out;
    write_palette then omits it with a warning. This is a deliberate,
    documented, conservative (false-negative-safe) trade-off — see SECURITY.md.

    Non-safe toolset names are skipped without probing (SAFE_TOOLSETS would drop
    them from the palette regardless of proof, so spending a live call on one
    would be wasted). Returns [] if there are no ok candidates, or no safe
    toolsets, to probe.
    """
    from fleet_engine.config import SAFE_TOOLSETS  # noqa: PLC0415

    ok_records = [r for r in records if r.ok]
    candidates = [t for t in toolsets if t in SAFE_TOOLSETS]
    if not ok_records or not candidates:
        return []

    provider, model = ok_records[0].provider, ok_records[0].model
    proven: list[str] = []
    for toolset in candidates:
        if _probe_toolset(provider, model, toolset):
            print(f"  ✓ toolset proven live: {toolset}")
            proven.append(toolset)
        else:
            print(f"  ✗ toolset NOT proven live: {toolset}  — will be omitted from the palette")
    return proven


# ---------------------------------------------------------------------------
# Pure writer (fully testable with fake records)
# ---------------------------------------------------------------------------


def write_palette(
    records: list[VerifyRecord],
    proven_toolsets: list[str],
    path: Path | str,
    *,
    declared_toolsets: list[str] | None = None,
) -> None:
    """Write the verified palette to ``path`` as owner-only YAML (0o600).

    Filters ``records`` to only ``ok=True`` pairs. Intersects ``proven_toolsets``
    with ``fleet_engine.config.SAFE_TOOLSETS``, preserving input order (list
    comprehension, not set intersection — order is locked by the downstream
    schema). Raises ValueError (before any filesystem side-effects) if there are
    zero ok records.

    ``proven_toolsets`` must already be the toolsets ``probe_toolsets`` observed
    firing live — NOT merely declared (U7/U8, issue #5 Finding 3). When
    ``declared_toolsets`` is also given, every declared-and-safe toolset that
    isn't in ``proven_toolsets`` is named in a loud warning — never silently
    dropped on an inconclusive probe. A toolset that's declared but not safe
    (e.g. ``terminal``) is dropped the same way it always was, silently — that's
    the separate, pre-existing privilege gate, not a probe outcome.

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
        proven_toolsets: Toolsets ``probe_toolsets`` proved fire live; non-safe
            names are dropped.
        path: Destination path for the palette YAML (parent is created if missing).
        declared_toolsets: The profile's originally declared toolsets, used only
            to name-and-warn about ones that didn't make it into the palette.

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

    # Order-preserving safe-toolset filter (NOT set intersection) — the
    # load-bearing allowlist still applies even to a toolset the live probe
    # proved fires.
    safe_toolsets = [t for t in proven_toolsets if t in SAFE_TOOLSETS]

    # Loudly name every declared-and-safe toolset that did NOT make it into the
    # palette — fail-closed: an inconclusive/unproven/errored probe omits, it
    # never guesses. (A declared-but-not-safe toolset, e.g. terminal, is
    # silently dropped as always — that's the unrelated privilege gate, not a
    # probe outcome, so it's excluded from this warning.)
    if declared_toolsets:
        omitted = [
            t for t in declared_toolsets
            if t in SAFE_TOOLSETS and t not in safe_toolsets
        ]
        for t in omitted:
            print(
                f"  ⚠ toolset '{t}' declared but NOT proven to fire live — "
                "omitted from the palette (a lane reading it would run silently "
                "ungrounded); provision it on this host/profile and re-run verify"
            )

    # Build the palette dict in the locked field order.
    palette = {
        "generated_at": datetime.now().isoformat(),
        "models": [{"provider": r.provider, "model": r.model} for r in ok_records],
        "toolsets": safe_toolsets,
    }

    # Honesty header at the point of use: models are live-verified AND each
    # listed toolset FIRED LIVE during a forced tool-call probe (probe_toolsets)
    # — not merely declared. A declared toolset the probe never observed firing
    # is omitted here and was already warned above by name. The one residual: a
    # provisioned tool a model simply DECLINES to call on this one forced probe
    # is indistinguishable from an unprovisioned one, so it is conservatively
    # omitted too (fail-closed, false-negative-safe — see SECURITY.md).
    header = (
        "# Cadre verified palette — generated by the verify step; do not hand-edit.\n"
        "# models:   provider/model pairs confirmed by a live chat call.\n"
        "# toolsets: each one FIRED LIVE in a forced tool-call probe (not merely\n"
        "#           declared) — see probe_toolsets. A declared toolset that\n"
        "#           never fired is OMITTED here (warned by name at verify time),\n"
        "#           so a lane reading a toolset below should not run silently\n"
        "#           ungrounded the way an unprobed declaration could.\n"
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

    if declared_toolsets:
        print(
            f"\nProbing {len(declared_toolsets)} declared toolset(s) live — a toolset\n"
            "that never fires a tool call in a forced probe is omitted (fail-closed),\n"
            "not merely skipped.\n"
        )
    proven_toolsets = probe_toolsets(records, declared_toolsets)

    try:
        write_palette(records, proven_toolsets, palette_path, declared_toolsets=declared_toolsets)
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
