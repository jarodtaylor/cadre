"""Model-client adapter — the only module that talks to Hermes's AIAgent.

Everything AIAgent-specific lives here: constructing a stateless agent for a
specialist, running one prompt, and turning any failure into a typed
``AgentResult``. The rest of the engine depends on this small surface and is
tested against a fake factory, so it imports and runs without ``hermes-agent``
installed. The real AIAgent import is lazy — only when a live agent is built.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from cadre.failure import FailureReason

# A factory builds a live agent: given (provider, model, toolset) it returns an
# object exposing ``.run_conversation(prompt) -> dict``. Injecting it is what makes
# the engine testable without live calls — tests pass a fake; production uses the
# default. (#76: the adapter reads the full result dict, never bare chat()
# text — chat() returns only ``final_response`` and discards the structured
# turn-outcome flags AND the usage/cost fields, which is exactly how an SDK-level
# failure whose error text came back as a normal assistant message passed as a
# healthy lane.)
AgentFactory = Callable[[str, str, list[str]], Any]


@dataclass
class AgentResult:
    """Outcome of running one agent (a specialist, or the synthesizer)."""

    role: str
    provider: str
    model: str
    ok: bool
    text: str | None = None
    error: str | None = None
    # Structured failure reason (TIMEOUT / SKIPPED / EMPTY_OUTPUT / MODEL_ERROR),
    # set at each failure's construction site; None on every success. See
    # __post_init__ for why the coercion below is conditional on non-None.
    reason: FailureReason | None = None
    # Capture signals — set by the engine at collection, never by ModelClient.run.
    # elapsed_s: wall-clock seconds from daemon launch to result collection.
    # toolset: the specialist's validated config toolset ([] means no tools).
    # timed_out: True only on the fabricated timeout result; False everywhere else.
    # skipped: True ONLY for a lane the sequential chain never ran because an upstream
    #   lane failed. Set by the chain executor (_run_chain, U3); default False. Distinct
    #   from timed_out (ran, hit the deadline) and from a real failure (ran, returned bad
    #   output) — a skipped lane never received a model call.
    elapsed_s: float | None = None
    toolset: list[str] = field(default_factory=list)
    timed_out: bool = False
    skipped: bool = False
    # Per-call usage/cost receipt (#76) — capture-don't-gate: recorded on success
    # AND on flagged failure (a failed call may still have burned tokens), and
    # NEVER consulted for ok/failure classification, exit codes, or convergence.
    # Sourced from the result dict when it carries the usage keys; when the dict
    # omits them (some early-failure paths) OR the call raised after the agent was
    # built, a fallback reads the agent instance's session counters and stamps
    # receipt_source: agent-session-counters (its ABSENCE means result-dict-sourced).
    # None only when neither source yields a receipt — the factory raised before an
    # agent existed, a non-dict return, a skipped/never-run lane, or an agent with
    # no readable counters. Zero values are recorded honestly as 0 (OAuth/quota-
    # billed rows may report 0 or nothing); cost_status/cost_source are the upstream
    # honesty qualifiers, passed through.
    usage: dict | None = None

    def __post_init__(self) -> None:
        # Normalize a raw-string reason to the FailureReason enum, mirroring
        # FleetStatus.__post_init__ (cadre/engine.py) so identity checks (`is`)
        # stay correct even when reason arrives as a manifest-serialized string.
        # Unlike status (never None), reason defaults to None on every successful
        # lane — an unconditional coerce would raise ValueError on every success,
        # so the coercion applies ONLY when a reason is actually present.
        if self.reason is not None:
            self.reason = FailureReason(self.reason)


def _default_agent_factory(provider: str, model: str, toolset: list[str]) -> Any:
    # Lazy import: hermes-agent lives only on the Hermes host, not on dev
    # machines. Importing here keeps the package importable and fake-testable
    # everywhere else. Provider + model are passed through as the config supplies
    # them (OpenRouter slug vs explicit provider + bare model); per-provider
    # format is the config's responsibility, not the adapter's.
    from run_agent import AIAgent

    return AIAgent(
        provider=provider,
        model=model,
        # Pass the toolset verbatim — NEVER collapse [] to None. In Hermes,
        # enabled_toolsets=None means "enable EVERY toolset" (terminal, file,
        # browser, code_execution, ...); [] means "no tools". An empty toolset (the
        # synthesizer, or a specialist configured with none) must get zero tools, not
        # the full privileged surface — [] is the fail-closed allowlist-of-nothing
        # that mirrors the config gate. Verified vs hermes-agent model_tools.py.
        enabled_toolsets=list(toolset),
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
    )


# Usage/cost whitelist (#76). Exact key names from the banked 2026-07-09 host
# probe (issue #76 comment; docs/reference/hermes/README.md fact 11):
# input_tokens / output_tokens / estimated_cost_usd / cost_status / cost_source.
# The probe also names cache + reasoning token COUNTERS without pinning their
# exact key strings, so token counters are matched structurally — any
# ``*_tokens`` key with a non-bool int value — rather than by guessed names.
# Every value is type-gated so a drifted upstream can't smuggle an arbitrary
# payload into the manifest under a known key; anything else in the result dict
# (messages, api_calls, ...) is deliberately NOT copied.
_USAGE_COST_NUMBER_KEYS = frozenset({"estimated_cost_usd"})
_USAGE_COST_STR_KEYS = frozenset({"cost_status", "cost_source"})
# Sanity ceiling for a token counter (folded review finding): a real count
# never approaches this; it only guards against a pathological upstream value.
_MAX_USAGE_INT = 10**15

# Agent-instance fallback candidate keys (folded cross-model review, 2026-07-10;
# docs/reference/hermes/README.md fact 11). Some early-failure run_conversation
# returns (repeated truncation / invalid-tool / compression paths) set the
# turn-outcome flags but OMIT the usage keys from the result dict, while the
# session-scoped counters remain on the AIAgent instance (Hermes's own gateway
# reads them off the agent after the call). When the result-dict pass yields no
# receipt, we probe these canonical names — and their ``session_``-prefixed
# variants — on the agent object as a fallback. Only the token names the probe
# banked by EXACT string (input/output) are probed: cache/reasoning counters were
# named by category only, so their instance-attribute names are unknown and are
# NOT guessed (a missing name reads None and is skipped; a wrong guess degrades to
# today's absent receipt, never invents data). Pending host re-verification.
_INSTANCE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "estimated_cost_usd",
    "cost_status",
    "cost_source",
)
# Cadre-authored honesty qualifier (NOT a hermes field): stamped on a receipt
# whose numbers came from the agent-instance fallback rather than the result
# dict. Its ABSENCE on a receipt means the numbers came from the result dict (the
# primary path) — so a manifest reader can tell recovered spend from reported spend.
_RECEIPT_SOURCE_AGENT = "agent-session-counters"

# Sentinel: a (key, value) pair is not a valid usage entry. Distinct from a
# stored value of None so _classify_usage_entry can accept any value type.
_SKIP = object()


def _is_safe_usage_number(value: int | float) -> bool:
    # A float must be JSON-finite (NaN/Inf are not valid JSON per RFC 8259);
    # an int must stay under a sane magnitude so a pathological upstream value
    # can't later blow past CPython's int-to-str conversion cap when the
    # manifest is serialized. Token counters and cost are inherently
    # non-negative, so a negative value is upstream drift too — dropped, not
    # gated, same as NaN/Inf or an oversized magnitude.
    if isinstance(value, float):
        return math.isfinite(value) and value >= 0
    return 0 <= value < _MAX_USAGE_INT


def _classify_usage_entry(key: str, value: Any) -> Any:
    """Return the value to store for one usage ``(key, value)``, or ``_SKIP`` to drop it.

    The single per-entry gate shared by the result-dict pass (``_usage_receipt``)
    and the agent-instance fallback (``_usage_from_agent``), so both apply identical
    type/sanity discipline. ``bool`` is excluded first (an int subclass, but a flag
    is never a counter); a token counter is a non-bool int matched by the
    ``*_tokens`` suffix; the cost keys are exact-named and type-gated; every number
    passes ``_is_safe_usage_number``. Strings (``cost_status``/``cost_source``) are
    stored raw and sanitized at the manifest sink (``capture._usage_for_manifest``),
    matching the pre-existing dict path. ``key`` is always a str at every call site.
    """
    if isinstance(value, bool):
        return _SKIP
    if key.endswith("_tokens") and isinstance(value, int) and _is_safe_usage_number(value):
        return value
    if key in _USAGE_COST_NUMBER_KEYS and isinstance(value, (int, float)) and _is_safe_usage_number(value):
        return value
    if key in _USAGE_COST_STR_KEYS and isinstance(value, str):
        return value
    return _SKIP


def _usage_receipt(result: dict) -> dict | None:
    """The whitelist-copied usage/cost subset of a result dict, or None if empty.

    Pure capture (#76): the receipt never influences classification. Zero values
    survive as 0 — dropping them would make a quota-billed lane's honest "0 cost
    accounted" indistinguishable from "no receipt at all". A value that fails
    the serialization sanity check (see _is_safe_usage_number) is dropped, not
    substituted — a missing receipt entry is capture-don't-gate honest.
    """
    usage: dict = {}
    for key, value in result.items():
        if not isinstance(key, str):
            continue
        stored = _classify_usage_entry(key, value)
        if stored is not _SKIP:
            usage[key] = stored
    return usage or None


def _read_agent_attr(agent: Any, name: str) -> Any:
    """Read one attribute off the agent, degrading any read failure to None.

    ``getattr(..., None)`` only suppresses ``AttributeError``; a pathological
    property could raise anything else. A counter that can't be read cleanly is
    simply treated as absent — this runs outside ``.run()``'s classification path,
    so it must never raise (a receipt is observational; classification is not).
    """
    try:
        return getattr(agent, name, None)
    except Exception:  # noqa: BLE001
        return None


def _usage_from_agent(agent: Any) -> dict | None:
    """Fallback receipt read from the AIAgent instance's session counters (#76).

    Used only when the result-dict pass yielded no receipt (some early-failure
    returns omit the usage keys) or the call raised after the agent was built.
    Probes the canonical usage names in ``_INSTANCE_USAGE_KEYS`` — each with its
    ``session_``-prefixed variant — reusing the SAME ``_classify_usage_entry`` gate
    as the dict path, and stamps ``receipt_source`` so a recovered receipt is
    honestly distinguishable from a reported one. Returns None when ``agent`` is
    None (the factory raised before an agent existed) or no counter reads cleanly.
    Never raises — every attribute read is guarded (``_read_agent_attr``).
    """
    if agent is None:
        return None
    usage: dict = {}
    for key in _INSTANCE_USAGE_KEYS:
        for attr in (key, f"session_{key}"):
            value = _read_agent_attr(agent, attr)
            if value is None:
                continue
            stored = _classify_usage_entry(key, value)
            if stored is not _SKIP:
                usage[key] = stored
                break  # first valid candidate (unprefixed preferred) wins
    if not usage:
        return None
    usage["receipt_source"] = _RECEIPT_SOURCE_AGENT
    return usage


def _flagged_failure_detail(result: dict) -> str | None:
    """The structured failure detail for a flagged result dict, or None if clean.

    Classification reads ONLY AIAgent's structured turn-outcome markers (#76):
    ``failed`` truthy, a non-empty ``error``, or ``interrupted`` truthy. The
    response text NEVER participates — a model legitimately QUOTING an error
    message (clean flags) must classify as success, so this must never grow a
    content check. ``turn_exit_reason`` is included as detail when present but
    is never a trigger by itself: successful turns carry an exit reason too.
    """
    failed = bool(result.get("failed"))
    interrupted = bool(result.get("interrupted"))
    error = result.get("error")
    has_error = error is not None and str(error).strip() != ""
    if not (failed or interrupted or has_error):
        return None
    parts = []
    if failed:
        parts.append("failed=True")
    if interrupted:
        parts.append("interrupted=True")
    if has_error:
        parts.append(f"error: {error}")
    exit_reason = result.get("turn_exit_reason")
    if exit_reason:
        parts.append(f"turn_exit_reason: {exit_reason}")
    return "AIAgent reported a failed turn — " + "; ".join(parts)


class ModelClient:
    """Runs a single agent and returns a typed result, never raising on model failure."""

    def __init__(self, agent_factory: AgentFactory | None = None):
        self._factory = agent_factory or _default_agent_factory

    def run(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
        toolset: Sequence[str] = (),
    ) -> AgentResult:
        # Bound before the try so the except branch can attempt the #76 usage
        # fallback iff an agent was actually constructed (stays None if the
        # factory itself raised — nothing to read).
        agent = None
        try:
            agent = self._factory(provider, model, list(toolset))
            # run_conversation, NOT chat (#76): chat() returns only
            # result["final_response"], discarding the structured turn-outcome
            # flags — so an SDK-level failure whose error text came back as a
            # normal assistant message counted as a healthy lane (false-verified
            # palette pairs, [ok] lanes, exit 0). If this method is absent or its
            # signature drifts (AIAgent is volatile), the call raises into the
            # boundary below and fails LOUD as MODEL_ERROR — never a silent
            # downgrade back to chat().
            result = agent.run_conversation(prompt)
        except Exception as exc:  # noqa: BLE001
            # Catch-all resilience boundary: any agent failure becomes a typed
            # failure so the fleet degrades rather than crashes. Covers agent
            # construction errors and any raise out of run_conversation (e.g. the
            # stale-response/client timeouts, which raise into the call). If the
            # agent was built before the raise, recover any burned spend from its
            # session counters (#76 fallback); the receipt is observational and
            # _usage_from_agent never raises, so it cannot affect this failure.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error=f"{type(exc).__name__}: {exc}", reason=FailureReason.MODEL_ERROR,
                usage=_usage_from_agent(agent),
            )

        if not isinstance(result, dict):
            # None or any non-dict return carries no structured flags and no text —
            # byte-compatible with the pre-#76 None path (the U1 2026-06-17 live
            # observation: AIAgent logs a non-retryable provider error rather than
            # raising). A drifted surface that stopped returning dicts lands here
            # too: no usable output is a failure, never a guessed success. No
            # agent-counter fallback here — the banked None shape is provider-
            # unusable-at-resolution (pre-call, no spend); the #76 fallback
            # deliberately covers only the dict-omits-usage and raised-after-
            # construction paths named by the finding.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error="empty response from model", reason=FailureReason.EMPTY_OUTPUT,
            )

        # Receipt extracted once, stamped on EVERY dict-shaped outcome below —
        # success, flagged failure, and empty output all may have burned tokens.
        # Dict-first, then an agent-instance fallback (#76): some early-failure
        # returns omit the usage keys from the dict while the session counters
        # remain on the agent, so when the dict yields nothing we recover them from
        # the agent (marked receipt_source: agent-session-counters). Dict wins when
        # present — the fallback only runs on a None dict-pass result.
        try:
            usage = _usage_receipt(result)
            if usage is None:
                usage = _usage_from_agent(agent)
        except Exception:  # noqa: BLE001
            # Resilience boundary: a pathological result shape must degrade the receipt, not raise out of .run() (verify_palette._verify_one would abort its whole loop).
            usage = None

        try:
            failure = _flagged_failure_detail(result)
        except Exception:  # noqa: BLE001
            # Same boundary; a crash here implies the dict already looked failure-shaped, so this keeps ok/reason classification unchanged — only the detail text degrades.
            failure = "structured failure flags present (detail unavailable)"
        if failure is not None:
            # Structured flags take precedence over everything, including a
            # non-empty final_response: on this path the text is AIAgent's own
            # error prose (the post-loop "I apologize, but I encountered repeated
            # errors: ..." fall-through), not an answer — it must never reach a
            # consumer as one. error carries the structured detail, never the text.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error=failure, reason=FailureReason.MODEL_ERROR, usage=usage,
            )

        text = result.get("final_response")
        if text is None or not str(text).strip():
            # Clean flags but no usable text (missing/None/whitespace
            # final_response) is a failure, not a labeled success — it must never
            # reach the synthesizer as silent ungrounded provenance.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error="empty response from model", reason=FailureReason.EMPTY_OUTPUT,
                usage=usage,
            )

        return AgentResult(
            role=role, provider=provider, model=model, ok=True, text=str(text),
            usage=usage,
        )
