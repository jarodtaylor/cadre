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

`iterative + judge` is supported but unrecommended: after N rounds the lanes are no longer independent — they have been mutually refining toward each other's positions — so per-lane grades of near-converged outputs are least meaningful exactly when the debate worked as intended.

### Result status
The explicit tri-state outcome of a completed fleet run. **SUCCESS** — the run produced its intended output (synthesize: synthesis present; collect: ≥1 specialist survived; judge: judge graded). **DEGRADED** — specialists survived but the convergence step ran and failed; a partial result is available (synthesize and judge only under parallel topology — parallel collect is always SUCCESS or FAILED; under sequential topology, collect can also be DEGRADED when the chain breaks after at least one lane completed; for sequential synthesize/judge the status is conjunctive — SUCCESS only if the chain completed AND the convergence step succeeded). **FAILED** — all specialists failed; convergence never ran. Under sequential topology, FAILED means the first lane failed and all downstream lanes were skipped — the chain never produced usable output.

The engine sets the status at every `run_fleet` return point. Two derived reads: `ok` (`status is SUCCESS`) is the exit-code signal for full success; `has_usable_output()` (`status is not FAILED`) covers both SUCCESS and DEGRADED. `synth_ok` and `judge_ok` remain as mode-specific detail for callers that need them; the run manifest records the `status` string (`success`/`degraded`/`failed`) alongside both.

Under iterative topology, `diversity_collapsed` (bool) is an additional mode-specific advisory signal: set when the last surviving round has ≤1 ok lane, or the fleet ran zero cross-round iterations (rounds=1, or all lanes dropped after round 1 — never debated). A collapsed run is still surfaced with its output; the flag is advisory and never changes `status`. It is recorded in the run manifest and render output alongside `status`.

### Topology
How a fleet's lanes relate in time: **parallel** — independent and concurrent (the fan-out topology); **sequential** — each lane consumes *all preceding* lanes' successful output, accumulating context through the chain (the third stage sees both the first and second stages' output, not just the immediately preceding one); **iterative** — lanes run for a fixed number of rounds (`rounds`, 1–10): round 1 fans all lanes out on the task; from round 2 each surviving lane re-runs seeing the previous round's role-attributed outputs from all lanes as untrusted data; a lane that fails a round is dropped and survivors carry forward; after the last surviving round the configured convergence runs over the survivors. Within a round, lanes run concurrently (wall-clock ≈ rounds×timeout for collect, (rounds+1)×timeout for synthesize/judge). A general primitive: debate (N diverse lanes arguing across rounds), critique-revise (producer+critic loop), and self-refine (N=1) are all iterative configurations. Distinct from convergence: topology is execution order, convergence is output handling. All three topologies are built.

### Skipped
A lane a sequential chain never ran because an upstream lane failed — distinct from a failure (no model call was attempted and no error occurred), excluded from the failure count, and recorded as a distinct state in the run manifest and progress output.

### Privileged toolset
A toolset that lets an agent act beyond reading or searching — running shell commands, writing files, executing code, or driving a browser or computer. Because a specialist may process untrusted content, privileged toolsets are denied by default and require an explicit per-fleet opt-in; anything outside the known-safe set is treated as privileged.

## Run capture

### Run capture
Persisting a fleet run's artifacts to disk so the run is auditable after the fact — each specialist's raw output and the synthesis as markdown, plus a run manifest — instead of only printing the synthesized result. On by default; a run that partially fails or times out still produces a folder holding whatever completed.

### Run manifest
The structured, machine-readable record of one run's health: per specialist, its outcome (success or failure, elapsed time, the toolset it was asked for, whether it timed out, and the file holding its output), plus run-level facts (the synthesizer model, whether synthesis succeeded, the active Hermes profile). It is the seam a later auditing or agent-handoff layer reads; the markdown files are for reading by hand.

## Report trust surfaces

### Combined surface
A rendered or captured surface where model output is aggregated beside rows the harness prints itself — the terminal result view and a collect/judge run's combined report — as opposed to an *isolated surface*: a single-author deliverable (a per-lane specialist file, or a synthesize-mode report) that carries no trusted grammar for a model to mimic. Anti-mimicry framing applies to combined surfaces only.

An isolated file is safe only while it stands alone: concatenating a whole run folder, or ingesting several per-lane files into one context, re-creates a combined surface — so a consumer of many files at once must take attribution from the run manifest, not from any in-file header.

### Report-grammar mimicry
A trust-safety threat in which a model's own output impersonates the harness's trusted report grammar — the status rows and role delimiters Cadre prints itself — so a reader mistakes fabricated structure for harness-emitted structure. Defended structurally on combined surfaces by framing every model-body line so that only harness-printed rows render at column 0.

The framing protects a reader that relies on visual column-0 position; a consumer that judges the report by substring match is unprotected, because the forged token still exists inside the framed body line. That is why the agent read-back takes structural provenance from the forgery-immune run manifest rather than the rendered report.

## Agent-run handoff

### Fleet library
The host-local directory of runnable fleets a Hermes agent selects from — `~/.cadre/fleets/<name>.yaml`, created owner-only (`0o700`) at install. Distinct from the repo's `fleets/`, which holds examples only; copy one into the library to make it runnable. Selected by name: the agent lists the directory and passes the chosen file's path to the runner. There is no registry, fuzzy-matching, or CLI subcommand — a flat directory of named YAML files is the whole convention.

### Verified palette
The host-confirmed menu an agent composes a new fleet from — `~/.cadre/palette.yaml`, written by the install's verify step. It records the `(provider, model)` pairs that actually resolved and responded on this host (**models verified by a live call**) plus the profile's safe toolsets (**declared and filtered to the safe set, not per-tool verified** — an unprovisioned tool still appears, so its lane can run silently ungrounded — as can a lane whose Focus doesn't demand retrieval). Composing only from the palette prevents two silent failure modes: a guessed model string (a dead lane) and an unverified provider. It carries a `generated_at` stamp because verified strings drift.

### Fleet preview
The mechanically rendered view of a *parsed* fleet config — synthesizer (with a cost flag for API-billed models), `allow_privileged_tools`, the verbatim synthesis prompt, and every lane's role/provider/model/toolset — printed by the runner's `--preview` mode without making any model call. It is the operative control of the agent-run handoff: a human approves this rendered fleet, not the agent's paraphrase of it, so a tampered or privileged fleet cannot slip through unseen.

### Preview-bound approval
A one-shot, owner-only token, minted by a fleet preview and required by a real agent-handoff run, that binds the run to a digest of the exact previewed surface — the parsed fleet config, the composed task (including any file contents read into it), the resolved personas, and the profile path. A run whose surface differs from a fresh preview is refused.

It is a *binding*, not a proof of human *presence*: the code enforces that what runs equals what was previewed, but because the agent mediates every channel to the human it cannot prove a human actually reviewed the preview — a colluding or fully prompt-injected agent that previews and immediately runs is a documented residual, not something the approval defends against. A fleet requesting privileged tools needs a distinct, deliberate approval act; unforgeability rests on the token directory's owner-only permissions, with no secret or signature.
