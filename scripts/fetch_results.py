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
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/17.6 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
}
TIMEOUT = 20
SESSION = requests.Session()
SESSION.headers.update(UA)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


FETCH_LOG: list[str] = []


def get(url: str, **kw) -> requests.Response | None:
    try:
        r = SESSION.get(url, timeout=TIMEOUT, **kw)
        line = f"GET {url} -> {r.status_code} ({len(r.content)} bytes) {r.headers.get('content-type','')}"
        log(line)
        FETCH_LOG.append(line)
        if r.status_code == 200:
            return r
    except requests.RequestException as e:
        log(f"GET {url} failed: {e}")
        FETCH_LOG.append(f"GET {url} -> EXC {e}")
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
    re.compile(r"Precincts Fully:\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"Polling Locations Reporting:?\s*(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)", re.I),
    re.compile(r"Election Districts Reporting:?\s*(\d[\d,]*)\s*of\s*(\d[\d,]*)", re.I),
    re.compile(r"Counties:\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)\s*(?:precincts|polling (?:places|locations)|vote centers)", re.I),
    re.compile(r"precincts?\s*(?:reporting|reported|complete)[^0-9]{0,40}(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)", re.I),
    re.compile(r"(\d[\d,]*)\s*(?:of|/)\s*(\d[\d,]*)\s*(?:precincts|counties)?\s*reporting", re.I),
]
PCT_REPORTED_RES = [
    re.compile(r"Polling Locations Reporting:?\s*(\d{1,3}(?:\.\d+)?)\s*%", re.I),
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
    if cand.get("match"):
        return re.search(cand["match"], text, re.I) is not None
    t = re.sub(r"[^a-z]", "", text.lower())
    first, last = name_tokens(cand["name"])
    if not last or last not in t:
        return False
    # Always require the first-name prefix too (two De La Cruzes are on the Texas ballot).
    return first[:3] in t


def to_int(s: str) -> int | None:
    try:
        return int(s.replace(",", ""))
    except Exception:
        return None


def parse_precincts(text: str, last: bool = False) -> tuple[int | None, int | None, float | None]:
    """Parse 'X of Y precincts' / 'NN% reporting'. With last=True, take the final match (used for
    the text window preceding a contest, where the nearest statement is the right one)."""
    def pick(rx):
        ms = list(rx.finditer(text))
        if not ms:
            return None
        return ms[-1] if last else ms[0]
    for rx in PRECINCT_RES:
        m = pick(rx)
        if m:
            a, b = to_int(m.group(1)), to_int(m.group(2))
            if a is not None and b:
                return a, b, round(a / b * 100, 1)
    for rx in PCT_REPORTED_RES:
        m = pick(rx)
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
    # "Other": contest total votes (if the block states one) minus the tracked candidates
    try:
        blk = container.get_text(" ", strip=True) if container is not soup else ""
        after = " ".join(str(x) for x in found[-1].find_all_next(string=True, limit=40))
        m = re.search(r"(?:Total(?:\s+Votes)?|total votes)\s*:?\s*(\d[\d,]*)", after, re.I) or re.search(r"(?:Total(?:\s+Votes)?|total votes)\s*:?\s*(\d[\d,]*)", blk, re.I)
        if m and all(c["votes"] is not None for c in out):
            tot = to_int(m.group(1)) or 0
            matched = sum(c["votes"] for c in out)
            # plausibility: minor candidates/write-ins never exceed a third of the major vote
            if tot >= matched and (matched == 0 or tot <= matched * 1.34):
                out.append(asdict(CandResult("Other / write-in", "OTH", tot - matched, False)))
    except Exception:
        pass
    res.candidates = out
    # Reporting status: look first in the text immediately preceding the first candidate row
    # (between the contest heading and the candidates), then the contest container, then the page.
    near = " ".join(reversed([str(x) for x in found[0].find_all_previous(string=True, limit=80)]))
    res.precincts_reporting, res.precincts_total, res.pct_reported = parse_precincts(near[-1500:], last=True)
    ctext = container.get_text(" ", strip=True) if container is not soup else soup.get_text(" ", strip=True)
    if res.pct_reported is None:
        res.precincts_reporting, res.precincts_total, res.pct_reported = parse_precincts(ctext)
    if res.pct_reported is None and container is not soup:
        # reporting status is often in a page header outside the contest block
        res.precincts_reporting, res.precincts_total, res.pct_reported = parse_precincts(soup.get_text(" ", strip=True))
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


# priority order: per-candidate keys first; contest-level "total*" keys last
VOTE_KEYS = ("votecount", "vcount", "vote_count", "votes", "tally", "ballots", "v", "count", "total_votes", "totalvotes", "total")


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
                keymap = {k.lower().replace("_", ""): k for k in node.keys()}
                for pk in VOTE_KEYS:
                    k = keymap.get(pk.replace("_", ""))
                    if k is not None and isinstance(node[k], (int, float, str)):
                        iv = to_int(str(node[k]).split(".")[0])
                        if iv is not None:
                            votes = iv
                            break
                if votes is not None:
                    break
        out.append(asdict(CandResult(c["name"], c["party"], votes, c.get("incumbent", False))))
    # "Other": every vote-bearing candidate dict in the contest that was not matched
    matched_total = sum((c["votes"] or 0) for c in out)
    all_total = 0
    for _, node in walk_json(contest):
        if isinstance(node, dict) and any(isinstance(v, str) and re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", v) for k, v in node.items() if k.lower() in ("name", "primarycandidate", "candidate", "candidatename", "fullname", "n", "choice")):
            keymap = {k.lower().replace("_", ""): k for k in node.keys()}
            for pk in VOTE_KEYS:
                k = keymap.get(pk.replace("_", ""))
                if k is not None and isinstance(node[k], (int, float, str)):
                    iv = to_int(str(node[k]).split(".")[0])
                    if iv is not None:
                        all_total += iv
                        break
    if all_total >= matched_total and (matched_total == 0 or all_total <= matched_total * 1.34) and all(c["votes"] is not None for c in out):
        out.append(asdict(CandResult("Other / write-in", "OTH", all_total - matched_total, False)))
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
            doc = json.loads(text)
            if race["source"]["adapter"] in ("va", "ga"):
                res = parse_va_ballot_items(doc, race, rx)
            elif race["source"]["adapter"] == "ia":
                res = parse_clarity_summary(doc, race, rx)
            elif race["source"]["adapter"] in ("tx", "nc"):
                res = parse_tx_office_summary(doc, race, rx)
            if res is None:
                res = extract_from_json(doc, race, rx)
        except json.JSONDecodeError:
            res = None
    if res is None and race["source"]["adapter"] == "tx" and "electionresults.sos.state.tx.us" in source_url:
        res = parse_tx_html(text, race, rx)
        if res is None:
            return None  # never fall back to the fuzzy extractor on this page
    if res is None and "apnews.com/projects/elections-2026" in source_url:
        res = parse_ap_page(text, race, rx)
        if res is None:
            return None  # structured AP parser only; never guess on these pages
    if res is None and "<" in stripped[:200]:
        res = extract_from_html(text, race, rx)
    if res is not None and race["source"]["adapter"] == "oh" and (not res.precincts_total):
        try:
            stats = {x.get("name"): x.get("value") for x in json.loads(text).get("contestStatistics", {}).get("allStatistics", [])}
            rep = to_int(str(stats.get("Election Day Precincts Reporting", "")).split(" ")[0]) 
            tot = to_int(str(stats.get("Total Number of Election Day Precincts", "")))
            if tot:
                res.precincts_reporting, res.precincts_total, res.pct_reported = rep, tot, round((rep or 0) / tot * 100, 1)
        except Exception:
            pass
    if res is not None:
        res.source_url = source_url
    return res



# --------------------------------------------------------------------------- #
# Virginia (Enhanced Voting ENR) — structured parser
# --------------------------------------------------------------------------- #
VA_BALLOT_ITEMS = "https://enr.elections.virginia.gov/results/public/api/elections/virginia/2026-November-General/ballot-items"


def _en(name_list) -> str:
    if isinstance(name_list, list):
        for x in name_list:
            if isinstance(x, dict) and x.get("languageId") == "en":
                return x.get("text", "")
        return name_list[0].get("text", "") if name_list and isinstance(name_list[0], dict) else ""
    return str(name_list or "")


def parse_va_ballot_items(doc: dict, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    items = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return None
    for it in items:
        title = _en(it.get("name"))
        if not contest_rx.search(title):
            continue
        res = RaceResult()
        opts = ((it.get("summaryResults") or {}).get("ballotOptions")) or []
        out = []
        used = set()
        for c in race["candidates"]:
            votes = None
            for i, o in enumerate(opts):
                if i not in used and text_matches_candidate(_en(o.get("name")), c):
                    votes = to_int(str(o.get("voteCount", "")))
                    used.add(i)
                    break
            out.append(asdict(CandResult(c["name"], c["party"], votes, c.get("incumbent", False))))
        other = sum((to_int(str(o.get("voteCount", "0"))) or 0) for i, o in enumerate(opts) if i not in used)
        out.append(asdict(CandResult("Other / write-in", "OTH", other, False)))
        res.candidates = out
        rs = it.get("reportingStatus") or {}
        res.precincts_reporting = rs.get("reportingUnits")
        res.precincts_total = rs.get("totalUnits")
        if res.precincts_total:
            res.pct_reported = round((res.precincts_reporting or 0) / res.precincts_total * 100, 1)
        res.source_updated = rs.get("asOf")
        return res
    return None


# --------------------------------------------------------------------------- #
# Texas (results.texas-election.com OfficeSummary.json) — structured parser
# --------------------------------------------------------------------------- #
def parse_tx_office_summary(doc: Any, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    offices = doc.get("OS") if isinstance(doc, dict) else None
    if not isinstance(offices, list):
        return None
    for off in offices:
        name = str(off.get("ON", ""))
        if not contest_rx.search(name):
            continue
        res = RaceResult()
        out = []
        used = set()
        opts = off.get("C") or []
        for c in race["candidates"]:
            votes = None
            for i, o in enumerate(opts):
                if i not in used and text_matches_candidate(str(o.get("N", "")), c):
                    votes = to_int(str(o.get("T", "")))
                    used.add(i)
                    break
            out.append(asdict(CandResult(c["name"], c["party"], votes, c.get("incumbent", False))))
        other = sum((to_int(str(o.get("T", "0"))) or 0) for i, o in enumerate(opts) if i not in used)
        out.append(asdict(CandResult("Other / write-in", "OTH", other, False)))
        res.candidates = out
        # precincts, if the summary carries them under any of the usual keys
        for k in ("PR", "PrecinctsReporting", "PRC"):
            if k in off and "PT" in off:
                res.precincts_reporting, res.precincts_total = to_int(str(off[k])), to_int(str(off["PT"]))
        if res.precincts_total:
            res.pct_reported = round((res.precincts_reporting or 0) / res.precincts_total * 100, 1)
        return res
    return None


# --------------------------------------------------------------------------- #
# Texas SOS static results page (electionresults.sos.state.tx.us/results.html) — strict parser.
# Structure: <div class="election">CONTEST</div><div class="polling">Polling Locations Reporting: N%</div>
#            <table class="election"> rows of <td class="party">..<td class="candidate">..<td class="ballot">N or empty
# --------------------------------------------------------------------------- #
def parse_tx_html(html: str, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    if "November 3, 2026" not in html:
        return None
    blocks = re.split(r'<div class="election">', html)
    for blk in blocks[1:]:
        title = re.sub(r"<.*?>", "", blk.split("</div>", 1)[0]).strip()
        if not contest_rx.search(title):
            continue
        m = re.search(r"Polling Locations Reporting:?\s*([\d.]+)\s*%", blk)
        pct = float(m.group(1)) if m else None
        rows = re.findall(r'<td class="party">(.*?)</td>\s*<td class="candidate">(.*?)</td>\s*<td class="ballot"\s*(?:/>|>(.*?)</td>)', blk, re.S)
        opts = []
        for party, cand, ballots in rows:
            cand = re.sub(r"<.*?>", "", cand).strip()
            v = to_int(re.sub(r"[^\d,]", "", ballots or "")) if (ballots or "").strip() else None
            opts.append((cand, party.strip(), v))
        if not opts:
            return None
        res = RaceResult()
        out, used = [], set()
        for c in race["candidates"]:
            votes = None
            for i, (n, p, v) in enumerate(opts):
                if i not in used and text_matches_candidate(n, c):
                    votes = v if v is not None else (0 if pct == 0 else None)
                    used.add(i); break
            out.append(asdict(CandResult(c["name"], c["party"], votes, c.get("incumbent", False))))
        other = sum((v or 0) for i, (n, p, v) in enumerate(opts) if i not in used)
        out.append(asdict(CandResult("Other / write-in", "OTH", other, False)))
        res.candidates = out
        res.pct_reported = pct
        return res
    return None

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
    links = harvest_links(r.text, home, re.compile(r"congress|U\.?S\.? ?Rep|Governor|StateOffices|ContestResults|Download|Export|\.txt|\.json", re.I))
    for u in links[:40]:
        rr = get(u)
        if rr:
            docs.append((u, rr.content))
            if probe:
                save_probe("fl_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", rr.content)
            # second hop: contest links inside the congressional index
            for u2 in harvest_links(rr.text, u, re.compile(r"ContestResults|District\s*25|Governor", re.I))[:20]:
                if u2 not in [d[0] for d in docs]:
                    r2 = get(u2)
                    if r2:
                        docs.append((u2, r2.content))
                        if probe:
                            save_probe("fl_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".html", r2.content)
    return docs


def adapter_mi(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """Michigan: the state links Nov-2026 results to the MVIC vote-history site, which loads
    statewide county-level results from /VoteHistory/GetCountyVoteRecords?electionId=N.
    The election id is discovered from the ElectionDateId <select>."""
    docs = []
    base = "https://mvic.sos.state.mi.us"
    idx = get(f"{base}/votehistory/Index?type=C&electionDate=11-3-2026")
    ids = []
    if idx:
        if probe:
            save_probe("mi_mvic_index.html", idx.content)
        soup = BeautifulSoup(idx.text, "lxml")
        sel = soup.find("select", id="ElectionDateId") or soup.find("select", attrs={"name": "ElectionDateId"})
        if sel:
            for o in sel.find_all("option"):
                txt = o.get_text(" ", strip=True)
                if re.search(r"11/0?3/2026", txt):
                    ids.append(o.get("value"))
            probe_only = False
            if probe and not ids:
                # validate the parser against the most recent election available
                first = [o.get("value") for o in sel.find_all("option") if (o.get("value") or "0") != "0"]
                ids = first[:1]
                probe_only = True
                log(f"MI: Nov 3 2026 not listed yet; probing with election id {ids}")
            if not ids:
                log("MI: Nov 3 2026 election not listed on MVIC yet")
    for eid in ids:
        u = f"{base}/VoteHistory/GetCountyVoteRecords?electionId={eid}"
        r = get(u, headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/votehistory/"})
        if r:
            docs.append((u + ("#probe-only" if probe_only else ""), r.content))
            if probe:
                save_probe(f"mi_county_vote_records_{eid}.html", r.content)
    # legacy election-night site (blocked from some networks; kept as a fallback)
    for u in [cfg.get("legacy", "https://mielections.us/election/results/2026GEN_CENR.html")]:
        r = get(u)
        if r:
            docs.append((u, r.content))
            if probe:
                save_probe("mi_legacy_cenr.html", r.content)
    return docs


def adapter_va(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """Virginia uses Enhanced Voting's ENR single-page app. Try the public API routes
    the app uses; also harvest the app bundle for route strings during a probe."""
    docs = []
    slug = "2026-November-General"
    base = "https://enr.elections.virginia.gov"
    api_guesses = [
        VA_BALLOT_ITEMS,
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
    """Texas SOS, Nov 2026: static HTML results page (electionresults.sos.state.tx.us/results.html)
    with one table per contest; fallback to the new Civix ENR app rendered in a browser."""
    docs = []
    for u in ("https://electionresults.sos.state.tx.us/results.html", "https://electionresults.sos.state.tx.us/"):
        r = get(u)
        if r and re.search(r"November 3, 2026", r.text):
            docs.append((u, r.content))
            if probe:
                save_probe("tx_results_html.html", r.content)
            return docs
    # browser fallbacks: the static page via Chromium, then the Civix app
    docs = _browser_fetch(["https://electionresults.sos.state.tx.us/results.html"], probe, "tx", re.compile(r"results"))
    docs = [(u, c) for u, c in docs if b"November 3, 2026" in c]
    if not docs:
        docs = _browser_capture(["https://goelect.txelections.civixapps.com/ivis-enr-ui/races"], probe, "tx")
    return docs


def adapter_mt(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    docs = []
    variants = [
        {},
        {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"},
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", "Referer": "https://sosmt.gov/elections/results/"},
        {"User-Agent": "curl/8.5.0", "Accept": "*/*"},
    ]
    urls = [cfg["home"], "https://electionresults.mt.gov/ResultsSW.aspx?type=FED", "https://electionresults.mt.gov/", "https://electionresults.mt.gov/Default.aspx"]
    for hv in variants:
        for u in urls:
            r = get(u, headers=hv)
            if r:
                docs.append((u, r.content))
                if probe:
                    save_probe("mt_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", r.content)
                for u2 in harvest_links(r.text, u, re.compile(r"type=FED|Representative|Export|Download|\.csv|\.xml|\.json", re.I))[:20]:
                    r2 = get(u2, headers=hv)
                    if r2 and u2 not in [d[0] for d in docs]:
                        docs.append((u2, r2.content))
                        if probe:
                            save_probe("mt_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".bin", r2.content)
                return docs
    # Cloudflare JS challenge blocks plain HTTP clients; render with a headless browser.
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        log(f"MT: playwright unavailable ({e!r})")
        return docs
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA["User-Agent"], locale="en-US", viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            # Discover the live election id from the landing page; fall back to the last
            # election (June 2026 primary) in probe mode so the parser can be validated.
            eids = []
            try:
                page.goto("https://electionresults.mt.gov/", wait_until="domcontentloaded", timeout=45000)
                for _ in range(20):
                    if "Just a moment" not in page.content():
                        break
                    page.wait_for_timeout(1000)
                landing = page.content()
                FETCH_LOG.append(f"PW  landing -> {page.url} ({len(landing)} chars)")
                eids = list(dict.fromkeys(re.findall(r"eid=(\d+)", landing)))
                if probe:
                    save_probe("mt_pw_landing.html", landing)
            except Exception as e:
                FETCH_LOG.append(f"PW  landing EXC {e!r}")
            if not eids and probe:
                eids = ["450002928"]
            for eid in eids[:2]:
                for tmpl in ("https://electionresults.mt.gov/ResultsSW.aspx?type=FED&map=CTY&eid={eid}",
                             "https://electionresults.mt.gov/ResultsSW.aspx?type=FED&eid={eid}",
                             "https://electionresults.mt.gov/ResultsList.aspx?eid={eid}"):
                    u = tmpl.format(eid=eid)
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2500)
                        html = page.content()
                        FETCH_LOG.append(f"PW  {u} -> {page.url} ({len(html)} chars)")
                        is_general = bool(re.search(r"General Election\s*[-–]\s*November 3, 2026|November 3, 2026|11/0?3/2026", html, re.I))
                        if "electionresults.mt.gov" in page.url and "Just a moment" not in html and (is_general or probe):
                            if not is_general:
                                log("MT: page is not the 2026 general (probe only) — not used for results")
                            docs.append((u, html.encode("utf-8")) if is_general else (u + "#probe-only", html.encode("utf-8")))
                            if probe:
                                save_probe("mt_pw_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", html)
                    except Exception as e:
                        FETCH_LOG.append(f"PW  {u} -> EXC {e!r}")
            for u in urls[:2]:
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=45000)
                    # give the challenge up to ~20s to clear
                    for _ in range(20):
                        html = page.content()
                        if "Just a moment" not in html and "Enable JavaScript" not in html:
                            break
                        page.wait_for_timeout(1000)
                    html = page.content()
                    FETCH_LOG.append(f"PW  {u} -> {len(html)} chars; challenge={'Just a moment' in html}")
                    if is_challenge(html) or not re.search(r"November 3, 2026|11/0?3/2026", html, re.I):
                        FETCH_LOG.append(f"PW  {u} skipped (not the 2026 general)")
                        continue
                    docs.append((u, html.encode("utf-8")))
                    if probe:
                        save_probe("mt_pw_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-80:] + ".html", html)
                    # follow the federal-results tab/link if the landing page isn't it
                    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
                        if re.search(r"type=FED|Federal|Representative", a.get_text(" ", strip=True) + a["href"], re.I):
                            u2 = requests.compat.urljoin(u, a["href"])
                            if u2 in [d[0] for d in docs]:
                                continue
                            page.goto(u2, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(2500)
                            h2 = page.content()
                            docs.append((u2, h2.encode("utf-8")))
                            if probe:
                                save_probe("mt_pw_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-80:] + ".html", h2)
                            break
                    break
                except Exception as e:
                    FETCH_LOG.append(f"PW  {u} -> EXC {e!r}")
            browser.close()
    except Exception as e:
        log(f"MT playwright failed: {e!r}")
    return docs


def adapter_nc(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """North Carolina: NCSBE re-uploads a precinct-level TSV zip every ~5 minutes on election night.
    Aggregate it into the OfficeSummary-style JSON the TX parser already understands."""
    import csv, io, zipfile
    docs = []
    url = "https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/2026_11_03/results_pct_20261103.zip"
    r = get(url, headers={"Accept": "*/*"})
    if not r:
        return docs
    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = [n for n in zf.namelist() if n.lower().endswith((".txt", ".tsv", ".csv"))][0]
        raw = zf.read(name).decode("utf-8", errors="replace")
    except Exception as e:
        log(f"NC: zip/tsv read failed: {e!r}")
        return docs
    if probe:
        save_probe("nc_results_pct_head.txt", "\n".join(raw.splitlines()[:200]))
    rdr = csv.DictReader(io.StringIO(raw), delimiter="\t")
    cols = {c.lower().strip(): c for c in (rdr.fieldnames or [])}
    def col(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        return None
    c_contest, c_choice, c_party, c_total, c_pct, c_real = (
        col("Contest Name", "contest_name"), col("Choice", "choice", "name_on_ballot"), col("Choice Party", "choice_party", "party_cd"),
        col("Total Votes", "total_votes", "ballot_count"), col("Precinct", "precinct"), col("Real Precinct", "real_precinct"))
    if not (c_contest and c_choice and c_total):
        log(f"NC: unexpected columns {rdr.fieldnames}")
        return docs
    want = [re.compile(rc["source"]["contest_pattern"], re.I) for rc in races]
    agg: dict[str, dict] = {}
    for row in rdr:
        cn = row.get(c_contest, "")
        if not any(w.search(cn) for w in want):
            continue
        a = agg.setdefault(cn, {"C": {}, "P_all": set(), "P_rep": set()})
        ch = row.get(c_choice, "").strip()
        v = to_int((row.get(c_total) or "0").strip() or "0") or 0
        ent = a["C"].setdefault(ch, {"N": ch, "P": (row.get(c_party) or "").strip(), "T": 0})
        ent["T"] += v
        if c_pct and (not c_real or str(row.get(c_real, "")).strip().upper() in ("Y", "TRUE", "1", "")):
            pk = (row.get("County") or row.get("county") or "") + "|" + row.get(c_pct, "")
            a["P_all"].add(pk)
            if v > 0:
                a["P_rep"].add(pk)
    out = {"OS": []}
    for cn, a in agg.items():
        out["OS"].append({"ON": cn, "C": list(a["C"].values()), "PR": len(a["P_rep"]), "PT": len(a["P_all"]),
                          "note": "precincts counted = precincts with any votes for this contest (approximation)"})
    payload = json.dumps(out).encode()
    docs.append((url, payload))
    if probe:
        save_probe("nc_aggregated.json", payload)
    return docs



# --------------------------------------------------------------------------- #
# Generic statewide adapter: fetch a few known URLs, harvest result-looking links,
# fall back to a headless browser for JS-rendered or challenge-protected sites.
# --------------------------------------------------------------------------- #
GENERIC_SITES = {
    "mn": {"urls": ["https://electionresults.sos.mn.gov/"],
           "links": r"U\.S\. Senate|scenario=USSenate|ersElectionId=\d+$", "require": r"11/0?3/2026|November 3, 2026"},
    "ks": {"urls": ["https://ent.sos.ks.gov/", "https://ent.sos.ks.gov/kssos_ent.html"],
           "links": r"Senat|General|2026|\.json", "require": r"General|2026", "browser_first": True},
    "ny": {"urls": ["https://nyenr.elections.ny.gov/", "https://nyenr.elections.ny.gov/Governor", "https://results.elections.ny.gov/"],
           "links": r"Governor|Statewide|Election Districts|Nov.*2026|General Election|2026|\.json", "require": r"2026 General|General Election|Nov(ember)?\.? 0?3, 2026|Election Districts Reporting", "browser_first": True},
}


def is_challenge(html: str) -> bool:
    h = html[:4000].lower()
    return ("<title>just a moment" in h) or ("just a moment..." in h and len(html) < 20000) or (len(html) < 6000 and "enable javascript and cookies" in h)


def _browser_fetch(urls: list[str], probe: bool, tag: str, link_rx: re.Pattern, wait_rx: re.Pattern | None = None) -> list[tuple[str, bytes]]:
    docs = []
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        log(f"{tag}: playwright unavailable ({e!r})")
        return docs
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA["User-Agent"], locale="en-US", viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            seen = set()
            queue = list(urls)
            hops = 0
            while queue and hops < 8:
                u = queue.pop(0)
                if u in seen:
                    continue
                seen.add(u); hops += 1
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=45000)
                    for _ in range(15):
                        html = page.content()
                        if not is_challenge(html):
                            break
                        page.wait_for_timeout(1000)
                    page.wait_for_timeout(2500)
                    if wait_rx is not None:
                        # client-rendered race lists: scroll and wait until the expected names appear
                        for _ in range(20):
                            try:
                                body = page.inner_text("body")
                            except Exception:
                                body = ""
                            if wait_rx.search(body):
                                break
                            page.evaluate("window.scrollBy(0, 1200)")
                            page.wait_for_timeout(1000)
                    html = page.content()
                    FETCH_LOG.append(f"PW  {u} -> {page.url} ({len(html)} chars) challenge={is_challenge(html)}")
                    if is_challenge(html):
                        continue
                    docs.append((page.url, html.encode("utf-8")))
                    if probe:
                        save_probe(f"{tag}_pw_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-70:] + ".html", html)
                    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
                        if link_rx.search(a.get_text(" ", strip=True) + " " + a["href"]):
                            u2 = requests.compat.urljoin(page.url, a["href"])
                            if u2 not in seen and len(queue) < 6:
                                queue.append(u2)
                except Exception as e:
                    FETCH_LOG.append(f"PW  {u} -> EXC {e!r}")
            browser.close()
    except Exception as e:
        log(f"{tag}: playwright failed: {e!r}")
    return docs



def _browser_capture(urls: list[str], probe: bool, tag: str, click_rx: str | None = None) -> list[tuple[str, bytes]]:
    """Load a JS app in a headless browser and keep every JSON response it fetches."""
    docs = []
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        log(f"{tag}: playwright unavailable {e!r}")
        return docs
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA["User-Agent"], locale="en-US")
            page = ctx.new_page()
            seen = {}
            def on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    if "/api/" in resp.url.lower():
                        FETCH_LOG.append(f"PW  api {resp.request.method} {resp.url} -> {resp.status} {ct}")
                    if "json" in ct and resp.status == 200:
                        body = resp.body()
                        if len(body) > 50:
                            seen[resp.url] = body
                except Exception:
                    pass
            page.on("response", on_response)
            for u in urls:
                try:
                    page.goto(u, wait_until="networkidle", timeout=60000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                if click_rx:
                    for txt in [t for t in re.split(r"\|", click_rx) if t]:
                        try:
                            loc = page.get_by_text(re.compile(txt, re.I)).first
                            if loc.count():
                                loc.click(timeout=5000)
                                page.wait_for_timeout(3500)
                        except Exception:
                            pass
                FETCH_LOG.append(f"PW  capture {u} -> {len(seen)} json responses")
            browser.close()
            for u, body in seen.items():
                docs.append((u, body))
                if probe:
                    save_probe(f"{tag}_cap_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-90:] + ".json", body)
    except Exception as e:
        log(f"{tag}: capture failed {e!r}")
    return docs


def adapter_generic(key: str):
    site = GENERIC_SITES[key]
    link_rx = re.compile(site["links"], re.I)

    def run(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
        docs = []
        blocked = False
        req = re.compile(site.get("require", "."), re.I | re.S)
        if site.get("browser_first"):
            docs = _browser_fetch(site["urls"], probe, key, link_rx)
            return [(u, c) for u, c in docs if req.search(c.decode("utf-8", "replace"))] or [(u + "#probe-only", c) for u, c in docs]
        for u in site["urls"]:
            r = get(u)
            if not r:
                blocked = True
                continue
            docs.append((u, r.content))
            if probe:
                save_probe(f"{key}_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-70:] + ".html", r.content)
            for u2 in harvest_links(r.text, u, link_rx)[:12]:
                if u2 in [d[0] for d in docs]:
                    continue
                r2 = get(u2)
                if r2:
                    docs.append((u2, r2.content))
                    if probe:
                        save_probe(f"{key}_" + re.sub(r"[^A-Za-z0-9]+", "_", u2)[-70:] + ".bin", r2.content)
        # If plain HTTP got nothing useful (blocked, or a JS app with no data in the HTML), try the browser.
        thin = all(len(c) < 3000 or b"<app-root" in c or b"id=\"root\"" in c for _, c in docs) if docs else True
        if blocked or thin:
            docs += _browser_fetch(site["urls"][:2], probe, key, link_rx)
        good = [(u, c) for u, c in docs if req.search(c.decode("utf-8", "replace"))]
        if not good:
            log(f"{key}: no document matched the 2026-general check; nothing used for results")
        return good or [(u + "#probe-only", c) for u, c in docs]
    return run


def adapter_manual(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    log("manual adapter: no official feed; waiting for manual_overrides.json")
    return []



# --------------------------------------------------------------------------- #
# Clarity (SOE Software) sites — Iowa. Standard layout: /{ST}/{id}/current_ver.txt
# then /{ST}/{id}/{ver}/json/en/summary.json. Bot check requires a real browser.
# --------------------------------------------------------------------------- #
def adapter_clarity(base: str, state_code: str, label_rx: str):
    def run(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
        docs = []
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as e:
            log(f"clarity {state_code}: playwright unavailable {e!r}")
            return docs
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(user_agent=UA["User-Agent"], locale="en-US")
                page = ctx.new_page()
                page.goto(f"{base}/{state_code}/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
                html = page.content()
                FETCH_LOG.append(f"PW  clarity landing {page.url} ({len(html)} chars)")
                if probe:
                    save_probe(f"{state_code.lower()}_clarity_landing.html", html)
                eid = None
                for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
                    if re.search(label_rx, a.get_text(" ", strip=True), re.I):
                        m = re.search(rf"/{state_code}/(\d+)", a["href"])
                        if m:
                            eid = m.group(1); break
                if not eid:
                    log(f"clarity {state_code}: 2026 general not listed yet")
                    browser.close(); return docs
                def text_of(url):
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500)
                    t = page.evaluate("() => document.body ? document.body.innerText : ''")
                    FETCH_LOG.append(f"PW  {url} -> {len(t)} chars")
                    return t
                ver = ""
                for _try in range(3):
                    ver = text_of(f"{base}/{state_code}/{eid}/current_ver.txt").strip()
                    if re.fullmatch(r"\d+", ver):
                        break
                    FETCH_LOG.append(f"clarity {state_code}: version text {ver[:60]!r}; retrying")
                    page.wait_for_timeout(3000)
                if not re.fullmatch(r"\d+", ver):
                    log(f"clarity {state_code}: bad version {ver[:40]!r}")
                    browser.close(); return docs
                summ = text_of(f"{base}/{state_code}/{eid}/{ver}/json/en/summary.json")
                if summ.lstrip().startswith("["):
                    docs.append((f"{base}/{state_code}/{eid}/{ver}/json/en/summary.json", summ.encode()))
                    if probe:
                        save_probe(f"{state_code.lower()}_clarity_summary.json", summ)
                browser.close()
        except Exception as e:
            log(f"clarity {state_code}: failed {e!r}")
        return docs
    return run


def parse_clarity_summary(doc: Any, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    """Clarity summary.json: [{"K": contest, "C": [names], "V": [votes], "P": precincts reporting %, ...}]"""
    if not isinstance(doc, list):
        return None
    for item in doc:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get(k, "")) for k in ("C", "K", "SUBCAT") if not isinstance(item.get(k), list))
        if not contest_rx.search(title):
            continue
        names, votes = item.get("CH") or item.get("C") or [], item.get("V") or []
        if not isinstance(names, list):
            continue
        res = RaceResult()
        out = []
        used = set()
        for c in race["candidates"]:
            v = None
            for i, (n, vv) in enumerate(zip(names, votes)):
                if i not in used and text_matches_candidate(str(n), c):
                    v = to_int(str(vv)); used.add(i); break
            out.append(asdict(CandResult(c["name"], c["party"], v, c.get("incumbent", False))))
        other = sum((to_int(str(vv)) or 0) for i, (n, vv) in enumerate(zip(names, votes)) if i not in used)
        out.append(asdict(CandResult("Other / write-in", "OTH", other, False)))
        res.candidates = out
        pr, tp = item.get("PR"), item.get("TP")
        if isinstance(pr, (int, float)) and isinstance(tp, (int, float)) and tp:
            res.precincts_reporting, res.precincts_total = int(pr), int(tp)
            res.pct_reported = round(pr / tp * 100, 1)
        return res
    return None


# --------------------------------------------------------------------------- #
# Enhanced Voting sites (Georgia uses the same platform as Virginia)
# --------------------------------------------------------------------------- #
def adapter_enhanced_voting(base: str, jurisdiction: str):
    def run(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
        docs = []
        jh = {"Accept": "application/json"}
        slugs = []
        for u in (f"{base}/results/public/api/elections/{jurisdiction}", f"{base}/results/public/api/jurisdictions/{jurisdiction}/elections",
                  f"{base}/results/public/api/elections?jurisdiction={jurisdiction}"):
            r = get(u, headers=jh)
            if r and "json" in r.headers.get("content-type", ""):
                if probe:
                    save_probe(f"{jurisdiction.lower()}_ev_elections.json", r.content)
                for _, node in walk_json(r.json()):
                    if isinstance(node, dict):
                        slug = node.get("publishPublicElectionId") or node.get("electionPublicId") or node.get("slug") or node.get("id")
                        txt = json.dumps(node)
                        if isinstance(slug, str) and re.search(r"2026", txt) and re.search(r"Nov|General", txt, re.I) and slug not in slugs:
                            slugs.append(slug)
                break
        for guess in ("2026-November-General", "2026NovGen", "2026-General", "2026-Nov-General", "2026GeneralElection"):
            if guess not in slugs:
                slugs.append(guess)
        for slug in slugs[:6]:
            u = f"{base}/results/public/api/elections/{jurisdiction}/{slug}/ballot-items"
            r = get(u, headers=jh)
            if r and "json" in r.headers.get("content-type", ""):
                docs.append((u, r.content))
                if probe:
                    save_probe(f"{jurisdiction.lower()}_ev_{slug}_ballot_items.json", r.content)
                break
        if probe and not docs:
            r = get(f"{base}/results/public/{jurisdiction}")
            if r:
                save_probe(f"{jurisdiction.lower()}_ev_directory.html", r.content)
        return docs
    return run


def adapter_oh(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """Ohio Decides (liveresults.ohiosos.gov): Cloudflare-fronted JS app with a JSON API.
    Pass the challenge once in a browser, then read the contest endpoints directly."""
    docs = []
    base = "https://liveresults.ohiosos.gov"
    targets = {"U.S. Senator": "U.S.%20Senator", "Governor and Lieutenant Governor": "Governor%20and%20Lieutenant%20Governor"}
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA["User-Agent"], locale="en-US")
            page = ctx.new_page()
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):
                if not is_challenge(page.content()):
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)
            for label, enc in targets.items():
                u = f"{base}/Api/v1/Contest/GetContestDetailsObject?contestName={enc}&contestObjectId=&resultsLevel=&countyId="
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(800)
                    t = page.evaluate("() => document.body ? document.body.innerText : ''")
                    FETCH_LOG.append(f"PW  {u} -> {len(t)} chars")
                    if t.lstrip().startswith("{"):
                        docs.append((u, t.encode()))
                        if probe:
                            save_probe("oh_" + re.sub(r"[^A-Za-z0-9]+", "_", label) + ".json", t)
                except Exception as e:
                    FETCH_LOG.append(f"PW  {u} -> EXC {e!r}")
            browser.close()
    except Exception as e:
        log(f"OH: browser failed {e!r}; falling back to capture")
        docs = _browser_capture([base + "/"], probe, "oh", click_rx="U\\.S\\. Congress|U\\.S\\. Senator|Statewide Offices|Governor and Lieutenant Governor")
    return docs


def adapter_ga(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    docs = []
    base = "https://results.sos.ga.gov"
    r = get(f"{base}/results/public/api/jurisdictions/Georgia", headers={"Accept": "application/json"})
    if r and "json" in r.headers.get("content-type", ""):
        try:
            els = r.json().get("elections") or []
        except Exception:
            els = []
        FETCH_LOG.append("GA elections: " + ", ".join(f"{e.get('publicElectionId')}@{e.get('electionDate')}" for e in els[:8]))
        for e in els:
            if str(e.get("electionDate", "")).startswith("2026-11-03"):
                slug = e.get("publicElectionId")
                u = f"{base}/results/public/api/elections/Georgia/{slug}/ballot-items"
                rr = get(u, headers={"Accept": "application/json"})
                if rr and "json" in rr.headers.get("content-type", ""):
                    docs.append((u, rr.content))
                    if probe:
                        save_probe(f"ga_ev_{slug}_ballot_items.json", rr.content)
                    return docs
        log("GA: Nov 3 2026 election not listed on results.sos.ga.gov yet")
        return docs
    docs = adapter_enhanced_voting(base, "Georgia")(cfg, races, probe)
    if docs:
        return docs
    # discover the election slug from the API calls the directory app makes
    caps = _browser_capture(["https://results.sos.ga.gov/results/public/Georgia"], probe, "ga")
    slugs = []
    for u, body in caps:
        try:
            for _, node in walk_json(json.loads(body)):
                if isinstance(node, dict):
                    slug = node.get("publishPublicElectionId") or node.get("electionPublicId")
                    txt = json.dumps(node)
                    if isinstance(slug, str) and "2026" in txt and re.search(r"Nov|General", txt, re.I) and slug not in slugs:
                        slugs.append(slug)
        except Exception:
            pass
    for m in re.finditer(r"elections/([A-Za-z0-9\-_]+)", " ".join(u for u, _ in caps)):
        if m.group(1) not in slugs and "2026" in m.group(1):
            slugs.append(m.group(1))
    FETCH_LOG.append(f"GA slugs discovered: {slugs}")
    for slug in slugs[:4]:
        u = f"https://results.sos.ga.gov/results/public/api/elections/Georgia/{slug}/ballot-items"
        r = get(u, headers={"Accept": "application/json"})
        if r and "json" in r.headers.get("content-type", ""):
            docs.append((u, r.content))
            if probe:
                save_probe(f"ga_ev_{slug}_ballot_items.json", r.content)
            break
    return docs


def adapter_ak(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
    """Alaska DOE election-night app (elections.alaska.gov/enr/<ELECTION>/): a jQuery page with a
    contest <select>; results are fetched by includes/map.js. Render in a browser, choose the
    contest, and keep both the rendered HTML and any data files the page fetched."""
    docs = []
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA["User-Agent"], locale="en-US", ignore_https_errors=True)
            page = ctx.new_page()
            seen = {}
            def on_response(resp):
                try:
                    u = resp.url
                    if "elections.alaska.gov" in u and resp.status == 200 and re.search(r"\.(json|csv|txt|xml|htm|html)(\?|$)|results|data", u, re.I) and not re.search(r"\.(js|css|png|jpg|gif|svg|woff2?)(\?|$)", u, re.I):
                        body = resp.body()
                        if 50 < len(body) < 20_000_000:
                            seen[u] = body
                except Exception:
                    pass
            page.on("response", on_response)
            for u in ("https://www.elections.alaska.gov/enr/26GENR/", "https://www.elections.alaska.gov/enr26/", "https://www.elections.alaska.gov/enr/"):
                try:
                    page.goto(u, wait_until="networkidle", timeout=60000)
                except Exception:
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        FETCH_LOG.append(f"PW  {u} -> EXC {e!r}"); continue
                page.wait_for_timeout(2500)
                FETCH_LOG.append(f"PW  AK {u} -> {page.url}")
                try:
                    sel = page.locator("select#contest")
                    if sel.count():
                        sel.select_option(label="U.S. Senator")
                        page.wait_for_timeout(3500)
                except Exception as e:
                    FETCH_LOG.append(f"PW  AK select failed {e!r}")
                html = page.content()
                is_general = bool(re.search(r"GENERAL ELECTION|November 3, 2026|26GENR", html + page.url, re.I))
                tag = "" if is_general else "#probe-only"
                docs.append((page.url + tag, html.encode("utf-8")))
                if probe:
                    save_probe("ak_enr_" + re.sub(r"[^A-Za-z0-9]+", "_", page.url)[-60:] + ".html", html)
                if "26PRIM" in page.url and not probe:
                    FETCH_LOG.append("AK: ENR still on the August primary; general not posted yet")
                break
            browser.close()
            for u, body in seen.items():
                FETCH_LOG.append(f"PW  AK data {u} ({len(body)} bytes)")
                is_general = "26GENR" in u or b"GENERAL" in body[:5000].upper()
                docs.append((u + ("" if is_general else "#probe-only"), body))
                if probe:
                    save_probe("ak_data_" + re.sub(r"[^A-Za-z0-9]+", "_", u)[-70:] + ".bin", body)
    except Exception as e:
        log(f"AK: browser failed {e!r}")
    return docs



# --------------------------------------------------------------------------- #
# AP-powered news-page fallback (personal use) for states without a usable official live feed.
# Renders the page in a browser and lets the name-anchored HTML extractor read candidate rows.
# --------------------------------------------------------------------------- #
AP_PAGES = {
    "mi": ["https://apnews.com/projects/elections-2026/michigan-us-senate-results/", "https://apnews.com/projects/elections-2026/michigan-us-house-results/#4"],
    "me": ["https://apnews.com/projects/elections-2026/maine-us-senate-results/"],
    "wi": ["https://apnews.com/projects/elections-2026/wisconsin-governor-results/"],
    "ks": ["https://apnews.com/projects/elections-2026/kansas-us-senate-results/"],
    "oh": ["https://apnews.com/projects/elections-2026/ohio-us-senate-results/", "https://apnews.com/projects/elections-2026/ohio-governor-results/"],
    "ia": ["https://apnews.com/projects/elections-2026/iowa-us-senate-results/"],
}
AP_LINK_RX = {}  # no link-following: the race pages are addressed directly


AP_ROW_RX = re.compile(r"\s*(?P<name>.+?)\s+(?P<party>Dem|GOP|Lib|Grn|Ind|UST|WFP|Con|NPA|Oth|[A-Z][A-Za-z]{1,3})\s+(?P<votes>\d[\d,]*)\s+(?P<pct>\d+(?:\.\d+)?)%")
AP_OTHER_RX = re.compile(r"\s*Other candidates\s+(?P<votes>\d[\d,]*)\s+(?P<pct>\d+(?:\.\d+)?)%")


def parse_ap_page(html: str, race: dict, contest_rx: re.Pattern) -> RaceResult | None:
    """AP results page (apnews.com/projects/elections-2026/<state>-<office>-results/). The rendered
    text reads '... estimates N% of votes counted candidate votes pct <Name> <Party> <votes> <pct>% ...
    Updated ...'. The statewide block comes first; county blocks follow. On a House page every
    district has its own block, so we take the first block whose rows name both major candidates."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True).replace("\u2011", "-").replace("\u2010", "-").replace("\u00a0", " ")
    if not contest_rx.search(text):
        return None
    cands = race["candidates"]
    majors = [c for c in cands if c["party"] in ("REP", "DEM")]
    blocks = list(re.finditer(r"candidate\s+votes\s+pct(.*?)(?=Updated\s+\w{3}\s+\d|candidate\s+votes\s+pct|$)", text, re.S))
    for i, m in enumerate(blocks):
        body = m.group(1)
        rows, other = [], None
        pos = 0
        while pos < len(body):
            mo = AP_OTHER_RX.match(body, pos)
            if mo:
                other = to_int(mo.group("votes")); pos = mo.end(); continue
            mr = AP_ROW_RX.match(body, pos)
            if not mr:
                break
            rows.append((mr.group("name").strip(), mr.group("party"), to_int(mr.group("votes"))))
            pos = mr.end()
        if not rows:
            continue
        if not all(any(text_matches_candidate(n, c) for n, _, _ in rows) for c in majors):
            continue
        res = RaceResult()
        out, used = [], set()
        for c in cands:
            v = None
            for j, (n, _, votes) in enumerate(rows):
                if j not in used and text_matches_candidate(n, c):
                    v = votes; used.add(j); break
            out.append(asdict(CandResult(c["name"], c["party"], v, c.get("incumbent", False))))
        rest = sum((v or 0) for j, (_, _, v) in enumerate(rows) if j not in used) + (other or 0)
        out.append(asdict(CandResult("Other / write-in", "OTH", rest, False)))
        res.candidates = out
        pre = text[max(0, m.start() - 200):m.start()]
        pm = re.search(r"(\d{1,3})%\s*of\s*votes\s*counted", pre)
        if pm:
            res.pct_reported = float(pm.group(1))
        return res
    return None


def adapter_ap(state: str, primary=None):
    def run(cfg: dict, races: list[dict], probe: bool) -> list[tuple[str, bytes]]:
        docs = primary(cfg, races, probe) if primary else []
        # if the official adapter produced something usable, keep it; otherwise try AP
        # usable = a non-probe document that actually mentions a tracked candidate
        surnames = sorted({name_tokens(c["name"])[1] for r in races for c in r["candidates"] if c["party"] in ("REP", "DEM")})
        name_rx = re.compile("|".join(surnames), re.I) if surnames else re.compile(r"$^")
        usable = [d for d in docs if not d[0].endswith("#probe-only") and name_rx.search(d[1].decode("utf-8", "replace"))]
        if usable:
            return docs
        wait_rx = name_rx if surnames else None
        ap_docs = []
        for u in AP_PAGES.get(state, []):
            ap_docs += _browser_fetch([u], probe, f"ap_{state}", re.compile(AP_LINK_RX.get(state, r"$^"), re.I), wait_rx)
        for u, c in ap_docs:
            if re.search(r"2026", c.decode("utf-8", "replace")):
                docs.append((u, c))
        return docs
    return run


ADAPTERS = {"fl": adapter_fl, "mi": adapter_ap("mi", adapter_mi), "va": adapter_va, "tx": adapter_tx, "mt": adapter_mt, "nc": adapter_nc,
            "manual": adapter_manual, **{k: adapter_generic(k) for k in GENERIC_SITES},
            "me": adapter_ap("me"), "wi": adapter_ap("wi"), "ks": adapter_ap("ks", adapter_generic("ks")),
            "ia": adapter_ap("ia", adapter_clarity("https://electionresults.iowa.gov", "IA", r"2026 General Election")),
            "ga": adapter_ga, "oh": adapter_ap("oh", adapter_oh), "ak": adapter_ak}


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
    ov_file = load_json(DATA / "manual_overrides.json", {})
    overrides = ov_file.get("overrides", {})
    calls = ov_file.get("calls", {})

    out_races: dict[str, dict] = {}
    any_ok = False
    errors = 0
    election_day = datetime.fromisoformat(cfg["election_date"]).date()
    # Treat anything before 5 PM CT on Election Day (22:00 UTC, CST) as pre-election.
    pre_election = datetime.now(timezone.utc) < datetime(election_day.year, election_day.month, election_day.day, 22, 0, tzinfo=timezone.utc)

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
                if url.endswith("#probe-only"):
                    continue
                try:
                    cand = extract(content, race, url)
                except Exception as e:
                    log(f"extract {rid} from {url} failed: {e!r}")
                    cand = None
                if cand:
                    majors = [c for c in cand.candidates if c["party"] in ("REP", "DEM")]
                    if majors and all(c["votes"] is not None for c in majors):
                        result = cand
                        break
                    log(f"{rid}: partial parse from {url} (missing a major-party candidate) — rejected")
            if result is None:
                errors += 1
                if pre_election:
                    # Nothing real can exist yet — never carry anything forward before Election Day.
                    rr = RaceResult()
                    rr.candidates = [asdict(CandResult(c["name"], c["party"], None, c.get("incumbent", False))) for c in race["candidates"]]
                    rr.source_status = "pending"
                    rr.error = None
                else:
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

    for rid, party in calls.items():
        if rid in out_races and party in ("REP", "DEM"):
            out_races[rid]["called"] = party
    for rid, r in out_races.items():
        if r.get("called"):
            r["status"] = "called"
            if not r.get("winner"):
                w = [c for c in r.get("candidates", []) if c.get("party") == r["called"]]
                r["winner"] = w[0]["name"] if w else None

    if pre_election and not overrides:
        status = "pre-election"
    elif any(r["status"] == "counting" or r["status"] == "called" for r in out_races.values()):
        status = "complete" if all(r["status"] == "called" for r in out_races.values()) else "live"
    else:
        status = "error" if not any_ok else "live"

    out = {
        "generated_at": now_iso(),
        "election_date": cfg["election_date"],
        "status": status,
        "feed_errors": errors,
        "races": out_races,
    }
    if args.probe:
        save_probe("fetch_log.txt", "\n".join(FETCH_LOG))
    if args.dry_run:
        print(json.dumps(out, indent=2))
    else:
        (DATA / "results.json").write_text(json.dumps(out, indent=2) + "\n")
        log(f"wrote data/results.json status={status} errors={errors}")


if __name__ == "__main__":
    main()
