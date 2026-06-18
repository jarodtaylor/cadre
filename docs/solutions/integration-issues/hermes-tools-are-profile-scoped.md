---
title: "Hermes tools are profile-scoped — a fleet runs ungrounded if the active profile lacks the tool creds"
date: 2026-06-17
category: integration-issues
module: fleet_engine
problem_type: integration_issue
component: service_object
symptoms:
  - "Web/search specialist lanes silently produce ungrounded output (training knowledge), no error raised"
  - "Specialists self-disclose 'no live web access' / 'web search not configured' in their output"
  - "Model providers resolve fine, but the tools the lanes declared never actually fire"
root_cause: config_error
resolution_type: config_change
severity: high
tags: [hermes, profiles, hermes-home, tools, grounding, integration]
---

# Hermes tools are profile-scoped — a fleet runs ungrounded if the active profile lacks the tool creds

## Problem

A Cadre fleet's web-research specialist lanes silently produced ungrounded output — they answered from the models' training knowledge instead of performing live web search. No error was raised by the engine, the adapter, or Hermes. The only signal was internal: the fleet's own synthesizer flagged that "two specialists disclosed they had no live web access."

The root cause is that **Hermes tools are profile-scoped**. Hermes resolves config, auth, and tools from the directory identified by the `HERMES_HOME` environment variable. The default profile is `~/.hermes`; a named profile lives at `~/.hermes/profiles/<name>`. A library-mode `AIAgent(...)` call — which is exactly what the Cadre adapter (`fleet_engine/model_client.py`) makes — uses whichever profile `HERMES_HOME` points to, defaulting to `~/.hermes`.

Tools in Hermes are **check-fn gated**: a toolset such as `web` (backed by exa/firecrawl) or `x_search` only registers itself if the active profile's config holds that tool's credentials. The default profile had the model-provider entries (so all model lanes resolved without error) but did not have the exa/firecrawl credentials — those lived in a different profile. The specialist lanes requested the `web` toolset, it silently failed to register, and the agents answered from training data with no exception or warning surfaced up the call stack.

## Symptoms

- Web/search specialist lanes produce fluent, coherent, but ungrounded output — they draw on model training knowledge rather than live retrieval.
- No error is raised by the engine fan-out, the model-client adapter, or Hermes itself; `AIAgent(...)` succeeds.
- The fleet synthesizer or the specialists themselves may self-disclose "I don't have live web access" or "web search is not configured in my environment" — this is the primary (and often only) observable signal.
- Model providers resolve and respond correctly, so the failure is invisible at the network/auth layer.

## What Didn't Work

Two separate fixes were applied before the profile issue was identified, and neither restored grounding:

1. **Fixing model-provider resolution** — ensuring all provider OAuth entries were present in the active profile — corrected model dispatch but had no effect on tool availability, because provider resolution and tool registration are entirely separate axes in Hermes.
2. **Fixing a YAML config-corruption error** — the fleet YAML had a malformed field that triggered a parse-error in the engine's config module. Fixing that error was necessary, but the fleet still ran ungrounded after the config was clean. Config validity does not imply tool availability.

Neither fix addressed the core issue because tool credentials are a distinct, profile-scoped axis. A profile can fully authorize every model provider while having zero tool creds — and Hermes will register zero tools without complaint.

## Solution

Ensure the Hermes profile you run the fleet under holds **both** all the fleet's model-provider credentials **and** all the tool credentials required by the fleet's toolsets. There are two paths:

**(a) Add tool credentials to the default profile.** Copy the exa/firecrawl (or other tool) credentials into `~/.hermes/config` (or its equivalent for the default profile). This is the simplest fix when you have a single host with a single fleet.

**(b) Run with `HERMES_HOME` pointing at a fully-provisioned profile.**

```bash
HERMES_HOME=~/.hermes/profiles/<name> python -m fleet_engine.cli run fleets/research-swarm.yaml
```

Or, for skill invocations: a Hermes skill **inherits the invoking agent's profile** — hand the fleet skill to an agent that already has both providers and tool creds, and those capabilities transfer automatically.

## Why This Works

Hermes resolves its entire capability set (providers, auth, tools) from one profile directory at process startup. Because the Cadre engine fans out in threads within a single process, all specialist lanes share the same `HERMES_HOME` and therefore the same resolved toolsets. Once the active profile contains the tool credentials, the check-fn gate for `web` (and other toolsets) passes, the toolset registers, and agent lanes can invoke live retrieval as intended.

The key insight is that **model resolution and tool registration are independent axes in Hermes**. A successful `AIAgent(...)` call does not imply that the declared toolsets are available — it only means the model provider resolved. Tool availability is determined separately, earlier, during Hermes profile load.

## Prevention

- **When verifying a Hermes host, confirm grounding end-to-end, not just provider resolution.** Run a tool-bearing lane (e.g., a `web` specialist) and confirm it actually invokes a live search — check retrieval timestamps, citations, or any observable artifact of real tool use. A lane that "succeeds" with plausible output can still be ungrounded.
- **Treat the Hermes profile as the unit of capability.** Provider creds and tool creds must both be present in the same profile. When standing up a new host or profile, provision both in the same pass.
- **The design constraint:** a fleet runs under one Hermes profile (fan-out is threads in one process sharing one `HERMES_HOME`). Per-lane profiles would require subprocess isolation, which is a future feature. Until then, the active profile must be fully provisioned for every lane's needs.
- **Document the profile name** in the fleet YAML or the runbook. The fact that a fleet worked on one host/profile tells you nothing about another host where the profile may be partially provisioned.

## Related Issues

- [Isolate a volatile dependency behind a lazy-imported adapter](../architecture-patterns/lazy-import-adapter-for-volatile-dependencies.md) — the Hermes adapter pattern this fleet uses; tool-registration behavior is a second volatile Hermes semantic to track alongside the toolset-truthiness issue.
- [Empty toolset collapsed to all tools (privilege bypass)](../security-issues/empty-toolset-collapsed-to-all-tools.md) — a related Hermes tool-handling gotcha: `enabled_toolsets=None` means all tools, while `[]` means none. Both issues stem from Hermes's tool-registration layer being opaque without reading the source.
- Hermes's `HERMES_HOME` and profile-resolution behavior is documented in the vendored Hermes docs at `docs/reference/hermes/` (see `llms-full.txt`, HERMES_HOME/profile resolution section).
