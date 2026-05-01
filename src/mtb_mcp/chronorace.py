"""ChronoRace live timing for UCI MTB DH events.

ChronoRace's Angular SPA at https://prod.chronorace.be/angular/results.html
fetches live race data from a JSON endpoint:

    https://prod.chronorace.be/api/results/generic/uci/{db}/dh?key={key}

Where:
- `db`  is the event identifier, e.g. "20260501_mtb" (YYYYMMDD_mtb where the
        date is the start of the multi-day event)
- `key` is the run slot. Different keys correspond to different categories
        and runs (e.g. Men Elite Q1, Women Elite Q1, Final). Discoverable
        via `list_runs()`.

The endpoint is unauthenticated, returns JSON, and updates live during
qualifying / finals. Polling interval used by the SPA is 2-3 seconds.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from . import cache

API_ROOT = "https://prod.chronorace.be/api/results/generic/uci"
DEFAULT_TIMEOUT = 15


@dataclass
class Split:
    index: int  # 0-based split index along the course
    race_time_ms: int  # cumulative race time at this split, ms
    race_time: str  # formatted "M:SS.mmm"
    gap_ms: int  # signed gap to leader at this split, ms (negative = ahead)
    gap: str  # formatted (e.g. "+1.434", "-0.222")
    position_at_split: int


@dataclass
class RiderTime:
    rider_id: str  # ChronoRace internal id (key in the Riders dict)
    bib: int | None  # race number
    family_name: str
    given_name: str
    nation: str | None
    team_name: str | None
    category: str | None  # ME = Men Elite, WE = Women Elite, MJ/WJ = Juniors
    uci_rider_id: str | None
    world_cup_rank: int | None
    injury_flag: int  # 0 = healthy
    status: str | None  # Finished / OnTrack / DNS / DNF / DSQ / etc.
    final_position: int | None
    final_time_ms: int | None  # cumulative race time, ms
    final_time: str | None  # formatted "M:SS.mmm"
    final_gap_ms: int | None  # gap to leader
    final_gap: str | None
    speed_kmh: float | None  # bottom-of-course speed if reported
    splits: list[Split] = field(default_factory=list)


@dataclass
class LiveRun:
    db: str
    key: int
    display_name: str  # e.g. "Qualification 1", "Final"
    rider_count: int
    on_track: list[RiderTime]  # currently riding
    next_to_start: list[RiderTime]  # in the start gate / queue
    last_finishers: list[RiderTime]  # most recent N finishers (rolling window)
    results: list[RiderTime]  # all riders with results (sorted by position)
    fetched_at: str  # ISO timestamp


@dataclass
class RunSummary:
    db: str
    key: int
    display_name: str
    rider_count: int
    on_track_count: int
    next_to_start_count: int
    has_results: bool


# ---------- helpers ----------


def _fmt_ms(ms: int | None, signed: bool = False) -> str | None:
    if ms is None:
        return None
    sign = "-" if ms < 0 else ("+" if signed else "")
    n = abs(ms)
    seconds, millis = divmod(n, 1000)
    minutes, seconds = divmod(seconds, 60)
    if minutes > 0:
        return f"{sign}{minutes}:{seconds:02d}.{millis:03d}"
    return f"{sign}{seconds}.{millis:03d}"


def _build_rider(
    rider_id: str, riders: dict[str, dict[str, Any]], result: dict[str, Any] | None
) -> RiderTime:
    info = riders.get(rider_id, {})
    splits: list[Split] = []
    final_time_ms = None
    final_gap_ms = None
    final_position = None
    final_time = None
    final_gap = None
    status = None
    speed = None
    if result is not None:
        status = result.get("Status")
        final_time_ms = result.get("RaceTime") or None
        final_time = _fmt_ms(final_time_ms)
        # Position on the result entry itself is the canonical race position.
        final_position = result.get("Position") or None
        speed = result.get("Speed")
        for i, split in enumerate(result.get("Times") or []):
            if not split:
                continue
            rt = split.get("RaceTime")
            gap = split.get("TimeGap")
            pos = split.get("Position")
            splits.append(
                Split(
                    index=i,
                    race_time_ms=rt or 0,
                    race_time=_fmt_ms(rt) or "",
                    gap_ms=gap or 0,
                    gap=_fmt_ms(gap, signed=True) or "",
                    position_at_split=pos or 0,
                )
            )
        if splits:
            last = splits[-1]
            final_gap_ms = last.gap_ms
            final_gap = last.gap
    return RiderTime(
        rider_id=rider_id,
        bib=info.get("RaceNr"),
        family_name=info.get("FamilyName", ""),
        given_name=info.get("GivenName", ""),
        nation=info.get("Nation"),
        team_name=info.get("UciTeamName"),
        category=info.get("CategoryCode"),
        uci_rider_id=info.get("UciRiderId"),
        world_cup_rank=info.get("WorldCupRank"),
        injury_flag=info.get("Injury", 0),
        status=status,
        final_position=final_position,
        final_time_ms=final_time_ms,
        final_time=final_time,
        final_gap_ms=final_gap_ms,
        final_gap=final_gap,
        speed_kmh=speed,
        splits=splits,
    )


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------- public API ----------


def get_run(db: str, key: int, force: bool = False) -> LiveRun:
    """Fetch and parse a single run by `db` + `key`.

    Live data is volatile during a race weekend, so we don't cache by default
    via the SQLite layer — every call hits the API. Caller is responsible for
    polling cadence (the SPA polls every 2-3s).
    """
    url = f"{API_ROOT}/{db}/dh?key={key}"
    resp = httpx.get(url, timeout=DEFAULT_TIMEOUT, headers={"Accept": "application/json"})
    resp.raise_for_status()
    payload = resp.json()
    if payload is None:
        raise ValueError(f"chronorace returned null for db={db} key={key}")

    riders = payload.get("Riders") or {}
    results_arr = payload.get("Results") or []
    on_track_arr = payload.get("OnTrack") or []
    next_arr = payload.get("NextToStart") or []
    last = payload.get("LastFinisher")

    # The Riders dict key (e.g. "1027") is the canonical rider id. Results
    # entries reference it via their `RaceNr` field; NextToStart is a flat
    # list of those bare ids; OnTrack / LastFinisher are entry objects too.
    def lookup_rid(entry: Any) -> str | None:
        if isinstance(entry, dict):
            for field in ("RaceNr", "Id"):
                v = entry.get(field)
                if v is not None and str(v) in riders:
                    return str(v)
            return None
        if isinstance(entry, (int, str)):
            return str(entry) if str(entry) in riders else None
        return None

    def hydrate(entries: list[Any]) -> list[RiderTime]:
        out: list[RiderTime] = []
        for e in entries:
            rid = lookup_rid(e)
            if rid is None:
                continue
            result_data = e if isinstance(e, dict) else None
            out.append(_build_rider(rid, riders, result_data))
        return out

    results = hydrate(results_arr)
    results.sort(
        key=lambda r: (r.final_position is None or r.final_position == 0,
                       r.final_position or 9999)
    )
    on_track = hydrate(on_track_arr)
    next_to_start = hydrate(next_arr)
    # LastFinisher is a list of recently-completed riders.
    last_list = last if isinstance(last, list) else ([last] if last else [])
    last_finishers = hydrate(last_list)

    return LiveRun(
        db=db,
        key=key,
        display_name=payload.get("DisplayName") or f"key {key}",
        rider_count=len(riders),
        on_track=on_track,
        next_to_start=next_to_start,
        last_finishers=last_finishers,
        results=results,
        fetched_at=_now_iso(),
    )


def list_runs(db: str, max_key: int = 20) -> list[RunSummary]:
    """Discover the active run keys for a given event db.

    Probes keys 1..max_key and returns each one that yields a non-null
    payload, with a brief summary so the caller can pick the run they care
    about.
    """
    out: list[RunSummary] = []
    for key in range(1, max_key + 1):
        url = f"{API_ROOT}/{db}/dh?key={key}"
        try:
            resp = httpx.get(
                url, timeout=DEFAULT_TIMEOUT, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError:
            continue
        if resp.status_code != 200 or resp.text.strip() == "null":
            continue
        try:
            d = resp.json()
        except ValueError:
            continue
        if d is None:
            continue
        out.append(
            RunSummary(
                db=db,
                key=key,
                display_name=d.get("DisplayName") or f"key {key}",
                rider_count=len(d.get("Riders") or {}),
                on_track_count=len(d.get("OnTrack") or []),
                next_to_start_count=len(d.get("NextToStart") or []),
                has_results=bool(d.get("Results")),
            )
        )
    return out


def db_for_date(date_iso: str) -> str:
    """Convert an ISO date (YYYY-MM-DD) into the ChronoRace db identifier.

    UCI MTB events use `YYYYMMDD_mtb`. The date is the start of the multi-day
    event — for round 1 of 2026 that's 2026-05-01, even when racing happens
    on subsequent days.
    """
    d = dt.date.fromisoformat(date_iso)
    return f"{d.year:04d}{d.month:02d}{d.day:02d}_mtb"
