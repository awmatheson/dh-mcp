"""Pinkbike DH Fantasy League scraper.

Two sources:

1. /contest/fantasy/dh/athletes/ — PUBLIC catalog of all 97 riders with current
   salaries, season points, gender, and an injury flag. No auth required.
   Use this for `get_fantasy_catalog()`.

2. /contest/fantasy/dh/editteam/ — REQUIRES LOGIN. Tells us which 6 riders the
   user has currently picked. We re-use the user's browser request via a
   "Copy as cURL" file (saved to .local/pinkbike_curl.txt) so we don't need to
   implement a full login flow. Cookies expire after weeks.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from . import cache

ATHLETES_URL = "https://www.pinkbike.com/contest/fantasy/dh/athletes/"
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
    gender: str | None  # "male" / "female"
    pinkbike_id: str | None
    injured: bool = False
    raw: dict[str, Any] | None = None


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


# ---------- public athletes page (no auth) ----------


_public_session: curl_requests.Session | None = None


def _public_session_get() -> curl_requests.Session:
    global _public_session
    if _public_session is None:
        _public_session = curl_requests.Session(
            impersonate="chrome131", timeout=30
        )
    return _public_session


def fetch_athletes_html() -> str:
    """Fetch the public Pinkbike fantasy athletes page (no auth required)."""
    resp = _public_session_get().get(ATHLETES_URL)
    resp.raise_for_status()
    return resp.text


def parse_athletes_page(html: str) -> list[FantasyRider]:
    """Parse the public athletes page into FantasyRider records.

    Each row in the table follows this layout:

      <tr>
        <td>
          <a id="name{ID}" onclick="showPBBox({ID})">
            <img alt="flag"/> {Name}
            <img alt="injury" title="Injured"/>   <!-- only if injured -->
          </a>
        </td>
        <td>$300,000</td>   <!-- cost -->
        <td>0</td>          <!-- points -->
        <td>Male|Female</td>
      </tr>
    """
    soup = BeautifulSoup(html, "lxml")
    riders: list[FantasyRider] = []
    seen: set[str] = set()
    for a in soup.select('a[id^="name"]'):
        pid_match = re.match(r"name(\d+)$", a.get("id", ""))
        if not pid_match:
            continue
        pid = pid_match.group(1)
        if pid in seen:
            continue
        # Name = the cell's text minus the flag/injury alt text. Strip both imgs.
        name_text = a.get_text(" ", strip=True)
        name = re.sub(r"\s+", " ", name_text).strip()
        if not name:
            continue

        injured = bool(a.find("img", alt="injury"))

        # Walk to the parent <tr> and read the cost/points/gender cells.
        tr = a.find_parent("tr")
        if tr is None:
            continue
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        cost_text = cells[1].get_text(" ", strip=True)
        points_text = cells[2].get_text(" ", strip=True)
        gender_text = cells[3].get_text(" ", strip=True).lower()

        cost = None
        cost_match = re.search(r"\$([\d,]+)", cost_text)
        if cost_match:
            cost = int(cost_match.group(1).replace(",", ""))
        points = None
        pts_match = re.search(r"\d+", points_text)
        if pts_match:
            points = int(pts_match.group(0))
        gender = "male" if gender_text.startswith("male") else "female"

        seen.add(pid)
        riders.append(
            FantasyRider(
                name=name,
                cost=cost,
                points=points,
                gender=gender,
                pinkbike_id=pid,
                injured=injured,
            )
        )
    return riders


def get_fantasy_catalog(refresh: bool = False) -> list[FantasyRider]:
    """Return the Pinkbike fantasy catalog from the public athletes page.

    No auth required. Cached short-term — Pinkbike updates prices after each
    round, so refresh after race weekends.
    """
    if not refresh:
        cached = cache.get_cached_results("pinkbike", "fantasy_catalog", "current")
        if cached is not None:
            return [FantasyRider(**r) for r in cached]

    html = fetch_athletes_html()
    riders = parse_athletes_page(html)
    cache.store_results(
        "pinkbike",
        "fantasy_catalog",
        "current",
        [asdict(r) for r in riders],
    )
    return riders


# ---------- editteam page (requires auth) — used only to read "my picks" ----------


_TEAMID_RE = re.compile(r'\?teamid=(\d+)')


def _parse_team_profile_table(html: str) -> list[tuple[str, str, int | None]]:
    """Parse the (rider_id, name, cost) rows from the team profile table.

    The locked-roster team-profile page renders picks as:
      <a id="name{ID}" onclick="showPBBox({ID})">
        <img alt="flag"/> {Name}
      </a></td><td>${cost}</td>
    """
    pat = re.compile(
        r'<a id="name(\d+)"[^>]*>'
        r'(?:<img[^>]*alt="flag"[^>]*/>\s*)?'
        r'([^<]+?)\s*</a>\s*</td>\s*'
        r'<td[^>]*>\$([\d,]+)',
        re.IGNORECASE,
    )
    rows: list[tuple[str, str, int | None]] = []
    for m in pat.finditer(html):
        pid = m.group(1)
        name = re.sub(r"\s+", " ", m.group(2)).strip()
        cost = int(m.group(3).replace(",", "")) if m.group(3) else None
        rows.append((pid, name, cost))
    return rows


def parse_my_team(html: str) -> list[FantasyRider]:
    """Parse the user's currently-picked riders from the editteam HTML.

    Pinkbike has two display states for the team:

    1. Pre-season / between rounds (roster editable): picks render as
         <a id="athlete1" class="athlete" onclick="showPBBox(1, 1);">
           <span id="athletedataid2"> ... markup ... </span>
         </a>
       where the second showPBBox arg encodes gender (1=male, 2=female).

    2. During a race weekend (roster locked): the editteam page no longer
       shows picks. The team is shown on /contest/fantasy/dh/?teamid={N},
       which uses the same simple table format as the public athletes page.

    The caller is expected to have already fetched the right HTML for the
    state — this just parses whichever pattern matches.
    """
    picks: list[FantasyRider] = []
    pat = re.compile(
        r'<a [^>]*id="athlete\d+"[^>]*'
        r'onclick="showPBBox\(\d+,\s*(\d)\)[^"]*"[^>]*>\s*'
        r'<span id="athletedataid(\d+)">'
        r'(?P<body>[\s\S]*?)'
        r'</span>\s*</a>',
        re.IGNORECASE,
    )
    for m in pat.finditer(html):
        gender = "male" if m.group(1) == "1" else "female"
        pid = m.group(2)
        body = m.group("body")
        name_match = re.search(r'alt="flag"\s*/>\s*([^<]+?)(?:&nbsp;|<)', body)
        if not name_match:
            continue
        name = re.sub(r"\s+", " ", name_match.group(1)).strip()
        cost = None
        cost_match = re.search(r"\$([\d,]+)", body)
        if cost_match:
            cost = int(cost_match.group(1).replace(",", ""))
        picks.append(
            FantasyRider(
                name=name, cost=cost, points=None,
                gender=gender, pinkbike_id=pid,
            )
        )

    if picks:
        return picks

    # State 2: locked roster. Parse the team-profile table; gender is unknown
    # here (the table doesn't include it), so caller fills it in from the
    # public catalog by pinkbike_id lookup.
    for pid, name, cost in _parse_team_profile_table(html):
        picks.append(
            FantasyRider(
                name=name, cost=cost, points=None,
                gender=None, pinkbike_id=pid,
            )
        )
    return picks


def get_my_pinkbike_team(refresh: bool = False) -> list[FantasyRider]:
    """Return the user's currently picked riders. Requires curl-file auth.

    Handles both the editable-roster and locked-roster page states. When the
    roster is locked, follows the `?teamid=N` link from the editteam page to
    the team-profile page, which still exposes the picks. Gender is filled in
    from the public catalog when the page itself doesn't include it.
    """
    if not refresh:
        cached = cache.get_cached_results("pinkbike", "my_team", "current")
        if cached is not None:
            return [FantasyRider(**r) for r in cached]

    parsed = parse_curl_file(DEFAULT_CURL_PATH)
    sess = curl_requests.Session(impersonate="chrome131", timeout=30)
    edit_resp = sess.get(parsed["url"], headers=parsed["headers"])
    edit_resp.raise_for_status()
    edit_html = edit_resp.text

    picks = parse_my_team(edit_html)

    # If the editteam page didn't yield picks, follow the team-profile link.
    if not picks:
        m = _TEAMID_RE.search(edit_html)
        if m is None:
            raise RuntimeError(
                "could not find a teamid link on the editteam page — "
                "either you don't have a team set up yet or the page "
                "layout has changed"
            )
        team_id = m.group(1)
        profile_url = (
            f"https://www.pinkbike.com/contest/fantasy/dh/?teamid={team_id}"
        )
        prof_resp = sess.get(profile_url, headers=parsed["headers"])
        prof_resp.raise_for_status()
        picks = parse_my_team(prof_resp.text)

    # Fill in gender from the public catalog when missing.
    if any(p.gender is None for p in picks):
        try:
            catalog = get_fantasy_catalog()
            by_pid = {r.pinkbike_id: r for r in catalog if r.pinkbike_id}
            for p in picks:
                if p.gender is None and p.pinkbike_id in by_pid:
                    p.gender = by_pid[p.pinkbike_id].gender
        except Exception:
            pass  # keep gender None if catalog fetch fails

    cache.store_results(
        "pinkbike", "my_team", "current",
        [asdict(p) for p in picks],
    )
    return picks
