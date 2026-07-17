import timers
import rheem_api
import sun

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
    """Schedule timers for today's sun angle crossings."""
    # Cancel any existing sun timers
    timers.cancel(name="sun_crossing")

    # Get crossing times for today
    crossings = sun.get_crossing_times()
    scheduled = 0
    for t, direction in crossings:
        delay = sun.seconds_until(t)
        if delay > 0:
            timers.add("sun_crossing", delay_sec=delay, data={"direction": direction})
            scheduled += 1

    # Schedule daily recompute at 4am if not already scheduled
    existing = timers.list_all()
    has_schedule = any(t["name"] == "sun_daily_schedule" for t in existing)
    if not has_schedule:
        # Compute seconds until next 4am
        from datetime import datetime, timedelta

        now = datetime.now(sun.tz)
        next_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if next_4am <= now:
            next_4am += timedelta(days=1)
        delay = (next_4am - now).total_seconds()
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
}

# Timer name -> handler function
# When a timer with this name fires, the handler is called
TIMER_HANDLERS = {
    "high_demand_reminder": timer_high_demand_reminder,
    "sun_crossing": timer_sun_crossing,
    "sun_daily_schedule": timer_sun_daily_schedule,
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
