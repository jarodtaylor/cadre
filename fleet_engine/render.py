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
from fleet_engine.engine import FleetResult
from fleet_engine.progress import (
    Completion,
    LaneDone,
    LaneLaunched,
    ProgressEvent,
    RunFolder,
    SynthDone,
    SynthStarted,
    Validated,
    outcome_label,
)

# Predicate: synthesizer looks API-billed (Anthropic/Claude/Opus) inside Hermes.
# False-negatives (missed expensive runs) are worse than false-positives (extra
# warnings), so we err toward flagging. The raw provider/model string is always
# shown so a missed heuristic can never hide an expensive run.
_BILLED_KEYWORDS = ("anthropic", "claude", "opus")


def _looks_api_billed(provider: str, model: str) -> bool:
    combined = f"{provider}/{model}".lower()
    return any(kw in combined for kw in _BILLED_KEYWORDS)


# Unicode line/paragraph separators and bidi format controls are never legitimate
# in a fleet field; >=0xA0 would otherwise pass them through and re-enable the
# fake-line / display-spoof the C0/C1 strip closes.
_UNSAFE_UNICODE = frozenset(
    "  "                      # line / paragraph separators
    "‪‫‬‭‮"    # bidi embeddings / overrides
    "⁦⁧⁨⁩"          # bidi isolates
)


def _sanitize(text: str, *, multiline: bool = False) -> str:
    """Strip terminal-control characters from fleet-controlled text before display.

    A fleet YAML is attacker-controllable (library tampering — see the cadre-fleet
    SKILL.md Safety section), and its strings flow into this preview, which is the
    operative human-okay control. An embedded ANSI/cursor escape sequence could
    otherwise overwrite or hide a printed warning (e.g. the privileged-tools line),
    spoofing the very output the human approves. Drop C0 controls (0x00–0x1F),
    DEL (0x7F), and C1 (0x80–0x9F): removing the ESC/CR/BS bytes defangs any
    sequence (a residual ``[2J`` then renders as inert text). Also drops Unicode
    line/paragraph separators (U+2028, U+2029) and bidi format controls
    (U+202A–U+202E, U+2066–U+2069), which >=0xA0 would otherwise pass through and
    re-enable the fake-line / display-spoof the C0/C1 strip closes. Newlines survive
    only for the multi-line synthesis prompt; TAB is also preserved in multiline mode
    only; elsewhere both are dropped so a single-line field cannot inject a fake line.
    Printable Unicode (>= 0xA0) other than the excluded set passes through untouched,
    so a legitimate prompt renders byte-identically.
    """
    return "".join(
        ch
        for ch in text
        if (
            (ch == "\n" and multiline)
            or (ch == "\t" and multiline)
            or (0x20 <= ord(ch) <= 0x7E)
            or ord(ch) >= 0xA0
        )
        and ch not in _UNSAFE_UNICODE
    )


def render_fleet_preview(config: FleetConfig) -> str:
    """Render a human-readable preview of a FleetConfig.

    Derived MECHANICALLY from the validated ``FleetConfig`` object — never from
    prose or agent paraphrase. The human approves this output, not the agent's
    summary of it, which is why it must be complete and exact. Every
    fleet-controlled string is passed through ``_sanitize`` so a tampered fleet
    cannot use terminal escapes to spoof or hide any part of the preview.

    Returns a multi-line string suitable for terminal display.
    """
    out = [f"=== fleet preview: {_sanitize(config.name)} ==="]

    # --- synthesizer (cost predicate runs on the RAW strings; only display is sanitized) ---
    synth_str = f"{_sanitize(config.synthesis.provider)}/{_sanitize(config.synthesis.model)}"
    out.append(f"\nSynthesizer: {synth_str}")
    if _looks_api_billed(config.synthesis.provider, config.synthesis.model):
        out.append("  ⚠ bills at API rates inside Hermes")

    # --- privileged tools flag (our text, not fleet-controlled — cannot be spoofed) ---
    if config.allow_privileged_tools:
        out.append("\n⚠ PRIVILEGED TOOLS ENABLED (allow_privileged_tools: true)")
    else:
        out.append("\nallow_privileged_tools: false")

    # --- synthesis prompt (multi-line: newlines preserved, other controls stripped) ---
    # Render the prompt byte-faithful to the parsed config (no .strip()): the
    # preview is the approval surface, so it must match what actually runs. Only
    # fall back to "(none)" when the sanitized prompt is truly empty.
    raw_prompt = config.synthesis.prompt or ""
    prompt_text = _sanitize(raw_prompt, multiline=True)
    if prompt_text == "":
        prompt_text = "(none)"
    out.append(f"\nSynthesis prompt:\n  {prompt_text.replace(chr(10), chr(10) + '  ')}")

    # --- specialists ---
    out.append(f"\nSpecialists ({len(config.specialists)}):")
    for s in config.specialists:
        toolset_str = ", ".join(_sanitize(t) for t in s.toolset) if s.toolset else "(none)"
        out.append(f"  [{_sanitize(s.role)}]  {_sanitize(s.provider)}/{_sanitize(s.model)}  toolset={toolset_str}")
        if s.focus:
            out.append(f"    focus: {_sanitize(s.focus)}")

    out.append("\n=== end preview ===")
    return "\n".join(out)


def render_result(result: FleetResult) -> str:
    header = "synthesized result" if result.ok else "partial result (no synthesis)"
    out = [f"=== {_sanitize(result.fleet)} — {header} ==="]
    if result.synth_ok is None:
        # All specialists failed — synthesis was never attempted. Surface a
        # prominent line so the caller never mistakes this for a valid result.
        n_failed = len(result.failures)
        n_total = len(result.specialists)
        out.append(f"No synthesis — {n_failed} of {n_total} specialists failed; synthesis was not attempted.")
    if result.synthesis:
        out.append(result.synthesis)
    elif result.successes:
        # No synthesis (synthesizer failed) but lanes succeeded — surface their raw
        # findings in labeled sections so the user still gets the work, not just
        # provenance rows.
        for r in result.successes:
            out.append(f"\n--- {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}) ---\n{r.text}")
    out.append("\n--- provenance ---")
    # Sanitize only the CONFIG-derived identity fields (fleet/role/provider/model)
    # so a tampered fleet can't forge provenance rows. Model output (r.text/r.error,
    # result.synthesis) is deliberately NOT stripped here — that's the deferred
    # injection->terminal chain (GH #5), not this surface's job.
    for r in result.specialists:
        if r.ok:
            tag = "ok  "
        elif r.timed_out:
            tag = "TIMEOUT"
        else:
            tag = "FAIL"
        suffix = "" if r.ok else f": {r.error}"
        out.append(f"[{tag}] {_sanitize(r.role)} ({_sanitize(r.provider)}/{_sanitize(r.model)}){suffix}")
    if result.notes:
        out.append("\nnotes:")
        out.extend(f"  - {n}" for n in result.notes)
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

        # Live tally — updated under self._lock on LaneLaunched and LaneDone.
        self._total: int = 0
        self._done: int = 0
        self._failed: int = 0

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
        """Update the active/done/failed tally from an event.

        Must be called under ``self._lock``, BEFORE writing the line for this
        event.  Only LaneLaunched and LaneDone change the tally.
        """
        if isinstance(event, LaneLaunched):
            self._total = len(event.roles)
        elif isinstance(event, LaneDone):
            label = outcome_label(event.result)
            if label == "ok":
                self._done += 1
            else:
                # Both "failed" and "timed-out" count toward the failure tally.
                self._failed += 1

    def _format(self, event: ProgressEvent) -> Optional[str]:
        """Return the ``[cadre] …`` line for ``event``, or ``None`` to skip.

        All config-sourced identity strings (fleet name, role) are sanitised
        before interpolation.  Internal labels, counts, elapsed times, paths,
        and pre-computed filenames are NOT sanitised — they are either our own
        strings or filesystem-safe by construction.
        """
        if isinstance(event, Validated):
            name = _sanitize(event.fleet)
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
            return f"[cadre] launched {len(event.roles)} specialists: {roles}"

        if isinstance(event, LaneDone):
            role = _sanitize(event.result.role)
            label = outcome_label(event.result)
            elapsed = f"{event.result.elapsed_s:.1f}" if event.result.elapsed_s is not None else "?.?"
            if self._filename_for is not None:
                # Capture on — look up the filename by the RAW role (the map key
                # the edge builds from config roles), display the sanitised role.
                filename = self._filename_for(event.result.role)
                return f"[cadre] lane {role} {label} {elapsed}s -> {filename}"
            else:
                return f"[cadre] lane {role} {label} {elapsed}s"

        if isinstance(event, SynthStarted):
            return f"[cadre] synthesizing over {event.survivors} survivor(s)"

        if isinstance(event, SynthDone):
            # SynthDone.outcome is already a label string — do NOT call outcome_label().
            elapsed = f"{event.elapsed_s:.1f}"
            return f"[cadre] synthesis {event.outcome} {elapsed}s"

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
                active = self._total - self._done - self._failed
                line = (
                    f"[cadre] heartbeat {mm:02d}:{ss:02d}"
                    f" active={active}/{self._total}"
                    f" done={self._done}"
                    f" failed={self._failed}"
                )
                self._write(line)
