#!/usr/bin/env python3
"""Discovery probe for Illinois Senate District 35 (Syverson vs Mertens).

Crawls the six election authorities whose territory overlaps the district, saves every
results-looking document into probe/il35/, and, where a Clarity (SOE) site is found, pulls
its summary.json directly. Pure discovery: nothing here feeds the dashboard yet.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_results as fr  # noqa: E402

OUT = fr.ROOT / "probe" / "il35"
LOG: list[str] = []

AUTHORITIES = {
    "rockford": {"start": [], "clarity": "https://results.enr.clarityelections.com/WRC/Rockford/127559"},
    "winnebago": {"start": [], "clarity": "https://results.enr.clarityelections.com/WRC/Winnebago/127560"},
    "boone": {"start": ["https://il-boone.pollresults.net/"], "hops": 0},
    "dekalb": {"start": ["https://platinumelectionresults.com/races/66/4", "https://platinumelectionresults.com/summary/66"], "hops": 1},
    "kane": {"start": ["https://electionresults.kanecountyil.gov/2026-11-03/Contests/"], "hops": 0},
    "mchenry": {"start": ["https://www.mchenrycountyil.gov/county-government/departments-a-i/county-clerk/elections/election-results",
                          "https://results.mchenrycountyil.gov/", "https://electionresults.mchenrycountyil.gov/",
                          "https://www.mchenrycountyil.gov/county-government/departments-a-i/county-clerk/elections"], "hops": 1, "browser": True},
}

LINK_RX = re.compile(r"result|election|clarity|platinum|unofficial|summary|\.pdf|enr|canvass|2026", re.I)
CLARITY_RX = re.compile(r"(https?://results\.enr\.clarityelections\.com/[A-Za-z]+/[A-Za-z_\-]+/\d+)")
SEN35_RX = re.compile(r"(STATE\s+SENATOR|SENAT)[^\n]{0,60}(35|THIRTY[- ]FIFTH)", re.I)


def slug(u: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", u)[-90:]


def save(auth: str, u: str, content: bytes, ext_hint: str = ""):
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"{auth}__{slug(u)}{ext_hint}"
    (OUT / name).write_bytes(content)
    mark = "  <-- mentions 35th" if SEN35_RX.search(content.decode("utf-8", "replace")) else ""
    LOG.append(f"{auth}: saved {len(content):>8} bytes {u}{mark}")


def clarity_pull(auth: str, root: str):
    """Clarity site: current_ver.txt -> summary.json (+ a browser fallback if the plain GET is blocked)."""
    root = root.rstrip("/")
    for getter in ("requests", "browser"):
        try:
            if getter == "requests":
                r = fr.get(f"{root}/current_ver.txt")
                ver = r.text.strip() if r else ""
            else:
                docs = fr._browser_fetch([f"{root}/current_ver.txt"], False, f"il35_{auth}", re.compile(r"$^"))
                ver = re.sub(r"<[^>]+>", "", docs[0][1].decode("utf-8", "replace")).strip() if docs else ""
            LOG.append(f"{auth}: clarity {root} current_ver via {getter} = {ver[:40]!r}")
            if re.fullmatch(r"\d+", ver):
                for path in (f"{ver}/json/en/summary.json", f"{ver}/json/en/electionsettings.json", f"{ver}/reports/summary.zip"):
                    u = f"{root}/{path}"
                    if getter == "requests":
                        r2 = fr.get(u)
                        if r2 and r2.status_code == 200:
                            save(auth, u, r2.content)
                    else:
                        d2 = fr._browser_fetch([u], False, f"il35_{auth}", re.compile(r"$^"))
                        for _, c in d2:
                            txt = re.sub(r"^.*?(\[|\{)", r"\1", c.decode("utf-8", "replace"), count=1, flags=re.S)
                            save(auth, u, txt.encode())
                return
        except Exception as e:
            LOG.append(f"{auth}: clarity {getter} failed {e!r}")


def crawl(auth: str, starts: list[str], hops: int = 2):
    seen: set[str] = set()
    frontier = list(starts)
    for depth in range(hops + 1):
        nxt: list[str] = []
        for u in frontier:
            if u in seen or len(seen) > 40:
                continue
            seen.add(u)
            r = fr.get(u)
            if r is None and AUTHORITIES[auth].get("browser"):
                docs = fr._browser_fetch([u], False, f"il35_{auth}", re.compile(r"result|election|contest|summary", re.I))
                for du, dc in docs:
                    LOG.append(f"{auth}: browser {du} -> {len(dc)} bytes")
                    save(auth, du, dc, ".html")
                    for m in CLARITY_RX.finditer(dc.decode("utf-8", "replace")):
                        clarity_pull(auth, m.group(1))
                continue
            if r is None:
                LOG.append(f"{auth}: GET {u} -> no response")
                continue
            ctype = r.headers.get("content-type", "")
            LOG.append(f"{auth}: GET {u} -> {r.status_code} {len(r.content)} {ctype[:30]}")
            if r.status_code != 200:
                continue
            if "pdf" in ctype or u.lower().endswith(".pdf"):
                save(auth, u, r.content, ".pdf")
                continue
            html = r.text
            if fr.is_challenge(html):
                LOG.append(f"{auth}: challenge page at {u}; trying browser")
                docs = fr._browser_fetch([u], False, f"il35_{auth}", re.compile(r"$^"))
                html = docs[0][1].decode("utf-8", "replace") if docs else html
            save(auth, u, html.encode("utf-8"), ".html")
            for m in CLARITY_RX.finditer(html):
                clarity_pull(auth, m.group(1))
            if depth < hops:
                for u2 in fr.harvest_links(html, u, LINK_RX)[:25]:
                    if u2 not in seen and not re.search(r"facebook|twitter|youtube|instagram|mailto:|javascript:", u2, re.I):
                        nxt.append(u2)
        frontier = nxt


def main():
    for auth, cfg in AUTHORITIES.items():
        try:
            if cfg.get("clarity"):
                clarity_pull(auth, cfg["clarity"])
            if cfg["start"]:
                crawl(auth, cfg["start"], hops=cfg.get("hops", 2))
        except Exception as e:
            LOG.append(f"{auth}: crawl crashed {e!r}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "il35_log.txt").write_text("\n".join(LOG + fr.FETCH_LOG))
    print("\n".join(LOG))


if __name__ == "__main__":
    main()
