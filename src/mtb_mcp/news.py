"""News and social-media signal sources for fantasy research.

Two sources:

1. Pinkbike news — articles indexed by tag (rider name, "downhill", etc.) at
   /news/tags/{slug}/. Each article card has author, date, title, and a 1-2
   sentence lead. Tag pages are stable and content-rich. Behind Cloudflare so
   we use curl_cffi.
2. Reddit /r/mtb — JSON API at /r/mtb/search.json. Reddit returns 403 to
   curl_cffi's Chrome impersonation (their anti-bypass kicks in), but happily
   serves plain urllib with a simple User-Agent.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from . import cache

PINKBIKE_BASE = "https://www.pinkbike.com"
REDDIT_BASE = "https://www.reddit.com"
REDDIT_USER_AGENT = "mtb-mcp/0.1 (fantasy research)"


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    author: str | None = None
    date: str | None = None
    summary: str | None = None
    score: int | None = None  # reddit upvotes
    comments: int | None = None  # reddit comment count


# ---------- pinkbike ----------


_pinkbike_session: curl_requests.Session | None = None


def _pb_session() -> curl_requests.Session:
    global _pinkbike_session
    if _pinkbike_session is None:
        _pinkbike_session = curl_requests.Session(impersonate="chrome131", timeout=30)
    return _pinkbike_session


_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s*\d{4}\b"
)


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _parse_pinkbike_listing(html: str, max_results: int) -> list[NewsItem]:
    """Parse article cards out of /news/tags/{slug}/ or /news/?search=... pages."""
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen: set[str] = set()
    article_re = re.compile(r"^https?://www\.pinkbike\.com/news/[^?#]+\.html$")
    for link in soup.find_all("a", href=article_re):
        url = link["href"]
        if url in seen:
            continue
        # The article title is the link's own text or its inner heading.
        title = " ".join(link.get_text(" ", strip=True).split())
        if not title or len(title) < 6:
            continue
        # Walk up to the card div for the byline/date/summary.
        card = link.find_parent("div", class_=re.compile(r"news-style|news-box"))
        if card is None:
            card = link.find_parent("div") or link.parent
        card_text = " ".join(card.get_text(" ", strip=True).split()) if card else ""
        # Heuristic byline: name + date pattern.
        author = None
        date = None
        m_date = _DATE_RE.search(card_text)
        if m_date:
            date = m_date.group(0)
            # The author is the text before the date.
            preceding = card_text[: m_date.start()].strip()
            # Last 1-3 words usually = author name.
            author = " ".join(preceding.split()[-3:]) if preceding else None
        # Summary: text immediately after the title.
        summary = None
        if title in card_text:
            idx = card_text.find(title) + len(title)
            tail = card_text[idx : idx + 240].strip()
            tail = re.sub(r"\|\s*\d+\s*Comments.*$", "", tail).strip()
            summary = tail or None
        seen.add(url)
        items.append(
            NewsItem(
                source="pinkbike",
                title=title,
                url=url,
                author=author,
                date=date,
                summary=summary,
            )
        )
        if len(items) >= max_results:
            break
    return items


def get_pinkbike_news(
    query: str, max_results: int = 10, prefer_tag: bool = True
) -> list[NewsItem]:
    """Search Pinkbike news for a rider/team/topic.

    Tries the tag page first (`/news/tags/{slug}/`) since results are
    higher-signal — articles tagged with the rider/team. Falls back to the
    search index (`/news/?search=...`) when the tag page is empty or 404.
    """
    cache_key = f"{query.lower()}|{max_results}|{int(prefer_tag)}"
    cached = cache.get_cached_results("pinkbike", "news_search", cache_key)
    if cached is not None:
        return [NewsItem(**i) for i in cached]

    sess = _pb_session()
    items: list[NewsItem] = []
    if prefer_tag:
        slug = _slugify(query)
        url = f"{PINKBIKE_BASE}/news/tags/{slug}/"
        resp = sess.get(url)
        if resp.status_code == 200:
            items = _parse_pinkbike_listing(resp.text, max_results)
    if not items:
        url = f"{PINKBIKE_BASE}/news/?search={urllib.parse.quote(query)}"
        resp = sess.get(url)
        resp.raise_for_status()
        items = _parse_pinkbike_listing(resp.text, max_results)

    cache.store_results(
        "pinkbike",
        "news_search",
        cache_key,
        [asdict(i) for i in items],
    )
    return items


def get_recent_dh_news(max_results: int = 20) -> list[NewsItem]:
    """Pull the Pinkbike DH-tagged news index for a 'what's new this week' view."""
    cache_key = f"recent|{max_results}"
    cached = cache.get_cached_results("pinkbike", "dh_news", cache_key)
    if cached is not None:
        return [NewsItem(**i) for i in cached]

    # /news/tags/downhill/ is mostly empty chrome — the DH category index lives
    # at /news/?cat=downhill which returns 60+ recent articles per page.
    url = f"{PINKBIKE_BASE}/news/?cat=downhill"
    resp = _pb_session().get(url)
    resp.raise_for_status()
    items = _parse_pinkbike_listing(resp.text, max_results)

    cache.store_results(
        "pinkbike", "dh_news", cache_key, [asdict(i) for i in items]
    )
    return items


# ---------- reddit ----------


def _reddit_get(path: str) -> dict[str, Any]:
    """GET a Reddit JSON endpoint via plain urllib.

    Reddit serves 403 to curl_cffi's Chrome impersonation but accepts plain
    urllib with a basic User-Agent.
    """
    req = urllib.request.Request(
        REDDIT_BASE + path,
        headers={"User-Agent": REDDIT_USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def get_reddit_mtb_mentions(
    query: str,
    max_results: int = 10,
    timeframe: str = "month",
) -> list[NewsItem]:
    """Search /r/mtb for posts mentioning `query`.

    timeframe: one of hour, day, week, month, year, all (Reddit's `t` param).
    """
    cache_key = f"{query.lower()}|{max_results}|{timeframe}"
    cached = cache.get_cached_results("reddit", "mtb_search", cache_key)
    if cached is not None:
        return [NewsItem(**i) for i in cached]

    qs = urllib.parse.urlencode(
        {
            "q": query,
            "restrict_sr": "1",
            "sort": "new",
            "t": timeframe,
            "limit": max_results,
        }
    )
    payload = _reddit_get(f"/r/mtb/search.json?{qs}")
    posts = payload.get("data", {}).get("children", [])

    items: list[NewsItem] = []
    for p in posts[:max_results]:
        d = p.get("data", {})
        url = d.get("url", "")
        permalink = d.get("permalink", "")
        if permalink and not url.startswith("http"):
            url = REDDIT_BASE + permalink
        created = d.get("created_utc")
        date_str = (
            dt.datetime.fromtimestamp(created, tz=dt.timezone.utc).date().isoformat()
            if created
            else None
        )
        body = (d.get("selftext") or "")[:300]
        items.append(
            NewsItem(
                source="reddit",
                title=d.get("title", ""),
                url=url,
                author=d.get("author"),
                date=date_str,
                summary=body if body else None,
                score=d.get("score"),
                comments=d.get("num_comments"),
            )
        )

    cache.store_results(
        "reddit", "mtb_search", cache_key, [asdict(i) for i in items]
    )
    return items
