# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Fleet engine

### Fleet
An ephemeral, fully-configured group of specialists plus a synthesizer that runs one task and returns one combined result. A fleet is defined entirely by configuration — adding or changing one requires no engine-code change.

A fleet is created per task and torn down on completion; it carries no state between tasks.

### Specialist
One stateless agent within a fleet — a role, a provider, a model, and a toolset — run once on the task. Specialists run concurrently and independently, and each may use a different model and provider.

### Synthesizer
The single strong-model step that combines the specialists' successful outputs into one grounded result. It runs after the specialists, over only the survivors, and is expected to attribute claims to the specialist that surfaced them and preserve citations.

### Fan-out → synthesize
The engine's one orchestration primitive: run every specialist concurrently (fan-out), then synthesize their successful outputs into one result. If some specialists fail, it synthesizes the survivors and reports the failures; it fails outright only when none succeed.

### Privileged toolset
A toolset that lets an agent act beyond reading or searching — running shell commands, writing files, executing code, or driving a browser or computer. Because a specialist may process untrusted content, privileged toolsets are denied by default and require an explicit per-fleet opt-in; anything outside the known-safe set is treated as privileged.

## Run capture

### Run capture
Persisting a fleet run's artifacts to disk so the run is auditable after the fact — each specialist's raw output and the synthesis as markdown, plus a run manifest — instead of only printing the synthesized result. On by default; a run that partially fails or times out still produces a folder holding whatever completed.

### Run manifest
The structured, machine-readable record of one run's health: per specialist, its outcome (success or failure, elapsed time, the toolset it was asked for, whether it timed out, and the file holding its output), plus run-level facts (the synthesizer model, whether synthesis succeeded, the active Hermes profile). It is the seam a later auditing or agent-handoff layer reads; the markdown files are for reading by hand.

## Agent-run handoff

### Fleet library
The host-local directory of runnable fleets a Hermes agent selects from — `~/.cadre/fleets/<name>.yaml`, created owner-only (`0o700`) at install. Distinct from the repo's `fleets/`, which holds examples only; copy one into the library to make it runnable. Selected by name: the agent lists the directory and passes the chosen file's path to the runner. There is no registry, fuzzy-matching, or CLI subcommand — a flat directory of named YAML files is the whole convention.

### Verified palette
The host-confirmed menu an agent composes a new fleet from — `~/.cadre/palette.yaml`, written by the install's verify step. It records the `(provider, model)` pairs that actually resolved and responded on this host (**models verified by a live call**) plus the profile's safe toolsets (**declared and filtered to the safe set, not per-tool verified** — an unprovisioned tool still appears, so its lane can run silently ungrounded). Composing only from the palette prevents two silent failure modes: a guessed model string (a dead lane) and an unverified provider. It carries a `generated_at` stamp because verified strings drift.

### Fleet preview
The mechanically rendered view of a *parsed* fleet config — synthesizer (with a cost flag for API-billed models), `allow_privileged_tools`, the verbatim synthesis prompt, and every lane's role/provider/model/toolset — printed by the runner's `--preview` mode without making any model call. It is the operative control of the agent-run handoff: a human approves this rendered fleet, not the agent's paraphrase of it, so a tampered or privileged fleet cannot slip through unseen.
