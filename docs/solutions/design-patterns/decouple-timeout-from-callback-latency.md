---
title: A deadline-bounded drain must not gate the deadline on callback latency
date: 2026-06-22
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "A deadline-bounded loop collects results from concurrent workers and runs a caller callback per result"
  - "That callback does real work (I/O, rendering, capture) whose latency is variable or unbounded"
  - "Already-completed work must survive — slow collection must not be reclassified as a timeout"
  - "Fanning out blocking calls and draining their completions under one shared wall-clock deadline"
tags: [concurrency, timeout, deadline, fan-out, progress-hook, side-effects, threads, fleet-engine]
---

# A deadline-bounded drain must not gate the deadline on callback latency

## Context

The fleet engine fans specialists out across daemon threads and drains their results under one shared wall-clock deadline (the per-call timeout backstop). When live progress was added, the drain loop gained a per-result callback — the progress hook — which in production renders a `[cadre]` stderr breadcrumb **and** does synchronous per-lane file capture (`save_lane`).

The callback ran *inside* the deadline-bounded loop, and the deadline was re-checked only **between** callbacks. So a slow callback on one lane could let the shared deadline lapse while other lanes' results — already finished and sitting in the completion queue — went unread. The engine then fabricated those completed lanes as **timed out**: real model work discarded, and a degenerate synthesis built from a lone "survivor".

The bug was invisible to the test suite (fakes return instantly, so the hook never stalled) and to ten same-model review personas plus an advisor pass. **Two independent cross-model reviewers — Codex and CodeRabbit — each flagged it.** Reproduced deterministically: an instant fake client + a hook that sleeps 250 ms under a 100 ms timeout returned two completed lanes as `timed_out=True`.

## Guidance

Keep timeout classification **independent of callback latency**. The deadline exists to bound how long you *wait* on a wedged worker — not to discard work that already finished. In a fan-out drainer:

1. **Each worker stamps its own completion timestamp** when it pushes its result. Compute `elapsed` from that stamp, not from when the drainer gets around to pulling it — so a slow callback can't inflate elapsed either.
2. **"Pushed a result" is the definition of completed.** A worker is a timeout *iff it never pushed*.
3. **Drain in phases:** (1) wait for arrivals up to the deadline; (2) when the deadline lapses, **non-blockingly drain whatever already arrived** — these completed in time even if a slow callback delayed the pull; (3) only fabricate timeouts for workers still missing.

Companion gotcha when you add the completion timestamp: **capture the worker's launch time *before* starting its thread.** If you capture it after `thread.start()`, an instant worker can stamp its completion before the launch timestamp is even taken, and `completed - launched` goes negative.

## Why This Matters

Coupling the deadline check to side-effect latency turns an *auxiliary* concern (rendering / capture I/O) into a *correctness* hazard for the *primary* result. The failure mode is the worst kind: **silent and data-corrupting** (completed lanes reported as timeouts), **load-bearing** (it drives a degraded synthesis), and triggered by **realistic** conditions (slow disk, many lanes, contended I/O) that never appear in fast-fake tests. It is exactly the class of bug that escapes happy-path coverage and same-model review — the deadline and the callback look independent until you notice they share the loop's wall-clock.

This is the *collection-side* companion to keeping the engine pure: the progress hook is an edge side-effect, and its latency must not leak into the engine's timeout accounting any more than its I/O may leak into the engine core (see Related).

## When to Apply

- A deadline-bounded loop collects results from concurrent workers and runs a caller callback per result.
- The callback does real work whose latency is variable or unbounded (I/O, rendering, capture, logging).
- Already-completed work must survive — slow collection must not be reclassified as a failure/timeout.
- Fanning out blocking calls and draining their completions under one shared wall-clock deadline.

## Examples

**Before** — the deadline check and the slow callback share the loop, so a slow hook starves the remaining (already-queued) results into false timeouts:

```python
while len(collected) < n:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break                          # deadline lapsed — but results may be queued!
    idx, lane = done_q.get(timeout=remaining)
    collected[idx] = lane
    progress(LaneDone(lane))           # slow: stderr render + save_lane file I/O
# every uncollected lane is fabricated as a timeout — WRONG for queued-but-unread lanes
```

**After** — workers stamp completion; the drain is phased so "pushed == completed":

```python
# worker stamps its OWN completion time at push
done_q.put((idx, result, time.monotonic()))

# Phase 1: wait up to the shared deadline, emitting in arrival order
while len(collected) < n:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    try:
        idx, lane, done_at = done_q.get(timeout=remaining)
    except queue.Empty:
        break
    record(idx, lane, done_at)          # elapsed = done_at - launched_at[idx]
    progress(LaneDone(lane))

# Phase 2: drain the backlog — a pushed result completed in time, even if a
# slow Phase-1 hook delayed the pull. get_nowait, so a wedged worker isn't waited on.
while len(collected) < n:
    try:
        idx, lane, done_at = done_q.get_nowait()
    except queue.Empty:
        break
    record(idx, lane, done_at)
    progress(LaneDone(lane))

# Phase 3: only never-pushed workers are genuine timeouts
for idx in still_missing:
    fabricate_timeout(idx)
```

**The launch-time gotcha** (introduced by the completion timestamp):

```python
# WRONG — an instant worker can stamp completed_at before this line runs → negative elapsed
def _start_lane(idx, fn, q, name):
    threading.Thread(target=lambda: q.put((idx, fn(), time.monotonic()))).start()
    return time.monotonic()             # launched_at captured AFTER start

# RIGHT — capture before start, so launched_at <= completed_at always holds
def _start_lane(idx, fn, q, name):
    launched_at = time.monotonic()
    threading.Thread(target=lambda: q.put((idx, fn(), time.monotonic()))).start()
    return launched_at
```

## Related

- [Wall-clock timeouts over uncancellable calls need daemon threads](daemon-threads-for-uncancellable-timeouts.md) — the *cancellation* facet of the same `_fan_out` / `_start_lane` fan-out; this doc is the *collection* facet (don't let the drain's callback latency corrupt the deadline).
- [Side-effects at the edge: pure engine core](../architecture-patterns/side-effects-at-the-edge-pure-engine-core.md) — the progress hook is the edge seam; keeping its *latency* out of the engine's timeout is the runtime companion to keeping its *I/O* out of the engine core.
- Process note: this was caught by **cross-model** review (Codex + CodeRabbit), not by same-model review (10 personas + the advisor). For load-bearing concurrency, a cross-model pass earns its keep — same-model reviewers share blind spots, and "the deadline is coupled to the callback's latency" is one of them.
- Implemented in `fleet_engine/engine.py` (`_fan_out`, `_start_lane`); guarded by `tests/test_progress.py` (`TestSlowHookDoesNotCauseFalseTimeouts`) and `tests/test_engine.py` (`TestCleanExitOnHang`).
