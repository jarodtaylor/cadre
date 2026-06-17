---
title: Isolate a volatile dependency behind a lazy-imported adapter
date: 2026-06-17
category: architecture-patterns
module: fleet_engine
problem_type: architecture_pattern
component: service_object
severity: medium
applies_when:
  - "Depending on a pre-1.0 or fast-churning external library"
  - "The dependency needs credentials or a heavy runtime absent on dev machines"
  - "You want the package to import and unit-test without the dependency installed"
  - "Build and runtime happen on different machines"
tags: [adapter-pattern, lazy-import, dependency-isolation, testability, dependency-injection, fake-adapter, python, hermes]
---

# Isolate a volatile dependency behind a lazy-imported adapter

## Context

The fleet engine runs on Hermes's `AIAgent` library — pre-1.0 (`v0.16.0`), git-installed (not on PyPI), with a 60+ parameter constructor, and it needs the Hermes runtime plus authenticated providers to do anything. It isn't installed on the dev machine and can't easily be: that box's system Python is 3.14, newer than the library's `>=3.11,<3.14` pin, and it has no authed providers. Hermes lives only on a separate host (auto memory [claude]: dev happens on the M5 Max MBP; the live runtime is the VPS / Mac Mini "Hermes host").

A top-level `from run_agent import AIAgent` would make the entire engine unimportable — and therefore untestable — anywhere the dependency isn't installed, i.e. the whole dev machine.

## Guidance

Confine every touch of the volatile dependency to one thin adapter module, and do three things there:

1. **Lazy-import** the dependency *inside* the function that constructs the live object — never at module top.
2. **Inject the constructor** (an "agent factory") so tests pass a fake; the real lazy-importing factory is only the default.
3. **Convert the dependency's failures into typed results** at this boundary, so the rest of the system never sees its exceptions.

Everything else then depends on a tiny local interface, not on the dependency. The package imports and the full unit suite run with the dependency absent.

## Why This Matters

- **Testability without the dependency.** The orchestration logic is exercised against a fake — no network, no credentials, no install. Here the full suite (41 stdlib-`unittest` tests) runs on a machine that can't even install `hermes-agent`.
- **Clean dev/runtime split.** You develop and test where the dependency can't run, and run live only where it can — with no conditional-import branching.
- **Churn containment.** A pre-1.0 API lives behind one file; when it shifts, one file changes, not the whole codebase.
- **A natural home for resilience.** The boundary that absorbs import/availability failures is also where runtime failures become typed results feeding graceful degradation.

## When to Apply

- The dependency is pre-1.0 / churning, credentialed, or has a heavy or version-pinned runtime.
- You need the package to import and unit-test without it installed.
- Dev and runtime environments differ (different machine, Python version, or credentials).

Skip it for stable, ubiquitous, side-effect-free libraries — the indirection isn't worth it there.

## Examples

Before — untestable anywhere the dependency is missing:

```python
from run_agent import AIAgent  # top-level: import fails on dev machines

class ModelClient:
    def run(self, *, provider, model, prompt, toolset=()):
        agent = AIAgent(provider=provider, model=model)  # ...
        return agent.chat(prompt)
```

After — lazy import + injectable factory + typed failures:

```python
AgentFactory = Callable[[str, str, list[str]], Any]  # returns something with .chat(prompt) -> str

def _default_agent_factory(provider, model, toolset):
    from run_agent import AIAgent          # imported only when building a live agent
    return AIAgent(provider=provider, model=model, enabled_toolsets=toolset or None,
                   skip_memory=True, skip_context_files=True, quiet_mode=True)

class ModelClient:
    def __init__(self, agent_factory: AgentFactory | None = None):
        self._factory = agent_factory or _default_agent_factory   # tests pass a fake

    def run(self, *, role, provider, model, prompt, toolset=()):
        try:
            text = self._factory(provider, model, list(toolset)).chat(prompt)
        except Exception as exc:           # boundary: dep failures become typed results
            return AgentResult(role=role, provider=provider, model=model, ok=False,
                               error=f"{type(exc).__name__}: {exc}")
        return AgentResult(role=role, provider=provider, model=model, ok=True, text=text)
```

Tests inject a fake factory and never import the real dependency:

```python
client = ModelClient(agent_factory=lambda p, m, t: FakeAgent(reply="..."))
```

See `fleet_engine/model_client.py` and `tests/test_model_client.py`.

## Related
- Origin design: the MVP plan and `STRATEGY.md` (local planning docs).
- Deployment-topology memory (dev MBP vs Hermes host).
