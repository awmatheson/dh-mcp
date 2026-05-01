"""Sync Pinkbike fantasy data into the local cache.

Two things get synced:

1. The public athletes catalog (/contest/fantasy/dh/athletes/) — all 97 riders
   with current salaries, points, and injury flags. No auth required.
2. Your currently-picked 6 riders (/contest/fantasy/dh/editteam/) — needs your
   browser cookie via .local/pinkbike_curl.txt. If that file is missing, the
   step is skipped with a warning.

Run after each World Cup round to refresh dynamic prices and surface new
injuries/transfers.

Usage:
    uv run python scripts/sync_pinkbike_catalog.py
    uv run python scripts/sync_pinkbike_catalog.py --show 0      # show full lists
    uv run python scripts/sync_pinkbike_catalog.py --no-team     # catalog only
"""

from __future__ import annotations

import argparse
import sys

from mtb_mcp import pinkbike


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="show top-N most expensive riders per gender (0 = all)",
    )
    parser.add_argument(
        "--no-team",
        action="store_true",
        help="skip the 'my team' sync (which needs the curl file)",
    )
    args = parser.parse_args()

    # 1. Public catalog — no auth.
    catalog = pinkbike.get_fantasy_catalog(refresh=True)
    men = sorted(
        [r for r in catalog if r.gender == "male"], key=lambda r: -(r.cost or 0)
    )
    women = sorted(
        [r for r in catalog if r.gender == "female"], key=lambda r: -(r.cost or 0)
    )
    print(f"synced catalog: {len(catalog)} riders ({len(men)} men, {len(women)} women)")

    injured = [r for r in catalog if r.injured]
    if injured:
        print(f"\n⚠  injured riders ({len(injured)}):")
        for r in sorted(injured, key=lambda r: -(r.cost or 0)):
            print(f"  ${r.cost:>8,}  {r.name}  ({r.gender})")
    else:
        print("\nno injuries flagged on the athletes page")

    n = args.show or max(len(men), len(women))
    print(f"\nMen (top {min(n, len(men))} by cost):")
    for r in men[: n if args.show else len(men)]:
        flag = " 🚑 INJURED" if r.injured else ""
        pts = f" pts={r.points}" if r.points is not None else ""
        print(f"  ${r.cost:>8,}  {r.name}{pts}{flag}")
    print(f"\nWomen (top {min(n, len(women))} by cost):")
    for r in women[: n if args.show else len(women)]:
        flag = " 🚑 INJURED" if r.injured else ""
        pts = f" pts={r.points}" if r.points is not None else ""
        print(f"  ${r.cost:>8,}  {r.name}{pts}{flag}")

    # 2. My team — auth-gated. Skip gracefully if no curl file.
    if args.no_team:
        return 0
    if not pinkbike.DEFAULT_CURL_PATH.exists():
        print(
            f"\n(skipping 'my team' sync — no curl file at "
            f"{pinkbike.DEFAULT_CURL_PATH})"
        )
        return 0
    try:
        team = pinkbike.get_my_pinkbike_team(refresh=True)
    except Exception as e:
        print(f"\n(my team sync failed: {e} — refresh the curl file)")
        return 0

    print(f"\nyour team ({len(team)} riders):")
    total = 0
    for r in team:
        cost = r.cost or 0
        total += cost
        print(f"  ${cost:>8,}  {r.name}  ({r.gender})")
    print(f"  {'─' * 40}")
    print(f"  ${total:>8,}  total  (cap $1,500,000, room ${1_500_000 - total:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
