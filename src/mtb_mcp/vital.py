"""Vital MTB video index — metadata only.

Vital MTB hosts "Vital RAW" run footage from World Cup DH and regional events.
We surface the listing as supplementary "watch this run" links next to results;
no transcription, OCR, or vision analysis (those are tracked separately).

Index URL: https://www.vitalmtb.com/videos/main (paginated via ?page=N).
Each listing card has title + author + date; the detail page exposes og:title
and og:description for a richer summary.

The site is behind Cloudflare, so we use curl_cffi with Chrome impersonation.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from typing import Iterable

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from . import cache

BASE_URL = "https://www.vitalmtb.com"
LISTING_URL = f"{BASE_URL}/videos/main"


@dataclass
class VitalVideo:
    title: str
    url: str
    slug: str
    section: str  # "features" or "member"
    author: str | None
    date: str | None  # raw date string from the listing card
    description: str | None = None  # populated only by get_video()
    image: str | None = None
    is_vital_raw: bool = False


_session: curl_requests.Session | None = None
_lock = threading.Lock()


def _client() -> curl_requests.Session:
    global _session
    with _lock:
        if _session is None:
            _session = curl_requests.Session(impersonate="chrome131", timeout=30)
        return _session


_VIDEO_HREF_RE = re.compile(r"^/videos/(features|member)/([^/?#]+)")
_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}(?:\s*-\s*[\w:]+(?:am|pm)?)?\b", re.I)


def _parse_card(card) -> VitalVideo | None:
    """Parse a single video card. Cards are <article> elements containing two
    anchors with the same href: a poster <img> link and the title <a> in <h6>.
    The title anchor has non-empty inner text; we pick that one."""
    title_anchor = None
    href = None
    for a in card.find_all("a", href=_VIDEO_HREF_RE):
        text = a.get_text(" ", strip=True)
        if text:
            title_anchor = a
            href = a["href"]
            break
    if title_anchor is None or href is None:
        return None
    m = _VIDEO_HREF_RE.match(href)
    if not m:
        return None
    section, slug = m.group(1), m.group(2)
    title = " ".join(title_anchor.get_text(" ", strip=True).split())

    author = None
    username_a = card.find("a", class_="username")
    if username_a is not None:
        author = username_a.get_text(" ", strip=True) or None

    date = None
    time_el = card.find("time")
    if time_el is not None:
        date = time_el.get("datetime") or time_el.get_text(" ", strip=True)

    is_raw = "vital-raw" in slug or "vital raw" in title.lower()
    return VitalVideo(
        title=title,
        url=BASE_URL + href,
        slug=slug,
        section=section,
        author=author,
        date=date,
        is_vital_raw=is_raw,
    )


def _fetch_listing(page: int = 0) -> list[VitalVideo]:
    url = LISTING_URL if page == 0 else f"{LISTING_URL}?page={page}"
    resp = _client().get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    out: list[VitalVideo] = []
    seen: set[str] = set()
    for card in soup.find_all("article", class_=re.compile(r"node--type-video")):
        v = _parse_card(card)
        if v is None or v.url in seen:
            continue
        seen.add(v.url)
        out.append(v)
    return out


def list_videos(
    query: str | None = None,
    max_results: int = 20,
    pages: int = 1,
    raw_only: bool = False,
) -> list[VitalVideo]:
    """List recent Vital MTB videos, optionally filtered.

    `query` is matched case-insensitively against the title and slug — useful
    for finding videos about a venue ("south korea", "windrock") or rider
    ("bruni", "vermette").

    `pages`: how many pages of the listing to scan. Each page is ~40 videos.
    `raw_only`: True restricts results to videos tagged Vital RAW (slug or
    title contains "vital-raw").
    """
    cache_key = f"{(query or '').lower()}|{max_results}|{pages}|{int(raw_only)}"
    cached = cache.get_cached_results("vital", "videos", cache_key)
    if cached is not None:
        return [VitalVideo(**v) for v in cached]

    videos: list[VitalVideo] = []
    for p in range(pages):
        videos.extend(_fetch_listing(p))
    seen: set[str] = set()
    deduped: list[VitalVideo] = []
    for v in videos:
        if v.url in seen:
            continue
        seen.add(v.url)
        deduped.append(v)

    out = deduped
    if raw_only:
        out = [v for v in out if v.is_vital_raw]
    if query:
        # Normalize both sides: strip dashes/spaces so "south korea" matches
        # the slug "south-korea-vital-raw-...".
        def norm(s: str) -> str:
            return re.sub(r"[\s\-_]+", "", s.lower())
        q = norm(query)
        out = [v for v in out if q in norm(v.title) or q in norm(v.slug)]
    out = out[:max_results]

    cache.store_results(
        "vital", "videos", cache_key, [asdict(v) for v in out]
    )
    return out


def get_video(url_or_slug: str) -> VitalVideo:
    """Fetch a video detail page and extract og:title / og:description / og:image.

    Accepts a full URL, a relative path like "/videos/features/...", or a bare
    slug (we'll guess the section).
    """
    url = url_or_slug
    if not url.startswith("http"):
        if url.startswith("/"):
            url = BASE_URL + url
        else:
            url = f"{BASE_URL}/videos/features/{url}"

    cache_key = url
    cached = cache.get_cached_results("vital", "video_detail", cache_key)
    if cached is not None:
        return VitalVideo(**cached)

    resp = _client().get(url)
    resp.raise_for_status()
    html = resp.text

    def og(field: str) -> str | None:
        m = re.search(
            rf'<meta[^>]+property="og:{field}"[^>]+content="([^"]*)"', html
        )
        return m.group(1) if m else None

    title = og("title") or ""
    description = og("description")
    image = og("image")

    m = _VIDEO_HREF_RE.search(url)
    section = m.group(1) if m else "features"
    slug = m.group(2) if m else url.rstrip("/").rsplit("/", 1)[-1]
    is_raw = "vital-raw" in slug or (title and "vital raw" in title.lower())

    v = VitalVideo(
        title=title,
        url=url,
        slug=slug,
        section=section,
        author=None,
        date=None,
        description=description,
        image=image,
        is_vital_raw=is_raw,
    )
    cache.store_results("vital", "video_detail", cache_key, asdict(v))
    return v
