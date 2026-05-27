"""Weather data for DH races via Open-Meteo (no API key, no rate-limit auth).

Three uses:
  1. Forecast for an upcoming race — will it rain at Loudenvielle this weekend?
  2. Historical lookup — did it rain at the same venue last year?
  3. Cross-reference rider results against wet/dry conditions to find
     rain specialists.

Endpoints:
  - geocode:    https://geocoding-api.open-meteo.com/v1/search
  - forecast:   https://api.open-meteo.com/v1/forecast
  - historical: https://archive-api.open-meteo.com/v1/archive

Both forecast and archive accept the same `daily=` parameter list, so the
same parser handles both.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from . import cache, scraper

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# A race is "wet" if either:
#   - Total precipitation across the race day + previous day ≥ 5 mm
#   - At least 4 hours of precipitation on race day itself
# These thresholds are tuned to catch slick-track conditions that meaningfully
# change DH racing — light morning drizzle that dries out by finals isn't enough.
WET_PRECIP_MM = 5.0
WET_HOURS = 4


@dataclass
class GeocodeResult:
    name: str
    latitude: float
    longitude: float
    country: str | None
    admin1: str | None  # state/region
    timezone: str | None


@dataclass
class WeatherDay:
    date: str  # YYYY-MM-DD
    precipitation_mm: float
    precipitation_hours: float
    temperature_max_c: float | None
    temperature_min_c: float | None
    weather_code: int | None  # WMO weather code
    is_forecast: bool


@dataclass
class RaceWeather:
    event_id: str | None
    event_name: str
    venue: str | None
    date_iso: str
    latitude: float | None
    longitude: float | None
    days: list[WeatherDay]
    classification: str  # "wet" | "dry" | "unknown"
    precipitation_total_mm: float
    notes: str | None = None


# ---------- geocode ----------


def _strip_venue(name: str) -> str:
    """Reduce a rootsandrain location like 'Loudenvielle , France' or
    'Whiteface, NY , USA' to just the city/town for geocoding."""
    if not name:
        return ""
    # Split on commas; first token is usually the most specific.
    parts = [p.strip() for p in name.split(",") if p.strip()]
    return parts[0] if parts else name.strip()


def geocode(venue: str) -> GeocodeResult | None:
    """Resolve a venue name to coordinates. Cached forever."""
    key = venue.lower().strip()
    cached = cache.get_cached_results("openmeteo", "geocode", key)
    if cached is not None:
        return GeocodeResult(**cached) if cached else None

    query = _strip_venue(venue) or venue
    resp = httpx.get(
        GEOCODE_URL,
        params={"name": query, "count": 5, "language": "en", "format": "json"},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("results") or []
    if not results:
        cache.store_results("openmeteo", "geocode", key, {})
        return None

    # Prefer a result whose country matches a hint in the venue string.
    hint_country = None
    parts = [p.strip() for p in venue.split(",") if p.strip()]
    if len(parts) >= 2:
        hint_country = parts[-1].lower()
    chosen = results[0]
    if hint_country:
        for r in results:
            if (r.get("country") or "").lower().startswith(hint_country[:3]):
                chosen = r
                break

    out = GeocodeResult(
        name=chosen.get("name") or query,
        latitude=chosen["latitude"],
        longitude=chosen["longitude"],
        country=chosen.get("country"),
        admin1=chosen.get("admin1"),
        timezone=chosen.get("timezone"),
    )
    cache.store_results("openmeteo", "geocode", key, asdict(out))
    return out


# ---------- weather lookup ----------


def _parse_daily(payload: dict[str, Any], is_forecast: bool) -> list[WeatherDay]:
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    days: list[WeatherDay] = []
    for i, date in enumerate(times):
        days.append(
            WeatherDay(
                date=date,
                precipitation_mm=float((daily.get("precipitation_sum") or [None])[i] or 0),
                precipitation_hours=float(
                    (daily.get("precipitation_hours") or [None])[i] or 0
                ),
                temperature_max_c=(daily.get("temperature_2m_max") or [None])[i],
                temperature_min_c=(daily.get("temperature_2m_min") or [None])[i],
                weather_code=(daily.get("weather_code") or [None])[i],
                is_forecast=is_forecast,
            )
        )
    return days


def get_weather(lat: float, lon: float, start: str, end: str) -> list[WeatherDay]:
    """Daily weather between start and end (inclusive). Uses archive for past
    dates, forecast for today and future. Both endpoints accept the same
    parameter list."""
    today = dt.date.today()
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    # If the whole range is in the past, use the archive. Otherwise use
    # forecast (which covers up to 16 days out and handles past-day fills).
    is_forecast = end_d >= today
    url = FORECAST_URL if is_forecast else ARCHIVE_URL
    cache_key = f"{lat:.3f}|{lon:.3f}|{start}|{end}|{int(is_forecast)}"
    cached = cache.get_cached_results("openmeteo", "weather", cache_key)
    if cached is not None:
        return [WeatherDay(**d) for d in cached]

    resp = httpx.get(
        url,
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": (
                "precipitation_sum,precipitation_hours,"
                "temperature_2m_max,temperature_2m_min,weather_code"
            ),
            "start_date": start,
            "end_date": end,
            "timezone": "auto",
        },
        timeout=20,
    )
    resp.raise_for_status()
    days = _parse_daily(resp.json(), is_forecast=is_forecast)
    # Cache historical data forever; forecast cache is short-lived via the
    # default 24h TTL.
    data_year = start_d.year if not is_forecast else None
    cache.store_results(
        "openmeteo", "weather", cache_key,
        [asdict(d) for d in days],
        data_year=data_year,
    )
    return days


def classify_days(days: list[WeatherDay]) -> tuple[str, float]:
    """Classify a multi-day race-weekend window as wet / dry / unknown.

    Returns (label, max_2day_precipitation_mm). For a 3-day window (Fri/Sat/Sun)
    we slide a 2-day window across it and take the wettest pair — captures the
    case where finals day is rainy even if practice was dry, or where rain the
    day before finals leaves a slick track.
    """
    if not days:
        return "unknown", 0.0
    # Slide a 2-day window across the days, find the wettest pair.
    best_total = 0.0
    best_hours = 0.0
    for i in range(len(days)):
        window = days[max(i - 1, 0):i + 1]
        total = sum(d.precipitation_mm for d in window)
        # On the second day of the pair, also track its standalone precip hours
        # — heavy single-day rain on finals matters as much as cumulative.
        hours = days[i].precipitation_hours
        if total > best_total:
            best_total = total
        if hours > best_hours:
            best_hours = hours
    if best_total >= WET_PRECIP_MM or best_hours >= WET_HOURS:
        return "wet", best_total
    return "dry", best_total


# ---------- race-level wrapper ----------


def _date_iso_from_event(event: scraper.EventInfo) -> str | None:
    """Best-effort race date. Use date_iso if present; otherwise parse `date`."""
    if event.date_iso:
        return event.date_iso
    if not event.date:
        return None
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.match(r"(\d+)(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})", event.date)
    if not m:
        return None
    day = int(m.group(1))
    mon = months.get(m.group(2)[:3].lower())
    year = int(m.group(3))
    if not mon:
        return None
    return f"{year:04d}-{mon:02d}-{day:02d}"


def race_weather(event: scraper.EventInfo) -> RaceWeather:
    """Return weather for a race day + previous day (slick-track window),
    with a wet/dry classification."""
    date_iso = _date_iso_from_event(event)
    if not date_iso:
        return RaceWeather(
            event_id=event.event_id,
            event_name=event.name,
            venue=event.location,
            date_iso="",
            latitude=None, longitude=None, days=[],
            classification="unknown", precipitation_total_mm=0.0,
            notes="no race date on event",
        )
    geo = geocode(event.location or "") if event.location else None
    if geo is None:
        return RaceWeather(
            event_id=event.event_id, event_name=event.name, venue=event.location,
            date_iso=date_iso, latitude=None, longitude=None, days=[],
            classification="unknown", precipitation_total_mm=0.0,
            notes=f"could not geocode {event.location!r}",
        )
    # DH events span Fri (qual) → Sat (semi) → Sun (final). rootsandrain
    # records a single calendar date that's usually the Saturday — but the
    # finals are what scores fantasy points, so we widen to ±1 day around the
    # listed date and classify on whichever day looks wettest.
    race_date = dt.date.fromisoformat(date_iso)
    start = (race_date - dt.timedelta(days=1)).isoformat()
    end = (race_date + dt.timedelta(days=1)).isoformat()
    days = get_weather(geo.latitude, geo.longitude, start, end)
    label, total = classify_days(days)
    return RaceWeather(
        event_id=event.event_id,
        event_name=event.name,
        venue=event.location,
        date_iso=date_iso,
        latitude=geo.latitude,
        longitude=geo.longitude,
        days=days,
        classification=label,
        precipitation_total_mm=total,
    )


# ---------- rain specialists ----------


@dataclass
class RainSpecialist:
    rider_id: str
    rider_slug: str | None
    name: str
    nationality: str | None
    wet_races: int
    dry_races: int
    wet_median_position: float | None
    dry_median_position: float | None
    delta: float | None  # dry_median - wet_median; positive = better in wet


def find_rain_specialists(
    year: int,
    category: str = "Male Elite",
    series: str = "uci",
    top: int = 10,
    min_wet_races: int = 2,
    min_dry_races: int = 2,
) -> list[RainSpecialist]:
    """Find riders whose median race position is meaningfully better in wet
    races than dry races, across the season's WC events.

    Steps:
      1. Get all events in the series for `year`.
      2. Classify each as wet/dry via historical Open-Meteo data.
      3. For each rider with min_wet_races wet starts AND min_dry_races dry
         starts, compute median position in each bucket.
      4. Return riders sorted by `dry_median - wet_median` desc (biggest
         positive delta = best at exploiting wet conditions).
    """
    if series in ("uci", "uci_full"):
        events = scraper.list_uci_dh_events(year)
        events = [e for e in events if "World Cup DH" in e.name]
    else:
        entry = next(
            (s for s in scraper.REGIONAL_DH_SERIES if s["key"] == series), None
        )
        if entry is None:
            return []
        events = scraper.list_series_dh_events(
            entry["query"], year, pure_dh=entry.get("pure_dh", False)
        )

    rider_buckets: dict[str, dict[str, Any]] = {}
    for ev in events:
        if not ev.event_id or not ev.slug:
            continue
        try:
            rw = race_weather(ev)
        except Exception:
            continue
        if rw.classification == "unknown":
            continue
        try:
            results = scraper.get_event_results(ev.event_id, ev.slug)
        except Exception:
            continue
        for r in results:
            if not r.rider_id or r.position is None:
                continue
            if (r.category or "").strip() != category:
                continue
            d = rider_buckets.setdefault(
                r.rider_id,
                {
                    "rider_id": r.rider_id,
                    "slug": r.rider_slug,
                    "name": r.rider_name,
                    "nationality": r.nationality,
                    "wet": [], "dry": [],
                },
            )
            d["name"] = r.rider_name or d["name"]
            d[rw.classification].append(r.position)

    out: list[RainSpecialist] = []
    for d in rider_buckets.values():
        if len(d["wet"]) < min_wet_races or len(d["dry"]) < min_dry_races:
            continue
        wm = statistics.median(d["wet"])
        dm = statistics.median(d["dry"])
        out.append(
            RainSpecialist(
                rider_id=d["rider_id"],
                rider_slug=d["slug"],
                name=d["name"],
                nationality=d["nationality"],
                wet_races=len(d["wet"]),
                dry_races=len(d["dry"]),
                wet_median_position=wm,
                dry_median_position=dm,
                delta=dm - wm,
            )
        )
    out.sort(key=lambda r: r.delta or 0, reverse=True)
    return out[:top]
