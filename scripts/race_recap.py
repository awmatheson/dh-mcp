"""Manual race recap: run after each UCI World Cup DH round.

Pulls the most recent WC race in the past N days, current season standings,
shifts vs. last year, news/injury alerts, and Reddit chatter for each rider in
your fantasy team. Prints a single report to stdout.

Usage:
    uv run python scripts/race_recap.py
    uv run python scripts/race_recap.py --days 14
    uv run python scripts/race_recap.py --team "Loic Bruni,Finn Iles,..."
    uv run python scripts/race_recap.py --no-reddit       # skip Reddit calls

The script does NOT refresh the Pinkbike fantasy catalog — run
`scripts/sync_pinkbike_catalog.py` before this one if you want fresh prices.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from typing import Iterable

from mtb_mcp import news, scraper

# Default fantasy team. Update this list when you change your roster.
DEFAULT_TEAM = [
    "Jackson Goldstone",
    "Loic Bruni",
    "Asa Vermette",
    "Finn Iles",
    "Vali Höll",
    "Marine Cabirou",
]

INJURY_KEYWORDS = (
    "injur", "crash", "broke", "fractur", "withdraw", "withdrawn", " out ",
    "replaces", "signs", "joins", "leaves", "comeback", "dsq",
)


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _hr() -> None:
    print("─" * 78)


def _section(title: str) -> None:
    print()
    print(f"## {title}")


def find_recent_race(year: int, days: int) -> scraper.EventInfo | None:
    cutoff = _today() - dt.timedelta(days=days)
    events = scraper.list_uci_dh_events(year)
    candidates = [
        e
        for e in events
        if "World Cup DH" in e.name
        and e.date_iso
        and dt.date.fromisoformat(e.date_iso) >= cutoff
        and dt.date.fromisoformat(e.date_iso) <= _today()
    ]
    candidates.sort(key=lambda e: e.date_iso, reverse=True)
    return candidates[0] if candidates else None


def find_next_race(year: int) -> scraper.EventInfo | None:
    today = _today()
    events = scraper.list_uci_dh_events(year)
    upcoming = [
        e
        for e in events
        if "World Cup DH" in e.name
        and e.date_iso
        and dt.date.fromisoformat(e.date_iso) > today
    ]
    upcoming.sort(key=lambda e: e.date_iso)
    return upcoming[0] if upcoming else None


def race_recap(event: scraper.EventInfo) -> None:
    _section(f"Race Recap — {event.name}, {event.date_iso} @ {event.location}")
    results = scraper.get_event_results(event.event_id, event.slug)
    for cat, label in (("Male Elite", "Top 5 Men"), ("Female Elite", "Top 5 Women")):
        rows = sorted(
            (r for r in results if (r.category or "").strip() == cat and r.position),
            key=lambda r: r.position,
        )[:5]
        print(f"\n{label}:")
        if not rows:
            print(f"  (no {cat} results parsed)")
            continue
        for r in rows:
            nat = f" ({r.nationality})" if r.nationality else ""
            team = f"  {r.team}" if r.team else ""
            time = f"  {r.time}" if r.time else ""
            print(f"  {r.position}. {r.rider_name}{nat}{time}{team}")


def standings_shifts(year: int) -> dict[str, list[tuple[str, int, int]]]:
    """Return movers (Δ ≥ 5 positions) per category vs prior year final."""
    out: dict[str, list[tuple[str, int, int]]] = {}
    for cat in ("Male Elite", "Female Elite"):
        cur = scraper.season_standings(year, series="uci", category=cat)
        prev = scraper.season_standings(year - 1, series="uci", category=cat)
        prev_rank = {r["rider_id"]: r["rank"] for r in prev}
        movers: list[tuple[str, int, int]] = []
        for r in cur[:15]:
            old = prev_rank.get(r["rider_id"])
            if old is not None and abs(old - r["rank"]) >= 5:
                movers.append((r["name"], r["rank"], old))
        out[cat] = movers
    return out


def print_standings(year: int) -> None:
    _section(f"{year} Season Standings (top 15)")
    for cat, label in (("Male Elite", "Men"), ("Female Elite", "Women")):
        rows = scraper.season_standings(year, series="uci", category=cat)[:15]
        print(f"\n{label}:")
        for r in rows:
            print(
                f"  #{r['rank']:>2}  {r['name']:24s}  pts={r['points']:>4}  "
                f"wins={r['wins']}  pod={r['podiums']}  t10={r['top10s']}"
            )


def print_shifts(shifts: dict[str, list[tuple[str, int, int]]], year: int) -> None:
    _section(f"Standings Shifts (Δ ≥ 5 positions vs. {year - 1} final)")
    any_shifts = False
    for cat, movers in shifts.items():
        if not movers:
            continue
        any_shifts = True
        print(f"\n{cat}:")
        for name, new_rank, old_rank in movers:
            arrow = "↑" if new_rank < old_rank else "↓"
            delta = abs(new_rank - old_rank)
            print(f"  {arrow} {name}:  #{old_rank} → #{new_rank}  (Δ {delta})")
    if not any_shifts:
        print("  (no top-15 riders moved 5+ positions)")


def print_news_alerts() -> None:
    _section("News Alerts (Pinkbike, last fetch)")
    items = news.get_recent_dh_news(max_results=25)
    flagged = []
    for n in items:
        text = (n.title + " " + (n.summary or "")).lower()
        if any(k in text for k in INJURY_KEYWORDS):
            flagged.append(n)
    if not flagged:
        print("  (no injury/team-change keywords in last 25 DH articles)")
        return
    for n in flagged[:10]:
        date = f"[{n.date}] " if n.date else ""
        print(f"  {date}{n.title}")
        if n.summary:
            print(f"      {n.summary[:140]}")


def print_reddit_chatter(team: Iterable[str]) -> None:
    _section("Reddit /r/mtb chatter (last week, score > 30)")
    any_hits = False
    for rider in team:
        try:
            posts = news.get_reddit_mtb_mentions(rider, timeframe="week", max_results=5)
        except Exception as e:
            print(f"  {rider}: error fetching Reddit ({e})")
            continue
        relevant = [p for p in posts if (p.score or 0) > 30 or (p.comments or 0) > 20]
        if not relevant:
            continue
        any_hits = True
        print(f"\n  {rider}:")
        for p in relevant:
            print(f"    [score={p.score} c={p.comments}] {p.title[:90]}")
            print(f"      {p.url}")
    if not any_hits:
        print("  (no high-engagement posts about your team this week)")


def print_team_pinkbike_costs(team: Iterable[str]) -> None:
    """If a synced Pinkbike catalog exists, show current costs and budget."""
    try:
        from mtb_mcp import pinkbike

        catalog = pinkbike.get_fantasy_catalog()
    except Exception:
        return
    if not catalog:
        return
    by_name = {r.name.lower(): r for r in catalog}

    _section("Your Team — Current Pinkbike Costs")
    total = 0
    for name in team:
        # Try a couple of name variants because Pinkbike strips accents.
        keys = [name.lower(), re.sub(r"[^a-z ]", "", name.lower())]
        match = next((by_name[k] for k in keys if k in by_name), None)
        if match is None:
            print(f"  {name:24s}  not in catalog (re-sync or rename)")
            continue
        cost = match.cost or 0
        total += cost
        print(f"  ${cost:>8,}  {match.name}  (gender={match.gender})")
    print(f"  {'─' * 40}")
    print(f"  ${total:>8,}  total  (cap $1,500,000, room: ${1_500_000 - total:,})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=_today().year)
    parser.add_argument("--days", type=int, default=7,
                        help="how far back to look for a 'recent' race")
    parser.add_argument(
        "--team",
        type=str,
        default=",".join(DEFAULT_TEAM),
        help="comma-separated rider names",
    )
    parser.add_argument("--no-reddit", action="store_true",
                        help="skip per-rider Reddit calls")
    args = parser.parse_args()

    team = [t.strip() for t in args.team.split(",") if t.strip()]
    today = _today()

    print(f"DH WC race recap — {today.isoformat()} (looking back {args.days}d)")
    _hr()

    recent = find_recent_race(args.year, args.days)
    if recent is None:
        nxt = find_next_race(args.year)
        if nxt:
            print(
                f"\nNo UCI WC DH race in the past {args.days} days. "
                f"Next race: {nxt.name} on {nxt.date_iso} @ {nxt.location}."
            )
        else:
            print(f"\nNo recent or upcoming WC DH races found for {args.year}.")
        return 0

    race_recap(recent)
    print_standings(args.year)
    print_shifts(standings_shifts(args.year), args.year)
    print_news_alerts()
    if not args.no_reddit:
        print_reddit_chatter(team)
    print_team_pinkbike_costs(team)

    _section("Action Items")
    nxt = find_next_race(args.year)
    if nxt:
        print(f"  - Next race: {nxt.name} on {nxt.date_iso} @ {nxt.location}")
    print("  - Re-sync Pinkbike prices: uv run python scripts/sync_pinkbike_catalog.py")
    print("  - Edit team if needed: https://www.pinkbike.com/contest/fantasy/dh/editteam/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
