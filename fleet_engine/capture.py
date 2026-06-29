"""Capture module — write a run folder with per-specialist markdown and a JSON manifest.

This is a caller-layer module: imported only by cli.py and skills/cadre-fleet/run.py.
NEVER imported by engine.py or model_client.py — the engine holds no file I/O.

Incremental capture split (U3):
  ``lane_filename_map(roles)`` pre-computes the role→filename map once, using the same
  stateful dedup that ``save_run`` used before the split.

  ``save_lane(lane, filename, run_dir)`` writes one specialist's ``.md`` the moment its
  lane finishes (called by the edge on each ``LaneDone`` event, R11).

  ``save_run(cfg, result, run_dir)`` now writes ONLY ``synthesis.md`` + ``manifest.json``
  (the run-completion marker). In a live run, per-lane files are already written by the
  time ``save_run`` runs; in a ``save_run``-only unit test they won't pre-exist — that is
  expected, the manifest still names them. ``prompt.txt`` moved to the edge (R2, U4).

``resolve_run_dir(task)`` is a PURE path resolver: returns ``CADRE_RUN_DIR``
(expanduser) when that env var is set, else ``~/.cadre/runs/<YYYY-MM-DD-HHMMSS>-<slug>``.
No filesystem access — just computes a Path.

``prepare_run_dir(task, run_dir=None)`` resolves, atomically reserves/creates, and probes
writability — raising OSError on any unrecoverable failure. Callers use this instead of
inline mkdir, then fail-fast on the OSError before calling run_fleet.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from fleet_engine.config import FleetConfig
from fleet_engine.engine import FleetResult, FleetStatus
from fleet_engine.judge_grade import parse_grades
from fleet_engine.render import _sanitize

# Default Hermes profile location — recorded in the manifest and shown in the
# fleet preview when HERMES_HOME is unset.
DEFAULT_HERMES_HOME = "~/.hermes"


def resolved_hermes_home() -> str:
    """The Hermes profile home as an absolute path (env-sourced) for the preview
    and manifest. Expands ``~`` and makes a relative value absolute so the operator
    sees the real directory the run uses — an unresolved ``~/.hermes`` or a relative
    path can hide which profile (and thus which tools/providers) is in play. An unset
    or empty ``HERMES_HOME`` falls back to the default. Symlinks are left unresolved:
    the named target is what the operator reasons about.
    """
    return os.path.abspath(os.path.expanduser(os.getenv("HERMES_HOME") or DEFAULT_HERMES_HOME))


# Default root for run folders when CADRE_RUN_DIR is not set.
_DEFAULT_RUNS_ROOT = "~/.cadre/runs"

# Max length of the task-derived slug in a default run-dir leaf. Truncation
# cuts on a word boundary (see _slugify), so a long task never ends mid-word.
_SLUG_MAX = 40

# Max length of the synthesizer-derived run title recorded in the manifest. The
# folder leaf built from it is bounded separately by _slugify's word-boundary cut.
_TITLE_MAX = 120

# A default run-dir leaf is "<YYYY-MM-DD-HHMMSS>-<slug>". This matches the fixed
# timestamp prefix so a post-run rename can preserve it (the prefix is the sort key
# under ~/.cadre/runs/); a leaf without it is caller-controlled and never renamed.
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-")


def _safe_role(role: str) -> str:
    """Return a filesystem-safe version of ``role`` for use in filenames ONLY.

    Replaces every character outside ``[A-Za-z0-9_-]`` with '-'.  Case is
    preserved (role-uniqueness is case-sensitive; lowercasing would collide
    'Web' and 'web').  Falls back to 'unknown' if the sanitized name is empty.

    The manifest's ``role`` field and the markdown header always use the TRUE
    (un-sanitized) role — this helper is called only where a filename is built.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", role)
    return sanitized or "unknown"


def _slugify(task: str) -> str:
    """Return a filesystem-safe slug derived from ``task``.

    Whitelist: ``[a-z0-9-]``.  All other characters (including path separators,
    '..', spaces, and control characters) are replaced with '-'.  The result is
    lowercase, has no leading/trailing dashes, and is bounded to ``_SLUG_MAX``
    characters.  A task that reduces to an empty string falls back to ``"run"``.

    Truncation cuts on a **word boundary**: an over-long slug is trimmed back to
    the last whole hyphen-separated word within the limit, so a long task yields
    ``...-agent-pattern`` rather than a sliced ``...-inspi``.  A single word
    longer than the limit (no boundary to cut on) is hard-cut.

    Security: the explicit ASCII whitelist ``[^a-z0-9]+`` (not ``\\W``) ensures
    no non-ASCII letters pass through, and the result can never contain '/' or
    '.' — so it cannot escape the runs directory when used as a path component.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    if len(slug) > _SLUG_MAX:
        head = slug[:_SLUG_MAX]
        boundary = head.rfind("-")
        # Cut at the last whole word; hard-cut only if the first word alone
        # already exceeds the limit (no usable boundary in range).
        slug = head[:boundary] if boundary > 0 else head
    return slug or "run"


def resolve_run_dir(task: str) -> Path:
    """Return the run directory Path for this task — PURE resolver, no FS access.

    If ``CADRE_RUN_DIR`` is set, use it verbatim (expanduser only — no stamp or
    slug leaf is appended; the caller controls the full path).

    Otherwise, build ``~/.cadre/runs/<YYYY-MM-DD-HHMMSS>-<slug>`` from the
    current time and a sanitized slug of the task.  The returned Path is NOT
    created — call ``prepare_run_dir`` to atomically reserve and create it.
    """
    cadre_run_dir = os.getenv("CADRE_RUN_DIR")
    if cadre_run_dir:
        return Path(cadre_run_dir).expanduser()

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    slug = _slugify(task)
    runs_root = Path(_DEFAULT_RUNS_ROOT).expanduser()
    return runs_root / f"{stamp}-{slug}"


def prepare_run_dir(task: str, run_dir: Path | None = None) -> Path:
    """Resolve, atomically create, and probe the run directory; return its Path.

    This is the single place that touches the filesystem for directory creation.
    Callers should call this BEFORE ``run_fleet`` so a bad path fails fast with
    no model calls wasted.

    Two modes:

    **Default path** (``run_dir`` is None AND ``CADRE_RUN_DIR`` is unset):
        Atomically reserves the directory by attempting ``mkdir(exist_ok=False)``.
        On ``FileExistsError`` (same-second collision), appends ``-2``, ``-3``, …
        until a unique leaf is created. The process that creates the directory is
        the sole owner — no TOCTOU race.

    **Explicit path** (``run_dir`` injected, OR ``CADRE_RUN_DIR`` is set):
        Creates with ``mkdir(parents=True, exist_ok=True)`` — reuse-by-design;
        the caller controls the full path.

    In both modes:
    - All mkdir calls run under a tightened ``umask(0o077)`` so every created
      directory component (including parents) is owner-only (0o700).
    - After creation, a writability probe creates and deletes a sentinel file.
      If that raises OSError, it is re-raised so the caller fails fast BEFORE
      any model calls are made.

    Args:
        task: The task string; used to derive the default leaf name.
        run_dir: Optional explicit path; overrides the default resolution.

    Returns:
        The Path of the created (or pre-existing explicit) run directory.

    Raises:
        OSError: If the directory cannot be created or is not writable.
    """
    # Decide which mode: explicit means the caller injected run_dir, OR
    # CADRE_RUN_DIR is set (resolve_run_dir will return it verbatim).
    cadre_run_dir = os.getenv("CADRE_RUN_DIR")
    explicit = (run_dir is not None) or bool(cadre_run_dir)

    if run_dir is None:
        run_dir = resolve_run_dir(task)

    old_umask = os.umask(0o077)
    try:
        if explicit:
            # Reuse-by-design: user controls the path.
            run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            # Atomic reservation: loop until we own a fresh leaf.
            base = run_dir
            counter = 2
            while True:
                try:
                    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                    break
                except FileExistsError:
                    run_dir = base.parent / f"{base.name}-{counter}"
                    counter += 1

        # Writability probe — fail fast before wasting model calls. Uses a
        # unique temp name (not a fixed sentinel) so two processes sharing an
        # explicit run_dir can't race on the probe's create/unlink.
        with tempfile.NamedTemporaryFile(
            dir=run_dir, prefix=".cadre-write-test-", delete=True
        ):
            pass
    finally:
        os.umask(old_umask)

    return run_dir


def lane_filename_map(roles: list[str]) -> dict[str, str]:
    """Return a deterministic role→filename map for the given specialist roles.

    Runs the same stateful dedup loop ``save_run`` used before the U3 split, so
    the filenames are identical to what ``save_run`` previously wrote on disk.
    Two distinct roles that collide under ``_safe_role`` (e.g. 'a/b' and 'a:b'
    both → 'a-b') get 'specialist-a-b.md' and 'specialist-a-b-2.md'.

    The map is keyed by the TRUE role (never the safe variant) so look-up is
    injective and callers can index by role regardless of the sanitized form.
    Roles are guaranteed unique per fleet (config validates), so this is safe.

    The map is computed once over the full specialist list before lanes launch,
    and is shared by both the per-lane writer (``save_lane``, called on each
    LaneDone) and the manifest builder (``save_run``).  Both reading the same
    pre-computed map means no lock is needed — each lane writes a distinct,
    pre-mapped file (KTD3 / atomic-reservation learning).

    Args:
        roles: Specialist roles in config order (``[spec.role for spec in fleet]``).

    Returns:
        ``{"web": "specialist-web.md", "a/b": "specialist-a-b.md", ...}``
    """
    used_stems: set[str] = set()
    # Local name avoids the module's `result` (a FleetResult) convention.
    mapping: dict[str, str] = {}
    for role in roles:
        stem = f"specialist-{_safe_role(role)}"
        if stem not in used_stems:
            used_stems.add(stem)
            filename = f"{stem}.md"
        else:
            counter = 2
            while f"{stem}-{counter}" in used_stems:
                counter += 1
            stem = f"{stem}-{counter}"
            used_stems.add(stem)
            filename = f"{stem}.md"
        mapping[role] = filename
    return mapping


def save_prompt(run_dir: Path, task: str) -> None:
    """Write ``prompt.txt`` (the run's task) into ``run_dir``, owner-only (0o600).

    Called by the edge BEFORE lanes launch (R2) — moved out of ``save_run`` in the
    U3 split so the run folder carries the task from the very start of a run, even
    if the run later crashes before ``save_run`` writes the manifest. Mirrors
    ``save_lane``'s mkdir so it lands whether or not the folder exists yet.
    """
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(run_dir / "prompt.txt", task)


def save_lane(lane, filename: str, run_dir: Path) -> None:  # lane: AgentResult
    """Write one specialist's ``.md`` the moment its lane finishes (R11).

    The edge calls this on each ``LaneDone`` event (capture on).  ``filename``
    comes from ``lane_filename_map`` — pre-computed over the full specialist list
    before any lanes launched, so each lane owns a distinct, pre-mapped file and
    no lock is needed (KTD3).

    Creates ``run_dir`` if it does not yet exist (mirrors ``save_run``'s mkdir so
    a lane-done arriving before ``save_run`` still lands safely).

    Args:
        lane:     An ``AgentResult`` from the engine (the ``LaneDone.result``).
        filename: The pre-computed filename from ``lane_filename_map``.
        run_dir:  The run directory (injected — same as ``save_run``'s run_dir).
    """
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(run_dir / filename, _specialist_md(lane))


def _synthesis_title(result: FleetResult) -> str | None:
    """The run's semantic title: the synthesizer's leading H1, reused at no extra
    model cost (#4). ``None`` when synthesis produced no report (collect or all-failed
    run — ``result.synthesis is None``) or the report did not open with an H1; the
    synthesis prompt does not mandate one, so this is best-effort. Sanitized (the H1
    is model-generated) and length-capped for the manifest.
    """
    if not result.synthesis:
        return None
    for line in result.synthesis.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            title = _sanitize(stripped[2:]).strip()
            return title[:_TITLE_MAX].strip() or None
        return None  # first non-blank line is not an H1 — no title
    return None


def _rename_to_title(run_dir: Path, title: str) -> Path:
    """Rename a default ``<stamp>-<slug>`` run dir to ``<stamp>-<title-slug>``,
    preserving the timestamp prefix and reusing the ``-2``/``-3`` collision suffix.

    Returns the new Path, or ``run_dir`` unchanged when the rename does not apply or
    fails — degrade, never crash a completed run. Skipped for caller-controlled dirs
    (``CADRE_RUN_DIR`` set, or a leaf without the stamp prefix). The folder name is
    built ONLY from ``_slugify(title)``: the title is model-generated, and slugifying
    is what defangs a path-like H1 (e.g. ``# ../../etc``) into a safe leaf — never use
    the raw title here.
    """
    if os.getenv("CADRE_RUN_DIR"):
        return run_dir  # caller owns the path
    match = _STAMP_RE.match(run_dir.name)
    if not match:
        return run_dir  # not a default stamped leaf
    title_slug = _slugify(title)
    if title_slug == "run":
        return run_dir  # _slugify's empty fallback — nothing better than the slug
    base = run_dir.parent / f"{match.group(1)}-{title_slug}"
    if base == run_dir:
        return run_dir  # title-slug already equals the current leaf
    target, counter = base, 2
    try:
        while target.exists():
            target = base.parent / f"{base.name}-{counter}"
            counter += 1
        run_dir.rename(target)
        return target
    except OSError:
        return run_dir  # rename failed — keep the completed run where it is


def save_run(cfg: FleetConfig, result: FleetResult, run_dir: Path) -> Path:
    """Write ``synthesis.md`` and ``manifest.json`` into ``run_dir`` (run-completion step).

    In a live run the edge has already written each specialist's ``.md`` via
    ``save_lane`` on each ``LaneDone`` event; ``save_run`` is called at the end
    to write the two run-wide artifacts.  In a ``save_run``-only unit test (no
    edge, no ``save_lane`` calls) the per-lane files won't pre-exist — that is
    expected; the manifest still names them.

    ``prompt.txt`` is written by the edge before lanes launch (R2, U4) — NOT here.

    ``manifest.json`` is written LAST intentionally — it serves as a
    run-completion marker.  A reader can treat its absence as a partial or
    failed write.

    The manifest's ``lanes[].file`` values come from ``lane_filename_map`` so
    they match exactly what ``save_lane`` wrote (or would write).

    The synthesizer is represented at run level only (synth_ok + synthesis.md),
    never as a per-lane entry.

    Args:
        cfg: The validated fleet configuration for this run.
        result: The FleetResult returned by run_fleet.
        run_dir: The directory to write artifacts into (injected — caller resolves).

    Returns:
        The final run directory — ``run_dir`` itself, or a sibling renamed to the
        synthesizer's H1 title (#4) when synthesis produced one and the dir is a
        default ``<stamp>-<slug>`` leaf. Callers print this for the ``Run folder:`` line.
    """
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Build the role→filename map from cfg.specialists — the SAME config-order source
    # the edge used for save_lane — so the manifest's lanes[].file always match the
    # on-disk filenames, by construction rather than by relying on result.specialists
    # happening to be config-ordered (matters only when two roles collide under
    # _safe_role and the -2 suffix would otherwise flip).
    fmap = lane_filename_map([spec.role for spec in cfg.specialists])
    lane_filenames = [fmap[lane.role] for lane in result.specialists]

    # synthesis.md — the synthesized output, or a failure note.
    _write(run_dir / "synthesis.md", _synthesis_md(result))

    # manifest.json — structured run-health record (written LAST as completion marker).
    _write(
        run_dir / "manifest.json",
        json.dumps(_build_manifest(cfg, result, lane_filenames), indent=2),
    )

    # Semantic run title (#4): if synthesis produced an H1, rename the default run
    # folder to <stamp>-<title-slug> so `ls ~/.cadre/runs/` reads well. The manifest
    # (the completion marker, written above) moves intact with the dir — no path is
    # stored in it. Return the final dir; the caller prints THIS for "Run folder:".
    title = _synthesis_title(result)
    if title:
        return _rename_to_title(run_dir, title)
    return run_dir


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` as UTF-8, owner-only (0o600).

    Opens with mode 0o600 so the file is owner-only *at creation* — never the
    momentary 0o644 a write-then-chmod leaves under a default umask. This bites
    in the explicit-dir case (e.g. ``CADRE_RUN_DIR`` pointing at a pre-existing
    world-traversable directory); the default ``~/.cadre`` chain is already
    0o700. The trailing chmod still tightens a pre-existing file, since O_CREAT
    does not alter the mode of a file that already exists (re-run into a dir).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    path.chmod(0o600)


def _specialist_md(lane) -> str:  # lane: AgentResult
    """Markdown file for one specialist lane (success or failure)."""
    lines = [
        f"# Specialist: {_sanitize(lane.role)}",
        "",
        f"- **Provider:** {_sanitize(lane.provider)}",
        f"- **Model:** {_sanitize(lane.model)}",
        f"- **OK:** {lane.ok}",
        f"- **Elapsed:** {lane.elapsed_s:.2f}s" if lane.elapsed_s is not None else "- **Elapsed:** n/a",
        f"- **Toolset:** {lane.toolset if lane.toolset else '(none)'}",
        "",
    ]
    if lane.ok:
        lines += ["## Output", "", lane.text or ""]
    else:
        lines += ["## Error", "", lane.error or "(no error detail)"]
    return "\n".join(lines)


def _synthesis_md(result: FleetResult) -> str:
    """Synthesis markdown: the synthesis text, a collect summary, or an accurate failure note."""
    # Collect mode: synthesis never ran by design — produce attributed specialist output.
    # Check convergence FIRST so synthesize mode falls through to its existing branches.
    if result.convergence == "collect":
        successes = result.successes
        if successes:
            # On-disk run records are themselves a trust surface: a persisted
            # synthesis.md vouches for which model produced which block. A tampered
            # fleet could embed newlines in role/provider/model to forge a fake
            # "--- role ---" delimiter and misattribute output, so the identity
            # fields are _sanitize'd here exactly as the terminal renderer does.
            # lane.text (model-generated CONTENT, not an identity claim) stays raw;
            # the two attributed-block renderers stay separate (terminal vs disk)
            # but share this identity-sanitize invariant.
            blocks = [
                f"--- {_sanitize(lane.role)} ({_sanitize(lane.provider)}/{_sanitize(lane.model)}) ---\n{lane.text or ''}"
                for lane in successes
            ]
            return "# Collected specialist outputs (collect mode — no synthesis)\n\n" + "\n\n".join(blocks)
        # All specialists failed in collect mode.
        n = len(result.specialists)
        return f"No specialist outputs — all {n} specialists failed (collect mode)."

    # Judge mode: write raw grade text (KTD8 — UNMUTATED, no _sanitize) + attributed blocks.
    if result.convergence == "judge":
        if result.status is FleetStatus.FAILED:
            # All specialists failed; judge was never invoked.
            n = len(result.specialists)
            return f"No specialist outputs — all {n} specialists failed (judge mode)."
        if result.status is FleetStatus.DEGRADED:
            # Judge ran but failed (degrade path). Match the note by its exact
            # prefix — the engine emits "judge failed: <error>". A loose substring
            # ("judge failed" in note) could match a specialist failure note whose
            # provider error text happens to contain that phrase (specialist notes
            # are appended first), misattributing the degrade reason on disk.
            judge_note = next(
                (note for note in result.notes if note.startswith("judge failed:")),
                "judge failed",
            )
            return f"No judge grade — {judge_note}."
        # status is SUCCESS — judge ran and succeeded.
        # KTD8: the judge text is written UNMUTATED so the record is accurate;
        # _sanitize is the render boundary's job, not capture's.
        # Identity fields on the delimiter (role/provider/model) are still
        # _sanitize'd to guard the trust surface (same as collect mode on-disk).
        successes = result.successes
        blocks = [
            f"--- {_sanitize(lane.role)} ({_sanitize(lane.provider)}/{_sanitize(lane.model)}) ---\n{lane.text or ''}"
            for lane in successes
        ]
        specialist_section = "\n\n".join(blocks)
        return (
            "# Judge grade\n\n"
            + (result.judge or "")
            + ("\n\n# Specialist outputs\n\n" + specialist_section if specialist_section else "")
        )

    # Synthesize mode: existing logic unchanged.
    if result.synthesis is not None:
        return result.synthesis

    # synthesis is None in two cases:
    #   status is FAILED  → all specialists failed, synthesis never attempted
    #   status is DEGRADED → synthesizer ran and failed
    if result.status is FleetStatus.FAILED:
        n_failed = len(result.failures)
        n_total = len(result.specialists)
        return f"No synthesis — {n_failed} of {n_total} specialists failed; synthesis was not attempted."

    # status is DEGRADED — synthesizer ran and failed.
    # Pull the synthesizer-failed note from result.notes if present.
    synth_note = next(
        (n for n in result.notes if "synthesizer failed" in n),
        "synthesizer failed",
    )
    return f"No synthesis — {synth_note}."


def _build_manifest(cfg: FleetConfig, result: FleetResult, lane_filenames: list[str]) -> dict:
    """Build the plain-dict manifest; serialized by the caller with json.dumps.

    ``lane_filenames`` is the list of actual on-disk filenames (one per specialist,
    in the same order as ``result.specialists``) so the manifest is an accurate
    index to the files — even when sanitization caused two roles to share a base
    name and one was renamed to ``...-2.md``.
    """
    participating_models = [
        {"provider": s.provider, "model": s.model}
        for s in result.specialists
    ]

    lanes = []
    for lane, filename in zip(result.specialists, lane_filenames):
        lanes.append({
            "role": lane.role,
            "provider": lane.provider,
            "model": lane.model,
            "ok": lane.ok,
            "error": lane.error,
            "elapsed_s": lane.elapsed_s,
            "toolset": list(lane.toolset),  # explicit list — never coerce [] to None
            "timed_out": lane.timed_out,
            "file": filename,
        })

    # Guard: cfg.synthesis is None for collect and judge fleets; cfg.judge is None
    # for synthesize and collect fleets.  Key on convergence (KTD1) — not the
    # optional block — so a collect/judge fleet with a stray block is still correct.
    manifest: dict = {
        "fleet": result.fleet,
        "task": result.task,
        "title": _synthesis_title(result),
        "timestamp": datetime.now().isoformat(),
        "models": participating_models,
        "synthesizer": {"provider": cfg.synthesis.provider, "model": cfg.synthesis.model}
                       if result.convergence == "synthesize" else None,
        "judge": {"provider": cfg.judge.provider, "model": cfg.judge.model}
                 if result.convergence == "judge" else None,
        "convergence": result.convergence,
        "status": result.status.value,
        "synth_ok": result.synth_ok,
        "judge_ok": result.judge_ok,
        "hermes_home": resolved_hermes_home(),
        "lanes": lanes,
    }

    # Judge mode: add per-lane structured grades and partial-coverage metadata.
    # Parse ONLY on a successful judge (judge_ok True). On judge failure (judge_ok
    # False) or all-specialists-fail (judge_ok None) no grading was attempted, so
    # emit empty grades/ungraded — never run parse_grades over an empty/None judge
    # text, which would list every survivor as ungraded and make a failed-judge run
    # read identically to a partial-coverage run to a manifest consumer. `ungraded`
    # non-empty must mean "the judge ran and skipped these lanes", not "no judge ran".
    if result.convergence == "judge":
        if result.judge_ok is True:
            pg = parse_grades(
                result.judge or "",
                [(r.role, r.model) for r in result.successes],
            )
            manifest["grades"] = pg.entries
            manifest["ungraded"] = [
                {"role": role, "model": model} for role, model in pg.ungraded
            ]
            if not pg.parsed_ok:
                # judge_ok is True here but nothing parsed — flag it even when the judge
                # text is empty (a successful call that returned ""), so an empty success
                # is never serialized as a partial-coverage run (grades=[] + all ungraded)
                # indistinguishable from a real partial (bot review). Defense-in-depth:
                # model_client maps an empty response to ok=False today, but the manifest
                # contract must hold regardless.
                manifest["parse_failed"] = True
                manifest["judge_text_raw"] = result.judge or ""
        else:
            manifest["grades"] = []
            manifest["ungraded"] = []

    return manifest
