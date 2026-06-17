#!/usr/bin/env python
"""U1 verification spike — confirm AIAgent provider/model resolution + failure mode.

THROWAWAY. Runs on the Hermes host with the Hermes venv Python:

    ~/.hermes/hermes-agent/venv/bin/python spikes/verify_aiagent_providers.py

Confirms, against your REAL Hermes auth, that:
  1. AIAgent(provider=..., model=..., api_key omitted) inherits your configured
     providers and returns output — for >=2 providers, >=1 non-Anthropic.
  2. A tool-enabled specialist actually invokes its tool (search creds wired).
  3. The exact exception type AIAgent raises on a deliberate failure — U2's catch
     logic targets this, not an assumed RuntimeError.

Edit PROVIDERS / TOOL_CHECK / FAILURE_CASE below with your real strings, then run.
If provider inheritance is REFUTED here, STOP and revisit the U2 adapter design
before building further.
"""

from __future__ import annotations

# (provider, model) pairs to verify. >=2 entries, >=1 non-Anthropic.
#   OAuth providers (xai, openai-codex, nous): provider + bare model id.
#   OpenRouter: provider="openrouter" + full vendor/model slug.
PROVIDERS: list[tuple[str, str]] = [
    # ("xai", "grok-4.3"),
    # ("openrouter", "google/gemini-3-flash"),
]

# One tool-enabled check: (provider, model, toolset, prompt-that-needs-the-tool).
TOOL_CHECK: tuple[str, str, list[str], str] | None = None
# e.g. ("openrouter", "google/gemini-3-flash", ["web"], "Search the web for today's top AI story.")

# Deliberate failure: a bad (provider, model) to capture the raised exception type.
FAILURE_CASE: tuple[str, str] = ("openrouter", "this/model-does-not-exist-xyz")


def _agent(provider: str, model: str, toolset: list[str] | None = None):
    from run_agent import AIAgent

    return AIAgent(
        provider=provider,
        model=model,
        # [] not None: in Hermes, enabled_toolsets=None enables EVERY toolset.
        enabled_toolsets=list(toolset) if toolset else [],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
    )


def main() -> int:
    if not PROVIDERS:
        print("Edit PROVIDERS (>=2, >=1 non-Anthropic) with your real strings first.")
        return 1

    print("=== provider / model resolution ===")
    for provider, model in PROVIDERS:
        try:
            text = _agent(provider, model).chat("Reply with the single word: ok")
            ok = bool(text and str(text).strip())
            print(f"[{'OK' if ok else 'EMPTY'}] {provider} / {model}: {str(text)[:60]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {provider} / {model}: {type(exc).__name__}: {exc}")

    if TOOL_CHECK:
        provider, model, toolset, prompt = TOOL_CHECK
        print("\n=== tool invocation ===")
        try:
            text = _agent(provider, model, toolset).chat(prompt)
            print(f"[tool {toolset}] {provider} / {model}: {str(text)[:120]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL tool] {type(exc).__name__}: {exc}")

    print("\n=== deliberate failure (records exception type for U2's catch) ===")
    provider, model = FAILURE_CASE
    try:
        _agent(provider, model).chat("hello")
        print("UNEXPECTED: bad model did not raise")
    except Exception as exc:  # noqa: BLE001
        print(f"AIAgent raised: {type(exc).__module__}.{type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
