"""Sync the Pinkbike DH Fantasy League rider catalog into the local cache.

Prereq: log in to Pinkbike in your browser, open
https://www.pinkbike.com/contest/fantasy/dh/editteam/, then in devtools
Network tab right-click the editteam request -> Copy -> Copy as cURL, and paste
the entire string into .local/pinkbike_curl.txt.

Run:
    uv run python scripts/sync_pinkbike_catalog.py

Cookies last as long as your Pinkbike session does (typically weeks). If you
see an auth error, refresh the curl file. Re-run after each round to refresh
prices — Pinkbike updates them after every event.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mtb_mcp import pinkbike


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--curl-file",
        type=Path,
        default=pinkbike.DEFAULT_CURL_PATH,
        help="path to a 'Copy as cURL' file from devtools",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="show top-N most expensive riders per gender (0 = all)",
    )
    args = parser.parse_args()

    if not args.curl_file.exists():
        print(f"error: no curl file at {args.curl_file}", file=sys.stderr)
        print(
            "save 'Copy as cURL' from devtools to that path and re-run.",
            file=sys.stderr,
        )
        return 1

    html = pinkbike.fetch_editteam_html(args.curl_file)
    catalog = pinkbike.parse_catalog(html)

    # Persist to cache by replaying the public function (it stores on success).
    pinkbike.get_fantasy_catalog(refresh=True)

    men = sorted(
        [r for r in catalog if r.gender == "male"], key=lambda r: -(r.cost or 0)
    )
    women = sorted(
        [r for r in catalog if r.gender == "female"], key=lambda r: -(r.cost or 0)
    )
    print(f"synced {len(catalog)} riders ({len(men)} men, {len(women)} women)")

    n = args.show or max(len(men), len(women))
    print(f"\nMen (top {min(n, len(men))} by cost):")
    for r in men[: n if args.show else len(men)]:
        pts = f" pts={r.points}" if r.points is not None else ""
        print(f"  ${r.cost:>8,}  {r.name}{pts}")
    print(f"\nWomen (top {min(n, len(women))} by cost):")
    for r in women[: n if args.show else len(women)]:
        pts = f" pts={r.points}" if r.points is not None else ""
        print(f"  ${r.cost:>8,}  {r.name}{pts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
