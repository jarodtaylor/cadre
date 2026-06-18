"""Model-client adapter — the only module that talks to Hermes's AIAgent.

Everything AIAgent-specific lives here: constructing a stateless agent for a
specialist, running one prompt, and turning any failure into a typed
``AgentResult``. The rest of the engine depends on this small surface and is
tested against a fake factory, so it imports and runs without ``hermes-agent``
installed. The real AIAgent import is lazy — only when a live agent is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

# A factory builds a live agent: given (provider, model, toolset) it returns an
# object exposing ``.chat(prompt) -> str``. Injecting it is what makes the engine
# testable without live calls — tests pass a fake; production uses the default.
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
            text = agent.chat(prompt)
        except Exception as exc:  # noqa: BLE001
            # Catch-all resilience boundary: any agent failure becomes a typed
            # failure so the fleet degrades rather than crashes. U1 (live, Hermes
            # host, 2026-06-17) found AIAgent does NOT raise on a non-retryable API
            # error — it logs and returns None — so the None/empty check below is the
            # PRIMARY dead-lane detector; this except covers the cases where chat()
            # does raise (e.g. agent construction). Both land on a typed failure.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        if text is None or not str(text).strip():
            # Empty/None/whitespace output is a failure, not a labeled success — it
            # must never reach the synthesizer as silent ungrounded provenance. This
            # is the PRIMARY failure path: U1 confirmed AIAgent returns None on a
            # failed/non-retryable provider call rather than raising.
            return AgentResult(
                role=role, provider=provider, model=model, ok=False,
                error="empty response from model",
            )

        return AgentResult(role=role, provider=provider, model=model, ok=True, text=str(text))
