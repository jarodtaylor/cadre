---
title: "Disclose material facts on the surface the human approves, not where only the machine sees them"
date: 2026-06-26
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A preview / approval surface gates a human go-or-no-go on what will run"
  - "The action silently transforms caller input — truncates, samples, filters, or defaults it — before the machine consumes it"
  - "The only record of that transformation is downstream (the artifact the model/job sees), not on the surface the human approved"
tags: [human-in-the-loop, preview, approval-surface, disclosure, truncation, trust-boundary, fleet-engine]
---

# Disclose material facts on the surface the human approves, not where only the machine sees them

## Context

Cadre's `--doc` file input caps each file at `MAX_FILE_BYTES` (256 KiB): an oversize file is truncated and a `[cadre: … truncated …]` note is appended **inside the composed task** — the string the model receives and that capture writes to `prompt.txt`. That looked complete: the transformation is recorded, and a test asserted the note is present in the block.

A cross-model review (Codex) found the gap a same-model panel of ten personas had missed: the note is in the wrong place for the human who matters. `--preview` is Cadre's **operative human-okay control** — the human approves the *parsed* fleet plus the resolved `--doc` paths. But `render_file_inputs` rendered only the path, not the truncation. So a reviewer would okay "review `plan.md`" while the fleet silently reviews only the first 256 KiB — producing a confident verdict over a partial document the human believed was whole. The disclosure existed; it just never reached the surface the decision was made on.

## Guidance

When a render *is* an approval control, every **material fact about what will actually run** must appear **on that surface** — not only in the downstream artifact the machine consumes. A silent transformation of the input (truncation, sampling, dedup, a defaulted value, a dropped field) is exactly such a fact: it changes what the human is approving.

- Return the transformation as data from the layer that performs it, so the surface can render it. Here `compose()` returns `(composed_task, doc_paths, truncated_paths)`; `render_file_inputs(paths, truncated)` flags each truncated path: `⚠ truncated — the review will run over a PARTIAL file`. The flag is the renderer's *own* trusted text (not the untrusted path), so it can't be spoofed.
- **Cover every surface where the human (or its proxy) acts, not just the obvious one.** The preview is one; the *non-preview run path* is another. `cli.py run` has no preview at all, and `run.py` skips it when `--preview` isn't passed — so both runners also emit a `[cadre] warn: … truncated …` to stderr before any model call. A fix that discloses on the preview but leaves the run path silent only half-closes the gap (see the next point).
- **A multi-part finding folded halfway reads as fully folded.** Codex's recommendation was two-part ("render it in `--preview` *and* run output"). Folding only the preview half and shipping it as "done" would misrepresent the diff. Either fold every part, or name the unfolded part explicitly in the residuals — never let "I addressed the finding" stand for "I addressed the load-bearing half." A second review pass caught the omission precisely because the first fold looked complete.

## Why This Matters

"Render from the parsed config, not the paraphrase" makes the preview *faithful to the fleet*; it does nothing for a fact about the **input** that lives only downstream. A truncation the human can't see defeats the approval the same way a spoofed line would — the human signs off on a review that silently isn't what they think. For a tool whose whole premise is trustworthy review, "reviewed a partial doc and presented it as whole" is the cardinal failure (the RUNBOOK's "never present a partial as the whole"), and it's invisible until you ask *where the human looks*, not *whether the fact is recorded somewhere*.

This is a distinct axis from output-sanitization of the approval surface (see Related): that one keeps untrusted content from **spoofing** the surface; this one ensures the surface **discloses** the material facts it vouches for. A surface can be perfectly un-spoofable and still lie by omission.

## When to Apply

- Any preview / approval / confirmation surface that gates a human decision, when the underlying action may silently transform its input.
- Especially when the transformation's only trace is in a machine-facing artifact (a prompt, a job payload, a serialized record) the human never reads.
- Audit move: for each silent transformation in the pipeline, ask "on which surface does the human (or its agent proxy) decide, and does *that* surface show this?" — then enumerate **all** such surfaces, not just the first.

## Examples

Before — the truncation is recorded only in the model-facing block; the preview shows just the path:

```python
# compose(): note appended inside the composed task (model sees it; human doesn't)
text += _TRUNCATION_NOTE.format(kib=MAX_FILE_BYTES // 1024)
...
# render_file_inputs(paths): only the path label reaches the preview
out.extend(f"  - {_sanitize(p)}" for p in paths)
```

After — the transformation is returned as data and disclosed on every surface the decision touches:

```python
# compose() -> (composed_task, doc_paths, truncated_paths)
# preview (run.py): flag truncated files where the human approves
doc_block = render_file_inputs(doc_paths, truncated_docs)
# render_file_inputs marks each: "  - <path>  ⚠ truncated — the review will run over a PARTIAL file"

# run path (both runners, no preview to disclose it): warn before any model call
for p in truncated_docs:
    print(f"[cadre] warn: --doc {_sanitize(p)} truncated to {MAX_FILE_BYTES // 1024} KiB "
          "— reviewing a partial file", file=sys.stderr)
```

Verify the *control*, not just the formatter: a test asserts the oversize file is flagged on the preview AND that a non-preview run warns on stderr — both the surfaces a human (or agent) could approve from.

## Related

- `docs/solutions/design-patterns/sanitize-trust-surface-renders-against-terminal-escapes.md` — the sibling axis: keep untrusted content from *spoofing* the approval surface. This doc is the *disclosure* axis (the surface must *show* what it vouches for). The two compose: an approval surface must be both un-spoofable and non-omitting.
- `docs/solutions/design-patterns/enumerate-consumers-when-a-new-value-aliases-a-load-bearing-state.md` — the same "enumerate every consumer/surface and treat them as one unit" discipline, here applied to approval surfaces rather than result-state branches.
- GitHub issue #5 — the deferred security pass (a non-forgeable, preview-bound approval artifact); the same surface this disclosure rides on.
