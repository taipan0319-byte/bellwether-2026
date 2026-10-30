#!/usr/bin/env python3
"""
Election-night fetcher for the 2026 Bellwether Watch dashboard.

Pulls unofficial returns for the configured U.S. House races from each state's
official election-night reporting site, normalizes them, applies any manual
overrides, and writes data/results.json for the static dashboard.

Design goals
------------
* Never crash the whole run because one state feed is down: each race keeps its
  last good numbers and records an error string instead.
* Be tolerant of markup/JSON changes: a name-anchored parser looks for the
  configured candidates' names and reads the vote counts next to them, in both
  HTML tables and JSON documents. State adapters only decide WHICH document(s)
  to fetch; a shared extractor does the reading.
* Manual overrides (data/manual_overrides.json) always win, so a race can be
  entered by hand from AP/network numbers if a feed breaks.

Run locally:  python3 scripts/fetch_results.py [--probe] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROBE_DIR = ROOT / "probe"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36 bellwether-2026/1.0 (+github.com/taipan0319-byte)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}
TIMEOUT = 30
SESSION = requests.Session()
SESSION.headers.update(UA)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get(url: str, **kw) -> requests.Response | None:
    try:
        r = SESSION.get(url, timeout=TIMEOUT, **kw)
        log(f"GET {url} -> {r.status_code} ({len(r.content)} bytes)")
        if r.status_code == 200:
            return r
    except requests.RequestException as e:
        log(f"GET {url} failed: {e}")
    return None


def save_probe(name: str, content: bytes | str):
    PROBE_DIR.mkdir(exist_ok=True)
    p = PROBE_DIR / name
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(p, mode) as f:
        f.write(content)
    log(f"probe saved {p.relative_to(ROOT)}")


# --------------------------------------------------------------------------- #
# Normalized model
# --------------------------------------------------------------------------- #
@dataclass
class CandResult:
    name: str
    party: str
    votes: int | None = None
    incumbent: bool = False


@dataclass
class RaceResult:
    status: str = "not_started"          # not_started | counting | called
    source_status: str = "pending"       # ok | error | manual | pending
    source_updated: str | None = None
    precincts_reporting: int | None = None
    precincts_total: int | None = None
    pct_reported: float | None = None
    called: str | None = None            # REP | DEM | None
    winner: str | None = None
    candidates: list[dict] = field(default_factory=list)
    error: str | None = None
    source_url: str | None = None


# --------------------------------------------------------------------------- #
# Generic, name-anchored extraction
# --------------------------------------------------------------------------- #
NUM_RE = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?![\d.])|(?<![\d.,])\d+(?![\d.,%])")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
PRECINCT_RES = [
    re.compile(r"(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)\s*(?:precincts|polling (?:places|locations)|vote centers)", re.I),
    re.compile(r"precincts?\s*(?:reporting|reported|complete)[^0-9]{0,40}(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)", re.I),
    re.compile(r"(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)\s*(?:precincts|counties)?\s*reporting", re.I),
]
PCT_REPORTED_RES = [
    re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:precincts|expected vote|reporting|reported|counted)", re.I),
    re.compile(r"(?:precincts|expected vote|est\.? votes?)[^%\d]{0,40}(\d{1,3}(?:\.\d+)?)\s*%", re.I),
]


def name_tokens(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    first = re.sub(r"[^a-z]", "", parts[0].lower()) if parts else ""
    last = re.sub(r"[^a-z]", "", parts[-1].lower()) if parts else ""
    # multi-word surnames (De La Cruz)
    if len(parts) >= 3 and parts[-3].lower() in ("de", "van", "von", "la", "del"):
        last = re.sub(r"[^a-z]", "", "".join(parts[-3:]).lower())
    return first, last


def text_matches_candidate(text: str, cand: dict) -> bool:
    t = re.sub(r"[^a-z]", "", text.lower())
    first, last = name_tokens(cand["name"])
    if not last or last not in t:
        return False
    # Require first name too when the surname is common/short, to avoid false hits.
    if len(last) <= 5 or last in ("garcia", "taylor", "prince", "flint", "singer"):
        return first[:3] in t
    return True


def to_int(s: str) -> int | None:
    try:
        return int(s.replace(",", ""))
    except Exception:
        return None


def parse_precincts(text: str) -> tuple[int | None, int | None, float | None]:
    for rx in PRECINCT_RES:
        m = rx.search(text)
        if m:
            a, b = to_int(m.group(1)), to_int(m.group(2))
            if a is not None and b:
                return a, b, round(a / b * 100, 1)
    for rx in PCT_REPORTED_RES:
        m = rx.search(text)
        if m:
            return None, None, float(m.group(1))
    return None, None, None


def extract_from_html(html: str, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    """Find the block of the page for this contest, then read votes beside each candidate name."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    cands = race["candidates"]

    # Candidate rows: the smallest element containing the name whose text also contains a number.
    def find_row(cand) -> Any:
        hits = [e for e in soup.find_all(string=lambda s: s and text_matches_candidate(s, cand))]
        for s in hits:
            node = s.parent
            for _ in range(6):
                if node is None:
                    break
                txt = node.get_text(" ", strip=True)
                nums = [to_int(n) for n in NUM_RE.findall(txt)]
                nums = [n for n in nums if n is not None]
                if nums and node.name in ("tr", "li", "div", "p", "section", "article", "td"):
                    return node
                node = node.parent
        return None

    rows = {c["name"]: find_row(c) for c in cands}
    found = [r for r in rows.values() if r is not None]
    if len(found) < max(1, len(cands) - 1):
        return None

    # Ensure we are inside the right contest: walk up from the first row to a container
    # whose text matches the contest pattern (district number).
    container = found[0]
    ok = False
    for _ in range(8):
        if container is None:
            break
        if contest_rx.search(container.get_text(" ", strip=True)):
            ok = True
            break
        container = container.parent
    if not ok:
        # Some pages put the contest title in a sibling header; accept if the whole page is
        # single-contest or the title appears anywhere near.
        page_text = soup.get_text(" ", strip=True)
        if not contest_rx.search(page_text):
            return None
        container = soup

    res = RaceResult()
    out = []
    for c in cands:
        row = rows.get(c["name"])
        votes = None
        if row is not None:
            txt = row.get_text(" ", strip=True)
            # Remove percentages, then take the largest remaining integer as the vote count.
            txt_wo_pct = PCT_RE.sub(" ", txt)
            nums = [to_int(n) for n in NUM_RE.findall(txt_wo_pct)]
            nums = [n for n in nums if n is not None and n < 5_000_000]
            # drop tiny numbers that are likely ballot positions / district numbers
            big = [n for n in nums if n >= 10] or nums
            votes = max(big) if big else None
        out.append(asdict(CandResult(c["name"], c["party"], votes, c.get("incumbent", False))))
    res.candidates = out
    ctext = container.get_text(" ", strip=True) if container is not soup else soup.get_text(" ", strip=True)
    res.precincts_reporting, res.precincts_total, res.pct_reported = parse_precincts(ctext)
    if re.search(r"\bwinner\b|\bwins\b|✓|✓", ctext, re.I):
        pass  # winner flags vary too much; leave 'called' to manual override
    return res


def walk_json(obj: Any, path=()) -> Iterable[tuple[tuple, Any]]:
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_json(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_json(v, path + (i,))


VOTE_KEYS = ("votes", "votecount", "vote_count", "totalvotes", "total_votes", "ballots", "count", "total", "tally", "v")


def extract_from_json(doc: Any, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    """Locate the contest object whose text matches the pattern and read candidate votes."""
    cands = race["candidates"]

    def dict_text(d: Any) -> str:
        return json.dumps(d, default=str)[:20000]

    # Candidate contest containers: dicts whose serialized text matches the contest pattern
    # and contains at least two of our candidates.
    best = None
    for path, node in walk_json(doc):
        if not isinstance(node, dict):
            continue
        txt = dict_text(node)
        if not contest_rx.search(txt):
            continue
        hits = sum(1 for c in cands if text_matches_candidate(txt, c))
        if hits >= min(2, len(cands)):
            # prefer the smallest matching container
            if best is None or len(txt) < len(best[1]):
                best = (node, txt)
    if best is None:
        return None
    contest = best[0]

    res = RaceResult()
    out = []
    for c in cands:
        votes = None
        for _, node in walk_json(contest):
            if isinstance(node, dict) and any(isinstance(v, str) and text_matches_candidate(v, c) for v in node.values()):
                for k, v in node.items():
                    if k.lower().replace("_", "") in [x.replace("_", "") for x in VOTE_KEYS] and isinstance(v, (int, float, str)):
                        iv = to_int(str(v).split(".")[0])
                        if iv is not None:
                            votes = iv
                            break
                if votes is not None:
                    break
        out.append(asdict(CandResult(c["name"], c["party"], votes, c.get("incumbent", False))))
    res.candidates = out
    # precincts: look for numeric fields in the contest dict
    def pick(keys):
        for _, node in walk_json(contest):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k.lower().replace("_", "") in keys and isinstance(v, (int, float, str)):
                        iv = to_int(str(v).split(".")[0])
                        if iv is not None:
                            return iv
        return None
    res.precincts_reporting = pick({"precinctsreporting", "precinctsreported", "reportingprecincts", "precinctsreportingcount", "precinctscounted"})
    res.precincts_total = pick({"precinctstotal", "totalprecincts", "precinctsparticipating", "precinctcount", "numprecincts"})
    if res.precincts_reporting is not None and res.precincts_total:
        res.pct_reported = round(res.precincts_reporting / res.precincts_total * 100, 1)
    else:
        for _, node in walk_json(contest):
            if isinstance(node, dict):
                for k, v in node.items():
                    if re.search(r"(percent|pct).*(report|precinct|expected)", k, re.I) and isinstance(v, (int, float)):
                        res.pct_reported = float(v) if v > 1 else float(v) * 100
    return res


def extract(content: bytes, race: dict, source_url: str) -> RaceResult | None:
    rx = re.compile(race["source"]["contest_pattern"], re.I)
    text = content.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    res = None
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            res = extract_from_json(json.loads(text), race, rx)
        except json.JSONDecodeError:
            res = None
    if res is None and "<" in stripped[:200]:
        res = extract_from_html(text, race, rx)
    if res is not None:
        res.source_url = source_url
    return res


# --------------------------------------------------------------------------- #
# State adapters: return a list of candidate documents (url, bytes) to try, in order.
# Each adapter is deliberately broad: it fetches an index page, harvests links/API
# routes that look relevant, and returns them. The extractor decides what parses.
# --------------------------------------------------------------------------- #
def harvest_links(html: str, base: str, must_match: re.Pattern) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = a["href"]
        if must_match.search(label) or must_match.search(href):
            out.append(requests.compat.urljoin(base, href))
    # also scripts / fetch() targets
    for m in re.finditer(r"""["'](https?://[^"']+|/[^"']+\.(?:json|txt|csv|xml)[^"']*)["']""", html):
        u = m.group(1)
        if must_match.search(u):
            out.append(requests.compat.urljoin(base, u))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def adapter_fl(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    home = cfg["home"]
    docs = []
    r = get(home)
    if not r:
        return docs
    if probe:
        save_probe("fl_home.html", r.content)
    docs.append((home, r.content))
    # Congressional index pages and per-contest pages
    links = harvest_links(r.text, home, re.compile(r"congress|U\.?S\.? ?Rep|ContestResults|Download|Export|\.txt|\.json", re.I))
    for u in links[:40]:
        rr = get(u)
        if rr:
            docs.append((u, rr.content))
            if probe:
                save_probe("fl_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", rr.content)
            # second hop: contest links inside the congressional index
            for u2 in harvest_links(rr.text, u, re.compile(r"ContestResults|District\s*25", re.I))[:20]:
                if u2 not in [d[0] for d in docs]:
                    r2 = get(u2)
                    if r2:
                        docs.append((u2, r2.content))
                        if probe:
                            save_probe("fl_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".html", r2.content)
    return docs


def adapter_mi(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    docs = []
    candidates = [
        cfg["home"],
        "https://mielections.us/election/results/2026GEN_CENR.html",
        "https://mielections.us/election/results/2026GEN_CENR_US_REP.html",
        "https://mielections.us/election/results/2026GEN_CENR_UNITED_STATES_REPRESENTATIVE.html",
        "https://mielections.us/election/results/",
        "https://electionreporting.sos.state.mi.us/",
    ]
    seen = set()
    for u in candidates:
        if u in seen:
            continue
        seen.add(u)
        r = get(u)
        if not r:
            continue
        docs.append((u, r.content))
        if probe:
            save_probe("mi_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", r.content)
        for u2 in harvest_links(r.text, u, re.compile(r"congress|U\.?S\.? ?Rep|representative|2026GEN|\.json|\.csv", re.I))[:30]:
            if u2 in seen:
                continue
            seen.add(u2)
            r2 = get(u2)
            if r2:
                docs.append((u2, r2.content))
                if probe:
                    save_probe("mi_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".html", r2.content)
    return docs


def adapter_va(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """Virginia uses Enhanced Voting's ENR single-page app. Try the public API routes
    the app uses; also harvest the app bundle for route strings during a probe."""
    docs = []
    slug = "2026-November-General"
    base = "https://enr.elections.virginia.gov"
    api_guesses = [
        f"{base}/results/public/api/elections/virginia/{slug}",
        f"{base}/results/public/api/elections/virginia/{slug}/contests",
        f"{base}/results/public/api/elections/virginia/{slug}/results",
        f"{base}/results/public/api/elections/virginia/{slug}/export/json",
        f"{base}/results/public/api/elections/virginia/{slug}/summary",
        f"{base}/results/public/api/virginia/elections/{slug}",
        f"{base}/results/public/api/virginia/elections/{slug}/contests",
        f"{base}/results/public/api/virginia/{slug}/summary.json",
        f"{base}/results/public/api/elections/virginia/{slug}/ballot-items",
        f"{base}/results/public/api/elections/virginia/{slug}/ballotItems",
        f"{base}/results/public/virginia/elections/{slug}/export/json",
        f"{base}/results/public/virginia/elections/{slug}.json",
    ]
    for u in api_guesses:
        r = get(u, headers={"Accept": "application/json"})
        if r and r.headers.get("content-type", "").startswith("application/json"):
            docs.append((u, r.content))
            if probe:
                save_probe("va_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".json", r.content)
    if probe or not docs:
        r = get(cfg["home"])
        if r:
            save_probe("va_home.html", r.content) if probe else None
            # pull JS bundles and record anything that looks like an API route
            soup = BeautifulSoup(r.text, "lxml")
            routes = set()
            for s in soup.find_all("script", src=True):
                js_url = requests.compat.urljoin(cfg["home"], s["src"])
                jr = get(js_url)
                if jr:
                    for m in re.finditer(r"""["'`](/?(?:results/public/)?api/[^"'`\s]{3,200})["'`]""", jr.text):
                        routes.add(m.group(1))
            if probe:
                save_probe("va_api_routes.txt", "\n".join(sorted(routes)))
            # Try harvested routes that look election-scoped, substituting the slug
            for rt in sorted(routes):
                if "${" in rt or "{" in rt:
                    continue
                u = requests.compat.urljoin(base + "/results/public/", rt)
                rr = get(u, headers={"Accept": "application/json"})
                if rr and rr.headers.get("content-type", "").startswith("application/json"):
                    docs.append((u, rr.content))
                    if probe:
                        save_probe("va_route_" + re.sub(r"[^A-Za-z0-9]+", "_", rt)[-80:] + ".json", rr.content)
    return docs


def adapter_tx(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """Texas SOS portal (results.texas-election.com) is a JS app backed by static JSON.
    Harvest the JSON/API URLs it references, then fetch them."""
    docs = []
    home = cfg["home"]
    r = get(home)
    if not r:
        return docs
    if probe:
        save_probe("tx_home.html", r.content)
    soup = BeautifulSoup(r.text, "lxml")
    urls = set()
    for m in re.finditer(r"""["'](https?://[^"']+|/[^"']+)["']""", r.text):
        u = m.group(1)
        if re.search(r"\.json|/api/|static/data|election", u, re.I):
            urls.add(requests.compat.urljoin(home, u))
    for s in soup.find_all("script", src=True):
        js_url = requests.compat.urljoin(home, s["src"])
        jr = get(js_url)
        if not jr:
            continue
        if probe:
            save_probe("tx_bundle_" + re.sub(r"[^A-Za-z0-9]+", "_", js_url)[-60:] + ".js", jr.content[:400000])
        for m in re.finditer(r"""["'`]((?:https?://[^"'`\s]+)?/?(?:static/data|api)/[^"'`\s]{3,200})["'`]""", jr.text):
            u = m.group(1)
            if "${" in u or "{" in u:
                continue
            urls.add(requests.compat.urljoin(home, u))
    for u in sorted(urls)[:60]:
        rr = get(u, headers={"Accept": "application/json"})
        if rr and ("json" in rr.headers.get("content-type", "") or rr.text.lstrip()[:1] in "{["):
            docs.append((u, rr.content))
            if probe:
                save_probe("tx_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".json", rr.content)
            # second hop: ids inside election index documents
            try:
                j = json.loads(rr.text)
                for _, node in walk_json(j):
                    if isinstance(node, str) and re.search(r"\.json$", node) and node not in urls:
                        u2 = requests.compat.urljoin(u, node)
                        r2 = get(u2)
                        if r2:
                            docs.append((u2, r2.content))
                            if probe:
                                save_probe("tx_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".json", r2.content)
            except Exception:
                pass
    return docs


def adapter_mt(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    docs = []
    for u in [cfg["home"], "https://electionresults.mt.gov/ResultsSW.aspx?type=FED", "https://electionresults.mt.gov/"]:
        r = get(u)
        if r:
            docs.append((u, r.content))
            if probe:
                save_probe("mt_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", r.content)
            for u2 in harvest_links(r.text, u, re.compile(r"type=FED|Representative|Export|Download|\.csv|\.xml|\.json", re.I))[:20]:
                r2 = get(u2)
                if r2 and u2 not in [d[0] for d in docs]:
                    docs.append((u2, r2.content))
                    if probe:
                        save_probe("mt_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".bin", r2.content)
            break
    return docs


ADAPTERS = {"fl": adapter_fl, "mi": adapter_mi, "va": adapter_va, "tx": adapter_tx, "mt": adapter_mt}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="save raw fetched documents to ./probe for inspection")
    ap.add_argument("--dry-run", action="store_true", help="do not write results.json")
    ap.add_argument("--only", help="comma-separated adapter keys to run (e.g. va,tx)")
    args = ap.parse_args()

    cfg = load_json(DATA / "races.json", None)
    if not cfg:
        log("races.json missing or invalid")
        sys.exit(1)
    prev = load_json(DATA / "results.json", {"races": {}})
    overrides = load_json(DATA / "manual_overrides.json", {}).get("overrides", {})

    out_races: dict[str, dict] = {}
    any_ok = False
    errors = 0

    by_adapter: dict[str, list[dict]] = {}
    for race in cfg["races"]:
        by_adapter.setdefault(race["source"]["adapter"], []).append(race)

    for key, races in by_adapter.items():
        if args.only and key not in args.only.split(","):
            continue
        docs: list[tuple[str, bytes]] = []
        try:
            docs = ADAPTERS[key](cfg["sources"][key], races, args.probe)
        except Exception as e:  # adapter bug or network trouble
            log(f"adapter {key} crashed: {e!r}")
        for race in races:
            rid = race["id"]
            prior = prev.get("races", {}).get(rid, {})
            result: RaceResult | None = None
            for url, content in docs:
                try:
                    cand = extract(content, race, url)
                except Exception as e:
                    log(f"extract {rid} from {url} failed: {e!r}")
                    cand = None
                if cand and any(c["votes"] is not None for c in cand.candidates):
                    result = cand
                    break
                if cand and result is None:
                    result = cand  # structure found but no votes yet (pre-close zeros)
            if result is None:
                errors += 1
                rr = RaceResult(**{k: prior.get(k) for k in RaceResult.__dataclass_fields__ if k in prior}) if prior else RaceResult()
                rr.candidates = prior.get("candidates") or [asdict(CandResult(c["name"], c["party"], None, c.get("incumbent", False))) for c in race["candidates"]]
                rr.source_status = "error"
                rr.error = f"Could not read {race['state']} feed ({len(docs)} documents tried). Showing last good data."
                result = rr
            else:
                any_ok = True
                result.source_status = "ok"
                result.source_updated = now_iso()
                result.error = None
                total = sum(c["votes"] or 0 for c in result.candidates)
                result.status = "counting" if total > 0 else "not_started"
                # carry forward a manual call / previous call
                result.called = prior.get("called")
            out_races[rid] = asdict(result)

    # Races whose adapter was skipped with --only keep previous data
    for race in cfg["races"]:
        out_races.setdefault(race["id"], prev.get("races", {}).get(race["id"], asdict(RaceResult())))

    # Manual overrides win
    for rid, ov in overrides.items():
        if rid not in out_races or ov.get("enabled") is False:
            continue
        cur = out_races[rid]
        cur.update({k: v for k, v in ov.items() if k not in ("enabled", "source_note")})
        cur["source_status"] = "manual"
        cur["source_updated"] = now_iso()
        cur["error"] = None
        total = sum((c.get("votes") or 0) for c in cur.get("candidates", []))
        cur["status"] = "called" if cur.get("called") else ("counting" if total > 0 else "not_started")

    for rid, r in out_races.items():
        if r.get("called"):
            r["status"] = "called"
            if not r.get("winner"):
                w = [c for c in r.get("candidates", []) if c.get("party") == r["called"]]
                r["winner"] = w[0]["name"] if w else None

    election_day = datetime.fromisoformat(cfg["election_date"]).date()
    today_utc = datetime.now(timezone.utc).date()
    if any(r["status"] == "counting" or r["status"] == "called" for r in out_races.values()):
        status = "complete" if all(r["status"] == "called" for r in out_races.values()) else "live"
    elif today_utc < election_day:
        status = "pre-election"
    else:
        status = "error" if not any_ok else "live"

    out = {
        "generated_at": now_iso(),
        "election_date": cfg["election_date"],
        "status": status,
        "feed_errors": errors,
        "races": out_races,
    }
    if args.dry_run:
        print(json.dumps(out, indent=2))
    else:
        (DATA / "results.json").write_text(json.dumps(out, indent=2) + "\n")
        log(f"wrote data/results.json status={status} errors={errors}")


if __name__ == "__main__":
    main()
