# dh-mcp

MCP server for UCI Downhill MTB race data — for fantasy league research and form-tracking.

Scrapes [rootsandrain.com](https://www.rootsandrain.com), which sits behind Cloudflare; uses [`curl_cffi`](https://github.com/lexiforest/curl_cffi) with Chrome TLS-fingerprint impersonation to avoid being blocked. Past-season data is cached forever in SQLite; current-season data uses a 24h TTL.

## Install

```bash
uv sync
```

That installs the package and its dependencies (`mcp`, `curl_cffi`, `httpx`, `beautifulsoup4`, `lxml`) into a local `.venv`.

The CLI entry point is `mtb-mcp-server` and runs an MCP stdio server.

## Wire it up to Claude Desktop

Merge the snippet from `claude_desktop_config.json` into your Claude Desktop config (typically `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "mtb-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/awmatheson/projects/dh-mcp", "mtb-mcp-server"],
      "env": {
        "MTB_CACHE_DB": "/Users/awmatheson/.cache/mtb-mcp/cache.db"
      }
    }
  }
}
```

Adjust the `--directory` path to wherever you cloned this repo. `MTB_CACHE_DB` is optional; defaults to `~/.cache/mtb-mcp/cache.db`.

Restart Claude Desktop. The tools below will appear under the `mtb-mcp` server.

## Quick check

To verify the server runs and parses live data:

```bash
uv run python scripts/smoke_test.py
```

This spawns the server over stdio, lists tools, and calls `search_riders`, `list_uci_dh_events`, `get_rider_stats`, and `list_regional_dh_events` against rootsandrain.

## Tools

All tools return JSON strings. Search uses rootsandrain's `/ajax/riders` JSON autocomplete; everything else parses HTML tables.

| Tool | Args | Returns |
|---|---|---|
| `search_riders` | `name` | List of `{rider_id, slug, name, nationality, url}` |
| `get_rider_results` | `rider_id`, `rider_slug`, `year?`, `category_filter?` | List of races (event, date, position, time, category) |
| `get_event_results` | `event_id`, `event_slug`, `category_filter?` | Full finisher list (position, rider, nat, team, time, gap, category) |
| `list_uci_dh_events` | `year?` | UCI World Cup + Worlds DH calendar |
| `list_regional_dh_events` | `year`, `series_keys?` | All regional series in one call, keyed by series |
| `list_series_dh_events` | `series_query`, `year` | Schedule for a single named series |
| `season_standings` | `year`, `series?`, `category?`, `top?` | Aggregated per-rider season standings ranked by points |
| `get_rider_stats` | `rider_id`, `rider_slug`, `year?` | Wins / podiums / top10s / avg position + per-year breakdown |
| `compare_riders` | `riders` (list of `{rider_id, rider_slug, name?}`), `year?` | Side-by-side stats sorted by avg position |
| `get_pinkbike_fantasy_catalog` | `refresh?` | Pinkbike DH Fantasy League riders + costs (requires sync, see below) |
| `get_cache_stats` | — | DB path, row counts, byte sizes, fetch timestamps |
| `invalidate_current_season_cache` | — | Wipe current-season + untagged cache entries |

### Regional series

`list_regional_dh_events` aggregates these series. Pass `series_keys` to limit to a subset:

| Key | Series | Region |
|---|---|---|
| `ixs_dh_cup` | iXS Downhill Cup | Europe |
| `ixs_eu_cup` | iXS DH European Cup | Europe |
| `crankworx` | Crankworx World Tour | Global |
| `nw_cup` | NW Cup | USA (Pacific NW) |
| `us_pro_dh` | Monster Energy Pro DH Series (formerly USAC ProGRT) | USA |

Series IDs are resolved at runtime via rootsandrain's `/ajax/search` endpoint — rootsandrain mints a new id per year, so there's no hardcoded table to maintain.

To add a series: append a row to `REGIONAL_DH_SERIES` in `src/mtb_mcp/scraper.py`. Use `pure_dh: True` if the series only runs DH events; `False` if it runs mixed disciplines (the keyword filter then drops non-DH events).

## Pinkbike Fantasy League integration

`get_pinkbike_fantasy_catalog` returns the official Pinkbike rider list with current salaries and carryover points. Pinkbike's edit-team page requires login, so the workflow is one-time auth via "Copy as cURL":

1. Log in at https://www.pinkbike.com
2. Open https://www.pinkbike.com/contest/fantasy/dh/editteam/
3. Devtools → Network tab → reload the page → right-click the `editteam/` request → Copy → Copy as cURL
4. Paste into `.local/pinkbike_curl.txt` (the `.local/` directory is gitignored)
5. Run the sync script:

```bash
uv run python scripts/sync_pinkbike_catalog.py
```

That populates the cache. The MCP tool reads from the cache, so once synced, Claude can ask for `get_pinkbike_fantasy_catalog` instantly.

**When to re-sync:** Pinkbike updates rider prices after every World Cup round. Re-run the script after each race weekend (or whenever you see stale prices). Cookies typically last weeks; if the script reports an auth error, refresh the curl file.

**Combining with `season_standings` for fantasy research:** the two tools together give you costs (from Pinkbike) + form (from rootsandrain) — exactly what's needed to identify dynamic-pricing arbitrage, e.g. riders priced off 2025 standings who are showing 2026 form before round 1.

## Cache behavior

SQLite at `MTB_CACHE_DB` (default `~/.cache/mtb-mcp/cache.db`) with two tables: `page_cache` (raw HTML) and `result_cache` (parsed dataclasses).

- **Past seasons** (data tagged with a year prior to the current calendar year): cached forever.
- **Current season / untagged**: 24h TTL.
- **Force refresh**: call `invalidate_current_season_cache` from the MCP, or `rm` the DB file.

`get_cache_stats` shows what's cached and when.

## Notes & caveats

- **Cloudflare**: rootsandrain returns a hard 403 to anything that isn't a real browser TLS fingerprint. `curl_cffi` impersonates Chrome 131 to get past this. If rootsandrain changes their bot strategy, swap the `_IMPERSONATE` constant in `scraper.py`.
- **Tissot / World Champs**: was originally in scope but the entire tissottiming.com site is now a Vue SPA behind Akamai bot protection. Not currently supported. World Championship results from rootsandrain are still accessible via `get_event_results` once the event has finished.
- **Position parsing**: rider-results pages encode round progression in the position column, e.g. `Q 1 SF F DNS / 84` (qualified 1st, raced semifinal, then DNS in finals out of 84). The parser extracts the token immediately before ` / N` as the final outcome — DNS/DNF/DSQ become `position=null`. If rootsandrain ever changes that layout, positions may silently regress; sanity-check after each World Cup round.
- **Event tables with multiple categories**: `get_event_results` returns rows from all categories in the page (Men Elite, Women Elite, Junior, etc.), tagged via the `category` field. Use `category_filter` to filter post-fetch.
- **Junior fields at WC rounds**: rootsandrain only catalogs Elite tables at World Cup events. Junior 17-18 results are accessible at World Championships pages — query `get_event_results` for the Worlds event with `category_filter="17-18"`.

## Development

```bash
uv sync
uv run python scripts/smoke_test.py   # live test against rootsandrain
```

Source layout:
- `src/mtb_mcp/server.py` — FastMCP server, tool definitions
- `src/mtb_mcp/scraper.py` — rootsandrain client + parsers
- `src/mtb_mcp/cache.py` — SQLite cache with season-aware TTL
