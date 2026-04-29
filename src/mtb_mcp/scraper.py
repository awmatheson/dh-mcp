"""rootsandrain.com scraper.

Uses curl_cffi with Chrome TLS-fingerprint impersonation — rootsandrain sits
behind Cloudflare and rejects vanilla Python clients (JA3/JA4 fingerprint), even
with full header spoofing. The session cookie jar persists across requests and
the first call warms up against the homepage so subsequent fetches look like a
returning browser.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from . import cache

BASE_URL = "https://www.rootsandrain.com"
_IMPERSONATE = "chrome131"

_client_lock = threading.Lock()
_client: curl_requests.Session | None = None
_warmed_up = False


def _client_get() -> curl_requests.Session:
    global _client
    with _client_lock:
        if _client is None:
            _client = curl_requests.Session(
                impersonate=_IMPERSONATE,
                timeout=30,
            )
            # curl_cffi sets a realistic Accept-Language by default; just pin Referer.
            _client.headers.update({"Referer": f"{BASE_URL}/"})
        return _client


def _warmup() -> None:
    global _warmed_up
    with _client_lock:
        if _warmed_up:
            return
        _warmed_up = True
    try:
        _client_get().get(BASE_URL + "/")
    except Exception:
        with _client_lock:
            globals()["_warmed_up"] = False


def _fetch(url: str, data_year: int | None = None) -> str:
    cached = cache.get_cached_page(url)
    if cached is not None:
        return cached
    _warmup()
    resp = _client_get().get(url)
    resp.raise_for_status()
    html = resp.text
    cache.store_page(url, html, data_year=data_year)
    return html


# ---------- dataclasses ----------


@dataclass
class RiderResult:
    event_id: str | None
    event_slug: str | None
    event_name: str
    date: str | None
    year: int | None
    discipline: str | None
    category: str | None
    position: int | None
    raw_position: str | None
    time: str | None
    note: str | None = None


@dataclass
class EventResult:
    position: int | None
    raw_position: str | None
    rider_id: str | None
    rider_slug: str | None
    rider_name: str
    nationality: str | None
    team: str | None
    time: str | None
    gap: str | None
    category: str | None


@dataclass
class RiderInfo:
    rider_id: str
    slug: str
    name: str
    nationality: str | None = None
    url: str | None = None


@dataclass
class EventInfo:
    event_id: str | None
    slug: str | None
    name: str
    date: str | None
    year: int | None
    date_iso: str | None = None
    location: str | None = None
    url: str | None = None
    disciplines: list[str] = field(default_factory=list)


# ---------- helpers ----------

_RIDER_HREF_RE = re.compile(r"/rider(\d+)/([^/?#]+)")
_EVENT_HREF_RE = re.compile(r"/event(\d+)/([^/?#]+)")
_INT_RE = re.compile(r"\d+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _txt(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _parse_int(s: str | None) -> int | None:
    if not s:
        return None
    m = _INT_RE.search(s)
    return int(m.group(0)) if m else None


def _parse_rider_href(href: str) -> tuple[str | None, str | None]:
    if not href:
        return None, None
    m = _RIDER_HREF_RE.search(href)
    return (m.group(1), m.group(2)) if m else (None, None)


def _parse_event_href(href: str) -> tuple[str | None, str | None]:
    if not href:
        return None, None
    m = _EVENT_HREF_RE.search(href)
    return (m.group(1), m.group(2)) if m else (None, None)


def _table_headers(table) -> list[str]:
    """Return header names as a positional list (preserves duplicates)."""
    head = table.find("thead")
    headers: list[str] = []
    if head:
        ths = head.find_all("th")
        headers = [_txt(th).lower() for th in ths]
    if not headers:
        first = table.find("tr")
        if first:
            headers = [_txt(c).lower() for c in first.find_all(["th", "td"])]
    return headers


def _find_col(headers: list[str], *names: str, last: bool = False) -> int | None:
    """Find a column index by substring match. `last=True` returns rightmost match."""
    matches: list[int] = []
    for i, h in enumerate(headers):
        for n in names:
            if n in h:
                matches.append(i)
                break
    if not matches:
        return None
    return matches[-1] if last else matches[0]


def _cell_at(cells, idx: int | None):
    if idx is None or idx >= len(cells):
        return None
    return cells[idx]


_TIME_TOKEN_RE = re.compile(r"\d+:\d+(?:\.\d+)?|\d+\.\d+s?")
# Position cell on rider results pages: '9 / 24' for finishers, or
# 'Q 1 SF F DNS / 84' for DNS/DNF — the final outcome is the token right before ' / N'.
_RESULT_BEFORE_SLASH_RE = re.compile(r"([A-Za-z]+|\d+)\s*/\s*\d+")


def _clean_time(s: str | None) -> str | None:
    """Strip rank suffixes from time cells like '3:30.096 1' -> '3:30.096'."""
    if not s:
        return None
    m = _TIME_TOKEN_RE.search(s)
    return m.group(0) if m else s.strip() or None


def _parse_position_cell(s: str | None) -> int | None:
    """Position from a rider-results cell. Returns None for DNS/DNF/DSQ etc."""
    if not s:
        return None
    m = _RESULT_BEFORE_SLASH_RE.search(s)
    if m:
        token = m.group(1)
        return int(token) if token.isdigit() else None
    # No "N / total" pattern — fall back to first integer in the string.
    return _parse_int(s)


def _looks_like_dh(text: str) -> bool:
    t = text.lower()
    return (
        "downhill" in t
        or re.search(r"\bdh\b", t) is not None
        or re.search(r"\bdhi\b", t) is not None
    )


# ---------- public API ----------


def search_riders(name: str) -> list[RiderInfo]:
    """Search rootsandrain for riders matching `name`. Returns possibly empty list.

    Uses the site's autocomplete JSON endpoint /ajax/riders?s=... which returns
    [meta, meta, {n, s, u, l, ...}, ...] — `n` is the rider id, `s` the display
    name, `u` the slug, `l` the lowercase country code.
    """
    import json as _json
    from urllib.parse import quote

    name = name.strip()
    if not name:
        return []
    cached = cache.get_cached_results("rootsandrain", "rider_search", name.lower())
    if cached is not None:
        return [RiderInfo(**r) for r in cached]

    _warmup()
    url = f"{BASE_URL}/ajax/riders?s={quote(name)}"
    resp = _client_get().get(url)
    resp.raise_for_status()
    payload = _json.loads(resp.text)

    out: list[RiderInfo] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        rid = item.get("n")
        slug = item.get("u") or ""
        display = item.get("s") or item.get("h") or ""
        if rid is None or not display:
            continue
        # Drop the "create new rider" suggestion (id < 0).
        if isinstance(rid, int) and rid < 0:
            continue
        rid_s = str(rid)
        if rid_s in seen:
            continue
        seen.add(rid_s)
        nat = item.get("l")
        out.append(
            RiderInfo(
                rider_id=rid_s,
                slug=slug,
                name=display,
                nationality=nat.upper() if nat else None,
                url=f"{BASE_URL}/rider{rid_s}/{slug}/results/" if slug else None,
            )
        )
    cache.store_results(
        "rootsandrain",
        "rider_search",
        name.lower(),
        [asdict(r) for r in out],
    )
    return out


def get_rider_results(
    rider_id: str,
    rider_slug: str,
    year: int | None = None,
    category_filter: str | None = None,
) -> list[RiderResult]:
    """Race history for a rider. `year` and `category_filter` are post-fetch filters."""
    url = f"{BASE_URL}/rider{rider_id}/{rider_slug}/results/"
    cached = cache.get_cached_results("rootsandrain", "rider_results", str(rider_id))
    if cached is None:
        html = _fetch(url)
        soup = BeautifulSoup(html, "lxml")
        results = _parse_rider_results_page(soup)
        cache.store_results(
            "rootsandrain",
            "rider_results",
            str(rider_id),
            [asdict(r) for r in results],
        )
    else:
        results = [RiderResult(**r) for r in cached]

    out = results
    if year is not None:
        out = [r for r in out if r.year == year]
    if category_filter:
        cf = category_filter.lower()
        out = [r for r in out if (r.category or "").lower().find(cf) >= 0]
    return out


def _parse_rider_results_page(soup: BeautifulSoup) -> list[RiderResult]:
    """Parse the results table(s) on a rider's results page."""
    results: list[RiderResult] = []
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        if not headers:
            continue
        if not any("event" in h or "race" in h for h in headers):
            continue
        c_event = _find_col(headers, "event", "race")
        c_date = _find_col(headers, "date")
        c_venue = _find_col(headers, "venue", "location")
        c_cat = _find_col(headers, "category", "class")
        c_pos = _find_col(headers, "position", "pos", "place", "rank")
        c_time = _find_col(headers, "result", "time", "ftc")
        c_disc = _find_col(headers, "discipline", "type")

        body = table.find("tbody") or table
        for row in body.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells or (row.find("th") and not row.find("td")):
                continue
            event_cell = _cell_at(cells, c_event)
            event_link = event_cell.find("a", href=True) if event_cell else None
            event_name = _txt(event_link) if event_link else _txt(event_cell)
            if not event_name:
                continue
            event_id, event_slug = (
                _parse_event_href(event_link["href"]) if event_link else (None, None)
            )
            date_cell = _cell_at(cells, c_date)
            date = _txt(date_cell) if date_cell else None
            year_match = _YEAR_RE.search(date or "") or _YEAR_RE.search(event_name)
            year = int(year_match.group(0)) if year_match else None
            discipline = _txt(_cell_at(cells, c_disc)) or None
            category = _txt(_cell_at(cells, c_cat)) or None
            pos_cell = _cell_at(cells, c_pos)
            raw_pos = _txt(pos_cell) if pos_cell else None
            time_cell = _cell_at(cells, c_time)
            time = _clean_time(_txt(time_cell)) if time_cell else None
            results.append(
                RiderResult(
                    event_id=event_id,
                    event_slug=event_slug,
                    event_name=event_name,
                    date=date,
                    year=year,
                    discipline=discipline,
                    category=category,
                    position=_parse_position_cell(raw_pos),
                    raw_position=raw_pos,
                    time=time,
                    note=_txt(_cell_at(cells, c_venue)) or None,
                )
            )
    return results


def get_event_results(
    event_id: str,
    event_slug: str,
    category_filter: str | None = None,
) -> list[EventResult]:
    """Full finisher list for an event. Cached unfiltered; filter applied after retrieval."""
    cached = cache.get_cached_results("rootsandrain", "event_results", str(event_id))
    if cached is None:
        url = f"{BASE_URL}/event{event_id}/{event_slug}/results/"
        html = _fetch(url)
        soup = BeautifulSoup(html, "lxml")
        results = _parse_event_results_page(soup)
        cache.store_results(
            "rootsandrain",
            "event_results",
            str(event_id),
            [asdict(r) for r in results],
        )
    else:
        results = [EventResult(**r) for r in cached]

    if category_filter:
        cf = category_filter.lower()
        results = [r for r in results if (r.category or "").lower().find(cf) >= 0]
    return results


def _parse_event_results_page(soup: BeautifulSoup) -> list[EventResult]:
    results: list[EventResult] = []
    # Each table on the page is a category (Men Elite, Women Elite, ...).
    # The nearest preceding heading text is the category — strip the trailing
    # " Spread" the page UI appends for an expand control.
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        if not headers:
            continue
        if not any("name" in h or "rider" in h for h in headers):
            continue
        cat = None
        prev = table.find_previous(["h1", "h2", "h3", "h4", "caption"])
        if prev:
            cat = _txt(prev)
            if cat and cat.lower().endswith(" spread"):
                cat = cat[: -len(" spread")].strip()

        c_pos = _find_col(headers, "pos", "rank", "place")
        c_name = _find_col(headers, "name", "rider", "athlete")
        c_nat = _find_col(headers, "nat", "country", "nation")
        c_team = _find_col(headers, "sponsor", "team")
        # Run-time columns: Q1, Q2, Final all share the trailing position. Pick
        # `Final` as the canonical race time, fallback to the rightmost run.
        c_time = _find_col(headers, "final")
        if c_time is None:
            c_time = _find_col(headers, "result", "time", last=True)
        c_gap = _find_col(headers, "gap", "diff", "behind")
        c_cat = _find_col(headers, "category", "class")

        body = table.find("tbody") or table
        for row in body.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells or (row.find("th") and not row.find("td")):
                continue
            name_cell = _cell_at(cells, c_name)
            # The name cell holds a country-flag <a> first and the rider <a class="rn"> second.
            # Pick the rider link by class, falling back to any /riderN/... href.
            rider_link = None
            if name_cell is not None:
                rider_link = name_cell.find("a", class_="rn")
                if rider_link is None:
                    for a in name_cell.find_all("a", href=True):
                        if _RIDER_HREF_RE.search(a["href"]):
                            rider_link = a
                            break
            rider_id, rider_slug = (
                _parse_rider_href(rider_link["href"]) if rider_link else (None, None)
            )
            rider_name = _txt(rider_link) if rider_link else _txt(name_cell)
            if not rider_name:
                continue
            raw_pos = _txt(_cell_at(cells, c_pos)) or None
            nationality = _txt(_cell_at(cells, c_nat)) or None
            team = _txt(_cell_at(cells, c_team)) or None
            time = _clean_time(_txt(_cell_at(cells, c_time))) if c_time is not None else None
            gap = _txt(_cell_at(cells, c_gap)) or None
            row_cat = _txt(_cell_at(cells, c_cat)) or None
            results.append(
                EventResult(
                    position=_parse_int(raw_pos),
                    raw_position=raw_pos,
                    rider_id=rider_id,
                    rider_slug=rider_slug,
                    rider_name=rider_name,
                    nationality=nationality,
                    team=team,
                    time=time,
                    gap=gap,
                    category=row_cat or cat,
                )
            )
    return results


def _parse_event_calendar(
    soup: BeautifulSoup, year: int | None, dh_only: bool
) -> list[EventInfo]:
    """Extract event rows from a calendar/schedule page.

    Both /organiserN/.../ and /seriesN/.../schedule/ render the same row layout:
    <tr>
      <td class="date" data-sb="<epoch>">3rd Oct 2026</td>
      <td><a href="/eventN/<slug>/">Event Name</a></td>
      <td>Venue, Region<span class="notes">, Country</span></td>
      ...
    </tr>
    """
    import datetime as _dt

    events: list[EventInfo] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        eid, slug = _parse_event_href(a["href"])
        if not eid or eid in seen:
            continue
        row = a.find_parent("tr")
        if row is None:
            continue
        row_text = _txt(row)
        if dh_only and not _looks_like_dh(row_text + " " + (slug or "")):
            continue
        seen.add(eid)

        date_cell = row.find("td", class_="date")
        date = _txt(date_cell) if date_cell else None
        ev_year: int | None = None
        date_iso: str | None = None
        if date_cell and date_cell.get("data-sb", "").isdigit():
            epoch = int(date_cell["data-sb"])
            d = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
            ev_year = d.year
            date_iso = d.date().isoformat()
        if ev_year is None:
            ymatch = _YEAR_RE.search(slug or "") or _YEAR_RE.search(date or "")
            ev_year = int(ymatch.group(0)) if ymatch else None
        if year is not None and ev_year is not None and ev_year != year:
            continue

        location: str | None = None
        name_td = a.find_parent("td")
        if name_td is not None:
            loc_td = name_td.find_next_sibling("td")
            if loc_td is not None:
                location = _txt(loc_td) or None

        events.append(
            EventInfo(
                event_id=eid,
                slug=slug,
                name=_txt(a),
                date=date,
                year=ev_year,
                date_iso=date_iso,
                location=location,
                url=urljoin(BASE_URL, a["href"]),
                disciplines=["DH"] if dh_only else [],
            )
        )
    return events


def list_uci_dh_events(year: int | None = None) -> list[EventInfo]:
    """Pull /organiser21/uci/ and filter to DH events."""
    cached = cache.get_cached_results(
        "rootsandrain", "uci_dh_events", "all", year_filter=year
    )
    if cached is not None:
        return [EventInfo(**e) for e in cached]

    url = f"{BASE_URL}/organiser21/uci/"
    html = _fetch(url, data_year=year)
    soup = BeautifulSoup(html, "lxml")
    events = _parse_event_calendar(soup, year=year, dh_only=True)
    cache.store_results(
        "rootsandrain",
        "uci_dh_events",
        "all",
        [asdict(e) for e in events],
        year_filter=year,
        data_year=year,
    )
    return events


# ---------- regional series ----------

# Curated list of DH-relevant series for fantasy form-tracking. We resolve the
# year-specific series ID at runtime via /ajax/search since rootsandrain mints a
# new ID per year.
# `pure_dh` series have only DH events on their schedule, so we skip the
# slug/text keyword filter (NW Cup's slug is just "nw-cup-N" with no "dh" token).
# Mixed series (like Crankworx, which runs Air DH, Slalom, Whip-Off, etc.) keep
# the filter so non-DH disciplines are dropped.
REGIONAL_DH_SERIES: list[dict[str, Any]] = [
    {"key": "ixs_dh_cup", "query": "iXS Downhill Cup",          "region": "EU",     "pure_dh": True},
    {"key": "ixs_eu_cup", "query": "iXS DH European Cup",       "region": "EU",     "pure_dh": True},
    {"key": "crankworx",  "query": "Crankworx World Tour",      "region": "Global", "pure_dh": False},
    {"key": "nw_cup",     "query": "NW Cup",                    "region": "USA",    "pure_dh": True},
    {"key": "us_pro_dh",  "query": "Monster Energy Pro DH Series", "region": "USA", "pure_dh": True},
]


def _resolve_series_id(query: str, year: int) -> tuple[str, str] | None:
    """Look up a year-specific series id+slug by name via /ajax/search.

    Returns (series_id, slug) or None. The search endpoint returns dicts with
    `z` (series id) and `s` (display string like "2026 iXS Downhill Cup"). We
    pick the one whose display string starts with the requested year.
    """
    import json as _json
    from urllib.parse import quote

    cached = cache.get_cached_results(
        "rootsandrain", "series_lookup", query.lower(), year_filter=year
    )
    if cached is not None:
        return tuple(cached) if cached else None

    _warmup()
    url = f"{BASE_URL}/ajax/search?s={quote(query)}"
    resp = _client_get().get(url)
    resp.raise_for_status()
    payload = _json.loads(resp.text)

    year_prefix = f"{year} "
    best: tuple[str, str] | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        z = item.get("z")
        s = item.get("s") or ""
        if z is None or not s.startswith(year_prefix):
            continue
        # Prefer exact name match within the year — rough containment check.
        if query.lower() in s.lower():
            slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
            best = (str(z), slug)
            break

    cache.store_results(
        "rootsandrain",
        "series_lookup",
        query.lower(),
        list(best) if best else [],
        year_filter=year,
        data_year=year,
    )
    return best


def list_series_dh_events(
    series_query: str,
    year: int,
    slug: str | None = None,
    pure_dh: bool = False,
) -> list[EventInfo]:
    """List events from a named series for `year`, treated as DH context.

    Resolves the year-specific series id via /ajax/search, then fetches
    /seriesN/{slug}/schedule/. When `pure_dh=True` we trust the whole series is
    DH and skip keyword filtering (some DH-only series have slugs like
    "nw-cup-N" that don't contain a 'dh' token).
    """
    cache_key = f"{series_query.lower()}|{year}|{int(pure_dh)}"
    cached = cache.get_cached_results(
        "rootsandrain", "series_dh_events", cache_key
    )
    if cached is not None:
        return [EventInfo(**e) for e in cached]

    resolved = _resolve_series_id(series_query, year)
    if resolved is None:
        return []
    series_id, resolved_slug = resolved
    use_slug = slug or resolved_slug

    url = f"{BASE_URL}/series{series_id}/{use_slug}/schedule/"
    html = _fetch(url, data_year=year)
    soup = BeautifulSoup(html, "lxml")
    events = _parse_event_calendar(soup, year=year, dh_only=not pure_dh)

    cache.store_results(
        "rootsandrain",
        "series_dh_events",
        cache_key,
        [asdict(e) for e in events],
        data_year=year,
    )
    return events


def list_regional_dh_events(
    year: int, series_keys: list[str] | None = None
) -> dict[str, list[EventInfo]]:
    """Aggregate DH events from regional series. Keyed by series `key`.

    `series_keys` filters to a subset of REGIONAL_DH_SERIES; default is all.
    """
    keys = set(series_keys) if series_keys else None
    out: dict[str, list[EventInfo]] = {}
    for entry in REGIONAL_DH_SERIES:
        if keys is not None and entry["key"] not in keys:
            continue
        try:
            events = list_series_dh_events(
                entry["query"], year, pure_dh=entry.get("pure_dh", False)
            )
        except Exception as e:
            out[entry["key"]] = []
            # Surface the error inline as a synthetic event so MCP callers see it.
            out[entry["key"]].append(
                EventInfo(
                    event_id=None,
                    slug=None,
                    name=f"[error fetching {entry['query']}: {e}]",
                    date=None,
                    year=year,
                )
            )
            continue
        out[entry["key"]] = events
    return out


# Default points-per-position curve. Loosely matches Pinkbike DH Fantasy
# scoring shape (steep at top, decaying through top 30). Callers can override.
DEFAULT_POINTS_BY_POSITION: dict[int, int] = {
    1: 100, 2: 80, 3: 65, 4: 55, 5: 50, 6: 46, 7: 42, 8: 39, 9: 36, 10: 33,
    11: 30, 12: 27, 13: 25, 14: 23, 15: 21, 16: 19, 17: 17, 18: 15, 19: 13, 20: 11,
    21: 10, 22: 9, 23: 8, 24: 7, 25: 6, 26: 5, 27: 4, 28: 3, 29: 2, 30: 1,
}


def season_standings(
    year: int,
    series: str = "uci",
    category: str | None = None,
    scoring: dict[int, int] | None = None,
    include_worlds: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate per-rider season standings across a series for `year`.

    series:
      - "uci"        → World Cup DH rounds (excludes Worlds/Masters by default)
      - "uci_full"   → all UCI DH events including Worlds
      - any REGIONAL_DH_SERIES key (ixs_dh_cup, ixs_eu_cup, crankworx, nw_cup, us_pro_dh)

    category: exact category string (e.g. "Male Elite", "Female Elite",
              "Male 17-18"). None = aggregate across all categories.
    scoring:  override the default points-by-position curve.
    """
    points_table = scoring or DEFAULT_POINTS_BY_POSITION

    if series in ("uci", "uci_full"):
        events = list_uci_dh_events(year)
        events = [e for e in events if e.year == year]
        if series == "uci":
            events = [e for e in events if "World Cup DH" in e.name]
        else:
            events = [
                e for e in events
                if "World Cup DH" in e.name or "World Championships" in e.name
            ]
            events = [e for e in events if "Masters" not in e.name]
    else:
        # Regional series — find the curated entry to get its pure_dh flag.
        entry = next((s for s in REGIONAL_DH_SERIES if s["key"] == series), None)
        if entry is None:
            raise ValueError(
                f"unknown series {series!r}. valid: uci, uci_full, "
                + ", ".join(s["key"] for s in REGIONAL_DH_SERIES)
            )
        events = list_series_dh_events(
            entry["query"], year, pure_dh=entry.get("pure_dh", False)
        )

    agg: dict[str, dict[str, Any]] = {}
    events_counted = 0
    for ev in events:
        if not ev.event_id or not ev.slug:
            continue
        try:
            results = get_event_results(ev.event_id, ev.slug)
        except Exception:
            continue
        events_counted += 1
        for r in results:
            if not r.rider_id:
                continue
            if category is not None and (r.category or "").strip() != category:
                continue
            d = agg.setdefault(
                r.rider_id,
                {
                    "rider_id": r.rider_id,
                    "rider_slug": r.rider_slug,
                    "name": r.rider_name,
                    "nationality": r.nationality,
                    "team": r.team,
                    "starts": 0,
                    "finishes": 0,
                    "wins": 0,
                    "podiums": 0,
                    "top10s": 0,
                    "top20s": 0,
                    "points": 0,
                    "best_position": None,
                    "_positions": [],
                },
            )
            d["name"] = r.rider_name or d["name"]
            d["nationality"] = r.nationality or d["nationality"]
            d["team"] = r.team or d["team"]
            d["starts"] += 1
            if r.position is not None:
                d["finishes"] += 1
                d["_positions"].append(r.position)
                if r.position == 1:
                    d["wins"] += 1
                if r.position <= 3:
                    d["podiums"] += 1
                if r.position <= 10:
                    d["top10s"] += 1
                if r.position <= 20:
                    d["top20s"] += 1
                d["points"] += points_table.get(r.position, 0)
                d["best_position"] = (
                    r.position
                    if d["best_position"] is None
                    else min(d["best_position"], r.position)
                )

    standings = []
    for d in agg.values():
        ps = d.pop("_positions")
        d["avg_position"] = (sum(ps) / len(ps)) if ps else None
        standings.append(d)
    standings.sort(key=lambda x: (-x["points"], x["avg_position"] or 1e9))
    for i, d in enumerate(standings, 1):
        d["rank"] = i
    return standings


def get_rider_stats(
    rider_id: str,
    rider_slug: str,
    year: int | None = None,
) -> dict[str, Any]:
    """Compute wins / podiums / top10s / avg_position from rider results."""
    results = get_rider_results(rider_id, rider_slug, year=year)
    positions = [r.position for r in results if r.position is not None]
    wins = sum(1 for p in positions if p == 1)
    podiums = sum(1 for p in positions if p <= 3)
    top10s = sum(1 for p in positions if p <= 10)
    avg = (sum(positions) / len(positions)) if positions else None

    by_year: dict[int, dict[str, Any]] = {}
    for r in results:
        if r.year is None:
            continue
        b = by_year.setdefault(
            r.year,
            {"races": 0, "wins": 0, "podiums": 0, "top10s": 0, "_positions": []},
        )
        b["races"] += 1
        if r.position is not None:
            b["_positions"].append(r.position)
            if r.position == 1:
                b["wins"] += 1
            if r.position <= 3:
                b["podiums"] += 1
            if r.position <= 10:
                b["top10s"] += 1
    for y, b in by_year.items():
        ps = b.pop("_positions")
        b["avg_position"] = (sum(ps) / len(ps)) if ps else None

    return {
        "rider_id": rider_id,
        "rider_slug": rider_slug,
        "year_filter": year,
        "races": len(results),
        "wins": wins,
        "podiums": podiums,
        "top10s": top10s,
        "avg_position": avg,
        "by_year": dict(sorted(by_year.items())),
    }
