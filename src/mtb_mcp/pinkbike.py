"""Pinkbike DH Fantasy League scraper.

The /contest/fantasy/dh/editteam/ page requires login. We re-use the request
that the user's browser already made: they save it as a `curl ...` file via
devtools' "Copy as cURL", and we parse the cookie + headers out of that file
to replay the same authenticated request from Python.

Cookies expire — when you see auth errors, refresh the curl file.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from curl_cffi import requests as curl_requests

from . import cache

EDITTEAM_URL = "https://www.pinkbike.com/contest/fantasy/dh/editteam/"
DEFAULT_CURL_PATH = (
    Path(os.environ.get("MTB_PINKBIKE_CURL")) if os.environ.get("MTB_PINKBIKE_CURL")
    else Path(__file__).resolve().parents[2] / ".local" / "pinkbike_curl.txt"
)


@dataclass
class FantasyRider:
    name: str
    cost: int | None
    points: int | None
    gender: str | None  # "men" / "women" if discoverable
    pinkbike_id: str | None  # whatever the page uses to identify the rider in the form
    raw: dict[str, Any] | None = None  # original JSON-ish blob for debugging


def parse_curl_file(path: Path) -> dict[str, Any]:
    """Extract URL + headers + cookies from a 'Copy as cURL' file.

    Browsers emit something like:
        curl 'https://www.pinkbike.com/contest/fantasy/dh/editteam/' \
          -H 'cookie: pb_user=...; PHPSESSID=...' \
          -H 'user-agent: Mozilla/5.0 ...' \
          -H 'accept: text/html,...' ...

    We pull out the URL and every -H header. shlex handles the multi-line
    backslash continuations and quoting.
    """
    text = path.read_text()
    # Collapse line continuations so shlex sees one logical command.
    text = re.sub(r"\\\n", " ", text)
    tokens = shlex.split(text)

    url: str | None = None
    headers: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "curl":
            i += 1
            continue
        if t in ("-H", "--header") and i + 1 < len(tokens):
            h = tokens[i + 1]
            if ":" in h:
                k, _, v = h.partition(":")
                headers[k.strip().lower()] = v.strip()
            i += 2
            continue
        if t.startswith("--header="):
            h = t.split("=", 1)[1]
            if ":" in h:
                k, _, v = h.partition(":")
                headers[k.strip().lower()] = v.strip()
            i += 1
            continue
        if t in ("-b", "--cookie") and i + 1 < len(tokens):
            headers["cookie"] = tokens[i + 1]
            i += 2
            continue
        # Bare argument that looks like a URL.
        if t.startswith("http"):
            url = t
            i += 1
            continue
        # Skip flags we don't care about.
        if t.startswith("-"):
            # Some flags take an arg; advance two to be safe on common ones.
            if t in ("-X", "--request", "-d", "--data", "--data-raw", "-A", "--user-agent"):
                i += 2
            else:
                i += 1
            continue
        i += 1

    if url is None:
        raise ValueError(f"no URL found in {path}")
    return {"url": url, "headers": headers}


def fetch_editteam_html(curl_path: Path | None = None) -> str:
    """Replay the saved authenticated request and return the page HTML."""
    path = curl_path or DEFAULT_CURL_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"no Pinkbike curl file at {path}. "
            "Save 'Copy as cURL' from devtools to that location."
        )
    parsed = parse_curl_file(path)

    # Use curl_cffi so TLS fingerprint matches Chrome (Pinkbike is also behind
    # Cloudflare). Headers from the saved curl call are authoritative.
    sess = curl_requests.Session(impersonate="chrome131", timeout=30)
    resp = sess.get(parsed["url"], headers=parsed["headers"])
    resp.raise_for_status()
    body = resp.text
    if "Login" in body[:2000] and "logout" not in body[:2000].lower():
        raise RuntimeError(
            "Pinkbike returned a login page — your saved cookies have expired. "
            "Re-save the curl file."
        )
    return body


def parse_catalog(html: str) -> list[FantasyRider]:
    """Extract the rider catalog from the editteam HTML.

    The page has two sections that both render rider rows:

    1. The user's currently-picked team (top of page):
         <a id="athlete1" class="athlete" onclick="showPBBox(1, 1);">
           <span id="athletedataid2"> ... Jackson Goldstone ... $300,000 ...

    2. The full catalog (the picker dropdowns, hidden until clicked):
         <span class="male">
           <a id="athleteitem8" class="athlete athleteitemdisabled"
              onclick="pickAthlete(8);">
             <span id="athletedataid8"> ... Benoit Coulanges ... $205,000 ...
           </a>
         </span>

    We parse both and de-duplicate by `athletedataid` (the stable Pinkbike
    rider id). For picks the gender is derived from the second `showPBBox` arg
    (1=male, 2=female); for catalog rows it's the explicit `<span class="...">`
    wrapper.
    """
    riders: dict[str, FantasyRider] = {}

    # 1. Picked-team rows.
    picked = re.compile(
        r'<a [^>]*id="athlete\d+"[^>]*'
        r'onclick="showPBBox\(\d+,\s*(\d)\)[^"]*"[^>]*>\s*'
        r'<span id="athletedataid(\d+)">'
        r'(?P<body>[\s\S]*?)'
        r'</span>\s*</a>',
        re.IGNORECASE,
    )
    for m in picked.finditer(html):
        gender_code = m.group(1)
        gender = "male" if gender_code == "1" else "female"
        pid = m.group(2)
        rider = _rider_from_block(pid, gender, m.group("body"))
        if rider is not None:
            riders[pid] = rider

    # 2. Catalog rows.
    catalog = re.compile(
        r'<span class="(male|female)">\s*'
        r'<a [^>]*id="athleteitem(\d+)"[^>]*'
        r'onclick="pickAthlete\((\d+)\);"[^>]*>\s*'
        r'<span id="athletedataid(\d+)">'
        r'(?P<body>[\s\S]*?)'
        r'</span>\s*</a>\s*</span>',
        re.IGNORECASE,
    )
    for m in catalog.finditer(html):
        gender = m.group(1).lower()
        pid = m.group(4)
        if pid in riders:
            continue
        rider = _rider_from_block(pid, gender, m.group("body"))
        if rider is not None:
            riders[pid] = rider

    return list(riders.values())


def _rider_from_block(pid: str, gender: str, body: str) -> FantasyRider | None:
    name_match = re.search(r'alt="flag"\s*/>\s*([^<]+?)(?:&nbsp;|<)', body)
    if not name_match:
        return None
    name = re.sub(r"\s+", " ", name_match.group(1)).strip()
    if not name:
        return None

    cost: int | None = None
    cost_match = re.search(r"\$([\d,]+)", body)
    if cost_match:
        cost = int(cost_match.group(1).replace(",", ""))

    points: int | None = None
    pts_match = re.search(
        r'<span class="prevpoints">[\s&nbsp;]*(\d+)', body
    )
    if pts_match:
        points = int(pts_match.group(1))

    return FantasyRider(
        name=name,
        cost=cost,
        points=points,
        gender=gender,
        pinkbike_id=pid,
        raw=None,
    )


def get_fantasy_catalog(refresh: bool = False) -> list[FantasyRider]:
    """Return the cached Pinkbike fantasy catalog, fetching if missing/stale."""
    if not refresh:
        cached = cache.get_cached_results("pinkbike", "fantasy_catalog", "current")
        if cached is not None:
            return [FantasyRider(**r) for r in cached]

    html = fetch_editteam_html()
    riders = parse_catalog(html)
    cache.store_results(
        "pinkbike",
        "fantasy_catalog",
        "current",
        [asdict(r) for r in riders],
    )
    return riders
