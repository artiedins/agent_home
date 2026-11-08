#!/usr/bin/env python3
# Telegram alert when the outside air crosses the inside temperature.
#
# Sources: weather_station.db. temp_in_c is the console's own sensor (indoors,
# above the piano); temp_out_c is the roof array. Values are stored Celsius, the
# threshold and the message are Fahrenheit to match the console display.
#
# Rule (set by the owner, 2026-11-08): alert when outside is at least 1.0 F
# above inside, and when it is at least 1.0 F below inside. Between -1 and +1
# is a dead band, so a crossing has to clear a full degree before it counts.
# Each direction has its own 15-minute debounce: a warm-then-cold round trip
# alerts twice, one message per direction, as long as 15 minutes separate two
# alerts in the same direction.
#
# State lives in temp_transition_state.json, not memory, for two reasons: the
# daemon is restarted by hand after edits, and a restart must not re-announce a
# crossing that already went out. The state records the last obs id seen, the
# side outside is currently on, and the last alert time per direction.

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

import weather_station

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "temp_transition_state.json")

ABOVE = "above"
BELOW = "below"
DEAD_BAND = "dead_band"

# A transition that sat unread for longer than this is not worth a phone buzz:
# after a daemon restart the backlog can be hours old, and "outside is warmer
# than inside" about this morning is noise. The side still advances, so the
# fresh data that follows does not look like a new crossing.
STALE_SEC = 15 * 60

# Rows pulled per check. Normal operation is one row per 60 s; this only matters
# when draining a long backlog, and the remainder is picked up next check.
BATCH = 20000


@dataclass(frozen=True)
class Settings:
    threshold_f: float = 1.0
    debounce_sec: int = 15 * 60
    stale_sec: int = STALE_SEC
    db_path: str = weather_station.DB_PATH
    state_path: str = STATE_PATH


@dataclass(frozen=True)
class Sample:
    obs_id: int
    local_time: str
    received_unix: float
    t_in_f: float
    t_out_f: float

    @property
    def delta_f(self):
        return self.t_out_f - self.t_in_f


def _f(c):
    return None if c is None else c * 9 / 5 + 32


def sample_from_row(row):
    t_in, t_out = _f(row["temp_in_c"]), _f(row["temp_out_c"])
    if t_in is None or t_out is None:
        return None
    return Sample(obs_id=row["id"], local_time=row["local_time"], received_unix=row["received_unix"], t_in_f=t_in, t_out_f=t_out)


def side_of(delta_f, threshold_f):
    if delta_f >= threshold_f:
        return ABOVE
    if delta_f <= -threshold_f:
        return BELOW
    return DEAD_BAND


def _empty_state():
    return {"last_obs_id": None, "side": None, "sent": {}}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(state, dict):
        return _empty_state()
    merged = _empty_state()
    merged.update(state)
    if not isinstance(merged.get("sent"), dict):
        merged["sent"] = {}
    return merged


def save_state(path, state):
    # Write beside the target and rename, so a kill mid-write cannot leave the
    # daemon unable to read its own debounce history.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _connection(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_after(db_path, obs_id, limit=BATCH):
    conn = _connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, local_time, received_unix, temp_in_c, temp_out_c FROM obs WHERE id > ? ORDER BY id LIMIT ?",
            (obs_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _newest_row(db_path):
    conn = _connection(db_path)
    try:
        row = conn.execute("SELECT id, local_time, received_unix, temp_in_c, temp_out_c FROM obs ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _say(log, msg):
    if log:
        log(msg)


def _clock(local_time):
    try:
        return datetime.fromisoformat(local_time).strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return local_time or "unknown time"


def render_message(sample, side, threshold_f=1.0):
    warmer = side == ABOVE
    headline = "Outside is now warmer than inside" if warmer else "Outside is now colder than inside"
    return "\n".join(
        [
            "🌡️ <b>%s</b>" % headline,
            "",
            "🏠 Inside: <b>%.1f °F</b>" % sample.t_in_f,
            "🌤️ Outside: <b>%.1f °F</b>" % sample.t_out_f,
            "Δ Difference: <b>%+.1f °F</b>" % sample.delta_f,
            "",
            "<i>±%.1f °F threshold · %s</i>" % (threshold_f, _clock(sample.local_time)),
        ]
    )


def _since(ts, now):
    if not ts:
        return "never"
    sec = max(0.0, now - ts)
    if sec < 90:
        return "%.0fs ago" % sec
    if sec < 5400:
        return "%.0fm ago" % (sec / 60)
    return "%.1fh ago" % (sec / 3600)


def status(settings=None, now=None):
    """One line for `ws status`: where the watcher is, and when it last spoke."""
    settings = settings or Settings()
    now = time.time() if now is None else now
    state = load_state(settings.state_path)
    if state["last_obs_id"] is None:
        return "no state yet: the first check runs 30 s after the daemon starts, silently"
    sent = state["sent"]
    return "outside %s, last above alert %s, last below alert %s" % (
        state["side"],
        _since(sent.get(ABOVE), now),
        _since(sent.get(BELOW), now),
    )


def check(settings=None, now=None, log=None):
    """Walk every unseen weather report and return the alert messages to send.

    First run on this host (no state file) adopts the current side silently:
    the daemon being restarted must not announce a crossing that is already
    hours old.
    """
    settings = settings or Settings()
    now = time.time() if now is None else now
    # sqlite3.connect() would happily create an empty database here, which would
    # leave a stray file and silently start the alert clock at nothing.
    if not os.path.exists(settings.db_path):
        return []
    state = load_state(settings.state_path)

    if state["last_obs_id"] is None:
        row = _newest_row(settings.db_path)
        sample = sample_from_row(row) if row else None
        if sample is None:
            return []
        state["last_obs_id"] = sample.obs_id
        state["side"] = side_of(sample.delta_f, settings.threshold_f)
        save_state(settings.state_path, state)
        _say(log, "temp alert: initialized side=%s obs=%d inside=%.1fF outside=%.1fF" % (state["side"], sample.obs_id, sample.t_in_f, sample.t_out_f))
        return []

    messages = []
    for row in _rows_after(settings.db_path, state["last_obs_id"]):
        state["last_obs_id"] = row["id"]
        sample = sample_from_row(row)
        if sample is None:
            continue

        side = side_of(sample.delta_f, settings.threshold_f)
        previous = state["side"]
        state["side"] = side
        if side == DEAD_BAND or side == previous:
            continue

        if now - row["received_unix"] > settings.stale_sec:
            _say(log, "temp alert: skipped stale %s crossing at obs %d" % (side, row["id"]))
            continue

        # Debounce on the report's own clock, not the poll time, so a backlog
        # drained in one check behaves the same as one report per check.
        last_sent = state["sent"].get(side, 0.0)
        if row["received_unix"] - last_sent < settings.debounce_sec:
            _say(log, "temp alert: %s crossing at obs %d inside debounce (%ds)" % (side, row["id"], settings.debounce_sec))
            continue

        state["sent"][side] = row["received_unix"]
        messages.append(render_message(sample, side, settings.threshold_f))
        _say(log, "temp alert: %s crossing at obs %d inside=%.1fF outside=%.1fF delta=%+.1fF" % (side, row["id"], sample.t_in_f, sample.t_out_f, sample.delta_f))

    save_state(settings.state_path, state)
    return messages


if __name__ == "__main__":
    for msg in check(log=lambda m: print(m, flush=True)):
        print()
        print(msg)
