"""FastMCP server exposing UCI DH MTB scraping tools."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import cache, chronorace, news, pinkbike, scraper

mcp = FastMCP("mtb-mcp")


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def _dump(payload: Any) -> str:
    return json.dumps(_to_jsonable(payload), default=str, indent=2)


@mcp.tool()
def search_riders(name: str) -> str:
    """Search rootsandrain.com for riders matching `name`. Returns rider IDs and slugs."""
    return _dump(scraper.search_riders(name))


@mcp.tool()
def get_rider_results(
    rider_id: str,
    rider_slug: str,
    year: int | None = None,
    category_filter: str | None = None,
) -> str:
    """Race history for a rider. Optional `year` and `category_filter` are applied post-fetch."""
    return _dump(
        scraper.get_rider_results(
            rider_id=rider_id,
            rider_slug=rider_slug,
            year=year,
            category_filter=category_filter,
        )
    )


@mcp.tool()
def get_event_results(
    event_id: str,
    event_slug: str,
    category_filter: str | None = None,
) -> str:
    """Full finisher list for an event. Cache holds the unfiltered set."""
    return _dump(
        scraper.get_event_results(
            event_id=event_id,
            event_slug=event_slug,
            category_filter=category_filter,
        )
    )


@mcp.tool()
def list_uci_dh_events(year: int | None = None) -> str:
    """UCI DH world cup calendar from rootsandrain.com (filtered to DH only)."""
    return _dump(scraper.list_uci_dh_events(year=year))


@mcp.tool()
def list_regional_dh_events(
    year: int,
    series_keys: list[str] | None = None,
) -> str:
    """DH events from regional series (iXS, Crankworx, NW Cup, US Pro DH, etc.).

    Returns a dict keyed by series `key` (one of: ixs_dh_cup, ixs_eu_cup,
    crankworx, nw_cup, us_pro_dh). Pass `series_keys` to limit to a subset.
    Useful for spotting riders heating up outside the World Cup circuit.
    """
    payload = scraper.list_regional_dh_events(year=year, series_keys=series_keys)
    return _dump(
        {
            "year": year,
            "available_series": [s["key"] for s in scraper.REGIONAL_DH_SERIES],
            "events_by_series": payload,
        }
    )


@mcp.tool()
def list_series_dh_events(series_query: str, year: int) -> str:
    """DH events from a single named series for a year.

    `series_query` is a free-text name like "iXS Downhill Cup" or "Crankworx
    World Tour". Year is required because rootsandrain mints a new series id per
    year. Returns an empty list if no matching series is found.
    """
    return _dump(scraper.list_series_dh_events(series_query=series_query, year=year))


@mcp.tool()
def get_rider_stats(
    rider_id: str,
    rider_slug: str,
    year: int | None = None,
) -> str:
    """Aggregate wins/podiums/top10s/avg_position plus per-year breakdown."""
    return _dump(scraper.get_rider_stats(rider_id, rider_slug, year=year))


@mcp.tool()
def season_standings(
    year: int,
    series: str = "uci",
    category: str | None = None,
    include_worlds: bool = False,
    top: int | None = None,
) -> str:
    """Aggregated season standings for a series, ranked by points.

    series options:
      - "uci"      → World Cup DH rounds only (default — matches Pinkbike WC overall)
      - "uci_full" → UCI DH including Worlds (excludes Masters)
      - regional series keys: ixs_dh_cup, ixs_eu_cup, crankworx, nw_cup, us_pro_dh

    category: exact category string like "Male Elite", "Female Elite",
              "Male 17-18". None = all categories combined.
    top:      limit to top N riders. None returns the full list.

    Uses a default Pinkbike-shape points curve. Each rider row has rank, points,
    wins/podiums/top10s/top20s, starts/finishes, avg_position, best_position.
    """
    standings = scraper.season_standings(
        year=year,
        series=series,
        category=category,
        include_worlds=include_worlds,
    )
    if top is not None:
        standings = standings[:top]
    return _dump(
        {
            "year": year,
            "series": series,
            "category": category,
            "rider_count": len(standings),
            "standings": standings,
        }
    )


@mcp.tool()
def compare_riders(
    riders: list[dict],
    year: int | None = None,
) -> str:
    """Side-by-side stats for several riders, sorted by avg_position ascending.

    `riders` is a list of dicts with `rider_id` and `rider_slug` keys.
    """
    rows: list[dict] = []
    for r in riders:
        rid = str(r["rider_id"])
        slug = str(r["rider_slug"])
        try:
            stats = scraper.get_rider_stats(rid, slug, year=year)
            stats["name"] = r.get("name")
            rows.append(stats)
        except Exception as e:  # surface per-rider failures rather than aborting the whole call
            rows.append(
                {
                    "rider_id": rid,
                    "rider_slug": slug,
                    "name": r.get("name"),
                    "error": str(e),
                }
            )
    rows.sort(
        key=lambda x: (
            x.get("avg_position") is None,
            x.get("avg_position") if x.get("avg_position") is not None else 1e9,
        )
    )
    return _dump({"year_filter": year, "riders": rows})


@mcp.tool()
def list_chronorace_runs(date_iso: str, max_key: int = 20) -> str:
    """Discover active ChronoRace timing runs for a UCI DH event date.

    `date_iso` is the YYYY-MM-DD date the event starts (e.g. round 1 of 2026
    is "2026-05-01"). For each non-null `key` value, returns the run name
    (Qualification 1, Final, etc), rider count, and live state — use this to
    figure out which key to pass to `get_chronorace_run`.

    Live data only exists during race weekends; off-weeks return empty.
    """
    db = chronorace.db_for_date(date_iso)
    runs = chronorace.list_runs(db, max_key=max_key)
    return _dump({"db": db, "date_iso": date_iso, "runs": [r.__dict__ for r in runs]})


@mcp.tool()
def get_chronorace_run(date_iso: str, key: int, top: int | None = None) -> str:
    """Live timing for a specific UCI DH run.

    `date_iso` = event start date (YYYY-MM-DD), `key` = run identifier from
    `list_chronorace_runs`. Returns:
      - DisplayName ("Qualification 1", "Final", etc.)
      - All finishers with cumulative time, gap to leader, sector splits
      - On-track riders (currently racing)
      - Next to start (queued)
      - Last finisher

    During an active race the data refreshes every few seconds. Pass `top`
    to limit the results array to the top N positions for compact output.
    """
    db = chronorace.db_for_date(date_iso)
    run = chronorace.get_run(db, key)

    def serialize(rt: chronorace.RiderTime) -> dict:
        return {**rt.__dict__, "splits": [s.__dict__ for s in rt.splits]}

    results = run.results[:top] if top else run.results
    return _dump(
        {
            "db": run.db,
            "key": run.key,
            "display_name": run.display_name,
            "rider_count": run.rider_count,
            "fetched_at": run.fetched_at,
            "on_track": [serialize(r) for r in run.on_track],
            "next_to_start": [serialize(r) for r in run.next_to_start],
            "last_finishers": [serialize(r) for r in run.last_finishers],
            "results": [serialize(r) for r in results],
        }
    )


@mcp.tool()
def get_pinkbike_news(query: str, max_results: int = 10) -> str:
    """Search Pinkbike news for a rider/team/topic.

    Tries the tag page (/news/tags/{slug}/) first since it's higher signal —
    only articles tagged with the rider/team. Falls back to the search index.
    Returns title, URL, author, date, and a short summary.
    """
    return _dump(news.get_pinkbike_news(query, max_results=max_results))


@mcp.tool()
def get_recent_dh_news(max_results: int = 20) -> str:
    """Latest DH-tagged Pinkbike articles. Use to see what's broken this week
    across the DH world (team announcements, injuries, race previews)."""
    return _dump(news.get_recent_dh_news(max_results=max_results))


@mcp.tool()
def get_reddit_mtb_mentions(
    query: str,
    max_results: int = 10,
    timeframe: str = "month",
) -> str:
    """Search /r/mtb for posts mentioning `query`.

    Useful for breaking news, race-day chatter, and the candid takes that
    don't make it into Pinkbike articles. `timeframe` is one of:
    hour, day, week, month, year, all.
    """
    return _dump(
        news.get_reddit_mtb_mentions(
            query, max_results=max_results, timeframe=timeframe
        )
    )


@mcp.tool()
def get_pinkbike_fantasy_catalog(refresh: bool = False) -> str:
    """Pinkbike DH Fantasy League rider catalog: name, cost, gender, points, injury.

    Sourced from the public athletes page (no auth). Each rider has:
      - cost: current salary in USD
      - points: season points to date
      - gender: male / female
      - injured: True if Pinkbike has flagged the rider as injured
      - pinkbike_id: stable Pinkbike rider id

    Pinkbike updates pricing after every round — re-run
    `scripts/sync_pinkbike_catalog.py` (or pass `refresh=true`) after each
    World Cup race weekend.
    """
    riders = pinkbike.get_fantasy_catalog(refresh=refresh)
    men = sorted(
        [r for r in riders if r.gender == "male"], key=lambda r: -(r.cost or 0)
    )
    women = sorted(
        [r for r in riders if r.gender == "female"], key=lambda r: -(r.cost or 0)
    )
    injured = [r for r in riders if r.injured]
    return _dump(
        {
            "budget": 1_500_000,
            "team_size": {"men": 4, "women": 2},
            "rider_count": len(riders),
            "injured_count": len(injured),
            "injured": [r.__dict__ for r in injured],
            "men": [r.__dict__ for r in men],
            "women": [r.__dict__ for r in women],
        }
    )


@mcp.tool()
def get_my_pinkbike_team(refresh: bool = False) -> str:
    """The user's currently picked 6 fantasy riders.

    Requires .local/pinkbike_curl.txt (a 'Copy as cURL' file from a
    logged-in browser request to /contest/fantasy/dh/editteam/). Returns the
    picks with cost and gender so the caller knows what's currently on the
    team. Use together with `get_pinkbike_fantasy_catalog` to evaluate swaps.
    """
    try:
        team = pinkbike.get_my_pinkbike_team(refresh=refresh)
    except FileNotFoundError as e:
        return _dump({"error": "no_curl_file", "message": str(e)})
    except Exception as e:
        return _dump({"error": "auth_or_parse_failure", "message": str(e)})

    total = sum((r.cost or 0) for r in team)
    return _dump(
        {
            "budget": 1_500_000,
            "spent": total,
            "remaining": 1_500_000 - total,
            "men": [r.__dict__ for r in team if r.gender == "male"],
            "women": [r.__dict__ for r in team if r.gender == "female"],
        }
    )


@mcp.tool()
def get_cache_stats() -> str:
    """Inspect SQLite cache: row counts, byte sizes, fetch timestamps."""
    return _dump(cache.get_cache_stats())


@mcp.tool()
def invalidate_current_season_cache() -> str:
    """Wipe current-season (and untagged) cache entries to force re-fetch."""
    return _dump(cache.invalidate_current_season())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
