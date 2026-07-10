"""Model-client adapter — the only module that talks to Hermes's AIAgent.

Everything AIAgent-specific lives here: constructing a stateless agent for a
specialist, running one prompt, and turning any failure into a typed
``AgentResult``. The rest of the engine depends on this small surface and is
tested against a fake factory, so it imports and runs without ``hermes-agent``
installed. The real AIAgent import is lazy — only when a live agent is built.
"""

from __future__ import annotations

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
    # None when the call produced no result dict at all (exception path, timeout,
    # skipped lane) or the dict carried no whitelisted usage keys. Zero values are
    # recorded honestly as 0 (OAuth/quota-billed rows may report 0 or nothing);
    # cost_status/cost_source are the upstream honesty qualifiers, passed through.
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


def _usage_receipt(result: dict) -> dict | None:
    """The whitelist-copied usage/cost subset of a result dict, or None if empty.

    Pure capture (#76): the receipt never influences classification. Zero values
    survive as 0 — dropping them would make a quota-billed lane's honest "0 cost
    accounted" indistinguishable from "no receipt at all".
    """
    usage: dict = {}
    for key, value in result.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool):
            continue  # bool is an int subclass; a flag is never a counter
        if key.endswith("_tokens") and isinstance(value, int):
            usage[key] = value
        elif key in _USAGE_COST_NUMBER_KEYS and isinstance(value, (int, float)):
            usage[key] = value
        elif key in _USAGE_COST_STR_KEYS and isinstance(value, str):
            usage[key] = value
    return usage or None


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
            # stale-response/client timeouts, which raise into the call).
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error=f"{type(exc).__name__}: {exc}", reason=FailureReason.MODEL_ERROR,
            )

        if not isinstance(result, dict):
            # None or any non-dict return carries no structured flags and no text —
            # byte-compatible with the pre-#76 None path (the U1 2026-06-17 live
            # observation: AIAgent logs a non-retryable provider error rather than
            # raising). A drifted surface that stopped returning dicts lands here
            # too: no usable output is a failure, never a guessed success.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error="empty response from model", reason=FailureReason.EMPTY_OUTPUT,
            )

        # Receipt extracted once, stamped on EVERY dict-shaped outcome below —
        # success, flagged failure, and empty output all may have burned tokens.
        usage = _usage_receipt(result)

        failure = _flagged_failure_detail(result)
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
