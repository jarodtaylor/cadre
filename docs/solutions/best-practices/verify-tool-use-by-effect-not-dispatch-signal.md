---
title: "Native tool use is invisible to dispatch-side detection — verify the effect, not the mechanism"
date: 2026-07-03
category: best-practices
module: fleet_engine
problem_type: best_practice
component: development_workflow
severity: medium
applies_when:
  - "You need to know whether a model/agent capability (a tool, a retrieval, a side effect) actually fired"
  - "The capability can be satisfied either by a call you dispatch OR by a provider's server-side / native integration"
  - "Your detection signal is a mechanical proxy — a round-trip count, a tool-call message, a log entry — not the capability's observable effect"
  - "A false 'it did not fire' would drop or disable something that actually works"
tags: [hermes, tool-detection, native-integration, grounding, proxy-signal, read-the-source, verification, dogfood]
---

# Native tool use is invisible to dispatch-side detection — verify the effect, not the mechanism

## Context

Cadre's palette builder records the toolsets a Hermes profile *declares*, but a declared-but-unprovisioned toolset (a `web` lane with no exa/firecrawl creds) answers from training knowledge with no error — silently ungrounded (see [Hermes tools are profile-scoped](../integration-issues/hermes-tools-are-profile-scoped.md)). Issue #48 (Part 2 of the trust-safety pass, PR #50) tried to close that gap with a **live per-toolset probe**: send a tool-forcing prompt, then decide "did it fire?" by scanning `run_conversation()["messages"]` for a tool-call / `role:tool` entry.

A paid host dogfood on `silas` proved the fire-signal is *structurally* wrong. Grok's **native** web search grounded the answer — real live date, cited URLs that training data cannot produce — but returned in a single round-trip, `finish_reason=stop`, with **no tool-call message**. The probe false-negatived `web` and would have *dropped a working toolset* from the palette — strictly worse than the declared baseline. `x_search` (an explicitly dispatched tool: `finish_reason=tool_calls`, tool messages present) detected correctly. Same probe, opposite verdicts, decided entirely by *how the provider happens to implement the capability*.

A free source read of Hermes (`agent/conversation_loop.py`, `run_agent.py` on the host) explained why, and set the ceiling: `run_conversation()` returns only `{final_response, messages, api_calls, ...}`. `api_calls` is a **round-trip counter** — a natively-grounded answer is one round-trip, identical by that count to answering from memory. The raw provider `usage` payload (which might carry a native-tool indicator such as a sources count) is normalized to bare token/cost counters — and nothing tool-shaped survives that normalization. *(Correction, 2026-07-10 / #76: the counters themselves — `input_tokens`, `output_tokens`, cache/reasoning counts, `estimated_cost_usd`, `cost_status`, `cost_source` — ARE returned on the result dict; the original "dropped from the return dict" reading was true of `chat()`, which discards everything but `final_response`. The ceiling this doc argues from is unchanged: no field distinguishes native grounding from a memory answer.)* So no mechanical signal Hermes exposes distinguishes "grounded via a server-side tool" from "answered from memory."

## Guidance

**A dispatch-side signal is a proxy for "the capability fired," and the proxy breaks the moment the capability moves server-side.** Counting API round-trips, scanning a message history for tool-call entries, or watching for a `role:tool` turn all detect a *locally dispatched* tool — one the orchestration layer executes itself. A provider that integrates the same capability natively (its own web search, code execution, retrieval) satisfies the request inside a single completion and emits none of those markers. The capability worked; your detector says it did not.

To know whether a capability actually fired, **verify its observable effect, not the mechanism that would have dispatched it:**

- The effect lives in the *output*, not the transcript shape. For grounding: does the answer contain something un-memorizable — today's real date, a fresh URL, a fact past the training cutoff? That survives native integration because it is a property of the result, not of how the result was produced.
- Effect-verification is fuzzier than a mechanical check, and that is the honest trade: a wrong "not grounded" verdict re-introduces the exact false-drop the mechanical probe caused. When the effect-check is not reliable enough to *gate* on, do not gate — record the capability as declared and warn, rather than omitting a possibly-working one. **Fail toward the honest baseline, not toward silent removal.**

And, upstream of all of it: **read the framework's source to find the detection ceiling before you build or pay for a probe.** Here a `$0` host source-read of `run_conversation` established that no return-dict field could ever carry the native-fire signal — collapsing a planned multi-provider *paid* characterization dogfood into a free investigation, and turning a "how do we build it" question into "it cannot be built this way; here is the honest landing."

## Why This Matters

The failure is asymmetric and silent. A dispatch-detector that false-negatives a natively-integrated tool does not error — it quietly *removes a working capability* (drops the toolset, disables the feature, marks the lane ungrounded) while reporting success. You ship a "verified" palette that is missing tools that actually work, and you trust it more *because* it claims to be verified. That is worse than the un-verified baseline it replaced.

It also generalizes past Hermes and past grounding. Any time you infer "did X happen?" from a mechanical artifact of *one* implementation of X — a retry counter, a log line, a spawned-subprocess check, a specific message shape — you have coupled your detector to that implementation. The day a dependency satisfies X a different way (server-side, batched, cached, inlined), the detector silently lies. The durable check is against X's *effect*.

## When to Apply

- Deciding whether a tool / retrieval / capability fired, when a provider *might* satisfy it natively rather than through a call you dispatch.
- Any "did it actually work?" verification where your signal is a proxy (a count, a message shape, a log entry) rather than the observable result.
- Before building or paying for a detection probe: read the source to confirm the signal you plan to key on can even carry the answer across the implementations you will face. A native-integration case makes "no local dispatch" indistinguishable from "no capability" at the mechanism layer.

## Examples

The probe that broke — a mechanical scan of the transcript shape (reverted; preserved in history at `fe4954c`):

```python
# Decides "fired" from the message history alone.
# True for x_search (dispatched: role:tool present); FALSE for grok's native web
# (server-side: [user, assistant], finish_reason=stop, no tool message) — even
# though the answer is grounded with live, cited data.
def _has_tool_call_evidence(messages) -> bool:
    for msg in messages:
        if msg.get("role") in ("tool", "function"): return True
        if msg.get("tool_calls") or msg.get("function_call"): return True
        # ... content-block tool_use / tool_result variants ...
    return False
```

The ceiling, established for free by reading the source (why no fix to the scan helps):

```
run_conversation() -> {final_response, messages, api_calls, completed, failed, error}
  api_calls  = round-trip COUNTER   (native grounding = 1 = a memory answer)
  messages   = only DISPATCHED tools leave a tool-call / role:tool entry
  usage      = normalized to token/cost counters — returned on the dict, but
               nothing tool-shaped survives (chat() discards even the counters;
               corrected 2026-07-10, #76)
∴ no return-dict field distinguishes native grounding from a memory answer.
```

The honest landing (fail toward the baseline; do not gate on a fuzzy effect-check): the palette stays *declared-and-warned* rather than *omit-unproven* — a working native toolset is never dropped, and the un-verifiable gap is disclosed, not hidden (`SECURITY.md`, "Palette toolsets are declared, not live-verified").

## Related

- [Hermes tools are profile-scoped](../integration-issues/hermes-tools-are-profile-scoped.md) — the *other* reason a lane silently answers ungrounded (a config/profile-scoping gap you can fix vs. this doc's structural-API blindness you cannot). Both are facets of "how do you know a toolset actually worked?"; its Prevention already hints at the answer — "a lane that 'succeeds' with plausible output can still be ungrounded."
- [In-band marker nonce defends echo, not injection](../design-patterns/in-band-marker-nonce-defends-echo-not-injection.md) — the sibling honest-scoping discipline: state precisely what a signal does *not* prove, right next to what it does. Here the message-scan "proves tool-fired" only for *dispatched* calls; there the nonce "proves authentic" only against blind echo.
- [Specialist focus is a grounding control](../design-patterns/specialist-focus-grounding-control.md) — the orthogonal, prompt-level grounding guard (make the focus demand citation) that operates whether or not the tool fired; complementary to verifying the effect.
- [Prove a threaded primitive actually threads](prove-a-threaded-primitive-actually-threads.md) — the same "verify the real effect, not a proxy for it" discipline; there a discriminating effect-test existed (round-1 vs round-2 citations), here none does.
- GitHub issue #48 (investigation close) · issue #5 Finding 3 · PR #50 · `spikes/verify_aiagent_providers.py`, `fleet_engine/model_client.py` — the concrete instance this generalizes from.
