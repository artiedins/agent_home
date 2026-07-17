import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from astral import LocationInfo
from astral.sun import elevation

load_dotenv()

SUN_ANGLE = 55

LAT = float(os.getenv("LAT", 0))
LON = float(os.getenv("LON", 0))
TZ = "America/Los_Angeles"

loc = LocationInfo(latitude=LAT, longitude=LON, timezone=TZ)
tz = ZoneInfo(TZ)


def get_crossing_times(date=None, angle=None):
    """Find times when sun crosses the threshold angle (rising and setting).
    Returns list of (datetime, direction) tuples where direction is 'rising' or 'setting'.
    Returns empty list if sun never reaches the angle that day.
    """
    if date is None:
        date = datetime.now(tz).date()
    if angle is None:
        angle = SUN_ANGLE

    # Sample every 5 minutes through the day
    crossings = []
    prev_elev = None
    dt = datetime(date.year, date.month, date.day, 0, 0, tzinfo=tz)

    for minute in range(0, 24 * 60, 5):
        t = dt + timedelta(minutes=minute)
        elev = elevation(loc.observer, t)

        if prev_elev is not None:
            # Check for crossing
            if prev_elev < angle <= elev:
                crossings.append((t, "rising"))
            elif prev_elev > angle >= elev:
                crossings.append((t, "setting"))
        prev_elev = elev

    return crossings


def next_crossing(angle=None):
    """Get next crossing time from now. Returns (datetime, direction) or None."""
    if angle is None:
        angle = SUN_ANGLE
    now = datetime.now(tz)

    # Check today
    for t, direction in get_crossing_times(now.date(), angle):
        if t > now:
            return (t, direction)

    # Check tomorrow
    tomorrow = now.date() + timedelta(days=1)
    crossings = get_crossing_times(tomorrow, angle)
    if crossings:
        return crossings[0]

    return None


def seconds_until(dt):
    """Seconds from now until datetime."""
    now = datetime.now(tz)
    return (dt - now).total_seconds()
