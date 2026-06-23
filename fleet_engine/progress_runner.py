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

from fleet_engine.capture import lane_filename_map, save_lane, save_prompt
from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult, run_fleet
from fleet_engine.model_client import ModelClient
from fleet_engine.progress import Completion, LaneDone, RunFolder, Validated
from fleet_engine.render import ProgressRenderer


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

    renderer = ProgressRenderer(
        stream=stream,
        filename_for=(lambda role: fmap[role]) if capture else None,
    )
    capture_errors: list[tuple[str, Exception]] = []

    def hook(event):
        # Called only from the engine's single drainer/main thread — never a worker.
        renderer.emit(event)
        if capture and isinstance(event, LaneDone):
            try:
                save_lane(event.result, fmap[event.result.role], run_dir)
            except Exception as exc:  # noqa: BLE001
                # A per-lane write failure must not crash the run. Collect it and warn
                # after the heartbeat stops; save_run still attempts the manifest.
                capture_errors.append((event.result.role, exc))

    renderer.emit(Validated(fleet=cfg.name, specialists=len(cfg.specialists)))
    if capture:
        renderer.emit(RunFolder(path=str(run_dir)))
        save_prompt(run_dir, task)  # R2: prompt.txt written up front, at the edge

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

    for role, exc in capture_errors:
        print(f"Warning: failed to write artifact for lane '{role}': {exc}", file=stream)

    return result
