# Strategy — Cadre

> The product's north star. Last updated 2026-06-22 (v0 complete — engine + run-capture + Hermes agent-handoff all shipped & dogfooded live; approach + tracks re-anchored).

## Target problem
Multi-model work has no good home. Hermes's `delegate_task` can't pick a model per task; single-vendor runtimes (e.g. Claude Code) fan out only within one provider's tiers; and a one-off script isn't a tool — no reusable patterns, no DX, no path to other use cases. The value left on the table: routing the right subtask to the right *provider*, then synthesizing.

## Approach
Cadre is the **provider-neutral fabric for safe, agent-driven multi-model orchestration.** Fan any task across whatever models you have (cloud APIs, OAuth subscriptions, OpenRouter, local) — no single-runtime lock-in — let an *agent* compose and run the fleet, but require a human to **approve the *parsed* fleet before anything executes** (the load-bearing trust control). New fleets are *configuration* on a small set of composable primitives (MVP = fan-out → synthesize), never new code. The bet deliberately rules out both single-vendor fan-out *and* autonomous orchestration with no human gate. Hermes-first; cross-runtime — thin wrappers over one standalone engine — is the reach.

## Users
- **Primary (now):** the author — dogfooding a multi-provider research/work tool.
- **Eventual:** OSS users who want multi-provider fleets without writing orchestration boilerplate.

## What "good" looks like
- A fleet's synthesized output visibly **beats a single strong model** on real tasks (human gut-check).
- You **reach for the engine instead of hand-writing a script**.
- Adding a new fleet — even an unforeseen shape — is **config only**, never "that's not a pattern."
- Output is **auditable**: every run is inspectable — each specialist's raw output preserved, each synthesized claim traceable to the model that surfaced it, and silent failures (a lane that ran without grounding) made visible, not buried in prose. *Shipped (run-capture) — and it earned its keep: a grounding-deficient lane was caught and fixed only because the evidence persisted alongside the polished synthesis.*
- An **agent can drive it safely**: it shows the human the *parsed* fleet and waits for approval before any model runs. *Proven live — the v0 gate.*

## Tracks
**v0 — shipped foundation (done):** the engine (one primitive — fan-out → synthesize — plus a fail-closed safe-toolset allowlist, a per-call wall-clock timeout, and degrade-and-report), run-capture & auditability, and the Hermes **agent-handoff** (an agent drives the `cadre-fleet` skill; preview-always human-okay). All on `main`, dogfooded live.

**v1 — investment areas (priority order):**
1. **Operator & agent experience.** Make every run feel *alive and trustworthy* — live progress breadcrumbs, resolved-profile clarity, preview/run polish. *Serves the human-gate: an agent and a human can only trust what they can watch happen.*
2. **Fleet library & primitives.** Make "new fleets = config" pay off — batteries-included starter fleets, a multi-model review-swarm catalog, richer per-role definitions, and the next primitives (critique-loop, debate-and-judge, an independent critic/scoring pass — separate, never self-grading). *Serves composability + adoption: ready-made for the many, agent-built for the few.*
3. **Trust & safety hardening.** Make the human-gate's preview-binding *code-enforced* — a run executes only when it's bound to its own preview (what runs is what was previewed, one-shot and fail-closed) plus live per-toolset verification (safe toolsets still read untrusted web, and the synthesis is consumed by a terminal-capable agent); human *presence* at the okay stays procedural in this single-operator deployment. *Serves the "human approves before execution" commitment as an enforced binding, not a presence proof.*
4. **Reach & packaging.** A real `cadre` package + CLI (also ends the install's symlink-to-repo fragility), then more runtime wrappers (Claude Code skill, MCP server) over the one standalone engine. *Serves the cross-runtime reach.*

*Not a track — the horizon:* plain-language task → a meta-orchestrator assembles the fleet on the fly.

*(Not exhaustive — more investment areas will surface as we build; these are the load-bearing four for v1.)*

## Not this product (identity boundaries)
- Not persistent-profile agent pipelines with crons/boards/human gates (that's the adjacent `hermes-multi-agent-workflow`).
- Not a bulletproof automated quality metric — efficacy is a harness property plus human spot-check.
- Not a LangGraph/CrewAI competitor on features and adoption — optimize for real use, not positioning.
