import os
import time
from datetime import datetime

import timers
import rheem_api
import sun
import temp_transition
import weather
import weather_station

# ============================================================
# WATER HEATER COMMANDS (wh)
# ============================================================


def cmd_wh(ctx, args):
    """Water heater control. Usage: wh <high|low|status|snooze>"""
    if not args:
        return "Usage: wh <high|low|status|snooze>"

    subcmd = args.split()[0].lower()

    if subcmd == "status":
        try:
            heaters = ctx.rheem.status()
            if not heaters:
                return "No water heaters found"
            h = heaters[0]
            return f"Water heater: {h['mode']}, {h['hot_water_pct']}% hot, set to {h['set_point']}F"
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "high":
        try:
            ctx.rheem.set_high_demand()
            timers.cancel(name="high_demand_reminder")
            timers.add("high_demand_reminder", delay_sec=20 * 60, interval_sec=5 * 60)
            return "Switched to HIGH DEMAND. Will remind you in 20 min."
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "low":
        try:
            ctx.rheem.set_energy_saver()
            n = timers.cancel(name="high_demand_reminder")
            msg = "Switched to ENERGY SAVER."
            if n > 0:
                msg += " Cancelled reminder."
            return msg
        except Exception as e:
            return f"Error: {e}"

    elif subcmd == "snooze":
        all_timers = timers.list_all()
        for t in all_timers:
            if t["name"] == "high_demand_reminder":
                timers.reschedule(t["id"], 10 * 60)
                return "Snoozed for 10 minutes"
        return "No reminder to snooze"

    else:
        return f"Unknown: wh {subcmd}. Try: wh high, wh low, wh status, wh snooze"


def cmd_timers(ctx, args):
    """List all pending timers."""
    all_timers = timers.list_all()
    if not all_timers:
        return "No pending timers"
    lines = []
    for t in all_timers:
        remaining = int(t["fire_at"] - __import__("time").time())
        mins = remaining // 60
        lines.append(f"  {t['name']}: {mins}m remaining")
    return "Timers:\n" + "\n".join(lines)


def cmd_cancel(ctx, args):
    """Cancel a timer by name. Usage: cancel <name>"""
    if not args:
        return "Usage: cancel <timer_name>"
    n = timers.cancel(name=args)
    if n:
        return f"Cancelled {n} timer(s)"
    return f"No timer named '{args}'"


# ============================================================
# TIMER HANDLERS
# When a timer fires, the matching handler is called
# ============================================================


def timer_high_demand_reminder(ctx, timer):
    """Called when high_demand_reminder timer fires."""
    ctx.send("Water heater still in HIGH DEMAND. Reply 'wh low' or 'wh snooze'.")
    return None


# ============================================================
# SUN ANGLE COMMANDS
# ============================================================


def schedule_sun_timers():
    """Schedule timers for today's and tomorrow's sun angle crossings."""
    # Cancel pending sun timers, but keep ones already due so a late recompute
    # does not silently drop a crossing that is about to fire.
    now_ts = __import__("time").time()
    for t in timers.list_all():
        if t["name"] == "sun_crossing" and t["fire_at"] > now_ts:
            timers.delete(t["id"])

    # Get crossing times for today and tomorrow. Scheduling tomorrow's too
    # means a recompute that runs after this morning's crossing still
    # schedules the next morning's rising message.
    from datetime import datetime, timedelta

    now = datetime.now(sun.tz)
    scheduled = 0
    for offset in (0, 1):
        date = now.date() + timedelta(days=offset)
        for t, direction in sun.get_crossing_times(date):
            delay = sun.seconds_until(t)
            if delay > 0:
                timers.add("sun_crossing", delay_sec=delay, data={"direction": direction})
                scheduled += 1

    # Keep the daily recompute anchored to 4am. If it fires later in the day it
    # skips the morning crossing and rising messages stop; re-anchor whenever
    # this runs so any drift heals on daemon start and after each daily fire.
    existing = timers.list_all()
    daily = next((t for t in existing if t["name"] == "sun_daily_schedule"), None)
    next_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if next_4am <= now:
        next_4am += timedelta(days=1)
    delay = (next_4am - now).total_seconds()
    if daily:
        timers.reschedule(daily["id"], delay)
    else:
        timers.add("sun_daily_schedule", delay_sec=delay, interval_sec=24 * 60 * 60)

    return scheduled


def cmd_sun(ctx, args):
    """Sun angle notifications. Usage: sun [status|init]"""
    subcmd = args.split()[0].lower() if args else "status"

    if subcmd == "status":
        nxt = sun.next_crossing()
        if nxt:
            t, direction = nxt
            return f"Sun {sun.SUN_ANGLE}° crossing: {direction} at {t.strftime('%H:%M')}"
        return f"No {sun.SUN_ANGLE}° crossing today or tomorrow (angle too high for season?)"

    elif subcmd == "init":
        n = schedule_sun_timers()
        return f"Scheduled {n} sun timer(s). Angle: {sun.SUN_ANGLE}°"

    else:
        return "Usage: sun [status|init]"


def timer_sun_crossing(ctx, timer):
    """Called when sun crosses threshold angle."""
    data = timers.get_data(timer)
    direction = data.get("direction", "crossing")
    ctx.send(f"Sun {direction} through {sun.SUN_ANGLE}°")
    return None


def timer_sun_daily_schedule(ctx, timer):
    """Called daily at 4am to schedule sun timers."""
    n = schedule_sun_timers()
    # Silent - don't notify user about the daily scheduling
    return None


# ============================================================
# WEATHER
# ============================================================


def schedule_weather_timer():
    """Anchor the daily weather message to 8am local, same re-anchor trick
    as schedule_sun_timers: whichever way this runs (daemon start or daily
    fire) the next fire lands on the next 8am and skips missed cycles."""
    from datetime import datetime, timedelta

    now = datetime.now(sun.tz)
    next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if next_8am <= now:
        next_8am += timedelta(days=1)
    delay = (next_8am - now).total_seconds()

    existing = next((t for t in timers.list_all() if t["name"] == "weather_daily"), None)
    if existing:
        timers.reschedule(existing["id"], delay)
    else:
        timers.add("weather_daily", delay_sec=delay, interval_sec=24 * 60 * 60)
    return next_8am


def cmd_weather(ctx, args):
    """Daily weather message. Usage: weather [init]"""
    subcmd = args.split()[0].lower() if args else "now"

    if subcmd == "init":
        next_fire = schedule_weather_timer()
        return f"Weather message scheduled daily at 8am. Next: {next_fire.strftime('%a %H:%M')}"
    elif subcmd == "now":
        try:
            return weather.daily_message()
        except Exception as e:
            return f"Weather fetch failed: {e}"
    else:
        return "Usage: weather [now|init]"


def timer_weather_daily(ctx, timer):
    """Called daily at 8am: three 3-line forecast messages (Google ensemble +
    NOAA/NWS + MET Norway), one CSV log line per source per day."""
    # Re-anchor to wall-clock 8am on every fire (same as the sun schedule);
    # a bare 86400s interval would drift an hour across DST changes.
    schedule_weather_timer()
    try:
        messages = weather.daily_messages(log=True)
    except Exception as e:
        # Loud rather than silent so a broken API key/endpoint gets noticed.
        return f"Weather update failed: {e}"
    for msg in messages:
        ctx.send(msg)
    return None


# Weather station on the roof (Ecowitt WS2910 -> weather_station.py -> SQLite).
# Values are stored metric and shown in the console's own units (F, inches) so
# what the phone says matches what the display by the piano says.


def _f(c):
    return None if c is None else c * 9 / 5 + 32


def _mph(ms):
    return None if ms is None else ms * 2.23694


def _in(mm):
    return None if mm is None else mm / 25.4


def _inhg(hpa):
    return None if hpa is None else hpa / 33.8639


def _deg(v, fmt="{:.1f}"):
    return "--" if v is None else fmt.format(v)


def _compass(deg):
    if deg is None:
        return "--"
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return points[int((deg % 360) / 22.5 + 0.5) % 16]


def _n_reports(n):
    return "%d %s" % (n, "report" if n == 1 else "reports")


def _age_str(sec):
    if sec is None:
        return "never"
    if sec < 120:
        return "%.0f s ago" % sec
    if sec < 7200:
        return "%.0f min ago" % (sec / 60)
    return "%.1f h ago" % (sec / 3600)


def _ws_now():
    row = weather_station.latest()
    if not row:
        return "Weather station: no reports received yet.\n" "Console Customized server should point at 192.168.1.12, port 8001, " "path /data/report/, protocol ecowitt, 60 s."
    age = time.time() - row["received_unix"]
    lines = ["Weather station %s (%s)" % (row["local_time"][11:19], _age_str(age))]
    lines.append("Outdoor: %sF, %s%% RH" % (_deg(_f(row["temp_out_c"])), _deg(row["humidity_out"], "{:.0f}")))
    if row["wind_speed_ms"] is not None:
        lines.append("Wind: %s mph from %s, gust %s mph" % (_deg(_mph(row["wind_speed_ms"])), _compass(row["wind_dir_deg"]), _deg(_mph(row["wind_gust_ms"]))))
    today = weather_station.rain_by_day(datetime.now(weather_station.STATION_TZ).strftime("%Y-%m-%d"))
    if today:
        lines.append("Rain: %s in/h now, %s in today (%d gauge tips)" % (_deg(_in(row["rain_rate_mmh"]), "{:.3f}"), _deg(_in(today["rain_mm"]), "{:.2f}"), today["tips"] or 0))
    else:
        lines.append("Rain: %s in/h, no completed day total yet" % _deg(_in(row["rain_rate_mmh"]), "{:.3f}"))
    lines.append("Sun: %s W/m2, UV %s" % (_deg(row["solar_wm2"], "{:.0f}"), _deg(row["uv_index"], "{:.0f}")))
    lines.append("Console: %sF, %s%% RH, %s inHg" % (_deg(_f(row["temp_in_c"])), _deg(row["humidity_in"], "{:.0f}"), _deg(_inhg(row["pressure_rel_hpa"]), "{:.2f}")))
    if row["battery_array_ok"] == 0:
        lines.append("Array battery LOW (replace the 2x AA lithium cells)")
    if age > 600:
        lines.append("Reading is stale: the console stops sending when it loses power or Wi-Fi.")
    return "\n".join(lines)


def _ws_today():
    date = datetime.now(weather_station.STATION_TZ).strftime("%Y-%m-%d")
    day = weather_station.day_summary(date)
    if not day:
        return "Weather station: nothing stored for %s yet." % date
    rain = weather_station.rain_by_day(date) or {}
    lines = ["Weather station today %s: %s, %.0f%% of the day" % (date, _n_reports(day["samples"]), day["samples"] * 60 / 86400 * 100)]
    if day["tmin_c"] is not None:
        lines.append("Outdoor low %sF, high %sF" % (_deg(_f(day["tmin_c"])), _deg(_f(day["tmax_c"]))))
    if rain:
        lines.append("Rain %s in (%d gauge tips)" % (_deg(_in(rain["rain_mm"]), "{:.2f}"), rain["tips"] or 0))
    lines.append("Max gust %s mph, UV max %s, sun %s kWh/m2" % (_deg(_mph(day["gust_max_ms"])), _deg(day["uv_max"], "{:.0f}"), _deg(day["insolation_kwh_m2"], "{:.2f}")))
    return "\n".join(lines)


def _ws_status():
    h = weather_station.health()
    lines = ["Weather station ingest"]
    if weather_station.LISTENER is not None:
        lines.append("Receiver: listening on %s:%d" % (weather_station.BIND, weather_station.PORT))
    else:
        lines.append("Receiver: NOT listening in this process (port %d busy, or the thread failed to start)" % weather_station.PORT)
    if h["rows"]:
        lines.append("Last report: %s, at %s" % (_age_str(h["age_s"]), h["last_local"][11:19]))
        lines.append("Stored: %s since %s" % (_n_reports(h["rows"]), h["first_local"][:16].replace("T", " ")))
    else:
        lines.append("Last report: none yet")
    if h["today"]:
        lines.append("Today: %s, %.0f%% of the day" % (_n_reports(h["today"]["samples"]), h["today"]["samples"] * 60 / 86400 * 100))
    lines.append("Database: %s (%d ingest runs logged)" % (os.path.basename(weather_station.DB_PATH), h["runs"]))
    lines.append("Temp alert: %s" % temp_transition.status())
    if h["counter_resets"]:
        lines.append("Rain counter resets logged: %d (RAIN button pressed)" % h["counter_resets"])
    return "\n".join(lines)


def _ws_tail():
    rows = weather_station.recent(limit=6)
    if not rows:
        return "Weather station: nothing stored yet."
    lines = ["Last %s:" % _n_reports(len(rows))]
    for r in rows:
        lines.append(
            "%s  %sF %s%%  %s %s mph  rain %s in/h  sun %s"
            % (
                r["local_time"][11:19],
                _deg(_f(r["temp_out_c"])),
                _deg(r["humidity_out"], "{:.0f}"),
                _compass(r["wind_dir_deg"]),
                _deg(_mph(r["wind_speed_ms"])),
                _deg(_in(r["rain_rate_mmh"]), "{:.3f}"),
                _deg(r["solar_wm2"], "{:.0f}"),
            )
        )
    return "\n".join(lines)


def cmd_ws(ctx, args):
    """Roof sensors right now. Usage: ws [now|today|status|tail]"""
    sub = args.split()[0].lower() if args else "now"
    if sub in ("now", "last", "latest", "readings"):
        return _ws_now()
    if sub == "today":
        return _ws_today()
    if sub == "status":
        return _ws_status()
    if sub in ("tail", "recent"):
        return _ws_tail()
    return "Usage: ws [now|today|status|tail]"


# The guide's failure mode is a station that answers OK while writing nothing:
# the console keeps pushing and the database stays empty, with no error anywhere.
# Alert only when reports actually stop, so a dead console (no batteries in it)
# or a wedged receiver gets noticed. Re-alert hourly while it stays down.
WS_STALE_SEC = 600
WS_REALERT_SEC = 3600
_ws_last_alert = 0.0


def schedule_ws_health():
    """Keep the 5-minute health check on its grid across daemon restarts."""
    existing = next((t for t in timers.list_all() if t["name"] == "ws_health"), None)
    if existing:
        timers.reschedule_interval(existing["id"], 5 * 60)
    else:
        timers.add("ws_health", delay_sec=5 * 60, interval_sec=5 * 60)


def timer_ws_health(ctx, timer):
    global _ws_last_alert
    h = weather_station.health()
    if h["age_s"] is not None and h["age_s"] <= WS_STALE_SEC:
        return None
    now = time.time()
    if now - _ws_last_alert < WS_REALERT_SEC:
        return None
    _ws_last_alert = now
    if h["age_s"] is None:
        return "Weather station: no reports received at all. Check the console is powered and on Wi-Fi, " "and that its Customized server points at this host port %d." % weather_station.PORT
    return "Weather station: no report for %.0f min (last %s). The console buffers nothing, so every " "minute down is weather we never get back. Check the console and port %d on this host." % (
        h["age_s"] / 60,
        h["last_local"][11:16],
        weather_station.PORT,
    )


# Inside/outside crossings. This was a 30 s poll, which cost a DB read every half
# minute for a check whose debounce is 15 minutes per direction: a crossing could
# wait up to 5 minutes without changing any alert that got sent. Threshold,
# per-direction debounce, and the state file are in temp_transition.
TEMP_ALERT_POLL_SEC = 5 * 60


def schedule_temp_alert():
    """Keep the crossing check on its grid across daemon restarts."""
    existing = next((t for t in timers.list_all() if t["name"] == "temp_alert"), None)
    if existing:
        timers.reschedule_interval(existing["id"], TEMP_ALERT_POLL_SEC)
    else:
        timers.add("temp_alert", delay_sec=TEMP_ALERT_POLL_SEC, interval_sec=TEMP_ALERT_POLL_SEC)


def timer_temp_alert(ctx, timer):
    """Send one HTML message per crossing temp_transition reports. Sent here
    rather than returned, because process_timers sends returns as plain text and
    the message carries <b>/<i> markup."""
    for msg in temp_transition.check(temp_transition.Settings(), log=getattr(ctx, "log", None)):
        ctx.send(msg, parse_mode="HTML")
    return None


# ============================================================
# UTILITY COMMANDS
# ============================================================


def cmd_help(ctx, args):
    """Show available commands."""
    cmds = list(COMMANDS.keys())
    return "Commands: " + ", ".join(cmds) + ". Freeform text and voice notes go to the life helper agent."


def cmd_ping(ctx, args):
    """Test that the bot is alive."""
    return "pong"


# ============================================================
# COMMAND REGISTRY - Add your commands here!
# ============================================================

COMMANDS = {
    "help": cmd_help,
    "ping": cmd_ping,
    "wh": cmd_wh,
    "timers": cmd_timers,
    "cancel": cmd_cancel,
    "sun": cmd_sun,
    "weather": cmd_weather,
    "ws": cmd_ws,
}

# Timer name -> handler function
# When a timer with this name fires, the handler is called
TIMER_HANDLERS = {
    "high_demand_reminder": timer_high_demand_reminder,
    "sun_crossing": timer_sun_crossing,
    "sun_daily_schedule": timer_sun_daily_schedule,
    "weather_daily": timer_weather_daily,
    "ws_health": timer_ws_health,
    "temp_alert": timer_temp_alert,
}

# ============================================================
# DEFAULT HANDLER (for messages that don't match a command)
# ============================================================


def handle_unknown(ctx, message):
    # Kept for callers/tests; daemon freeform path now hands off to the agent
    # instead of invoking this. Exact command registry is unchanged.
    return None


# ============================================================
# STARTUP INITIALIZATION
# ============================================================


def init(ctx):
    """Called once when daemon starts. Set up recurring schedules here."""
    schedule_sun_timers()
    schedule_weather_timer()
    schedule_ws_health()
    schedule_temp_alert()
