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
import sys
import time

# The installed `cadre` package is importable directly (R15) — this runner is
# executed by the Hermes venv Python, which is the same interpreter `cadre`
# was pip-installed into (KTD2); no repo-relative sys.path bootstrap needed.
from cadre.approval import (
    consume_approval,
    default_approval_path,
    surface_digest,
    write_approval,
)
from cadre.capture import prepare_run_dir, resolved_hermes_home, save_run
from cadre.config import ConfigError, FleetConfig
from cadre.exit_codes import ExitCode, status_to_exit
from cadre.file_input import MAX_FILE_BYTES, compose
from cadre.model_client import ModelClient
from cadre.personas import default_pool_dir, resolve
from cadre.preflight import preflight_refusal
from cadre.preview_lint import render_preview_warnings
from cadre.progress_runner import run_with_progress
from cadre.text_safety import sanitize as _sanitize  # GH #23
from cadre.render import (
    render_composed_task,
    render_file_inputs,
    render_fleet_preview,
    render_result,
)


def _surface_digest_for(cfg, composed_task):
    """The surface digest that BOTH the preview-mint (U3) and the run-enforce
    (U4) bind to — one call site so the two can never drift on which inputs
    they hash (KTD1). Reads the resolved profile internally so both paths
    bind the same HERMES_HOME."""
    return surface_digest(cfg, composed_task, resolved_hermes_home())


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
            "--task. The paths you give are shown in --preview before any run."
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
        "--approve-privileged",
        action="store_true",
        default=False,
        help=(
            "Mint a PRIVILEGED preview-bound approval for a fleet with "
            "allow_privileged_tools: true. A plain --preview does not approve a "
            "privileged fleet; this is the deliberate act that does."
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
        composed_task, doc_paths, truncated_docs = compose(args.task, args.doc)
    except ConfigError as err:
        print(str(err))
        return ExitCode.ERROR
    except OSError as exc:
        print(
            f"Could not read fleet spec '{args.fleet}': {exc}\n"
            "Pass a specific .yaml file (not a directory). List ~/.cadre/fleets/ for "
            "a curated fleet (seeded by `cadre setup`), or compose one from "
            "~/.cadre/palette.yaml."
        )
        return ExitCode.ERROR

    # --preview: render the parsed config and exit — BEFORE ModelClient, BEFORE
    # prepare_run_dir, BEFORE run_fleet. Zero model calls, zero capture.
    # THIS SHORT-CIRCUIT IS LOAD-BEARING: the human approves the parsed fleet,
    # not the agent's paraphrase of it.
    if args.preview or args.approve_privileged:
        # Show the profile the run will use (env-sourced, not part of the fleet
        # config) so the human okays the fleet AND the profile it runs under — an
        # unset HERMES_HOME silently falls back to the default, which is how a run
        # lands on the wrong (e.g. ungrounded) profile unnoticed.
        print(f"Profile (HERMES_HOME): {resolved_hermes_home()}")
        print(render_fleet_preview(cfg))
        # The --doc paths the run will read into the task, as the caller named them
        # (R7). Skipped when there are none, so a plain preview stays byte-identical.
        # Because compose already ran (above), a missing/unreadable/non-UTF-8 --doc
        # has already failed loudly — the preview doubles as a read-check (KTD7).
        doc_block = render_file_inputs(doc_paths, truncated_docs)
        if doc_block:
            print(doc_block)
        # Palette + focus validation — warn-never-block (KTD5). Warnings go to
        # stdout as part of the preview the human approves (no [cadre] stderr
        # infra on the preview path).
        print(render_preview_warnings(cfg))
        # Render the composed task the run will feed the models, then mint an
        # approval token bound to this exact surface. A task-less preview
        # (composed_task is None) renders the fleet shape only and mints nothing
        # — matching the run path's own None refusal.
        if composed_task is not None:
            print(render_composed_task(composed_task))
            if cfg.allow_privileged_tools and not args.approve_privileged:
                # A privileged fleet is NOT approved by a plain --preview — it
                # needs the deliberate --approve-privileged act (F2/R5). Mint
                # nothing; tell the operator how to approve it.
                print(
                    "\n⚠ This fleet enables privileged tools and is NOT approved by a "
                    "plain --preview. Re-run with --approve-privileged to mint a "
                    "privileged approval for this exact surface."
                )
            else:
                # Mint the approval. Flavor = whether this was the privileged act.
                # A non-privileged fleet under --approve-privileged still mints a
                # privileged-flavored token; the run accepts either flavor for a
                # non-privileged fleet (no lock-out).
                digest = _surface_digest_for(cfg, composed_task)
                try:
                    write_approval(digest, privileged=args.approve_privileged)
                except OSError as exc:
                    # Never a traceback (the repo's clean-error contract): a symlinked
                    # or otherwise-unwritable approval path fails closed HERE (no token
                    # minted -> the subsequent run is refused) with a clear message
                    # instead of crashing the preview. Sanitize the exception text (it
                    # can carry a path with control bytes) like every other rendered string.
                    print(
                        f"\nCould not write the approval token at {_sanitize(default_approval_path())}: "
                        f"{_sanitize(str(exc))}\n"
                        "Ensure ~/.cadre (or CADRE_APPROVAL_PATH) is an owner-only, "
                        "non-symlink path, then re-preview."
                    )
                    return ExitCode.ERROR
                flavor = "privileged " if args.approve_privileged else ""
                # Sanitize the path label: CADRE_APPROVAL_PATH is caller-controlled and
                # flows onto the approval surface, so a control byte in it must not spoof
                # this line (CodeRabbit — same trust-surface rule as every other print here).
                print(f"\nPreview-bound {flavor}approval written: {_sanitize(default_approval_path())}")
                print("This run is now approved to execute this exact previewed surface once.")
        return ExitCode.SUCCESS

    # Real run: need at least one of --task / --doc. composed_task is None only when
    # both are absent (compose returns the base task unchanged for a no-doc run).
    if composed_task is None:
        print("provide --task and/or --doc (unless --preview)")
        return ExitCode.USAGE

    # #62 preflight-refuse (R4, KTD4): refuse an off-palette model BEFORE
    # consume_approval and BEFORE run_with_progress — before any spend AND
    # before the one-shot approval token is burned. A host-side palette fix
    # (adding the model via `cadre verify-palette`) leaves the fleet YAML —
    # and therefore the surface digest — unchanged, so the same approval
    # still works on a subsequent run. An absent or malformed palette ALSO
    # refuses (fail closed; absent flipped by #61) — the refusal text names
    # the host-appropriate fix (see preflight_refusal).
    refusal = preflight_refusal(cfg)
    if refusal is not None:
        print(refusal)
        return ExitCode.PREFLIGHT_REFUSE

    # Preview-bound approval gate (#5 Part 2, R1/R4): a real run executes only
    # when it presents a valid, unconsumed approval bound to THIS exact surface
    # — the config, the composed task (--task + --doc bytes), and the resolved
    # profile. consume_approval unlinks the token before reading, so one-shot
    # holds even for a mismatch (a burned attempt forces a fresh --preview).
    # Fail-closed: absent / mismatched / expired all refuse. The direct-human
    # `python -m cadre.cli` runner is intentionally NOT gated (a human
    # invoking it IS the operator); this binding scopes to the agent handoff.
    token = consume_approval()
    expected_digest = _surface_digest_for(cfg, composed_task)
    if token is None or token.digest != expected_digest or token.is_expired(time.time()):
        # Point at the command that actually MINTS for this fleet: a privileged
        # fleet's plain --preview mints nothing (U5), so telling its operator to
        # "--preview first" would loop — send them to --approve-privileged instead
        # (Copilot review).
        mint_cmd = "--approve-privileged" if cfg.allow_privileged_tools else "--preview"
        print(
            f"No valid preview-bound approval for this run. Run `{mint_cmd}` first to "
            "approve this exact fleet + task + docs + profile, then run again."
        )
        return ExitCode.ERROR

    # Privileged fleets require a privileged-flavored approval (R5/AE4). The digest
    # already binds allow_privileged_tools, so a tampered false->true is caught by
    # mismatch above; this forces the deliberate --approve-privileged act on a
    # legitimately-privileged fleet. A non-privileged fleet accepts either flavor
    # (no lock-out), so this check only fires for a privileged fleet.
    if cfg.allow_privileged_tools and not token.privileged:
        print(
            "This fleet enables privileged tools and requires a privileged approval. "
            "Run `--approve-privileged` (not a plain `--preview`) to approve it."
        )
        return ExitCode.ERROR

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
            return ExitCode.ERROR

    result = run_with_progress(
        cfg,
        composed_task,
        ModelClient(),
        run_dir=run_dir if capture else None,
    )
    output = render_result(result)

    if capture:
        try:
            run_dir = save_run(cfg, result, run_dir)
            output = f"{output}\n\nRun folder: {run_dir}"
        except Exception as exc:  # noqa: BLE001
            # Best-effort warning on the [cadre] stream: sanitize the exception text
            # (a run_dir in the message could carry control bytes) and never let a dead
            # stderr turn a save_run failure into a lost report — the warning print must
            # not raise before print(output).
            with contextlib.suppress(OSError, ValueError):
                print(f"[cadre] warn: failed to save run artifacts: {_sanitize(str(exc))}", file=sys.stderr)

    print(output)
    return status_to_exit(result.status)


if __name__ == "__main__":
    raise SystemExit(main())
