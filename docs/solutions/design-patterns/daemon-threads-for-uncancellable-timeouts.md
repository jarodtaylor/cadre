---
title: Wall-clock timeouts over uncancellable calls need daemon threads, not ThreadPoolExecutor
date: 2026-06-17
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: medium
applies_when:
  - "Bounding wall-clock time on a blocking call you cannot cancel (a third-party SDK or network call)"
  - "The call can hang indefinitely and the process must still return and exit cleanly"
  - "Working in Python, or any runtime that cannot forcibly kill a thread"
  - "Fanning out blocking calls concurrently and degrading on the slow ones"
tags: [concurrency, threading, timeout, daemon-threads, python, graceful-degradation]
---

# Wall-clock timeouts over uncancellable calls need daemon threads, not ThreadPoolExecutor

## Context

The fleet engine fans out one blocking model call per specialist, then runs a synthesizer call. Any of these can hang on a stuck provider. We needed a per-call wall-clock timeout so a hung lane can't stall the fan-out — and, just as important, can't keep the process from exiting. The obvious tool (`ThreadPoolExecutor` + `future.result(timeout=...)`) returns from the *call* on time but does **not** solve the second half.

## Guidance

For a wall-clock timeout over a blocking call you cannot cancel (a dependency that exposes no cancellation token), run the call in a **daemon thread** and `join(deadline)`; treat an unfinished thread as a timeout failure. Do **not** use `ThreadPoolExecutor`.

Python cannot kill a thread. `ThreadPoolExecutor` registers an `atexit` hook that **joins its (non-daemon) worker threads at interpreter shutdown** — so a genuinely hung worker blocks the process from exiting, *even if* you already returned from `future.result(timeout=...)`, and even with `shutdown(wait=False, cancel_futures=True)` (`cancel_futures` only drops tasks that never started). A daemon thread is abandoned at interpreter exit, so the process exits cleanly while the doomed call dies with it.

This is a *soft* deadline: the underlying call keeps running until it returns on its own. So (a) never retry on a timeout, and (b) prefer to layer it over the dependency's own request/network timeout — that inner layer is what actually aborts the work and stops spend; the daemon deadline is the outer backstop for when the inner layer wedges.

## Why This Matters

A timeout that returns control but hangs the process at exit is worse than no timeout: the CLI looks done but never returns, and a long-lived host leaks a non-daemon thread its runtime will later join. The daemon-thread version is the only one that bounds *both* the call and interpreter exit.

## When to Apply
- Bounding wall-clock on a blocking call with no cancellation API.
- The call can hang and the process must still exit.
- Python, or any runtime that can't force-kill a thread.

## Examples

Verified empirically (Python 3.11) — time to **interpreter exit** with a worker sleeping 5s under a 0.3s timeout:

| Strategy | Time to exit |
|---|---|
| `with ThreadPoolExecutor()` + `future.result(timeout=0.3)` | ~5.0s (blocks) |
| `shutdown(wait=False, cancel_futures=True)` | ~5.0s (still blocks) |
| daemon `Thread` + `join(0.3)` | ~0.3s ✓ |

Shape of the implementation:

```python
def _start_daemon(fn, name):
    holder = []
    t = threading.Thread(target=lambda: holder.append(fn()), name=name, daemon=True)
    t.start()
    return t, holder

# Fan out: start all, THEN join each against one shared deadline
# (total wall-clock ~= timeout, not N x timeout).
deadline = time.monotonic() + call_timeout
for spec, (t, holder) in started:
    t.join(max(0.0, deadline - time.monotonic()))
    results.append(holder[0] if holder else timeout_failure(spec))
```

Closure gotcha: build each thunk in its own frame (a helper that takes `spec` and returns the lambda), or bind `spec=spec` — otherwise a thread started inside the loop can capture the loop's *last* item.

A subprocess test is what actually guards the exit property: an in-process test can prove "returns within N seconds" but cannot observe interpreter shutdown, so a silent revert to `ThreadPoolExecutor` would stay green. Run `run_fleet` with a hung call in a child process and assert it exits.

## Related
- [Empty toolset collapsed to ALL tools](../security-issues/empty-toolset-collapsed-to-all-tools.md) — another lesson from the same engine-hardening pass.
- Implemented in `fleet_engine/engine.py` (`run_fleet`, `_start_daemon`, `_collect`); guarded by `tests/test_engine.py` (`TestCleanExitOnHang`).
