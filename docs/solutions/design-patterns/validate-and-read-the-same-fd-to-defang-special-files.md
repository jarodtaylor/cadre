---
title: "Validate and read the same fd (O_NONBLOCK + fstat) to make a caller-path file read hang-proof and race-free"
date: 2026-06-26
category: design-patterns
module: fleet_engine
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "You read a file from a caller- or env-supplied path without confinement (a cat-equivalent)"
  - "A non-regular target — directory, FIFO, device — could block open()/read() forever or differ from what a pre-check saw"
  - "You must refuse non-regular files WITHOUT a path jail (no realpath confinement; symlinks intentionally followed)"
tags: [toctou, fifo, o-nonblock, fstat, file-descriptor, no-hang, posix-footgun, fleet-engine]
---

# Validate and read the same fd (O_NONBLOCK + fstat) to make a caller-path file read hang-proof and race-free

## Context

Cadre's `--doc` reader (`fleet_engine/file_input._read_doc`) opens exactly the path the caller named — no confinement by design (the `cat`-equivalent: no `realpath` jail, no `O_NOFOLLOW`, symlinks intentionally followed). But it carries a hard requirement: **no lane hangs** (R6). A directory, a FIFO with no writer, or a device node would block `open()`/`read()` forever — and there is no model-call timeout in front of the read, so a `--preview --doc <fifo>` would hang at the very read-check sold as "fails *here*, before approval," with no human to interrupt on the agent path.

A first fix `os.stat(path)`'d the target and rejected a non-`S_ISREG` file before `open(path)`. A cross-model review (Codex) flagged it as **raceable**: a regular file swapped for a FIFO between the `stat` and the `open` can still block, and the bytes finally read can differ from what the check approved. A stat-then-open-by-*path* guard validates one thing and reads another.

## Guidance

Validate and read **the same file descriptor**, and open non-blocking so the open itself can never hang:

```python
fd = None
try:
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    # fstat the RAW fd and reject a non-regular file BEFORE handing it to a reader
    # (os.fdopen on a directory fd itself raises). Validates the SAME fd that is read.
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        errors.append(f"--doc path is not a regular file (refusing a directory / FIFO / device): {path!r}")
        return None
    with os.fdopen(fd, "rb") as fh:
        fd = None                      # fdopen now owns it; don't double-close in finally
        data = fh.read(MAX_FILE_BYTES + 1)
except (OSError, ValueError) as exc:   # ValueError = embedded NUL in the path
    errors.append(f"--doc file could not be read: {path!r}: {exc}")
    return None
finally:
    if fd is not None:                 # open failed (None) / reject / fstat error -> close
        try:
            os.close(fd)
        except OSError:
            pass
```

Three properties this buys, that stat-then-open does not:

- **No hang.** `O_NONBLOCK` makes `open()` of a FIFO/device return immediately instead of blocking for a writer; `O_NONBLOCK` has no effect on a regular-file read (always ready), so the happy path is unchanged.
- **No TOCTOU.** The `fstat` and the `read` are on the *same* fd, so a regular→FIFO swap after the check cannot slip past — there is no second path lookup to race.
- **Type guard, not a path jail.** It constrains the *kind* of file, never *which* path. No `O_NOFOLLOW`, so a symlink to a regular file is still followed and read — the deliberate no-confinement behavior is preserved.

## Why This Matters

"Reject non-regular files" sounds like a one-liner, but the obvious one-liner (`os.stat` then `open`) is both raceable and — if you keep `open()` blocking — still hangs on the swap it failed to prevent. The fd-based form is the only version that actually satisfies a *no-hang* requirement against an adversarial or merely unlucky filesystem race. It also mirrors the codebase's existing fd idiom: `personas.resolve` opens with `os.open(... | O_NOFOLLOW)` + `os.fdopen` to close a *symlink* TOCTOU on a *confined* pool. Same shape, different knob: `O_NONBLOCK` for hang/type, `O_NOFOLLOW` for symlink confinement — pick by what the surface is allowed to follow.

## When to Apply

- Any read of a caller- or env-supplied path that is intentionally unconfined (a `cat`-equivalent) but must still refuse special files and never hang.
- When a requirement says "no hang" / "fail fast" on a path that could be a FIFO, device, or directory.
- Not needed when the path set is fully trusted and known-regular (a build artifact you just wrote), or when full confinement (`realpath` + `O_NOFOLLOW`) is already in play for other reasons.

## Examples

The fd lifecycle is the part that bites — get it wrong and you leak descriptors or double-close:

- **`fstat` BEFORE `fdopen`.** `os.fdopen()` on a *directory* fd raises `IsADirectoryError` itself, so a "fstat inside the `with`" structure lets that escape as a traceback. Reject on the raw fd first.
- **Hand-off sentinel.** Set `fd = None` immediately after `os.fdopen` takes ownership, so the `finally` doesn't double-close the descriptor the `with` already closes.
- **`finally` covers the gaps.** `os.open` failed → `fd` stays `None` → finally skips. A non-regular reject or an `fstat` error → `fd` still open → finally closes it. Read raised inside the `with` → `fh` closed by the context, `fd` already `None` → finally skips.

Regression test (won't hang because the guard rejects before `open`-for-read):

```python
@unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo not available")
def test_fifo_errors_and_does_not_hang(self):
    fifo = os.path.join(self.tmp, "pipe"); os.mkfifo(fifo)
    with self.assertRaises(ConfigError) as ctx:
        compose("task", [fifo])
    self.assertIn("regular file", str(ctx.exception))
```

Guard the *intentional* symlink-following too, or a later "harden with `O_NOFOLLOW`" silently breaks it: a symlink→regular-file `--doc` must still read (same discipline as a `~`-path test for `expanduser`).

## Related

- `docs/solutions/design-patterns/expanduser-before-realpath-for-confined-paths.md` — the adjacent path-handling footgun; both are "the obvious path call has a silent failure mode," surfaced only by exercising the real special-input case.
- `fleet_engine/personas.py` `resolve` — the codebase's `os.open(... | O_NOFOLLOW)` + `os.fdopen` precedent this mirrors, for symlink confinement on a *trusted* pool rather than hang/type on an *unconfined* read.
- The "run it once for real" discipline (memory *dogfood-before-agent-handoff*): the FIFO no-hang and the directory-fd `fdopen` raise were both confirmed by running the real `--preview`/run path, not just mocks.
