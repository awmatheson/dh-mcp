"""End-to-end MCP smoke test: spawn the server over stdio and invoke tools.

Run with: uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command="uv",
        args=["run", "mtb-mcp-server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"server exposes {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"  - {t.name}")
            print()

            print("calling search_riders('Loic Bruni')...")
            res = await session.call_tool("search_riders", {"name": "Loic Bruni"})
            payload = json.loads(res.content[0].text)
            print(f"  hits: {len(payload)}")
            for h in payload[:3]:
                print(f"    {h['rider_id']} {h['slug']}  {h['name']}  ({h['nationality']})")
            print()

            print("calling list_uci_dh_events(year=2026)...")
            res = await session.call_tool("list_uci_dh_events", {"year": 2026})
            events = json.loads(res.content[0].text)
            print(f"  {len(events)} events")
            if events:
                first = sorted(events, key=lambda e: e.get("date") or "")[0]
                print(f"  first: {first['date']}  {first['name']}  @ {first['location']}")
            print()

            print("calling get_rider_stats(Loic Bruni, year=2025)...")
            res = await session.call_tool(
                "get_rider_stats",
                {"rider_id": "18198", "rider_slug": "loic-bruni", "year": 2025},
            )
            stats = json.loads(res.content[0].text)
            print(
                f"  races={stats['races']}  wins={stats['wins']}  "
                f"podiums={stats['podiums']}  top10s={stats['top10s']}  "
                f"avg_position={stats['avg_position']:.2f}"
            )
            print()

            print("calling list_regional_dh_events(year=2026, series_keys=['nw_cup'])...")
            res = await session.call_tool(
                "list_regional_dh_events",
                {"year": 2026, "series_keys": ["nw_cup"]},
            )
            payload = json.loads(res.content[0].text)
            nw = payload["events_by_series"]["nw_cup"]
            print(f"  nw_cup 2026: {len(nw)} events")
            for e in nw[:3]:
                print(f"    {e['date']}  {e['name']}  @ {e['location']}")


if __name__ == "__main__":
    asyncio.run(main())
