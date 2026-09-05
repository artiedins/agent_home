#!/usr/bin/env python3

import csv
import json
import os
import statistics
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

LAT = float(os.getenv("LAT", 0))
LON = float(os.getenv("LON", 0))
TZ = ZoneInfo("America/Los_Angeles")

ROOT = os.path.dirname(os.path.abspath(__file__))

# Source 1: Google WeatherNext 2 ensemble via Open-Meteo's Ensemble API. Free
# for non-commercial use, no API key, one request returns daily highs/lows/
# precip plus ~63 ensemble members per field. See task report 2026-09-05.
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Source 2: NOAA/NWS point forecast (api.weather.gov). Free, no key. The NWS
# Los Angeles office (LOX) hand-edits these grids and is the local reference
# for marine layer / beach forecasts, so it is both an independent second
# opinion and the regional gold standard. Fields used: 12h periods for
# today/tonight/tomorrow temps, hourly periods for PoP and sky condition.
NWS_UA = "home-daemon-weather/1.0 (github.com/artie/home-automation)"

# A day counts as "rainy" when the median ensemble member puts down at least
# this much rain. 2 mm is about what 5 minutes of an average residential
# sprinkler zone applies. This is the rain-icon threshold for the Google source.
RAIN_MM = 2.0

# NWS hourly forecast has no precip amount, so the rain-icon analog is the
# max probability-of-precipitation over the day. >= 50% is forecasters
# calling showers "likely", which in practice means measurable rain here.
RAIN_POP_PCT = 50

# Cloud icon = solid marine layer that never burns off. Google source: mean
# ensemble cloud cover across the 11:00-19:00 window must reach this. NWS
# source: at least CLOUD_MIN_HOURS of the 9 window hours must be broken or
# overcast (bkn/ovc in the hourly icon code). A cloudy morning that clears
# by 11am fails both tests, which is the point.
CLOUD_MEAN_PCT = 80.0
CLOUD_WINDOW_HOURS = range(11, 20)  # 11:00 through 19:00 inclusive
CLOUD_MIN_HOURS = 6

# Emoji-presentation variants (VS16) so the phone renders them in color.
ICON_SUN = "☀️"
ICON_CLOUD = "☁️"
ICON_RAIN = "🌧️"

# Today plus four more days of icons.
FORECAST_DAYS = 6
ICON_DAYS = 5

CSV_PATH = os.path.join(ROOT, "weather_predictions.csv")
CSV_FIELDS = [
    "sent_at",
    "source",
    "date",
    "high_f",
    "low_f",
    "tomorrow_high_f",
    "icons",
    "day0_precip_mm",
    "day0_pop_pct",
    "day0_cloud_pct",
    "day0_cloud_hours",
]


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": NWS_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _median_across_members(series, n_points):
    """Median across ensemble member columns, one value per time point.

    The response carries a plain mean field plus one column per member.
    Members are discovered dynamically (63 of 64 come back; see the task
    report in the 2026-09-04 run).
    """
    # Member columns look like "temperature_2m_max_member01"; a plain field
    # (ensemble mean) has no "_member" substring.
    keys = [k for k in series if "_member" in k]
    if keys:
        return [statistics.median(series[k][i] for k in keys) for i in range(n_points)]
    return list(series[next(iter(series))])


# ---------------------------------------------------------------- google --


def _google_fetch():
    """One request for daily temps/precip plus hourly cloud cover, ensemble
    median per point."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "models": "google_weathernext2_ensemble",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "hourly": "cloud_cover",
        "temperature_unit": "fahrenheit",
        "forecast_days": FORECAST_DAYS,
        "timezone": "auto",
    }
    url = ENSEMBLE_URL + "?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    daily, hourly = data["daily"], data["hourly"]
    n_days = len(daily["time"])
    n_hours = len(hourly["time"])

    highs = _median_across_members({k: v for k, v in daily.items() if "temperature_2m_max" in k}, n_days)
    lows = _median_across_members({k: v for k, v in daily.items() if "temperature_2m_min" in k}, n_days)
    rain = _median_across_members({k: v for k, v in daily.items() if "precipitation_sum" in k}, n_days)
    cloud = _median_across_members({k: v for k, v in hourly.items() if "cloud_cover" in k}, n_hours)

    # Mean cloud cover over 11:00-19:00 local, per day.
    cloud_by_day = {}
    for i, iso in enumerate(hourly["time"]):
        hour = int(iso[11:13])
        if hour in CLOUD_WINDOW_HOURS:
            cloud_by_day.setdefault(iso[:10], []).append(cloud[i])

    days = []
    for i, iso in enumerate(daily["time"]):
        window = cloud_by_day.get(iso, [])
        days.append(
            {
                "date": date.fromisoformat(iso),
                "high_f": round(highs[i]),
                "low_f": round(lows[i]),
                "precip_mm": rain[i] or 0.0,
                "cloud_pct": round(statistics.mean(window)) if window else None,
            }
        )
    return days


def icon_google(day):
    if day["precip_mm"] >= RAIN_MM:
        return ICON_RAIN
    if day["cloud_pct"] is not None and day["cloud_pct"] >= CLOUD_MEAN_PCT:
        return ICON_CLOUD
    return ICON_SUN


def google_message(log=False):
    days = _google_fetch()
    today, tomorrow = days[0], days[1]
    # "Tonight low" is the upcoming overnight min, which lands in tomorrow's
    # calendar day (4-6am). Open-Meteo's day-0 min is this morning's already
    # observed low, so day 1 is the right number for the template.
    icons = " ".join(d["date"].strftime("%A")[0] + icon_google(d) for d in days[:ICON_DAYS])
    msg = render_message("google", today["high_f"], days[1]["low_f"], tomorrow["high_f"], icons)
    if log:
        log_prediction(
            "google",
            days[0]["date"],
            today["high_f"],
            days[1]["low_f"],
            tomorrow["high_f"],
            icons,
            precip=days[0]["precip_mm"],
            cloud_pct=days[0]["cloud_pct"],
        )
    return msg


# ------------------------------------------------------------------- nws --


def _nws_urls():
    points = "https://api.weather.gov/points/%s,%s" % (LAT, LON)
    props = _get_json(points)["properties"]
    return props["forecast"], props["forecastHourly"]


def _nws_sky(icon_url):
    """Sky code from an NWS icon URL, e.g. .../day/rain_showers,20?size=small
    -> 'rain_showers'. Sky codes: few < sct < bkn < ovc."""
    if not icon_url:
        return ""
    tail = icon_url.split("/day/")[-1].split("/night/")[-1]
    return tail.split("?")[0].split(",")[0]


def nws_message(log=False):
    daily_url, hourly_url = _nws_urls()

    periods = _get_json(daily_url)["properties"]["periods"]
    today_high = tonight_low = tomorrow_high = None
    seen_daytime = 0
    for p in periods:
        if p["isDaytime"]:
            if seen_daytime == 0:
                today_high = p["temperature"]
            elif seen_daytime == 1:
                tomorrow_high = p["temperature"]
                break
            seen_daytime += 1
        elif seen_daytime == 1 and tonight_low is None:
            tonight_low = p["temperature"]

    hours = _get_json(hourly_url)["properties"]["periods"]
    by_day = {}
    for h in hours:
        day = h["startTime"][:10]
        pop = (h.get("probabilityOfPrecipitation") or {}).get("value") or 0
        entry = by_day.setdefault(day, {"pop_max": 0, "cloud_hours": 0, "window_hours": 0})
        entry["pop_max"] = max(entry["pop_max"], pop)
        hour = int(h["startTime"][11:13])
        if hour in CLOUD_WINDOW_HOURS:
            entry["window_hours"] += 1
            if _nws_sky(h.get("icon")) in ("bkn", "ovc"):
                entry["cloud_hours"] += 1

    today = datetime.now(TZ).date()
    glyphs = []
    for offset in range(ICON_DAYS):
        d = today + timedelta(days=offset)
        info = by_day.get(d.isoformat(), {"pop_max": 0, "cloud_hours": 0, "window_hours": 0})
        if info["pop_max"] >= RAIN_POP_PCT:
            icon = ICON_RAIN
        elif info["cloud_hours"] >= CLOUD_MIN_HOURS:
            icon = ICON_CLOUD
        else:
            icon = ICON_SUN
        glyphs.append(d.strftime("%A")[0] + icon)
    icons = " ".join(glyphs)

    msg = render_message("noaa", today_high, tonight_low, tomorrow_high, icons)
    if log:
        info = by_day.get(today.isoformat(), {})
        log_prediction(
            "noaa",
            today,
            today_high,
            tonight_low,
            tomorrow_high,
            icons,
            pop=info.get("pop_max"),
            cloud_hours=info.get("cloud_hours"),
        )
    return msg


# --------------------------------------------------------------- shared --


def render_message(source, high_f, low_f, tomorrow_high_f, icons):
    now = datetime.now(TZ)
    return "\n".join(
        [
            "weather %s %s" % (source, now.strftime("%d/%m")),
            "%s, %s, %s" % (high_f, low_f, tomorrow_high_f),
            icons,
        ]
    )


def log_prediction(source, day_date, high_f, low_f, tomorrow_high_f, icons, precip=None, pop=None, cloud_pct=None, cloud_hours=None):
    # One CSV line per source per day. The 8am timer logs, but the manual
    # 'weather' command and any retry can also reach this; the date check
    # keeps the file to exactly one row per source per calendar day.
    if os.path.isfile(CSV_PATH):
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("source") == source and row.get("date") == day_date.isoformat():
                    return
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not os.path.isfile(CSV_PATH) or os.path.getsize(CSV_PATH) == 0:
            writer.writeheader()
        writer.writerow(
            {
                "sent_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "date": day_date.isoformat(),
                "high_f": high_f,
                "low_f": low_f,
                "tomorrow_high_f": tomorrow_high_f,
                "icons": icons,
                "day0_precip_mm": precip,
                "day0_pop_pct": pop,
                "day0_cloud_pct": cloud_pct,
                "day0_cloud_hours": cloud_hours,
            }
        )


def daily_messages(log=False):
    """Both forecast messages for the morning push. With log=True one CSV line
    is appended per message (two per day). If one source fails, the other
    still goes out and the failure is stated in place so it gets noticed."""
    out = []
    try:
        out.append(google_message(log=log))
    except Exception as e:
        out.append("weather google failed: %s" % e)
    try:
        out.append(nws_message(log=log))
    except Exception as e:
        out.append("weather noaa failed: %s" % e)
    return out


def daily_message():
    """Plain-text digest for the 'weather' command: both 3-line messages."""
    return "\n\n".join(daily_messages())


if __name__ == "__main__":
    for m in daily_messages():
        print(m)
        print()
