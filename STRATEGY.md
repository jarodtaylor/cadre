# Strategy — Cadre

> The product's north star. Last updated 2026-06-18 (v0 status corrected; capture/auditability track added).

## Target problem
Multi-model work has no good home. Hermes's `delegate_task` can't pick a model per task; single-vendor runtimes (e.g. Claude Code) fan out only within one provider's tiers; and a one-off script isn't a tool — no reusable patterns, no DX, no path to other use cases. The value left on the table: routing the right subtask to the right *provider*, then synthesizing.

## Approach
A **provider-neutral engine** that spins up **ephemeral, multi-model fleets** — fan a task out across whatever models you have (cloud APIs, OAuth subscriptions, OpenRouter, local), each a specialist, then synthesize one grounded, attributed result. Built on a small set of **composable orchestration primitives** (MVP = parallel fan-out → synthesize), so new fleets are *configuration*, not new code. Hermes-first; cross-runtime is the north star (the engine is standalone; runtimes are thin wrappers).

## Users
- **Primary (now):** the author — dogfooding a multi-provider research/work tool.
- **Eventual:** OSS users who want multi-provider fleets without writing orchestration boilerplate.

## What "good" looks like
- A fleet's synthesized output visibly **beats a single strong model** on real tasks (human gut-check).
- You **reach for the engine instead of hand-writing a script**.
- Adding a new fleet — even an unforeseen shape — is **config only**, never "that's not a pattern."
- Output is **auditable**: every run is inspectable — each specialist's raw output preserved, each synthesized claim traceable to the model that surfaced it, and silent failures (a lane that ran without its tools) made visible, not buried in prose. *Today only half-built: the synthesis prints, the evidence doesn't — Track 2 closes the gap.*

## Tracks
1. **MVP — engine + research-swarm on Hermes.** Core built. Demonstrated **human-run** on the host — grounded multi-model visibly beat a single model, verified by hand. **Agent-run** (a Hermes agent invoking Cadre itself) is the remaining gap, not yet tested.
2. **Run capture & auditability (next).** Persist each run: every specialist's raw output, the resolved config (models / tools / profile), and a run-health manifest (tools fired, silent fallbacks, timeouts, tokens/context). Makes the "auditable" promise real, and is the **gate before agent-handoff** — you can't trust a run you can't see. (Was backlog "trace/raw-dump mode.")
3. **Resilience:** per-specialist timeout shipped; then an independent critic/scoring stage — a *separate* pass or external model, never the Advisor grading itself.
4. **Cross-runtime / agent-handoff:** an AGENTS.md usage contract + thin per-runtime wrappers (Hermes skill, Claude Code skill, standalone CLI, MCP server) over the same engine.
5. **More fleets / primitives:** code/PR review, output validator, advisor panel; critique-loop, debate-and-judge.
6. **North star:** plain-language task → a meta-orchestrator assembles the fleet on the fly.

## Not this product (identity boundaries)
- Not persistent-profile agent pipelines with crons/boards/human gates (that's the adjacent `hermes-multi-agent-workflow`).
- Not a bulletproof automated quality metric — efficacy is a harness property plus human spot-check.
- Not a LangGraph/CrewAI competitor on features and adoption — optimize for real use, not positioning.
