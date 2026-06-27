# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Fleet engine

### Fleet
An ephemeral, fully-configured group of specialists plus a synthesizer that runs one task and returns one combined result. A fleet is defined entirely by configuration — adding or changing one requires no engine-code change.

A fleet is created per task and torn down on completion; it carries no state between tasks.

### Specialist
One stateless agent within a fleet — a role, a provider, a model, a toolset, and a focus — run once on the task. Specialists run concurrently and independently, and each may use a different model and provider.

### Lane
The runtime execution of one specialist within a fan-out run — its concurrent worker, its outcome (success, failure, or timeout), its elapsed time, and the artifact it produces. *Specialist* names the configured participant; *Lane* names that participant actually running.

A Lane counts as completed once its worker reports a result, even if the engine reads that result slightly after the shared deadline; only a Lane whose worker never reports by the deadline is a timeout. Each Lane's artifact is written the moment the Lane finishes, not at the end of the run.

### Focus
The role-specific instruction that tells a specialist what to investigate and how to ground its output.

A focus that does not explicitly require live retrieval and citation will let the model answer from training knowledge even when a retrieval toolset is provisioned — so the focus, not the toolset alone, is the operative grounding control for a lane. Pairing a citation demand with explicit permission to mark an item "unsourced" keeps the demand from inducing fabricated sources.

### Persona
A rich, editable markdown specialist definition — the lens's full role, reasoning scaffolding, and grounding controls — referenced by name from a fleet as an alternative to an inline one-line *Focus*. A specialist carries exactly one instruction source: a persona or a focus. A persona carries no provider/model/toolset binding, so the same lens can be reused across fleets and models.

### Persona pool
The shared, flat directory of persona files (`personas/` in the repo; `~/.cadre/personas` on a host) that a `persona:` reference resolves against. A caller-layer resolver reads the named file, confined to the pool, and populates the specialist's instruction before the run — the engine never sees a path or a persona name.

### Synthesizer
The single strong-model step that combines the specialists' successful outputs into one grounded result. It runs after the specialists, over only the survivors, and is expected to attribute claims to the specialist that surfaced them and preserve citations.

### Fan-out → synthesize
The engine's one orchestration primitive: run every specialist concurrently (fan-out), then synthesize their successful outputs into one result. If some specialists fail, it synthesizes the survivors and reports the failures; it fails outright only when none succeed.

### Convergence
What a fleet does with its specialists' outputs, configured per fleet. Three modes: **synthesize** — a strong model blends the survivors into one grounded report (the default; see Fan-out → synthesize); **collect** — return the raw, attributed outputs for the caller to review, with no blend (see Collect); **judge** — an independent critic grades each surviving specialist's output in place, attributed per lane, without blending (see Judge). Convergence is one of a fleet's two shape axes; the other is topology.

### Collect
A convergence mode in which a fleet has no synthesizer: it fans out the specialists and returns their raw, attributed outputs (role, model, text) intact, leaving synthesis to the caller. The correct contract for review and adversarial fleets, where blending independent findings would destroy the signal the review exists to surface. A collect run succeeds when at least one specialist returns. (Shipped alongside synthesize.)

### Judge
A convergence mode in which, after the fan-out, one independent judge model grades each surviving specialist's output as a distinct item — assessed and attributed per lane, never blended. The grade's *form* (numeric score, rank, pass/fail) is prompt-determined; the per-lane *container* `{role, model, grade, rationale}` is fixed and lands in the run manifest. The judge is advisory by design: a low grade is a successful, usable run; it never blocks, discards, or makes a binding selection. A partial-coverage run — the judge returned grades for only some surviving lanes — also succeeds; ungraded lanes are named in the manifest and in the report. Judge failure (call error or timeout) degrades to the attributed specialist outputs plus a note; only failure exits non-zero, never silence or crash.

No-self-grading principle: give the judge a model distinct from the specialists. A same-model judge may over-rate its sibling lane (self-favoring bias — the models share training-data priors). Cadre does not enforce distinctness; the judge is defined by its role and instructions, not its model identity. Cross-model assignment is the recommended default.

### Topology
How a fleet's lanes relate in time: **parallel** — independent and concurrent (the built topology, the basis of fan-out); **sequential** — each lane consumes the previous lane's output; **iterative** — lanes run in rounds and see each other's output. Distinct from convergence: topology is execution order, convergence is output handling. Only parallel is built; sequential and iterative are future primitives.

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
The host-confirmed menu an agent composes a new fleet from — `~/.cadre/palette.yaml`, written by the install's verify step. It records the `(provider, model)` pairs that actually resolved and responded on this host (**models verified by a live call**) plus the profile's safe toolsets (**declared and filtered to the safe set, not per-tool verified** — an unprovisioned tool still appears, so its lane can run silently ungrounded — as can a lane whose Focus doesn't demand retrieval). Composing only from the palette prevents two silent failure modes: a guessed model string (a dead lane) and an unverified provider. It carries a `generated_at` stamp because verified strings drift.

### Fleet preview
The mechanically rendered view of a *parsed* fleet config — synthesizer (with a cost flag for API-billed models), `allow_privileged_tools`, the verbatim synthesis prompt, and every lane's role/provider/model/toolset — printed by the runner's `--preview` mode without making any model call. It is the operative control of the agent-run handoff: a human approves this rendered fleet, not the agent's paraphrase of it, so a tampered or privileged fleet cannot slip through unseen.
