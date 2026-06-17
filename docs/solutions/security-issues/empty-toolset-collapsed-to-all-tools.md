---
title: "`list(x) or None` collapsed an empty toolset to ALL tools (privilege bypass)"
date: 2026-06-17
category: security-issues
module: fleet_engine
problem_type: security_issue
component: service_object
symptoms:
  - "Synthesizer and any specialist with an empty toolset received every Hermes toolset (terminal, file, browser, code_execution) instead of none"
  - "The fail-closed config allowlist was correct but silently overridden at the adapter boundary"
  - "An agent processing untrusted web content could be driven to privileged actions via prompt injection"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [security, privilege-escalation, prompt-injection, hermes, truthiness, python]
---

# `list(x) or None` collapsed an empty toolset to ALL tools (privilege bypass)

## Problem

The model-client adapter built each agent with `enabled_toolsets=list(toolset) or None`. An empty toolset (`[]` or `()`) is falsy, so `or None` rewrote it to `None` — and in Hermes, `enabled_toolsets=None` means "enable **every** toolset," while `[]` means "none." The idiom silently inverted the safest input into the most-privileged one.

## Symptoms
- Synthesizer (always built with no toolset) and any specialist with an empty toolset received every Hermes toolset — terminal, file, browser, code_execution — instead of none.
- The fail-closed config allowlist was correct but silently overridden at the adapter boundary.
- An agent processing untrusted web content could be driven to take privileged actions via prompt injection.

## What Didn't Work
- The earlier fix that converted the config gate from a denylist to a fail-closed allowlist was correct but **insufficient on its own**. The synthesizer is not a config specialist (it never passes the gate), and an empty specialist toolset passes the gate cleanly — then the adapter handed both `None`, i.e. all tools. Fixing only the config layer left the runtime hole open.

## Solution
Drop the `or None`; pass the list verbatim so `[]` stays `[]` (zero tools):

```python
# before
enabled_toolsets=list(toolset) or None,   # [] -> None -> ALL toolsets
# after
enabled_toolsets=list(toolset),           # [] -> [] -> no tools (fail-closed)
```

`[]` is the fail-closed allowlist-of-nothing — verified against hermes-agent source (`model_tools.py` resolves `None` via an `else: enable everything` branch, while `[]` takes the `is not None` branch and iterates nothing). The synthesizer now correctly runs tool-free.

## Why This Works
Removing the truthiness collapse stops the two semantically-opposite inputs (`[]` = none, `None` = all) from aliasing. The root cause was two things compounding: (1) a volatile dependency whose `None` default is "everything," and (2) the `x or None` idiom, which is only safe when the empty collection and `None` mean the same thing — here they mean the opposite.

## Prevention
- **Never use `x or None` (or `x or DEFAULT`) when the empty value and the default are semantically different.** A collection that must mean "none" has to be passed explicitly; collapsing it to a sentinel inverts intent.
- **Verify a volatile dependency's security-relevant defaults against its source, not assumption.** "No toolsets specified" defaulting to "all toolsets" is the opposite of a safe default, and only reading `model_tools.py` revealed it. (AIAgent is pre-1.0; vendored docs live at `docs/reference/hermes/`.)
- **Test the adapter's real boundary, not just the gate.** A regression test stubs `run_agent.AIAgent` and asserts the factory passes `enabled_toolsets=[]` (not `None`) for an empty/omitted toolset — the synthesizer's only protection.

## Related Issues
- [Fail-closed allowlist for capability gates](../design-patterns/fail-closed-allowlist-for-capability-gates.md) — the gate this bug bypassed at runtime.
- [Isolate a volatile dependency behind a lazy-imported adapter](../architecture-patterns/lazy-import-adapter-for-volatile-dependencies.md) — same adapter; this bug is why "isolate it" must be paired with "verify its semantics."
- Fixed in `fleet_engine/model_client.py` (`_default_agent_factory`); guarded by `tests/test_model_client.py` (`TestDefaultFactoryToolsetIsFailClosed`).
