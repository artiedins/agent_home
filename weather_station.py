#!/usr/bin/env python3
# Ecowitt WS2910 console -> SQLite receiver.
#
# The console pushes one HTTP POST per 60 s (Ecowitt "Customized server",
# /data/report/ on 192.168.1.12:8001) and keeps no history: any minute this
# process is not listening is a minute of weather that cannot be recovered.
# It does not read the reply, so this answers 200/OK even when its own database
# write fails. Full wire protocol and the field quirks: ws_data_guide.md.
#
# Two ways to run it, same code:
#   python3 weather_station.py          standalone (foreground, ^C to stop)
#   weather_station.start(log=...)      a supervised thread inside home_daemon
#
# home_daemon is the normal path, so the single
# `nohup python3 -u home_daemon.py &` command covers Telegram and weather both.
#
# Env overrides (all optional): ECOWITT_BIND, ECOWITT_PORT, ECOWITT_DB,
# ECOWITT_TZ.

import atexit
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
BIND = os.environ.get("ECOWITT_BIND", "0.0.0.0")
PORT = int(os.environ.get("ECOWITT_PORT", "8001"))
DB_PATH = os.environ.get("ECOWITT_DB", os.path.join(ROOT, "weather_station.db"))
STATION_TZ = ZoneInfo(os.environ.get("ECOWITT_TZ", "America/Los_Angeles"))
SENTINEL = -9999.0

# The server this process is serving, for `ws status` to report. None means the
# port could not be bound (a second daemon, or a standalone receiver already up).
LISTENER = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (
  id                 INTEGER PRIMARY KEY,
  station_utc        TEXT NOT NULL UNIQUE,
  received_unix      REAL NOT NULL,
  received_at        TEXT NOT NULL,
  local_time         TEXT NOT NULL,
  local_date         TEXT NOT NULL,
  temp_out_c         REAL,  temp_in_c        REAL,
  humidity_out       INTEGER, humidity_in    INTEGER,
  pressure_rel_hpa   REAL,  pressure_abs_hpa REAL,
  wind_dir_deg       INTEGER,
  wind_speed_ms      REAL,  wind_gust_ms     REAL,  max_daily_gust_ms REAL,
  solar_wm2          REAL,  uv_index         INTEGER, vpd_kpa          REAL,
  rain_rate_mmh      REAL,  rain_event_mm    REAL,  rain_hourly_mm    REAL,
  rain_day_console_mm   REAL, rain_week_console_mm  REAL,
  rain_month_console_mm REAL, rain_year_console_mm  REAL, rain_total_mm REAL,
  battery_array_ok   INTEGER, runtime_s INTEGER, heap_b INTEGER,
  stationtype        TEXT, interval_s INTEGER, station_skew_s REAL, gap_s REAL,
  client_ip          TEXT, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS obs_received ON obs(received_unix);
CREATE INDEX IF NOT EXISTS obs_local_date ON obs(local_date);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY, started_unix REAL NOT NULL, stopped_unix REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS counter_reset (
  id INTEGER PRIMARY KEY, at_unix REAL, counter TEXT, old_value REAL, new_value REAL
);

-- Day rain comes from the monotonic lifetime counter, so it does not care what
-- the console's own rain reset hour is set to.
CREATE VIEW IF NOT EXISTS obs_step AS
SELECT id, local_date, local_time, received_unix, rain_total_mm,
       lag(rain_total_mm) OVER (ORDER BY received_unix) AS prev_total
FROM obs;

CREATE VIEW IF NOT EXISTS rain_by_local_day AS
SELECT local_date,
       round(sum(max(0.0, rain_total_mm - coalesce(prev_total, rain_total_mm))), 2) AS rain_mm,
       round(sum(max(0.0, rain_total_mm - coalesce(prev_total, rain_total_mm))) / 0.254) AS tips,
       count(*) AS samples
FROM obs_step GROUP BY local_date;

CREATE VIEW IF NOT EXISTS day_summary AS
SELECT local_date, count(*) AS samples,
       min(temp_out_c) AS tmin_c, max(temp_out_c) AS tmax_c,
       round(max(max_daily_gust_ms), 2) AS gust_max_ms,
       round(max(uv_index), 1) AS uv_max,
       round(sum(solar_wm2) / 1000.0 * (60.0 / 3600.0), 3) AS insolation_kwh_m2,
       max(rain_total_mm) - min(rain_total_mm) AS rain_mm_raw
FROM obs GROUP BY local_date;
"""


def f_to_c(v):
    return (v - 32) * 5 / 9


def c_to_f(v):
    return None if v is None else v * 9 / 5 + 32


def mm_to_in(v):
    return None if v is None else v / 25.4


def first(params, *names):
    for n in names:
        if params.get(n) not in (None, ""):
            return params[n]
    return None


def num(params, *names):
    v = first(params, *names)
    if v is None:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if f == SENTINEL else f


def pick(params, *pairs):
    # pairs are (wire name, factor); the first present field wins, so a console
    # switched to metric output decodes with the same code.
    for name, factor in pairs:
        v = num(params, name)
        if v is not None:
            return round(v * factor, 3)
    return None


def temp_c(params, *names):
    # Degrees need the offset, so temperature cannot use pick()'s plain factor:
    # tempc is already Celsius, tempf is Fahrenheit.
    for name in names:
        v = num(params, name)
        if v is not None:
            return round(v if not name.endswith("f") else f_to_c(v), 2)
    return None


def as_int(params, *names):
    v = num(params, *names)
    return None if v is None else int(v)


def pressure_kpa(temp_celsius, humidity):
    # Saturation vapour pressure in kPa; VPD follows from it. The console reports
    # vpd in whatever barometer unit it is set to (inHg on ours), so derive it.
    if temp_celsius is None or humidity is None:
        return None
    es = 0.6108 * pow(2.718281828, 17.27 * temp_celsius / (temp_celsius + 237.3))
    return round(es * (1 - humidity / 100.0), 4)


def metric_row(params):
    row = {
        "temp_out_c": temp_c(params, "tempc", "tempf"),
        "temp_in_c": temp_c(params, "tempinc", "tempinf"),
        "humidity_out": as_int(params, "humidity"),
        "humidity_in": as_int(params, "humidityin"),
        "pressure_rel_hpa": pick(params, ("baromrelhpa", 1.0), ("baromrelin", 33.8639)),
        "pressure_abs_hpa": pick(params, ("baromabshpa", 1.0), ("baromabsin", 33.8639)),
        "wind_dir_deg": as_int(params, "winddir"),
        "wind_speed_ms": pick(params, ("windspeedms", 1.0), ("windspeedmph", 0.44704)),
        "wind_gust_ms": pick(params, ("windgustms", 1.0), ("windgustmph", 0.44704)),
        "max_daily_gust_ms": pick(params, ("maxdailygustms", 1.0), ("maxdailygustmph", 0.44704), ("maxdailygust", 0.44704)),
        "solar_wm2": pick(params, ("solarradiation", 1.0)),
        "uv_index": as_int(params, "uv"),
        "rain_rate_mmh": pick(params, ("rainratemm", 1.0), ("rainratein", 25.4)),
        "rain_event_mm": pick(params, ("eventrainmm", 1.0), ("eventrainin", 25.4)),
        "rain_hourly_mm": pick(params, ("hourlyrainmm", 1.0), ("hourlyrainin", 25.4)),
        "rain_day_console_mm": pick(params, ("dailyrainmm", 1.0), ("dailyrainin", 25.4)),
        "rain_week_console_mm": pick(params, ("weeklyrainmm", 1.0), ("weeklyrainin", 25.4)),
        "rain_month_console_mm": pick(params, ("monthlyrainmm", 1.0), ("monthlyrainin", 25.4)),
        "rain_year_console_mm": pick(params, ("yearlyrainmm", 1.0), ("yearlyrainin", 25.4)),
        "rain_total_mm": pick(params, ("totalrainmm", 1.0), ("totalrainin", 25.4)),
        "battery_array_ok": None if as_int(params, "wh65batt") is None else int(as_int(params, "wh65batt") == 0),
        "runtime_s": as_int(params, "runtime"),
        "heap_b": as_int(params, "heap"),
        "stationtype": first(params, "stationtype"),
        "interval_s": as_int(params, "interval"),
    }
    row["vpd_kpa"] = pressure_kpa(row["temp_out_c"], row["humidity_out"])
    return row


def station_utc(params):
    # dateutc arrives as '2026-09-18+03:20:39' (plus sign for the space), UTC.
    raw = first(params, "dateutc")
    if not raw:
        return None
    try:
        stamp = datetime.strptime(raw.replace("+", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)


class Db:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        # WAL so the daemon's own readers (the `ws` command) never block ingest.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.last_unix = None
        self.last_total = None

    def start_run(self, note):
        self.close_stale_runs()
        cur = self.conn.execute("INSERT INTO ingest_runs (started_unix, note) VALUES (?, ?)", (time.time(), note))
        self.conn.commit()
        return cur.lastrowid

    def close_stale_runs(self):
        # A killed process never writes its own stop time, which would leave the
        # outage ledger permanently open. Close any run left open by a previous
        # process at that run's last report, so gaps between runs are the real
        # outage intervals.
        open_runs = self.conn.execute("SELECT id FROM ingest_runs WHERE stopped_unix IS NULL").fetchall()
        if not open_runs:
            return 0
        last = self.conn.execute("SELECT max(received_unix) FROM obs").fetchone()[0]
        for (run_id,) in open_runs:
            self.conn.execute("UPDATE ingest_runs SET stopped_unix = ? WHERE id = ?", (last or time.time(), run_id))
        self.conn.commit()
        return len(open_runs)

    def stop_run(self, run_id):
        try:
            self.conn.execute("UPDATE ingest_runs SET stopped_unix = ? WHERE id = ? AND stopped_unix IS NULL", (time.time(), run_id))
            self.conn.commit()
        except sqlite3.Error:
            pass

    def insert(self, client_ip, path, params):
        now = time.time()
        utc = station_utc(params)
        local = datetime.fromtimestamp(now, timezone.utc).astimezone(STATION_TZ)
        row = metric_row(params)
        row.update(
            {
                "station_utc": utc.strftime("%Y-%m-%d %H:%M:%S") if utc else None,
                "received_unix": round(now, 3),
                "received_at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "local_time": local.isoformat(timespec="seconds"),
                "local_date": local.strftime("%Y-%m-%d"),
                "station_skew_s": None if utc is None else round(now - utc.timestamp(), 1),
                "gap_s": None if self.last_unix is None else round(now - self.last_unix, 1),
                "client_ip": client_ip,
                "raw_json": json.dumps(params, separators=(",", ":")),
            }
        )
        if self.last_total is not None and row["rain_total_mm"] is not None and row["rain_total_mm"] < self.last_total:
            # The lifetime counter only ever goes up unless someone presses RAIN.
            self.conn.execute("INSERT INTO counter_reset (at_unix, counter, old_value, new_value) " "VALUES (?, ?, ?, ?)", (now, "rain_total_mm", self.last_total, row["rain_total_mm"]))
        self.last_total = row["rain_total_mm"]
        self.last_unix = now
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT OR IGNORE INTO obs ({cols}) VALUES ({marks})", list(row.values()))
        self.conn.commit()
        return row, cur.rowcount


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ws-ingest/1.0"
    timeout = 15

    def log_message(self, fmt, *args):
        pass

    def reply(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def body(self):
        n = self.headers.get("Content-Length")
        try:
            n = int(str(n).strip())
        except (TypeError, ValueError):
            return b""
        return self.rfile.read(n) if n > 0 else b""

    def params(self, query, payload):
        out = {}
        for chunk in (query, payload):
            if chunk:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", "replace")
                out.update({k: v[-1] for k, v in parse_qs(chunk.strip(), keep_blank_values=True).items()})
        return out

    def do_POST(self):
        parsed = urlparse(self.path)
        params = self.params(parsed.query, self.body())
        self.server.count += 1
        try:
            row, inserted = self.server.db.insert(self.client_address[0], parsed.path, params)
            print(
                f'{row["received_at"]}Z {row["local_time"]} {parsed.path} '
                f'temp={row["temp_out_c"]}C hum={row["humidity_out"]} '
                f'wind={row["wind_speed_ms"]}m/s rain_day_console={row["rain_day_console_mm"]}mm '
                f"new={inserted}",
                flush=True,
            )
        except Exception as exc:
            # Never answer a bad status: the console is unattended and a lost row
            # is better than an upload it might abandon.
            print(f"db error on {parsed.path}: {exc!r}", file=sys.stderr, flush=True)
        self.reply(b"OK")

    def do_GET(self):
        parsed = urlparse(self.path)
        params = self.params(parsed.query, b"")
        proto = "wunderground" if params.get("action") == "updateraw" else "ecowitt"
        if parsed.path.rstrip("/") == "/status":
            self.reply(self.server.status().encode())
            return
        try:
            self.server.db.insert(self.client_address[0], parsed.path, params)
        except Exception as exc:
            print(f"db error on GET {parsed.path}: {exc!r}", file=sys.stderr, flush=True)
        self.reply(b"success" if proto == "wunderground" else b"OK")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, db):
        super().__init__(addr, Handler)
        self.db = db
        self.started = time.time()
        self.count = 0

    def status(self):
        age = None if self.db.last_unix is None else round(time.time() - self.db.last_unix, 1)
        return f"reports={self.count} since={datetime.fromtimestamp(self.started, timezone.utc)}Z " f"last_report_age_s={age}\n"


def _serve_forever(server, log):
    # The console is unattended (no batteries in it, above a piano), so losing
    # the socket for a minute is losing weather. Re-bind if serve_forever ever
    # returns or raises instead of dying quietly.
    while True:
        try:
            server.serve_forever()
        except Exception as exc:
            log(f"weather_station: server error: {exc!r}")
        log("weather_station: server stopped; re-binding in 5s")
        time.sleep(5)


def start(db_path=DB_PATH, bind=BIND, port=PORT, log=print):
    """Bind and serve in a daemon thread. Returns the server, or None if the
    port is taken (a second daemon, or a stale receiver): the caller keeps
    running either way, because Telegram must not die with the weather socket."""
    global LISTENER
    db = Db(db_path)
    try:
        httpd = Server((bind, port), db)
    except OSError as exc:
        LISTENER = None
        log(f"weather_station: cannot bind {bind}:{port} ({exc}); weather ingest disabled " f"(another receiver running? check: ss -ltnp | grep {port})")
        return None
    # Only a listener that actually bound opens an ingest run, so a failed start
    # leaves no ledger row behind.
    run_id = db.start_run(f"{bind}:{port}")
    thread = threading.Thread(target=_serve_forever, args=(httpd, log), name="ws-ingest", daemon=True)
    thread.start()
    LISTENER = httpd

    def _shutdown():
        httpd.shutdown()
        db.stop_run(run_id)

    atexit.register(_shutdown)
    log(f"weather_station: listening on {bind}:{port} -> {db_path} (ingest run {run_id})")
    return httpd


def main():
    httpd = start(log=lambda m: print(m, flush=True))
    if httpd is None:
        sys.exit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ readers --
# Read-only helpers for the Telegram `ws` command. Each opens its own short
# connection so it also works from a process that is not the receiver.


def _rows(db_path, sql, args=()):
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def latest(db_path=None):
    rows = _rows(db_path or DB_PATH, "SELECT * FROM obs ORDER BY received_unix DESC LIMIT 1")
    return rows[0] if rows else None


def recent(limit=6, db_path=None):
    return _rows(db_path or DB_PATH, "SELECT * FROM obs ORDER BY received_unix DESC LIMIT ?", (limit,))


def day_summary(local_date, db_path=None):
    rows = _rows(db_path or DB_PATH, "SELECT * FROM day_summary WHERE local_date = ?", (local_date,))
    return rows[0] if rows else None


def rain_by_day(local_date, db_path=None):
    rows = _rows(db_path or DB_PATH, "SELECT * FROM rain_by_local_day WHERE local_date = ?", (local_date,))
    return rows[0] if rows else None


def health(db_path=None):
    """Enough to answer 'is the station still coming in, and how completely'."""
    db_path = db_path or DB_PATH
    out = {"rows": 0, "age_s": None, "first_local": None, "last_local": None, "today": None, "runs": 0, "run_started_unix": None, "counter_resets": 0}
    if not os.path.exists(db_path):
        return out
    row = _rows(db_path, "SELECT count(*) AS n, min(received_unix) AS a, max(received_unix) AS b, " "max(local_time) AS last_local FROM obs")
    if row and row[0]["n"]:
        out["rows"] = row[0]["n"]
        out["age_s"] = round(time.time() - row[0]["b"], 1)
        out["last_local"] = row[0]["last_local"]
    first = _rows(db_path, "SELECT local_time FROM obs ORDER BY received_unix LIMIT 1")
    out["first_local"] = first[0]["local_time"] if first else None
    today = datetime.now(STATION_TZ).strftime("%Y-%m-%d")
    out["today"] = day_summary(today, db_path)
    out["today_rain"] = rain_by_day(today, db_path)
    runs = _rows(db_path, "SELECT count(*) AS n FROM ingest_runs")
    out["runs"] = runs[0]["n"] if runs else 0
    last_run = _rows(db_path, "SELECT started_unix, stopped_unix FROM ingest_runs ORDER BY id DESC LIMIT 1")
    if last_run:
        out["run_started_unix"] = last_run[0]["started_unix"]
        out["run_stopped_unix"] = last_run[0]["stopped_unix"]
    resets = _rows(db_path, "SELECT count(*) AS n FROM counter_reset")
    out["counter_resets"] = resets[0]["n"] if resets else 0
    return out


if __name__ == "__main__":
    main()
