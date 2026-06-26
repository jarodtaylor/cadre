#!/usr/bin/env python
"""Hermes skill entry: run any Cadre fleet on a task.

Generalized runner — takes any fleet spec via --fleet (required). Supports a
--preview mode that renders the parsed FleetConfig to stdout and exits without
making any model calls. Runs on the Hermes host, where hermes-agent and the
user's providers are available.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

# Make the repo root importable when run from the skill directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from fleet_engine.capture import DEFAULT_HERMES_HOME, prepare_run_dir, save_run  # noqa: E402
from fleet_engine.config import ConfigError, FleetConfig  # noqa: E402
from fleet_engine.file_input import MAX_FILE_BYTES, compose  # noqa: E402
from fleet_engine.model_client import ModelClient  # noqa: E402
from fleet_engine.personas import default_pool_dir, resolve  # noqa: E402
from fleet_engine.preview_lint import render_preview_warnings  # noqa: E402
from fleet_engine.progress_runner import run_with_progress  # noqa: E402
from fleet_engine.render import (  # noqa: E402
    _sanitize,
    render_file_inputs,
    render_fleet_preview,
    render_result,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Cadre fleet on a task.")
    parser.add_argument("--fleet", required=True, help="Path to the fleet spec YAML")
    parser.add_argument(
        "--task",
        default=None,
        help="The task / query to run (required unless --preview or --doc is given)",
    )
    parser.add_argument(
        "--doc",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Read a file's contents into the task (repeatable). Each --doc PATH is "
            "appended as a labeled block, in flag order; use with or instead of "
            "--task. The resolved paths are shown in --preview before any run."
        ),
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        default=False,
        help=(
            "Render the parsed fleet config and exit without running. "
            "Shows synthesizer, allow_privileged_tools, each lane, and synthesis "
            "prompt — the human approves this output before any run."
        ),
    )
    parser.add_argument(
        "--no-capture",
        action="store_true",
        default=False,
        help="Disable run capture (no folder written to disk)",
    )
    args = parser.parse_args(argv)

    # Load + validate the fleet, resolve persona instructions, then compose any
    # --doc files into the task. ConfigError (invalid fleet OR an unreadable --doc)
    # and any OSError (missing fleet file, a directory path, bad perms) all produce
    # a clean message and exit 1 — compose raises ConfigError, so it shares the
    # handler that already guards load + resolve (KTD5).
    try:
        cfg = FleetConfig.load(args.fleet)
        resolve(cfg, default_pool_dir())
        composed_task, resolved_docs, truncated_docs = compose(args.task, args.doc)
    except ConfigError as err:
        print(str(err))
        return 1
    except OSError as exc:
        print(
            f"Could not read fleet spec '{args.fleet}': {exc}\n"
            "Pass a specific .yaml file (not a directory). Copy one from the repo's "
            "fleets/ into ~/.cadre/fleets/, or compose one from ~/.cadre/palette.yaml."
        )
        return 1

    # --preview: render the parsed config and exit — BEFORE ModelClient, BEFORE
    # prepare_run_dir, BEFORE run_fleet. Zero model calls, zero capture.
    # THIS SHORT-CIRCUIT IS LOAD-BEARING: the human approves the parsed fleet,
    # not the agent's paraphrase of it.
    if args.preview:
        # Show the profile the run will use (env-sourced, not part of the fleet
        # config) so the human okays the fleet AND the profile it runs under — an
        # unset HERMES_HOME silently falls back to the default, which is how a run
        # lands on the wrong (e.g. ungrounded) profile unnoticed.
        print(f"Profile (HERMES_HOME): {os.getenv('HERMES_HOME', DEFAULT_HERMES_HOME)}")
        print(render_fleet_preview(cfg))
        # Resolved --doc paths the run will read into the task (R7). Skipped when
        # there are none, so a plain preview stays byte-identical. Because compose
        # already ran (above), a missing/unreadable/non-UTF-8 --doc has already
        # failed loudly — the preview doubles as a read-check (KTD7).
        doc_block = render_file_inputs(resolved_docs, truncated_docs)
        if doc_block:
            print(doc_block)
        # Palette + focus validation — warn-never-block (KTD5). Warnings go to
        # stdout as part of the preview the human approves (no [cadre] stderr
        # infra on the preview path).
        print(render_preview_warnings(cfg))
        return 0

    # Real run: need at least one of --task / --doc. composed_task is None only when
    # both are absent (compose returns the base task unchanged for a no-doc run).
    if composed_task is None:
        print("provide --task and/or --doc (unless --preview)")
        return 2

    # No --preview here to disclose truncation (or it was skipped), so warn on the
    # [cadre] stream that an oversize --doc is being reviewed only partially — the
    # in-block note is model-facing and the operator would otherwise never know
    # (cross-model review: surface truncation on the run path, not just preview).
    for p in truncated_docs:
        print(
            f"[cadre] warn: --doc {_sanitize(p)} truncated to {MAX_FILE_BYTES // 1024} KiB "
            "— reviewing a partial file",
            file=sys.stderr,
        )

    capture = not args.no_capture

    if capture:
        try:
            run_dir = prepare_run_dir(composed_task)
        except OSError as exc:
            print(
                f"Cannot create run directory: {exc}\n"
                "Use --no-capture to bypass run capture."
            )
            return 1

    result = run_with_progress(
        cfg,
        composed_task,
        ModelClient(),
        run_dir=run_dir if capture else None,
    )
    output = render_result(result)

    if capture:
        try:
            save_run(cfg, result, run_dir)
            output = f"{output}\n\nRun folder: {run_dir}"
        except Exception as exc:  # noqa: BLE001
            # Best-effort warning on the [cadre] stream: sanitize the exception text
            # (a run_dir in the message could carry control bytes) and never let a dead
            # stderr turn a save_run failure into a lost report — the warning print must
            # not raise before print(output).
            with contextlib.suppress(OSError, ValueError):
                print(f"[cadre] warn: failed to save run artifacts: {_sanitize(str(exc))}", file=sys.stderr)

    print(output)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
