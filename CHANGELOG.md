# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Auto-discovery for your model palette: a `discover` command populates your candidate list from your already-authenticated providers, `verify-palette` verifies a capped subset by default without ever silently shrinking a previously-verified palette on re-run, and a ready-to-run palette fleet is generated automatically once two or more providers verify (#74)
- Fleet runs now refuse up front — before any model call — if a fleet references a model outside your verified palette, with a dedicated exit code for that case; every lane and run failure now carries a structured reason (timeout, empty output, model error, etc.) instead of being guessed from error text (#72)

### Fixed
- `cadre setup` now prints a ready-to-paste `PATH` export so the bare `cadre` command works without manually activating the virtualenv; the install guide now tells first-time hosts to skip the maintainer-only publishing steps (#81)

### Docs
- Documented a runtime gotcha in Hermes's `AIAgent` library: some failures surface as ordinary response text instead of raising an error (#77)
- Recorded implementation notes from the palette auto-discovery work (#75)
- Recorded implementation notes from the preflight-refusal and failure-legibility work (#73)
- Reworked the roadmap in STRATEGY.md into shipped-foundation, near-term-hardening, and longer-term-reach milestones (#69)
- Clarified the install walkthrough (why the Hermes Python matters, what personas are, a validate-before-run step) and tidied the starter fleet YAML comments (#65)

## [0.1.0] - 2026-07-05

Initial public release — a working alpha.

### Added
- Fan-out engine that runs a fleet of specialists (independent provider/model/toolset combinations) against one task, in three topologies: **parallel** (concurrent and independent), **sequential** (a chain where each stage consumes all prior stages' output), and **iterative** (bounded rounds of debate / critique-revise)
- Three convergence modes: **synthesize** (one grounded, attributed report), **collect** (raw attributed outputs side by side), and **judge** (an independent critic grades each surviving lane)
- Fail-closed toolset allowlist — a specialist gets only safe (read/search/analyze) tools by default; privileged tools (shell, file, browser, code execution) require an explicit per-fleet opt-in
- Per-call wall-clock timeouts as a backstop over the underlying agent runtime's own request handling
- Run capture: every run is written to an auditable folder with per-specialist output, the synthesis, and a JSON manifest
- Preview-bound approval for the agent-run handoff — a human approves the exact parsed fleet, not the agent's paraphrase of it, before a run executes
- A `verify-palette` step plus starter fleets and reusable personas, so a freshly-installed host has a runnable fleet with minimal editing
- A defensive trust-safety pass: sanitized output at every model-facing sink, a per-run judge nonce to prevent forged grading markers, and symlink guards on file writes
- Packaged as the `cadre` CLI, installable directly from the repository

[Unreleased]: https://github.com/jarodtaylor/cadre/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jarodtaylor/cadre/releases/tag/v0.1.0
