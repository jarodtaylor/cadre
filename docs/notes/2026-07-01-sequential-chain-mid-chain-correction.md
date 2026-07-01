# Case study: a mid-chain correction (sequential topology)

*A documented live run of the `research-brief` flagship fleet, showing what a
**sequential** chain produces that a **parallel** fan-out structurally cannot.
Context: [#17](https://github.com/jarodtaylor/cadre/issues/17) (PR #38), dogfooded 2026-07-01.*

## The gap in a parallel fan-out

Cadre's original primitive is a **parallel fan-out**: send a task to several
specialists at once, then converge their outputs (synthesize into one report,
collect them side by side, or judge them). It's a strong pattern for *coverage*
and *cross-checking* — decompose a task into lenses, run those lenses across
diverse models, and see where they agree or diverge.

But parallel lanes run **blind to each other**. Each specialist sees only the
task. A synthesizer sees their *outputs*, but only after every specialist is
already done. So a fan-out can surface *disagreement* (two lanes reach different
answers) — but it cannot produce a *correction* (one lane fixing a specific
error in another's work), because a correction requires one model to read
another's output **while it is still working**.

## Sequential topology

A fleet has two independent shape axes (see `CONCEPTS.md`): **topology** (how
lanes relate in time) and **convergence** (what happens to their outputs).
Sequential topology makes the lanes a **chain**: each stage receives all
preceding stages' output as prior-stage context and builds on it.

The flagship `fleets/research-brief.example.yaml` is a three-stage chain:

1. **Scout** (`[web, search]`) — gather authoritative sources; cite every finding.
2. **Analyst** (`[web]`) — treat the Scout's findings as *claims to audit*, not
   facts to repeat: verify each against a live primary source; confirm, correct,
   or flag it. Produce a stress-tested thesis.
3. **Writer** (`[]`, no tools) — synthesize the audited findings into a brief,
   preserving citations and carrying forward any corrections.

Three different providers, deliberately — so the audit is independent of the
model that produced the claims.

## The live run

- **Fleet:** `research-brief` (topology `sequential`, convergence `collect`).
- **Lanes:** scout `xai-oauth/grok-4.3` → analyst `openrouter/deepseek-v4-pro` → writer `copilot/claude-opus-4.8`.
- **Task:** *"What does the current evidence say about whether AI coding assistants actually improve developer productivity?"*
- **Result:** exit 0, ~5m19s (serial: 49s + 180s + 90s), all three lanes ok,
  grounded on real primary sources (arXiv / SSRN / METR).

The Scout did its job — ten findings, each cited: the 2023 Copilot lab study
(55.8% faster on a narrow task), the large field experiments (~26% more tasks
completed), the METR RCT (experienced open-source developers were 19% *slower*),
a Feb 2026 METR follow-up, the Stack Overflow survey.

Then the Analyst earned its seat. On the field-experiments finding, the Scout
had written that senior developers saw "smaller or **negligible**" gains. The
Analyst verified that specific claim against the primary source and pushed back:

> **Correction:** The Scout claims senior developers saw "negligible" gains. The
> actual paper reports senior developers gained **7–16%** and long-tenure
> developers **8–13%** — smaller than juniors' 21–40%, but statistically and
> practically meaningful, not "negligible." The Scout's phrasing overstates the gap.

It also flagged that the headline study measured task *quantity*, not *quality*,
and surfaced counter-evidence the Scout had missed. The Writer then folded the
correction into the final brief and **attributed it to the Analyst** — no
invented facts, citations preserved.

## Why this is the point

The correction is only possible *because the Analyst read the Scout's specific
words.* In a parallel fan-out, an "analyst" lane would have researched
independently, and a synthesizer would have merged two separate takes — the word
"negligible" would never have been on trial. The chain produces something a
collapsed topology can't: an **auditable, mid-task correction of a specific prior
claim**, plus a stress-tested intermediate thesis you can inspect (every run is
captured to `~/.cadre/runs/<slug>/` — per-lane output, the audit, and a JSON
manifest).

This generalizes the design bet behind Cadre: multi-model agent systems get more
valuable when you can *compose* how the lanes relate — parallel for coverage,
sequential for verification — and keep every step attributed and inspectable.

## Honest caveats

- **This is one run — a demonstration, not a benchmark.** It shows a *structural*
  capability with a concrete instance, not a measured win rate.
- **There is a real trust seam.** The Analyst runs web tools on text produced
  upstream, so a prompt injection in a scouted source could in principle steer
  it. Today that seam is *disclosed* in the run preview (`⚠ cross-stage tool
  exposure`) and *bounded* (read-only toolsets, a tool-less Writer → worst case a
  skewed brief, not privilege escalation). Hardening it structurally is tracked
  in [#5](https://github.com/jarodtaylor/cadre/issues/5).
- **Inter-stage output is capped** (4,000 chars today), so on this run the
  Analyst audited the Scout's first ~4 findings; a token-aware cap is
  [#39](https://github.com/jarodtaylor/cadre/issues/39).
