"""Render a FleetResult for human-facing entry surfaces (CLI, Hermes skill).

Kept out of the engine — the engine returns structured data, each surface
formats it — and shared here so the skill renders without depending on the CLI
command layer.

``ProgressRenderer`` is a third sibling of ``render_fleet_preview`` and
``render_result``: it turns lifecycle events into ``[cadre] …`` breadcrumbs on
stderr (serialized, sanitized) and owns the heartbeat timer.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

from fleet_engine.config import FleetConfig
from fleet_engine.text_safety import sanitize
from fleet_engine.engine import DEFAULT_CALL_TIMEOUT, FleetResult, FleetStatus, CHAIN_STAGE_CAP
from fleet_engine.judge_grade import parse_grades
from fleet_engine.progress import (
    Completion,
    JudgeDone,
    JudgeStarted,
    LaneDone,
    LaneLaunched,
    LaneStarted,
    ProgressEvent,
    RoundStarted,
    RunFolder,
    SynthDone,
    SynthStarted,
    Validated,
    outcome_label,
)

# Premium model-class trigger: an Anthropic/Claude/Opus synthesizer or judge is
# worth a cost note. False-negatives (missed expensive runs) are worse than
# false-positives (extra warnings), so we err toward flagging, and the raw
# provider/model string is always shown so a missed heuristic can never hide it.
_PREMIUM_MODEL_KEYWORDS = ("anthropic", "claude", "opus")

# Providers that bill against an OAuth / subscription quota, NOT per-token API
# rates — e.g. a ``copilot/claude-opus`` run is quota, not API $ (the #8 case).
# Membership is the ONLY thing that downgrades the "bills at API rates" wording,
# so it is exact-match and conservative: an unknown provider falls through to the
# API-rates warning (over-warning about cost is the safe direction; wrongly
# claiming "quota" is not). Matched on the lowercased provider string.
_OAUTH_QUOTA_PROVIDERS = frozenset({"copilot", "xai", "xai-oauth", "openai-codex", "nous"})


def _looks_premium(provider: str, model: str) -> bool:
    combined = f"{provider}/{model}".lower()
    return any(kw in combined for kw in _PREMIUM_MODEL_KEYWORDS)


def _cost_warning(provider: str, model: str) -> str | None:
    """The cost note for a premium lane, or ``None`` when no note applies.

    Classifies on the RAW provider (an OAuth/subscription-quota provider gets an
    honest quota note; everything else — including unknown providers — gets the
    per-token API-rates warning). The provider is ``_sanitize``-d before it reaches
    the rendered line: it is fleet-controlled, and this line is part of the
    human-approval surface a tampered fleet must not be able to spoof.
    """
    if not _looks_premium(provider, model):
        return None
    if provider.strip().lower() in _OAUTH_QUOTA_PROVIDERS:
        return f"  ⚠ uses your {_sanitize(provider)} OAuth quota, not per-token API billing"
    return "  ⚠ bills at API rates inside Hermes"


# The sanitizer now lives in fleet_engine.text_safety (GH #23 — a shared trust
# boundary imported across modules deserves a public home). Keep the private
# alias so render's in-file call sites and any legacy importer keep working.
_sanitize = sanitize


def render_fleet_preview(
    config: FleetConfig,
    call_timeout: float | None = DEFAULT_CALL_TIMEOUT,
) -> str:
    """Render a human-readable preview of a FleetConfig.

    Derived MECHANICALLY from the validated ``FleetConfig`` object — never from
    prose or agent paraphrase. The human approves this output, not the agent's
    summary of it, which is why it must be complete and exact. Every
    fleet-controlled string is passed through ``_sanitize`` so a tampered fleet
    cannot use terminal escapes to spoof or hide any part of the preview.

    ``call_timeout`` is the per-stage wall-clock budget (seconds); for sequential
    fleets the max total wall-clock ceiling is ``call_timeout`` times ``N`` — where
    ``N`` is the stage count plus one for the convergence call under synthesize/judge
    (collect has none). Pass ``None`` to indicate an unlimited per-stage budget.
    Parallel fleets ignore this parameter — the parallel preview stays byte-identical.

    Returns a multi-line string suitable for terminal display.
    """
    out = [f"=== fleet preview: {_sanitize(config.name)} ==="]
    # Catalog metadata (R11): show the fleet's description so the human approving
    # the preview sees what it is for. Sanitized single-line like every other
    # config-sourced field — a tampered description cannot inject a fake line.
    if config.description:
        out.append(f"\n{_sanitize(config.description)}")

    # --- synthesizer / convergence ---
    # Branch EXPLICITLY on config.convergence (the authoritative field — KTD1: a
    # stray synthesis: block in a collect fleet must still preview as collect;
    # synthesis=None in judge mode must not crash the else branch).
    if config.convergence == "collect":
        out.append("\nConvergence: collect (no synthesizer)")
    elif config.convergence == "judge":
        # judge path — cost predicate runs on the RAW strings; only display is sanitized
        judge_str = f"{_sanitize(config.judge.provider)}/{_sanitize(config.judge.model)}"
        out.append("\nConvergence: judge")
        out.append(f"Judge: {judge_str}")
        cost_note = _cost_warning(config.judge.provider, config.judge.model)
        if cost_note:
            out.append(cost_note)
    else:
        # synthesize path — cost predicate runs on the RAW strings; only display is sanitized
        synth_str = f"{_sanitize(config.synthesis.provider)}/{_sanitize(config.synthesis.model)}"
        out.append(f"\nSynthesizer: {synth_str}")
        cost_note = _cost_warning(config.synthesis.provider, config.synthesis.model)
        if cost_note:
            out.append(cost_note)

    # --- privileged tools flag (our text, not fleet-controlled — cannot be spoofed) ---
    # Unconditional: a collect fleet can carry privileged tools, so this must render
    # regardless of convergence mode.
    if config.allow_privileged_tools:
        out.append("\n⚠ PRIVILEGED TOOLS ENABLED (allow_privileged_tools: true)")
    else:
        out.append("\nallow_privileged_tools: false")

    # --- judge/synthesis prompt (multi-line: newlines preserved, other controls stripped) ---
    # Render the prompt byte-faithful to the parsed config (no .strip()): the
    # preview is the approval surface, so it must match what actually runs. Only
    # fall back to "(none)" when the sanitized prompt is truly empty.
    # Skipped for collect fleets — there is no synthesizer or judge to run a prompt.
    if config.convergence == "synthesize":
        raw_prompt = config.synthesis.prompt or ""
        prompt_text = _sanitize(raw_prompt, multiline=True)
        if prompt_text == "":
            prompt_text = "(none)"
        out.append(f"\nSynthesis prompt:\n  {prompt_text.replace(chr(10), chr(10) + '  ')}")
    elif config.convergence == "judge":
        raw_prompt = config.judge.prompt or ""
        prompt_text = _sanitize(raw_prompt, multiline=True)
        if prompt_text == "":
            prompt_text = "(none)"
        out.append(f"\nJudge prompt:\n  {prompt_text.replace(chr(10), chr(10) + '  ')}")

    # --- specialists ---
    out.append(f"\nSpecialists ({len(config.specialists)}):")
    for s in config.specialists:
        toolset_str = ", ".join(_sanitize(t) for t in s.toolset) if s.toolset else "(none)"
        out.append(f"  [{_sanitize(s.role)}]  {_sanitize(s.provider)}/{_sanitize(s.model)}  toolset={toolset_str}")
        if s.effective_instruction:
            if s.persona:
                # Persona lane: multi-line label so the human sees the full instruction.
                # persona name is sanitized like every other fleet-controlled field.
                body = _sanitize(s.effective_instruction, multiline=True)
                out.append(f"    persona: {_sanitize(s.persona)}")
                out.append(f"      {body.replace(chr(10), chr(10) + '      ')}")
            else:
                # Focus lane: single-line (focus text is always a one-liner).
                out.append(f"    focus: {_sanitize(s.effective_instruction)}")

    # Sequential-specific summary (parallel preview stays byte-identical — no new line).
    if config.topology == "sequential":
        stages = len(config.specialists)
        # synthesize/judge run one MORE bounded model call (the convergence step) AFTER
        # the lane chain; collect does not. Count it so the disclosed wall-clock ceiling
        # isn't undercounted on the approval surface (the exact risk this line discloses).
        has_convergence_call = config.convergence in ("synthesize", "judge")
        calls = stages + 1 if has_convergence_call else stages
        tail = " + convergence" if has_convergence_call else ""
        if call_timeout is not None:
            total_s = call_timeout * calls
            mm = int(total_s) // 60
            ss = int(total_s) % 60
            ceiling_str = f"{mm}m{ss:02d}s" if mm > 0 else f"{ss}s"
            out.append(f"\nTopology: sequential — {stages} stage(s){tail}, max wall-clock {ceiling_str}")
        else:
            out.append(f"\nTopology: sequential — {stages} stage(s){tail}, no per-stage timeout")
        out.append(f"  Inter-stage output cap: {CHAIN_STAGE_CAP:,} chars")
        # Cross-stage trust disclosure: a non-first chain lane that carries tools receives
        # the prior stages' UNTRUSTED model output threaded into its prompt, THEN runs its
        # tools — so a prompt injection in an upstream stage can steer this lane's tool use
        # (a stronger vector than a single tool-gated lane). Read-only SAFE_TOOLSETS bounds
        # the blast radius; forgery/injection hardening of the seam is GH #5. Surface it on
        # the approval surface (our own trusted text; role labels are fleet-controlled, so
        # sanitized). The first lane is exempt — it consumes no upstream output.
        tool_lanes = [s.role for s in config.specialists[1:] if s.toolset]
        if tool_lanes:
            out.append(
                "  ⚠ cross-stage tool exposure: "
                + ", ".join(_sanitize(r) for r in tool_lanes)
                + " run tools after consuming prior stages' untrusted output"
                " — a prompt injection upstream can steer their tool use (GH #5 hardens this seam)"
            )

    # Iterative-specific summary: paid-call count + wall-clock differ from sequential
    # because within-round lanes run CONCURRENTLY (wall-clock per round ≈ 1 × timeout,
    # not lanes × timeout). Show both the call count AND the wall-clock ceiling so the
    # human approving the preview can evaluate the cost before okaying the run.
    if config.topology == "iterative":
        rounds = config.rounds  # validated int (1..MAX_ROUNDS) for iterative fleets
        lanes = len(config.specialists)
        has_convergence_call = config.convergence in ("synthesize", "judge")
        # Paid calls: specialist lanes run in each round (concurrent within a round);
        # synthesize/judge adds one convergence call over the survivors after all rounds.
        calls = lanes * rounds + (1 if has_convergence_call else 0)
        # Wall-clock: concurrent within-round → one timeout per round; +1 for convergence.
        wall_rounds = rounds + (1 if has_convergence_call else 0)
        if call_timeout is not None:
            total_s = call_timeout * wall_rounds
            mm = int(total_s) // 60
            ss = int(total_s) % 60
            ceiling_str = f"{mm}m{ss:02d}s" if mm > 0 else f"{ss}s"
            out.append(
                f"\nTopology: iterative — {rounds} round(s), {lanes} lane(s),"
                f" {calls} paid call(s), max wall-clock {ceiling_str}"
            )
        else:
            out.append(
                f"\nTopology: iterative — {rounds} round(s), {lanes} lane(s),"
                f" {calls} paid call(s), no per-round timeout"
            )
        out.append(f"  Inter-round output cap: {CHAIN_STAGE_CAP:,} chars")
        # Cross-round trust: ALL lanes from round 2+ consume prior-round outputs — unlike
        # sequential, even lane 0 receives upstream model output in later rounds.
        # role labels are fleet-controlled → sanitized.
        tool_lanes = [s.role for s in config.specialists if s.toolset]
        # Only rounds >= 2 actually thread prior-round output into a later round; at
        # rounds == 1 no lane consumes another's output, so the cross-round exposure
        # warning would be misleading (CodeRabbit review).
        if tool_lanes and rounds >= 2:
            out.append(
                "  ⚠ cross-round tool exposure: "
                + ", ".join(_sanitize(r) for r in tool_lanes)
                + " run tools after consuming prior rounds' untrusted output"
                " — a prompt injection upstream can steer their tool use (GH #5 hardens this seam)"
            )
        if rounds == 1 and lanes >= 2:
            out.append(
                "  note: rounds=1 — zero cross-round iterations;"
                " run will flag diversity_collapsed"
            )
        out.append("  Stopping: fixed round count; early stop if all lanes fail")

    out.append("\n=== end preview ===")
    return "\n".join(out)


def render_file_inputs(paths: list[str], truncated: list[str] | None = None) -> str:
    """Render the "--doc files to read" preview block from the list of ``--doc`` paths.

    The paths are shown exactly as the caller named them (no canonicalization /
    realpath) — this surface lists what will be read, it does not resolve it.

    Returns an empty string for an empty list — no block when no ``--doc`` was
    given, so the preview stays byte-identical to a plain run. Otherwise it names
    each file that will be read into the task, one per line, and flags any file in
    ``truncated`` (capped at ``MAX_FILE_BYTES``) so the human approving the preview
    sees that the review will run over a PARTIAL file — the in-block truncation note
    is model-facing and invisible here, so without this the previewer would okay a
    silently partial review (cross-model review finding).

    A third preview sibling of ``render_fleet_preview`` and the ``preview_lint``
    warnings: a path label is a fleet-/caller-controlled string flowing into the
    human-approval surface, so each is passed through ``_sanitize`` single-line
    (KTD6) — a control byte or bidi char in a path must not spoof or hide a line.
    The ``⚠ truncated`` marker is our OWN trusted text (not sanitized — it cannot be
    spoofed). Only the path *labels* are shown and sanitized here; the file *content*
    is never rendered on this surface and never sanitized (sanitizing it would
    corrupt the reviewer's document — the deferred #5 / #23 boundary).
    """
    if not paths:
        return ""
    truncated_set = set(truncated or ())
    out = ["Files read into the task (--doc):"]
    for p in paths:
        marker = "  ⚠ truncated — the review will run over a PARTIAL file" if p in truncated_set else ""
        out.append(f"  - {_sanitize(p)}{marker}")
    return "\n".join(out)


def render_composed_task(task: str) -> str:
    """Render the composed task (--task + any --doc blocks) for the preview.

    The composed task is what the run actually feeds the models and what the
    approval binds; the operator must see it to approve the real inputs, not a
    config in isolation. Sanitized multi-line like the synthesis-prompt block —
    a --doc's content could otherwise smuggle a terminal escape onto the
    approval surface.
    """
    body = _sanitize(task, multiline=True)
    indented = body.replace(chr(10), chr(10) + "  ")
    return f"\nComposed task (--task + --doc):\n  {indented}"


def render_result(result: FleetResult) -> str:
    # Key the header on (convergence, status) so every (mode, outcome) pair is read from
    # the engine-declared status, not re-derived from ok/synth_ok/judge_ok.
    if result.convergence == "collect":
        if result.status is FleetStatus.SUCCESS:
            header = "collect result"
        elif result.status is FleetStatus.DEGRADED:
            # Sequential+collect: chain broke mid-run (some stages ran, terminal skipped).
            header = "collect result — chain failed mid-run"
        elif result.topology == "sequential":
            # FAILED + sequential: the FIRST lane failed and the rest were skipped
            # (never ran) — "all specialists failed" would be false here.
            header = "collect result — chain failed at the first lane"
        else:
            header = "collect result — all specialists failed"
    elif result.convergence == "judge":
        if result.status is FleetStatus.SUCCESS:
            header = "judge result"
        elif result.status is FleetStatus.DEGRADED:
            # DEGRADED has two meanings under sequential topology, told apart by the
            # mode-detail (NOT the aggregate status): judge present = the chain broke
            # mid-run but the judge still succeeded over the survivors; judge None = the
            # judge ran and failed. Parallel DEGRADED is always the latter (judge None).
            header = (
                "judge result — chain failed mid-run"
                if result.judge is not None
                else "judge result — judge failed"
            )
        elif result.topology == "sequential":
            # FAILED + sequential: the first lane failed and the rest were skipped.
            header = "judge result — chain failed at the first lane"
        else:
            # FAILED + parallel: all specialists failed, the judge never ran.
            header = "judge result — all specialists failed"
    else:
        # Gate on the mode-detail (synthesis presence), not the aggregate status: a
        # sequential chain that broke mid-run but synthesized over its survivors is
        # DEGRADED yet carries a real synthesis body. Parallel SUCCESS always has a
        # synthesis and parallel DEGRADED/FAILED never do, so this is unchanged there.
        if result.synthesis is not None:
            header = (
                "synthesized result"
                if result.status is FleetStatus.SUCCESS
                else "synthesized result — chain failed mid-run"
            )
        else:
            header = "partial result (no synthesis)"
    out = [f"=== {_sanitize(result.fleet)} — {header} ==="]
    # Guard: only emit the "synthesis was not attempted" preamble for synthesize
    # fleets where all specialists failed (FAILED — synthesis was never attempted).
    # DEGRADED (synthesizer ran + failed, specialists survived) must NOT emit this.
    # The convergence guard is preserved: collect-FAILED and judge-FAILED must not
    # emit a synthesize-specific preamble.
    if result.convergence == "synthesize" and result.status is FleetStatus.FAILED:
        # Synthesis was never attempted. Surface a prominent line so the caller never
        # mistakes this for a valid result. Under sequential topology FAILED means the
        # first lane failed and the rest were skipped, so a count ("1 of 3 failed")
        # misleads — it reads as if the other 2 succeeded; say what happened instead,
        # mirroring the collect/judge FAILED wording. Parallel keeps the count (there
        # n_failed == n_total and nothing was skipped, so it is meaningful).
        if result.topology == "sequential":
            out.append("No synthesis — the chain halted at the first lane; synthesis was not attempted.")
        else:
            n_failed = len(result.failures)
            n_total = len(result.specialists)
            out.append(f"No synthesis — {n_failed} of {n_total} specialists failed; synthesis was not attempted.")
    # Diversity-collapse advisory: iterative only; never gates or discards output.
    # Placed after the "all failed" preamble (if any) and before the body so the
    # human sees it regardless of whether a synthesis body follows. Static trusted
    # text — NOT passed through _sanitize (same pattern as threading_truncated).
    if result.topology == "iterative" and result.diversity_collapsed:
        out.append(
            "\n⚠ Diversity collapsed — the debate ended with ≤1 distinct position"
            " or ran zero cross-round iterations; treat the result with caution."
        )
    if result.convergence == "judge":
        # Judge body: lead with the judge's OWN raw text (KTD2 — the human report
        # always shows the judge's text, even if a parse falls short). Reconstructing
        # from parsed per-lane entries would silently drop anything the judge wrote
        # outside the per-lane blocks (an overall summary, a ranking, cross-lane notes);
        # the parsed structure is for the manifest, not the report (plan §High-Level
        # Design). parse_grades drives ONLY the partial-coverage note here. Grade text
        # and specialist r.text are both sanitized (#5 U2 — model output can carry
        # escape bytes that spoof a provenance row or hide a warning on this surface).
        judge_text = result.judge or ""
        surviving = [(r.role, r.model) for r in result.successes]
        pg = parse_grades(judge_text, surviving, result.judge_marker_nonce)
        if judge_text:
            out.append(f"\n{_sanitize(judge_text, multiline=True)}")
        # Partial-coverage note (R14/AE7): only when we parsed structure AND a survivor
        # went ungraded — flag the gap without hiding the judge's own words above.
        if pg.parsed_ok and pg.ungraded:
            ungraded_roles = ", ".join(_sanitize(role) for role, _ in pg.ungraded)
            out.append(f"\nnote: {len(pg.ungraded)} lane(s) not graded by judge: {ungraded_roles}")
        # Attributed specialist outputs always follow.
        for r in result.successes:
            out.append(f"\n--- {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}) ---\n{_sanitize(r.text or '', multiline=True)}")
    elif result.synthesis:
        out.append(_sanitize(result.synthesis, multiline=True))
    elif result.successes:
        # No synthesis (synthesizer failed) but lanes succeeded — surface their raw
        # findings in labeled sections so the user still gets the work, not just
        # provenance rows.
        for r in result.successes:
            out.append(f"\n--- {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}) ---\n{_sanitize(r.text or '', multiline=True)}")
    if result.threading_truncated:
        # Our own trusted text — not sanitized. A sequential chain (inter-stage) or an
        # iterative round (inter-round) produced threaded output that exceeded
        # CHAIN_STAGE_CAP chars; the downstream lane received partial upstream context.
        unit = "inter-round" if result.topology == "iterative" else "inter-stage"
        out.append(f"\nnote: {unit} output was capped — some context may have been truncated")
    out.append("\n--- provenance ---")
    # Every fleet/model-derived field on this surface is sanitized against escape
    # bytes: the identity fields (fleet/role/provider/model) and r.error
    # (model/adapter output) so neither can smuggle a control sequence into the row
    # it renders (#5 U2). r.text/synthesis are sanitized above where they render.
    # NOTE: this strips escapes, not plain-text grammar — a model body can still
    # print a look-alike "[ok  ] x" line inertly (report-grammar mimicry residual,
    # SECURITY.md); framing model bodies is a tracked fast-follow.
    for r in result.specialists:
        if r.skipped:
            tag = "SKIP"
            suffix = ""
        elif r.ok:
            tag = "ok  "
            suffix = ""
        elif r.timed_out:
            tag = "TIMEOUT"
            suffix = f": {_sanitize(r.error)}" if r.error else ""
        else:
            tag = "FAIL"
            suffix = f": {_sanitize(r.error)}" if r.error else ""
        out.append(f"[{tag}] {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}){suffix}")
    if result.notes:
        out.append("\nnotes:")
        # notes embed fleet-controlled role names and adapter error text
        # (e.g. "specialist 'x' failed: <error>") — sanitize so this same-surface
        # block can't forge a row or hide a warning the provenance rows above guard (#5 U2).
        out.extend(f"  - {_sanitize(n)}" for n in result.notes)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Live breadcrumb renderer + heartbeat (U2)
# ---------------------------------------------------------------------------

# Default heartbeat cadence — chosen so the silent gap never exceeds 15 s
# during a normal run (specialists take 30-120 s each in practice). An env
# override is cheap to add later (plan Open Questions); hardcode for now.
_HEARTBEAT_INTERVAL_S = 15.0


class ProgressRenderer:
    """Turn lifecycle events into serialized ``[cadre] …`` breadcrumbs on stderr.

    This is the third sibling of ``render_fleet_preview`` and ``render_result``:
    every config-sourced identity string that flows into a breadcrumb (fleet name,
    role) is passed through ``_sanitize()`` before emission, for the same reason
    as the sibling surfaces — a tampered fleet must not be able to spoof the
    stream an agent reads (KTD4, ``docs/solutions/design-patterns/
    sanitize-trust-surface-renders-against-terminal-escapes.md``).

    Threading contract (KTD3): ``emit()`` is called from exactly ONE thread
    (the engine's arrival-order drainer, then the main thread for synthesis
    events). The ONLY concurrent actor is the heartbeat timer thread.  A single
    ``threading.Lock`` serializes every write to the stream AND every tally
    mutation; the heartbeat acquires that same lock to read the tally and write
    its line, ensuring no two lines are ever interleaved on the stream.

    Per-lane file I/O is NOT in scope here — that is U3's responsibility.
    """

    def __init__(
        self,
        stream=None,
        filename_for: Optional[Callable[[str], str]] = None,
        interval_s: float = _HEARTBEAT_INTERVAL_S,
    ) -> None:
        """Initialise the renderer.

        Args:
            stream: Write target for breadcrumbs.  When ``None``, resolves to
                ``sys.stderr`` at init time — deliberately NOT at import time so
                that tests can patch stderr before constructing the renderer.
            filename_for: Optional callable mapping a lane role (raw, as in the
                config) to its resolved on-disk artifact filename.  ``None`` when
                capture is off — lane-done lines then omit the filename (R13).
                The edge guarantees every emitted role is covered by the map.
            interval_s: Heartbeat cadence in seconds.  Defaults to
                ``_HEARTBEAT_INTERVAL_S``; tests pass a tiny value (e.g. 0.05)
                to keep runtime short.
        """
        self._stream = stream if stream is not None else sys.stderr
        self._filename_for = filename_for
        self._interval_s = interval_s

        # Latched True on the first failed write (e.g. the supervising agent stops
        # draining stderr -> BrokenPipeError). Breadcrumbs are auxiliary; the run
        # must survive a dead progress pipe — see _write.
        self._stream_dead = False

        # Serialises every write to self._stream and every tally mutation.
        # The heartbeat thread acquires this same lock before reading the tally
        # or writing its line — one lock, one critical section.
        self._lock = threading.Lock()

        # Live tally — updated under self._lock on LaneLaunched, LaneStarted, and LaneDone.
        self._total: int = 0
        self._done: int = 0
        self._failed: int = 0
        self._skipped: int = 0   # chain lanes that never ran (sequential only)
        self._stage: int = 0     # count of LaneStarted events seen (used to format "stage k/N")

        # Heartbeat timer state — set by start_heartbeat, consumed by stop.
        self._stop_event: threading.Event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_start: Optional[float] = None  # monotonic seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(self, event: ProgressEvent) -> None:
        """Format ``event`` into a ``[cadre] …`` line and write it to the stream.

        Must be called from a single thread only (the engine drainer or the main
        thread for edge events).  The write is serialised under ``self._lock``
        so heartbeat ticks cannot interleave.

        NEVER emits ``result.error`` — the breadcrumb carries the outcome label
        only, keeping untrusted model-failure strings off the agent's control
        stream (restriction lives HERE, not in the event dataclass).
        """
        # _format runs BEFORE _update_tally on purpose: the LaneStarted breadcrumb
        # reads self._stage as the count of PRIOR started lanes and renders _stage + 1
        # for a 1-based "stage k/N". Moving _format inside the lock (after the tally
        # increment) would silently shift every stage number by one — keep it here.
        line = self._format(event)
        if line is None:
            return
        with self._lock:
            # Update the tally BEFORE writing the line so the count on the line
            # is accurate for anything that reads back the stream.
            self._update_tally(event)
            self._write(line)

    def note(self, text: str) -> None:
        """Write an out-of-band ``[cadre] warn: …`` breadcrumb (best-effort, locked).

        For edge warnings that are not lifecycle events (a failed artifact write)
        but should still ride the same parseable stream an agent reads — so a
        supervisor sees that capture degraded, the line carries the stable
        ``[cadre]`` prefix, and a dead pipe still can't crash the run (shares
        ``emit``'s lock and the guarded ``_write``).
        """
        with self._lock:
            self._write(f"[cadre] warn: {_sanitize(text)}")

    def start_heartbeat(self) -> None:
        """Start the daemon heartbeat timer.

        The first tick fires after ``interval_s``; subsequent ticks repeat at
        the same cadence while the stop event is clear.  ``daemon=True`` so the
        thread is abandoned at interpreter exit and never blocks process
        termination (``docs/solutions/design-patterns/
        daemon-threads-for-uncancellable-timeouts.md``).
        """
        self._heartbeat_start = time.monotonic()
        self._stop_event.clear()
        t = threading.Thread(
            target=self._heartbeat_loop,
            name="cadre-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = t
        t.start()

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat timer cleanly.

        Sets the stop event (which wakes ``Event.wait()`` immediately, so the
        thread exits on the next loop iteration) then joins briefly.  Idempotent:
        safe to call before ``start_heartbeat`` or more than once.  Never blocks
        interpreter exit (the thread is a daemon).
        """
        self._stop_event.set()
        t = self._heartbeat_thread
        if t is not None and t.is_alive():
            t.join(timeout=self._interval_s + 0.5)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, line: str) -> None:
        """Write ``line + newline`` to the stream in one atomic call.

        Must be called under ``self._lock``.  A single ``write()`` of the full
        ``line + "\\n"`` is important: two separate ``write()`` calls would let
        a concurrent heartbeat interleave between them and garble the output.

        Best-effort by design. The progress stream is auxiliary — the run's
        deliverable is the FleetResult, rendered to stdout by the caller. If the
        stream dies mid-run (the supervising agent stops draining stderr ->
        ``BrokenPipeError``; a closed stream -> ``ValueError``), we must NOT let
        that raise out of the hook and unwind ``run_with_progress`` before it can
        ``return result`` — that would discard completed model work. Latch on the
        first failure (this is the single chokepoint every emit AND the heartbeat
        funnel through, so one guard covers them all) and skip further writes.
        """
        if self._stream_dead:
            return
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            self._stream_dead = True

    def _update_tally(self, event: ProgressEvent) -> None:
        """Update the active/done/failed/skipped tally from an event.

        Must be called under ``self._lock``, BEFORE writing the line for this
        event (``emit`` calls ``_format`` then acquires the lock, then calls
        ``_update_tally`` then ``_write``).  For ``LaneStarted``, this ordering
        means ``self._stage`` in ``_format`` still holds the count of PRIOR started
        lanes — ``_stage + 1`` is the 1-based display index.  ``LaneLaunched``,
        ``LaneStarted``, and ``LaneDone`` change the tally; all other event types
        are no-ops.
        """
        if isinstance(event, LaneLaunched):
            # A LaneLaunched marks a fresh fan-out — parallel/sequential emit it once,
            # iterative emits it once PER ROUND. Reset the per-fan-out tally so the
            # heartbeat's active count is relative to THIS round's lanes. Without the
            # reset, iterative rounds 2+ subtract cumulative completions from the new
            # round's total and report active=0 (or negative) while lanes are in flight
            # (Codex cross-model finding). No-op for parallel/sequential: their single
            # LaneLaunched fires while the counters are already 0.
            self._total = len(event.roles)
            self._done = 0
            self._failed = 0
            self._skipped = 0
            self._stage = 0
        elif isinstance(event, LaneStarted):
            # Increment AFTER _format so the "stage k/N" display uses the prior count.
            self._stage += 1
        elif isinstance(event, LaneDone):
            label = outcome_label(event.result)
            if label == "ok":
                self._done += 1
            elif label == "skipped":
                self._skipped += 1
            else:
                # Both "failed" and "timed-out" count toward the failure tally.
                self._failed += 1

    def _format(self, event: ProgressEvent) -> Optional[str]:
        """Return the ``[cadre] …`` line for ``event``, or ``None`` to skip.

        Every interpolated string that originates outside this renderer is passed
        through ``_sanitize()`` before it reaches the line: config-sourced identity
        fields (fleet name, role), caller/env-supplied paths (run folder, run_dir),
        and the resolved artifact filename. The breadcrumb is a control stream an
        agent parses, so nothing external may carry a newline or escape into it.
        Only our OWN strings and numbers — the internal outcome labels, counts, and
        elapsed times — are interpolated unsanitised.
        """
        if isinstance(event, Validated):
            name = _sanitize(event.fleet)
            if event.convergence == "judge":
                return (
                    f"[cadre] validated fleet '{name}'"
                    f" — {event.specialists} specialists, 1 judge"
                )
            return (
                f"[cadre] validated fleet '{name}'"
                f" — {event.specialists} specialists, {event.synthesizers} synthesizer(s)"
            )

        if isinstance(event, RunFolder):
            # Sanitize the path too: an explicit run_dir / CADRE_RUN_DIR is caller- or
            # env-controlled, and a POSIX path may carry newlines or ESC bytes that
            # would otherwise forge a second [cadre] line or inject a terminal escape
            # into the stream a supervising agent parses (cross-model adversarial finding).
            return f"[cadre] run folder: {_sanitize(event.path)}"

        if isinstance(event, LaneLaunched):
            roles = ", ".join(_sanitize(r) for r in event.roles)
            if event.queued:
                # Sequential roster: all stages announced up front but not yet started.
                return f"[cadre] queued {len(event.roles)} stages: {roles}"
            return f"[cadre] launched {len(event.roles)} specialists: {roles}"

        if isinstance(event, LaneStarted):
            role = _sanitize(event.role)
            # _update_tally hasn't run yet for this event, so self._stage is the count
            # of PRIOR started lanes — add 1 for the 1-based display index.
            stage_num = self._stage + 1
            return f"[cadre] stage {stage_num}/{self._total}: {role}"

        if isinstance(event, LaneDone):
            role = _sanitize(event.result.role)
            label = outcome_label(event.result)
            elapsed = f"{event.result.elapsed_s:.1f}" if event.result.elapsed_s is not None else "?.?"
            if self._filename_for is not None:
                # Capture on — look up the filename by the RAW role (the map key
                # the edge builds from config roles), display the sanitised role.
                # Sanitize the filename too: it is _safe_role-derived (safe by
                # construction) today, but filename_for is a caller-injected callable
                # and the renderer must not trust it to keep the [cadre] stream clean.
                filename = _sanitize(self._filename_for(event.result.role))
                return f"[cadre] lane {role} {label} {elapsed}s -> {filename}"
            else:
                return f"[cadre] lane {role} {label} {elapsed}s"

        if isinstance(event, SynthStarted):
            return f"[cadre] synthesizing over {event.survivors} survivor(s)"

        if isinstance(event, SynthDone):
            # SynthDone.outcome is already a label string — do NOT call outcome_label().
            elapsed = f"{event.elapsed_s:.1f}"
            return f"[cadre] synthesis {event.outcome} {elapsed}s"

        if isinstance(event, JudgeStarted):
            return f"[cadre] judging over {event.survivors} survivor(s)"

        if isinstance(event, JudgeDone):
            # JudgeDone.outcome is already a label string — do NOT call outcome_label().
            elapsed = f"{event.elapsed_s:.1f}"
            return f"[cadre] judge {event.outcome} {elapsed}s"

        if isinstance(event, RoundStarted):
            # Iterative-topology breadcrumb: emitted at the start of each round
            # before its concurrent lane fan-out (U6). Fields are our own ints —
            # not fleet-controlled — so no _sanitize needed.
            return f"[cadre] round {event.round}/{event.total}"

        if isinstance(event, Completion):
            total = f"{event.elapsed_s:.1f}"
            if event.run_dir is not None:
                # Sanitize the path (see RunFolder above — same caller/env-controlled
                # source, same forge/escape risk on the agent's control stream).
                return f"[cadre] done in {total}s — run folder: {_sanitize(event.run_dir)}"
            else:
                return f"[cadre] done in {total}s"

        # Unknown event type — skip silently rather than crashing.
        return None

    def _heartbeat_loop(self) -> None:
        """Body of the daemon heartbeat thread.

        Loops ``while not stop_event.wait(interval_s)`` — the ``wait`` doubles
        as the cadence delay AND the stop check.  The first tick fires after
        ``interval_s``, not immediately (matching spec: "first tick at
        +interval_s").  Every tick reads the live tally and writes one line,
        both under the shared lock so no partial line can interleave with an
        ``emit()`` call on the main thread.
        """
        while not self._stop_event.wait(self._interval_s):
            with self._lock:
                elapsed_s = int(time.monotonic() - self._heartbeat_start)
                mm = elapsed_s // 60
                ss = elapsed_s % 60
                active = self._total - self._done - self._failed - self._skipped
                line = (
                    f"[cadre] heartbeat {mm:02d}:{ss:02d}"
                    f" active={active}/{self._total}"
                    f" done={self._done}"
                    f" failed={self._failed}"
                    f" skipped={self._skipped}"
                )
                self._write(line)
