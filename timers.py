import sqlite3
import json
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "home_automation.db"


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS timers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        fire_at REAL NOT NULL,
        interval REAL,
        data TEXT,
        created_at REAL
    )""")
    c.commit()
    return c


def add(name, delay_sec, interval_sec=None, data=None):
    """Schedule a timer. Returns timer id."""
    conn = _conn()
    cur = conn.cursor()
    fire_at = time.time() + delay_sec
    cur.execute("INSERT INTO timers (name, fire_at, interval, data, created_at) VALUES (?,?,?,?,?)", (name, fire_at, interval_sec, json.dumps(data) if data else None, time.time()))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def cancel(name=None, timer_id=None):
    """Cancel timer(s) by name or id."""
    conn = _conn()
    cur = conn.cursor()
    if timer_id:
        cur.execute("DELETE FROM timers WHERE id=?", (timer_id,))
    elif name:
        cur.execute("DELETE FROM timers WHERE name=?", (name,))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def get_due():
    """Get all timers that should fire now."""
    c = _conn()
    now = time.time()
    rows = c.execute("SELECT * FROM timers WHERE fire_at <= ?", (now,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def reschedule(timer_id, delay_sec):
    """Push a timer forward by delay_sec from now."""
    c = _conn()
    fire_at = time.time() + delay_sec
    c.execute("UPDATE timers SET fire_at=? WHERE id=?", (fire_at, timer_id))
    c.commit()
    c.close()


def reschedule_interval(timer_id, interval_sec):
    """Push a repeating timer forward from its scheduled fire time by interval.

    Missed cycles are skipped instead of shifting the schedule later, so a
    daily timer keeps its time of day even if the daemon was down when it was
    due. Rescheduling from the actual fire time would let the timer drift
    permanently (e.g. a 4am job pushed to mid-afternoon).

    The stored interval is updated too, not just the fire time. Before
    2026-11-08 it was not, so a timer created at 30 s kept firing every 30 s
    after the code asked for 5 minutes: the next reschedule falls back on the
    interval column (see home_daemon's timer loop), which still held the old
    value.
    """
    if not interval_sec or interval_sec <= 0:
        return
    c = _conn()
    now = time.time()
    row = c.execute("SELECT fire_at FROM timers WHERE id=?", (timer_id,)).fetchone()
    if row is None:
        c.close()
        return
    fire_at = row["fire_at"]
    while fire_at <= now:
        fire_at += interval_sec
    c.execute("UPDATE timers SET fire_at=?, interval=? WHERE id=?", (fire_at, interval_sec, timer_id))
    c.commit()
    c.close()


def delete(timer_id):
    """Delete a timer by id."""
    c = _conn()
    c.execute("DELETE FROM timers WHERE id=?", (timer_id,))
    c.commit()
    c.close()


def list_all():
    """List all pending timers."""
    c = _conn()
    rows = c.execute("SELECT * FROM timers ORDER BY fire_at").fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_data(timer):
    """Parse the data field from a timer dict."""
    if timer.get("data"):
        return json.loads(timer["data"])
    return {}
