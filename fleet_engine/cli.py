"""Standalone CLI: ``validate`` a fleet spec and ``run`` it on a task.

This is the no-Hermes entry surface — it enables the baseline gut-check and
dogfooding. Rendering lives in ``fleet_engine.render``, shared with the skill.
Usage:

    python -m fleet_engine.cli validate fleets/research-swarm.yaml
    python -m fleet_engine.cli run fleets/research-swarm.yaml --task "..."
    python -m fleet_engine.cli run fleets/research-swarm.yaml --task "..." --no-capture
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from fleet_engine.capture import prepare_run_dir, save_run
from fleet_engine.config import ConfigError, FleetConfig
from fleet_engine.model_client import ModelClient
from fleet_engine.preview_lint import render_preview_warnings
from fleet_engine.progress_runner import run_with_progress
from fleet_engine.render import _sanitize, render_result


def validate_command(path: str) -> tuple[int, str]:
    try:
        cfg = FleetConfig.load(path)
    except ConfigError as err:
        return 1, str(err)
    except FileNotFoundError:
        return 1, f"Fleet spec not found: {path}"
    lines = [f"OK: {cfg.name}", f"  specialists: {len(cfg.specialists)}"]
    for s in cfg.specialists:
        lines.append(f"    - {s.role}: {s.provider}/{s.model} tools={s.toolset}")
    if cfg.convergence == "collect":
        lines.append("  convergence: collect (no synthesizer)")
    else:
        lines.append(f"  synthesis: {cfg.synthesis.provider}/{cfg.synthesis.model}")
    # Palette + focus validation — warn-never-block (KTD5). A missing or
    # unreadable palette yields a "validation skipped" note; validation
    # never causes a non-zero exit code from validate_command.
    lines.append(render_preview_warnings(cfg))
    return 0, "\n".join(lines)


def run_command(
    path: str,
    task: str,
    client: ModelClient | None = None,
    *,
    run_dir: Path | None = None,
    capture: bool = True,
    progress_stream=None,
) -> tuple[int, str]:
    """Load the fleet spec and run it on ``task``.

    When ``capture`` is True (the default):
    - Resolves ``run_dir`` from env/default if not injected.
    - Creates the directory owner-only (0o700) BEFORE calling ``run_fleet`` —
      a bad or unwritable location fails fast with a clear error and makes no
      model calls.
    - After the run, writes the captured artifacts via ``save_run``; a write
      failure is warned to stderr but does not discard the synthesis output.
    - Appends the run-folder path to the returned output string (R5).

    When ``capture`` is False, behaves exactly as before (no dir, no save_run).
    """
    try:
        cfg = FleetConfig.load(path)
    except ConfigError as err:
        return 1, str(err)
    except FileNotFoundError:
        return 1, f"Fleet spec not found: {path}"

    if capture:
        try:
            run_dir = prepare_run_dir(task, run_dir=run_dir)
        except OSError as exc:
            # run_dir is still None here on the default path (prepare_run_dir
            # raised before reassigning it); the OSError carries the attempted
            # path, so don't interpolate run_dir and risk printing "None".
            return (
                1,
                f"Cannot create run directory: {exc}\n"
                "Use --no-capture to bypass run capture.",
            )

    result = run_with_progress(
        cfg,
        task,
        client or ModelClient(),
        run_dir=run_dir if capture else None,
        progress_stream=progress_stream,
    )
    output = render_result(result)
    exit_code = 0 if result.ok else 1

    if capture:
        try:
            save_run(cfg, result, run_dir)
            output = f"{output}\n\nRun folder: {run_dir}"
        except Exception as exc:  # noqa: BLE001
            # Best-effort warning on the [cadre] stream: route to the SAME stream as the
            # breadcrumbs (progress_stream), sanitize the exception text (a run_dir in
            # the message could carry control bytes that forge a line), and never let a
            # dead stream turn a save_run failure into a lost report.
            warn_stream = progress_stream if progress_stream is not None else sys.stderr
            with contextlib.suppress(OSError, ValueError):
                print(f"[cadre] warn: failed to save run artifacts: {_sanitize(str(exc))}", file=warn_stream)

    return exit_code, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet", description="Run provider-neutral agent fleets.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Validate a fleet spec")
    p_validate.add_argument("spec", help="Path to a fleet YAML spec")

    p_run = sub.add_parser("run", help="Run a fleet on a task")
    p_run.add_argument("spec", help="Path to a fleet YAML spec")
    p_run.add_argument("--task", required=True, help="The task / query for the fleet")
    p_run.add_argument(
        "--no-capture",
        action="store_true",
        default=False,
        help="Disable run capture (no folder written to disk)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        code, out = validate_command(args.spec)
    else:
        code, out = run_command(args.spec, args.task, capture=not args.no_capture)
    print(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
