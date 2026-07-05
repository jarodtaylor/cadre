"""Edge orchestration: drive a fleet run with live progress + incremental capture.

Both entry points — ``cli.py`` and ``skills/cadre-fleet/run.py`` — share this so
they behave IDENTICALLY (the U4 verification). It builds the breadcrumb renderer
(always) and wires per-lane capture (when a ``run_dir`` is given), emits the
edge-only events the engine can't know (validated, run folder, completion), runs
the heartbeat for the run's duration, and threads the progress hook into
``run_fleet``. Progress goes to stderr; the caller renders the result to stdout and
writes ``synthesis.md`` + ``manifest.json`` via ``save_run`` afterward (R9).

This is a caller-layer module (it composes the engine, the renderer, and the
capturer) — never imported by the engine, which stays a pure event source
(``docs/solutions/architecture-patterns/side-effects-at-the-edge-pure-engine-core.md``).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TextIO

from cadre.capture import lane_filename_map, round_subdir, save_lane, save_prompt
from cadre.config import FleetConfig
from cadre.engine import FleetResult, run_fleet
from cadre.model_client import ModelClient
from cadre.progress import Completion, LaneDone, RoundStarted, RunFolder, Validated
from cadre.render import ProgressRenderer


def run_with_progress(
    cfg: FleetConfig,
    task: str,
    client: ModelClient,
    *,
    run_dir: Path | None,
    progress_stream: TextIO | None = None,
) -> FleetResult:
    """Run ``cfg`` on ``task`` with live ``[cadre]`` breadcrumbs and incremental capture.

    ``run_dir`` is the resolved run folder when capture is ON, or ``None`` when capture
    is off (then: no per-lane files, no run-folder breadcrumb, no ``prompt.txt`` — R13).
    Returns the ``FleetResult``; the caller renders it to stdout and calls ``save_run``
    for the end-of-run synthesis + manifest.

    Progress (breadcrumbs + heartbeat) is written to ``progress_stream`` (default
    ``sys.stderr``); the report stays on stdout (R9). A per-lane capture write that
    fails never crashes the run — the hook runs on the engine's drainer thread, so it
    must not raise; failures are collected and warned after the heartbeat stops (so the
    warning can't interleave with a concurrent tick).
    """
    stream = progress_stream if progress_stream is not None else sys.stderr
    capture = run_dir is not None
    # Pre-compute the role->filename map once, before any lane finishes, so the
    # per-lane writer and the breadcrumb agree on every filename (KTD6).
    fmap = lane_filename_map([s.role for s in cfg.specialists]) if capture else None
    current_round: int = 0  # updated to event.round on each RoundStarted (iterative only)

    # filename_for must name where save_lane ACTUALLY writes — the breadcrumb's
    # "-> <file>" line means "this file is on disk". For iterative that is
    # round-<current_round>/<filename>, so the lambda reads current_round at call time;
    # the serial drainer sets it from the preceding RoundStarted before this round's
    # LaneDone breadcrumbs render (filename_for and the hook share the same closure cell).
    if not capture:
        filename_for = None
    elif cfg.topology == "iterative":
        filename_for = lambda role: f"{round_subdir(current_round)}/{fmap[role]}"
    else:
        filename_for = lambda role: fmap[role]

    renderer = ProgressRenderer(
        stream=stream,
        filename_for=filename_for,
    )
    capture_errors: list[tuple[str, Exception]] = []

    def hook(event):
        # Called only from the engine's single drainer/main thread — never a worker.
        # Write the per-lane artifact BEFORE emitting its breadcrumb, so the
        # "lane … -> <file>" line a supervisor parses reliably means the file is
        # already on disk (success case). A write failure is collected and warned
        # later — it must not crash the run.
        nonlocal current_round
        if isinstance(event, RoundStarted):
            current_round = event.round
        if capture and isinstance(event, LaneDone):
            try:
                if cfg.topology == "iterative":
                    # Write under round-N/ so each round's files are distinct;
                    # current_round is always set by the preceding RoundStarted.
                    save_lane(
                        event.result, fmap[event.result.role], run_dir,
                        subdir=round_subdir(current_round),
                    )
                else:
                    save_lane(event.result, fmap[event.result.role], run_dir)
            except Exception as exc:  # noqa: BLE001
                capture_errors.append((event.result.role, exc))
        renderer.emit(event)

    # Key on cfg.convergence (KTD1): a collect fleet runs no synthesizer regardless of
    # whether a stray synthesis block is present in the config.
    renderer.emit(Validated(
        fleet=cfg.name,
        specialists=len(cfg.specialists),
        synthesizers=1 if cfg.convergence == "synthesize" else 0,
        convergence=cfg.convergence,
    ))
    if capture:
        renderer.emit(RunFolder(path=str(run_dir)))
        try:
            save_prompt(run_dir, task)  # R2: prompt.txt written up front, at the edge
        except OSError as exc:
            # Symmetric with save_lane/save_run: a capture write failure degrades
            # (warn, continue), never aborts the run before any model call.
            renderer.note(f"failed to write prompt.txt: {exc}")

    start = time.monotonic()
    renderer.start_heartbeat()
    try:
        result = run_fleet(cfg, task, client, progress=hook)
    finally:
        # Always stop the timer, even if run_fleet raised — no leaked heartbeat thread.
        renderer.stop_heartbeat()
    renderer.emit(
        Completion(
            elapsed_s=time.monotonic() - start,
            run_dir=str(run_dir) if capture else None,
        )
    )

    # Route warnings through the renderer's guarded [cadre] stream so they ride
    # the same parseable, best-effort channel a supervising agent reads.
    for role, exc in capture_errors:
        renderer.note(f"failed to write artifact for lane '{role}': {exc}")

    return result
