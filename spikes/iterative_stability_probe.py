"""Validate-first probe: does round-over-round text-stability track convergence?

Evidence behind the GH #42 defer (consensus auto-stop). Reads a captured
iterative run's `round-<k>/specialist-<lane>.md` transcript and computes, per
lane, the round-over-round text similarity a naive `stop_when: stable` heuristic
would see (stdlib difflib.SequenceMatcher — the no-dependency tool feasibility
confirmed such a detector could use). Then reports, across a spread of
thresholds, whether an all-lanes-stable trigger would fire early (single-round
and 2-consecutive), so the signal can be cross-checked against a hand-verdict of
whether each round was actually still-moving.

Reusable for any Cadre iterative run folder — replays already-captured output,
zero model calls. See docs/solutions/best-practices/
probe-a-proxy-detector-against-real-output-before-building.md.

Usage:  python spikes/iterative_stability_probe.py <run-folder-with-round-dirs>

#42 verdict (replaying the #18 debate transcript): per-lane similarity 0.03-0.13
(lanes rewrite ~87-97% of their text each round); the trigger never fires at any
threshold 0.95..0.50 -> saves 0 rounds. Cross-round threading forces heavy
rewriting even at semantic convergence, so raw text-similarity cannot see it.
"""
import sys
import difflib
from pathlib import Path

run_dir = Path(sys.argv[1])
rounds = sorted(run_dir.glob("round-*"))
lanes = sorted({p.name for r in rounds for p in r.glob("specialist-*.md")})

def read(r, lane):
    f = r / lane
    return f.read_text(encoding="utf-8") if f.exists() else None

print(f"run dir: {run_dir}")
print(f"rounds: {[r.name for r in rounds]}   lanes: {lanes}\n")

transitions = []  # (from_round_name, to_round_name)
sim = {lane: [] for lane in lanes}
for i in range(len(rounds) - 1):
    a, b = rounds[i], rounds[i + 1]
    transitions.append((a.name, b.name))
    for lane in lanes:
        ta, tb = read(a, lane), read(b, lane)
        sim[lane].append(
            difflib.SequenceMatcher(None, ta, tb).ratio() if ta is not None and tb is not None else None
        )

print("=== per-lane round-over-round similarity (SequenceMatcher ratio) ===")
print("lane".ljust(26) + "".join(f"{a}->{b}".ljust(16) for a, b in transitions))
for lane in lanes:
    row = lane.replace("specialist-", "").replace(".md", "").ljust(26)
    row += "".join((f"{v:.3f}" if v is not None else "n/a").ljust(16) for v in sim[lane])
    print(row)

print("\n=== trigger analysis across thresholds (cap = number of rounds) ===")
for T in (0.50, 0.60, 0.70, 0.80, 0.90, 0.95):
    all_stable = [
        bool([sim[l][ti] for l in lanes if sim[l][ti] is not None])
        and all(sim[l][ti] >= T for l in lanes if sim[l][ti] is not None)
        for ti in range(len(transitions))
    ]
    single = next((transitions[i][1] for i, s in enumerate(all_stable) if s), None)
    two = next(
        (transitions[i][1] for i in range(1, len(all_stable)) if all_stable[i] and all_stable[i - 1]),
        None,
    )
    print(
        f"T={T:.2f}  all_stable={['Y' if s else '.' for s in all_stable]}  "
        f"single-round stop@{single or 'never (ran to cap)'}   "
        f"2-consecutive stop@{two or 'never (ran to cap)'}"
    )
