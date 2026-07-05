"""Standalone CLI: ``validate`` a fleet spec and ``run`` it on a task.

This is the no-Hermes entry surface — it enables the baseline gut-check and
dogfooding. Rendering lives in ``cadre.render``, shared with the skill.
Usage:

    python -m cadre.cli validate cadre/data/fleets/research-swarm.example.yaml
    python -m cadre.cli run cadre/data/fleets/research-swarm.example.yaml --task "..."
    python -m cadre.cli run cadre/data/fleets/research-swarm.example.yaml --task "..." --no-capture
    python -m cadre.cli setup
    python -m cadre.cli verify-palette
    HERMES_SKILLS_DIR=<dir> python -m cadre.cli install-skill
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

from cadre import provision, verify_palette
from cadre.capture import prepare_run_dir, save_run
from cadre.config import ConfigError, FleetConfig
from cadre.file_input import MAX_FILE_BYTES, compose
from cadre.install_skill import install_skill
from cadre.model_client import ModelClient
from cadre.personas import default_pool_dir, resolve
from cadre.preview_lint import render_preview_warnings
from cadre.progress_runner import run_with_progress
from cadre.render import render_result
from cadre.text_safety import sanitize as _sanitize


def validate_command(path: str) -> tuple[int, str]:
    try:
        cfg = FleetConfig.load(path)
        resolve(cfg, default_pool_dir())
    except ConfigError as err:
        return 1, str(err)
    except FileNotFoundError:
        return 1, f"Fleet spec not found: {path}"
    # Fleet-controlled fields are _sanitize()d — validate output is a terminal surface,
    # same as the preview, so a tampered fleet must not inject escapes into it.
    lines = [f"OK: {_sanitize(cfg.name)}"]
    if cfg.description:
        lines.append(f"  {_sanitize(cfg.description)}")  # catalog metadata (R11)
    lines.append(f"  specialists: {len(cfg.specialists)}")
    for s in cfg.specialists:
        lines.append(
            f"    - {_sanitize(s.role)}: {_sanitize(s.provider)}/{_sanitize(s.model)} "
            f"tools={[_sanitize(t) for t in s.toolset]}"
        )
    if cfg.convergence == "collect":
        lines.append("  convergence: collect (no synthesizer)")
    elif cfg.convergence == "judge":
        lines.append("  convergence: judge")
        lines.append(f"  judge: {_sanitize(cfg.judge.provider)}/{_sanitize(cfg.judge.model)}")
    else:
        lines.append(f"  synthesis: {_sanitize(cfg.synthesis.provider)}/{_sanitize(cfg.synthesis.model)}")
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
        resolve(cfg, default_pool_dir())
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
            run_dir = save_run(cfg, result, run_dir)
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


def setup_command(
    venv_python: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Provision ~/.cadre from the installed cadre package (KTD11 fail-closed).

    Resolves the Hermes Python to RECORD, precedence:
      1. venv_python (e.g. --venv-python), expanduser'd.
      2. env['CADRE_HERMES_PYTHON'] (env defaults to os.environ), expanduser'd.
      3. sys.executable — when `cadre setup` runs as the installed console
         script inside the target venv (the load-bearing KTD2 install target),
         sys.executable IS that venv's authoritative Python; no probe list
         needed (unlike scripts/resolve_venv.py's bootstrap-time resolver,
         which runs BEFORE cadre is installed anywhere).

    Before any write, verifies `import cadre` resolves under the recorded
    Python (provision.verify_importable, a subprocess check) and fails closed
    — clear message, non-zero exit, nothing written, not even ~/.cadre itself —
    if it does not. Only on success does it scaffold + seed + write config:
    ensure_cadre_dirs() -> seed_starter_fleets() -> seed_personas() ->
    seed_palette_candidates() -> write_config(). Idempotent overall (each step
    preserves existing operator edits; write_config overwrites the single
    config line every run).

    Args:
        venv_python: Explicit Hermes Python path (highest precedence).
        env: Mapping to use instead of os.environ. Defaults to os.environ.

    Returns:
        (exit_code, message) — 0 and a confirmation on success; 1 and a clear
        error (naming the recorded path) on the KTD11 fail-closed path.
    """
    if env is None:
        env = os.environ

    if venv_python is not None:
        recorded_python = str(Path(venv_python).expanduser())
    elif env.get("CADRE_HERMES_PYTHON"):
        recorded_python = str(Path(env["CADRE_HERMES_PYTHON"]).expanduser())
    else:
        recorded_python = sys.executable

    if not provision.verify_importable(recorded_python):
        return (
            1,
            f"error: `import cadre` does not resolve under {recorded_python}\n"
            "Install cadre into that interpreter first, e.g.:\n"
            f'  {recorded_python} -m pip install --force-reinstall --no-deps '
            '"git+https://github.com/jarodtaylor/cadre@<ref>"\n'
            "then re-run `cadre setup`. Nothing was written. See docs/RUNBOOK.md.",
        )

    try:
        home = provision.ensure_cadre_dirs()
        provision.seed_starter_fleets(home)
        provision.seed_personas(home)
        provision.seed_palette_candidates(home)
        provision.write_config(recorded_python)
    except OSError as exc:
        # The pre-#11 scripts/resolve_venv.py wrapped this identical
        # scaffold-then-seed-then-write sequence in the same guard (there:
        # print-to-stderr-then-return-1; here: the (exit_code, message) tuple
        # contract this function already uses elsewhere) — an mkdir/write
        # failure (e.g. a read-only $HOME, a full disk) must degrade to a
        # clean error, never a raw traceback.
        return (1, f"error provisioning ~/.cadre: {exc}")

    return (
        0,
        f"Provisioned {home} from the installed cadre package.\n"
        f"Recorded Hermes Python: {recorded_python}\n"
        "Next: edit ~/.cadre/palette-candidates.yaml for your authenticated providers, "
        "then run `cadre verify-palette`.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cadre", description="Run provider-neutral agent fleets.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Validate a fleet spec")
    p_validate.add_argument("spec", help="Path to a fleet YAML spec")

    p_run = sub.add_parser("run", help="Run a fleet on a task")
    p_run.add_argument("spec", help="Path to a fleet YAML spec")
    # --task is optional (default None): a --doc-only run is valid, so it can't be an
    # argparse-required flag. The at-least-one check below enforces "task and/or doc".
    p_run.add_argument(
        "--task",
        default=None,
        help="The task / query for the fleet (required unless --doc is given)",
    )
    p_run.add_argument(
        "--doc",
        action="append",
        default=[],
        metavar="PATH",
        help="Read a file's contents into the task (repeatable); use with or instead of --task",
    )
    p_run.add_argument(
        "--no-capture",
        action="store_true",
        default=False,
        help="Disable run capture (no folder written to disk)",
    )

    p_setup = sub.add_parser("setup", help="Provision ~/.cadre from the installed package")
    p_setup.add_argument(
        "--venv-python",
        metavar="PATH",
        default=None,
        help=(
            "Hermes Python to record in ~/.cadre/config (highest precedence; "
            "overrides CADRE_HERMES_PYTHON env and the sys.executable default)"
        ),
    )

    sub.add_parser(
        "verify-palette",
        help=(
            "Verify authenticated (provider, model) candidates against this host "
            "and write ~/.cadre/palette.yaml (host-only, live — makes real model calls)"
        ),
    )

    p_install_skill = sub.add_parser(
        "install-skill",
        help="Materialize the Hermes cadre-fleet skill into a skills directory",
    )
    p_install_skill.add_argument(
        "--skills-dir",
        metavar="PATH",
        default=None,
        help=(
            "Target Hermes skills directory (falls back to HERMES_SKILLS_DIR "
            "if not given)"
        ),
    )

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        code, out = validate_command(args.spec)
    elif args.cmd == "setup":
        code, out = setup_command(args.venv_python)
    elif args.cmd == "verify-palette":
        # verify_palette.main() streams its own live per-candidate progress lines
        # directly to stdout as it runs (unlike validate/setup's single end-of-call
        # message) — so it owns its own printing; propagate its exit code as-is
        # rather than forcing it through the (code, out); print(out) shape below.
        return verify_palette.main()
    elif args.cmd == "install-skill":
        code, out = install_skill(args.skills_dir)
    else:
        # A real run needs at least one of --task / --doc. Explicit exit-2 usage
        # error (not argparse's required-flag error, since --task is now optional).
        if args.task is None and not args.doc:
            print("Provide --task and/or --doc.")
            return 2
        # Compose any --doc files into the task at the caller layer — the engine
        # stays path-free. compose raises ConfigError on an unreadable --doc, caught
        # here so it exits cleanly (exit 1) rather than tracebacking (KTD5). The
        # composed string is passed to run_command, whose signature is unchanged.
        try:
            # cli.py has no --preview surface to disclose truncation, so the warn
            # below is the operator's only signal; the doc-paths list is unused here.
            task, _doc_paths, truncated = compose(args.task, args.doc)
        except ConfigError as err:
            print(str(err))
            return 1
        # Surface oversize truncation on the run path too (run has no preview gate) —
        # the in-block note is model-facing, so without this the operator never knows
        # the review ran over a partial file.
        for p in truncated:
            print(
                f"[cadre] warn: --doc {_sanitize(p)} truncated to {MAX_FILE_BYTES // 1024} KiB "
                "— reviewing a partial file",
                file=sys.stderr,
            )
        code, out = run_command(args.spec, task, capture=not args.no_capture)
    print(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
