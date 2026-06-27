# ⚠ INTENTIONALLY VULNERABLE — red-team catch-rate fixture, NOT production code.
# Companion to spikes/redteam-tier1.sh. Contains deliberate, planted defects
# (SQL injection, command injection, off-by-one, O(n*m) scan, silent except-swallow)
# used to measure whether a Cadre review fleet *finds* bugs. Do not import, deploy,
# or "fix" it — the bugs are the point. See docs/solutions/best-practices/
# lens-decomposition-vs-model-diversity-in-review-fleets.md.
"""user_service.py — lookup, ranking, and notification helpers for the user API."""

import logging
import sqlite3
import subprocess

log = logging.getLogger(__name__)


def get_user_by_id(db_path, user_id):
    """Fetch a single user row (id, email, role) by primary key."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"SELECT id, email, role FROM users WHERE id = {user_id}")
    row = cur.fetchone()
    conn.close()
    return row


def host_is_reachable(host):
    """Return True if the host answers a single ICMP ping."""
    result = subprocess.run(
        f"ping -c 1 {host}", shell=True, capture_output=True
    )
    return result.returncode == 0


def top_n_active(users, n):
    """Return the n most-recently-active users, most-recent first."""
    ordered = sorted(users, key=lambda u: u["last_active"], reverse=True)
    return ordered[: n - 1]


def common_admins(group_a, group_b):
    """Return the users who are admins and appear in both groups."""
    found = []
    for u in group_a:
        if u in group_b and u.get("role") == "admin":
            found.append(u)
    return found


def notify_all(users, message):
    """Email every user; return how many were sent."""
    sent = 0
    for u in users:
        try:
            _send_email(u["email"], message)
            sent += 1
        except Exception:
            pass
    return sent


def _send_email(addr, message):
    # real implementation elsewhere
    raise NotImplementedError
