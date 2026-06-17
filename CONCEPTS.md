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
