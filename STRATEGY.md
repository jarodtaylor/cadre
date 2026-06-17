# Strategy — Cadre

> The product's north star. Stubbed from the 2026-06-16 brainstorm; re-run `/ce-strategy` to deepen.

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
- Output is **spot-checkable**: each claim traces to the specialist/model that surfaced it, citations preserved.

## Tracks
1. **MVP (in progress):** the engine + the research-swarm fleet, on Hermes. Core built; live demo pending on the Hermes host.
2. **Resilience:** per-specialist timeout (deferred), then an independent critic/scoring stage (the seam exists).
3. **More fleets / primitives:** code/PR review, output validator, advisor panel; critique-loop, debate-and-judge.
4. **Cross-runtime:** Claude Code skill, standalone CLI, MCP server — thin wrappers over the same engine.
5. **North star:** describe a task in plain language → a meta-orchestrator assembles the fleet on the fly.

## Not this product (identity boundaries)
- Not persistent-profile agent pipelines with crons/boards/human gates (that's the adjacent `hermes-multi-agent-workflow`).
- Not a bulletproof automated quality metric — efficacy is a harness property plus human spot-check.
- Not a LangGraph/CrewAI competitor on features and adoption — optimize for real use, not positioning.
