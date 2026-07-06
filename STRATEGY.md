---
name: Cadre
last_updated: 2026-07-05
---

# Cadre Strategy

## Target problem

Multi-model work has no good home. Hermes's `delegate_task` can't pick a model per task; single-vendor runtimes (e.g. Claude Code) fan out only within one provider's tiers; and a one-off script isn't a tool — no reusable patterns, no DX, no path to other use cases. The value left on the table: routing the right subtask to the right *provider*, then synthesizing one grounded, attributed answer.

## Our approach

Cadre is the **provider-neutral fabric for safe, agent-driven multi-model orchestration.** Fan any task across whatever models you have (cloud APIs, OAuth subscriptions, OpenRouter, local) — no single-runtime lock-in — let an *agent* compose and run the fleet, but require a human to **approve the *parsed* fleet before anything executes** (the load-bearing trust control). New fleets are *configuration* on a small set of composable primitives (fan-out→synthesize, collect, sequential, iterative, judge), never new code.

The bet deliberately rules out both single-vendor fan-out *and* autonomous orchestration with no human gate. And because the day-to-day **operator is an AI agent**, not a human at a keyboard, the product optimizes for **correctness DevEx** — fail loud, early, and legibly, with fewer manual steps — over ergonomic polish. Hermes-first (single-runtime); runtime-agnostic reach is the **northstar, not the near-term**.

## Who it's for

- **Primary (now):** the author — dogfooding a multi-provider research/work tool, driven by an agent and gated by his approval. The job: *fan a task across the best models I have and get back one grounded, attributed, auditable answer — without hand-writing orchestration.*
- **Eventual:** OSS users who want multi-provider fleets without writing orchestration boilerplate.

## What "good" looks like

- A fleet's synthesized output visibly **beats a single strong model** on real tasks (human gut-check).
- You **reach for the engine instead of hand-writing a script**.
- Adding a new fleet — even an unforeseen shape — is **config only**, never "that's not a pattern."
- Output is **auditable**: each specialist's raw output preserved, each synthesized claim traceable to the model that surfaced it, silent failures (a lane that ran without grounding) made visible. *Shipped (run-capture) — it earned its keep: a grounding-deficient lane was caught only because the evidence persisted.*
- An **agent can drive it safely**: it shows the human the *parsed* fleet and waits for approval before any model runs. *Proven live — the v0 gate.*
- A **stranger can follow the README cold and it works** — no tribal knowledge. *Validated 2026-07-05: a clone-hidden, uninstalled cold run passed.*

## Tracks

The four durable investment domains. **v0 shipped the foundation** (`v0.1.0`, 2026-07-05): the engine (fan-out→synthesize plus collect / sequential / iterative / judge, a fail-closed safe-toolset allowlist, per-call timeout, degrade-and-report), run-capture & auditability, the Hermes **agent-handoff**, trust-safety pass 1, and packaging as `cadre`. **The V1 scope below = harden the single-runtime Hermes alpha.**

### Operator & agent experience

Make every run legible and trustworthy to its two operators — the human at the approval gate and the agent driving the fleet. *V1: correctness-first DevEx — fail loud/early/legibly, fewer manual steps (palette auto-discovery, `cadre setup` PATH helper, RUNBOOK consumer fixes, structured signals when a lane/model fails).*

_Why it serves the approach:_ both operators can only trust what they can watch happen and act on clear signals — the agent-first frame made concrete.

### Fleet library & primitives

Make "new fleets = config" pay off. The primitives already shipped; V1 makes them usable and populated, not new. *V1: batteries-included starter fleets fleshed out, fleet-authoring ergonomics, research-tools as a scoped toolset.*

_Why it serves the approach:_ composability + adoption — ready-made for the many, agent-built for the few.

### Trust & safety hardening

Keep the human-gate's preview-binding code-enforced and the alpha safe under agent operation. *V1: preflight strictness — fail closed and clear on a broken or unprovisioned setup before any paid run.* (Live per-toolset verification stays at an honest declared-and-warned baseline — [#48](https://github.com/jarodtaylor/cadre/issues/48) found no mechanical tool-fire signal Hermes exposes.)

_Why it serves the approach:_ makes "human approves before execution" an enforced binding, and keeps correctness-DevEx honest when the operator can't see a silent misconfig.

### Reach & packaging

A real `cadre` package + CLI (shipped, #11) that ends the install's symlink-to-repo fragility. *V1 (late): PyPI publish as `cadre-fleet` ([#59](https://github.com/jarodtaylor/cadre/issues/59)) for clean `pip install` ergonomics.* → V2 elevates this domain to the northstar.

_Why it serves the approach:_ cross-runtime reach starts with a clean, repo-independent install.

*Not a track — the far horizon:* plain-language task → a meta-orchestrator assembles the fleet on the fly.

## Milestones

- **2026-07-05 — `v0.1.0` shipped.** Working, packaged Hermes alpha; cold-dogfood PASS.
- **`v0.2.0` — V1: single-runtime Hermes alpha hardened.** The four tracks' V1 scope landed (incremental `v0.1.x` patches along the way).
- **V2 (parked) — runtime-agnostic reach ([#60](https://github.com/jarodtaylor/cadre/issues/60)).** Install-once, multi-runtime — evaluate the Hermes plugin as the first adapter. The real public-reach unlock; waits until V1 is solid (don't build on sand).

## Not this product (identity boundaries)

- Not persistent-profile agent pipelines with crons/boards/human gates (that's the adjacent `hermes-multi-agent-workflow`).
- Not a bulletproof automated quality metric — efficacy is a harness property plus human spot-check.
- Not a LangGraph/CrewAI competitor on features and adoption — optimize for real use, not positioning.
