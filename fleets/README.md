# Fleet examples

These are **examples**, not runnable fleets. Copy one into your host fleet
library to use it:

```bash
cp fleets/research-swarm.example.yaml ~/.cadre/fleets/research-swarm.yaml
cp fleets/code-review.example.yaml ~/.cadre/fleets/code-review.yaml
cp fleets/doc-review.example.yaml ~/.cadre/fleets/doc-review.yaml
cp fleets/review-scoring.example.yaml ~/.cadre/fleets/review-scoring.yaml
# then set provider/model strings to your host-verified values (see ~/.cadre/palette.yaml)
```

All four starter fleets are seeded into `~/.cadre/fleets/` at install (stripping
`.example` from the filename) so they are ready to configure without copying
manually.

---

## Starter fleets

### `research-swarm.example.yaml` — synthesize shape

The flagship curated fleet: a multi-provider research swarm (real-time social
via Grok, broad web via a fast model, deep analysis via a strong model) that
fans out and synthesizes **one attributed report**.

All three specialists carry explicit sourcing directives so retrieval lanes
produce grounded, linked findings rather than reciting from training memory.
The synthesizer attributes each claim to the specialist that surfaced it and
calls out conflicts.

Shape: **fan-out → synthesize**. `convergence` is absent (defaults to
`synthesize`), so every existing fleet that omits the field parses identically.

---

## Review catalog — review fleets

Three of the seeded starters review your work by fanning a set of **lenses**
across different models in parallel. Two (`code-review`, `doc-review`) use the
collect shape — no synthesizer, each lane's raw attributed critique returned
intact. The third (`review-scoring`) uses the judge shape — an independent
critic grades each reviewer's output in place after the fan-out. The `--doc`,
cross-model, and toolset guidance below applies to all three.

**Name the artifact with `--doc`, don't paste it.** Pass the code, diff, or
planning document with `--doc PATH` (repeatable) and the runner reads the file
into the task — no copy-paste. `--preview` lists the paths (as you gave them) and
read-checks them before any model runs. Pasting into `--task` still works; the
two combine (e.g. `--task "Review this PLAN" --doc plan.md`).

**Cross-model beats all-Claude.** The whole point is independent perspective: a
lane on Grok, one on Gemini, one on GPT, one on DeepSeek, one on Claude will
disagree — and the disagreement is the signal, because one model catches what
another's training blind spots miss. The example assignments use diverse
providers to demonstrate this, but they are example strings you swap for your
verified palette, not a requirement: you can run fewer lanes or even all-same
model — you just give up the cross-model edge that is the reason these fleets
exist.

**Prefer reasoning/completion models, not agentic ones.** Every review lane is
`toolset: []` (fail-closed zero tools): reviewers reason over the code or
document you supply (via `--doc` or pasted into `--task`), not live retrieval. Agentic, tool-happy models
may try to emit tool calls even when none are available — under `toolset: []`
those calls no-op and the lane can return **no review at all**. Each lane's
`focus` opens with "you have no tools — review only from the provided artifact"
to steer the model to answer inline; choosing a model that reasons over given
context makes this robust.

**The empty toolset is intentional — do not add a retrieval toolset.** Review
lenses read the artifact in the task; they do not fetch. Adding a `web` (or any
retrieval) toolset to a review lane buys nothing and risks a silently
ungrounded lane if that tool isn't provisioned in your Hermes profile (tools
are profile-scoped — see `docs/RUNBOOK.md`). `[]` is also the load-bearing
security control: it keeps prompt-injection in the reviewed content from
escalating to actions.

### `code-review.example.yaml` — four lenses

Reviews the code or diff you pass via `--doc` (or `--task`). Lenses, each on a
different provider/model:

- **security** — injection (SQL/command/prompt), auth/authz gaps, insecure defaults, exposed secrets, OWASP patterns
- **architecture** — coupling, separation-of-concerns violations, abstraction leaks, dead code
- **performance** — algorithmic complexity, unnecessary I/O, blocking calls, allocation pressure
- **correctness** — logic errors, off-by-one, edge-case handling, error-propagation failures

### `doc-review.example.yaml` — five lenses

Reviews a **planning document** — a requirements doc or a plan — that you name
with `--doc` (or paste as `--task`). The lenses are ported from the
`ce-doc-review` personas and are
**doc-type-agnostic**: they apply to either artifact, so name the type in the
task when it matters (e.g. "Review this PLAN: …" vs "Review these
REQUIREMENTS: …"). Lenses, each on a different provider/model:

- **coherence** — internal contradictions, terminology drift, broken cross-references, structural gaps
- **feasibility** — can it be built as described? stack conflicts, unaddressed nil/empty/error paths, hand-waved migrations
- **scope-guardian** — right-sized for its goals? scope creep, speculative abstractions, framework-ahead-of-need
- **product** — building the wrong thing well? premise challenge, orphan requirements, simpler alternatives
- **adversarial** — falsify it: unstated assumptions, load-bearing decisions on thin evidence, omitted alternatives

Each focus demands the lane **cite the exact passage** it flags and blesses
"no finding" as an honest result, steering a weak model away from fabricating
plausible-but-empty structural critiques.

**Optional add-on lanes.** `doc-review` ships two extra lenses commented-out in
the example — **design** (information architecture, interaction states, user
flows, AI-slop risk) and **security** (plan-level attack surface, auth/authz,
data exposure, secrets). Uncomment them for UI-heavy or security-sensitive
documents and assign each a palette model.

Shape for both: **fan-out → collect**. `convergence: collect` is explicit in
each spec.

---

### `review-scoring.example.yaml` — judge shape

Reviews the code, diff, or document you pass via `--doc`, then runs an
**independent judge** that grades each reviewer's output in place — ranked and
calibrated, with each lane kept separate and attributed to its role and model.
The judge's grade text leads the report; the attributed reviewer outputs follow
so you can verify the grade against the underlying findings. Structured per-lane
grades (`{role, model, grade, rationale}`) land in the run manifest for machine
consumption or later analytics.

**The judge is advisory.** A low score is a successful, usable run; the judge
never blocks, discards, or selects. A partial-coverage run (the judge graded
only some lanes) also succeeds — the manifest lists the ungraded lanes and the
report names them. Only a judge call error or timeout (or total specialist
failure) exits non-zero.

**No-self-grading: prefer a different model for the judge.** A judge sharing a
model with a specialist may over-rate that sibling lane (self-favoring bias).
Cadre does not enforce model-distinctness — the judge is defined by its role and
instructions, not its model identity — but a cross-model assignment is the
recommended default and what the example demonstrates.

Specialist lanes follow the same empty-toolset setup and placeholder model
strings as `code-review` and `doc-review` — swap them to your palette. The
judge model call also runs with no tools (fail-closed over untrusted specialist
text).

Shape: **fan-out → judge**. `convergence: judge` plus a `judge:` block
(provider, model, prompt) are explicit; the `judge:` block is required.

---

## The three fleet shapes

| Shape | `convergence:` | Output |
|---|---|---|
| `synthesize` | `synthesize` (default, may be omitted) | One synthesized report on stdout |
| `collect` | `collect` (must be explicit) | Attributed specialist blocks on stdout |
| `judge` | `judge` (must be explicit; requires a `judge:` block) | Judge grade + attributed specialist outputs on stdout |

**Synthesize** is the default: the synthesizer model reads all specialist
outputs and produces a single, attributed consensus report. Best for research
and summarization tasks where you want one integrated answer.

**Collect** skips the synthesizer: each specialist's raw output is returned
with its role and model labeled. Best for review tasks where you want
independent perspectives without a model collapsing them into one voice — you
rank the outputs yourself.

**Judge** runs an independent critic after the fan-out that grades each
specialist's output in place: ranked and calibrated, but each lane preserved
and attributed. Best for the same review tasks as collect, when you want an
independent ranking of the findings rather than doing it by hand. `synthesize`
blends (erasing per-finding signal); `collect` preserves signal but leaves
ranking to you; `judge` preserves signal and adds the calibrated perspective.

---

## `palette.example.yaml` — candidate-seed / palette template

The install seeds `~/.cadre/palette-candidates.yaml` from this file. Edit it
to match the providers you've authenticated in your Hermes profile, then run
the verify step:

```bash
PYBIN="${CADRE_HERMES_PYTHON:-$(grep -E '^CADRE_HERMES_PYTHON=' ~/.cadre/config | cut -d= -f2-)}"
"$PYBIN" spikes/verify_aiagent_providers.py
```

Verification keeps only the `(provider, model)` pairs that actually resolve on
your host and writes the confirmed `~/.cadre/palette.yaml` — the menu an agent
uses when composing new fleets. See `docs/RUNBOOK.md` for the full install and
verification flow.

---

Never commit API keys or tokens — credentials live in Hermes auth/env.
