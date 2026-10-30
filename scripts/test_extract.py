#!/usr/bin/env python3
"""Offline sanity tests for the name-anchored extractor using synthetic pages that
mimic the state sites' shapes (HTML tables and nested JSON)."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import fetch_results as fr

cfg = json.loads((fr.DATA / "races.json").read_text())
R = {r["id"]: r for r in cfg["races"]}

FL_HTML = """<html><body><h2>Representative in Congress, District 25</h2>
<p>Precincts Reporting: 412 of 611 (67.43%)</p>
<table><tr><th>Candidate</th><th>Party</th><th>Votes</th><th>%</th></tr>
<tr><td>Scott Singer</td><td>REP</td><td>101,224</td><td>49.8%</td></tr>
<tr><td>Jared Moskowitz</td><td>DEM</td><td>99,870</td><td>49.1%</td></tr>
<tr><td>Peter Jassenoff</td><td>LPF</td><td>2,201</td><td>1.1%</td></tr></table>
<h2>Representative in Congress, District 26</h2><table><tr><td>Someone Else</td><td>REP</td><td>5</td></tr></table>
</body></html>"""

MI_HTML = """<html><body><div class="office"><h3>4th District Representative in Congress 2 Year Term (1) Position</h3>
<div>Precincts Reporting 220 / 301</div>
<ul><li>Bill Huizenga Republican 143,882 52.1%</li><li>Sean McCann Democratic 131,002 47.4%</li></ul></div></body></html>"""

MT_HTML = """<html><body><table><tr><td colspan=3>UNITED STATES REPRESENTATIVE DISTRICT 1 (WESTERN)</td></tr>
<tr><td>Precincts Reporting</td><td>300 of 356</td></tr>
<tr><td>AARON FLINT</td><td>REP</td><td>152,334</td><td>50.2%</td></tr>
<tr><td>SAM FORSTAG</td><td>DEM</td><td>145,001</td><td>47.8%</td></tr>
<tr><td>NICK SHEEDY</td><td>LIB</td><td>6,120</td><td>2.0%</td></tr></table></body></html>"""

# Mirrors the real Enhanced Voting ballot-items response shape (verified Oct 29, 2026)
VA_JSON = {"data": [
  {"name": [{"languageId": "en", "text": "Member, House of Representatives (1st District)"}],
   "reportingStatus": {"reportingUnits": 180, "totalUnits": 212, "asOf": "2026-11-03T01:00:00Z"},
   "summaryResults": {"ballotOptions": [
     {"name": [{"languageId": "en", "text": "Robert J. Wittman"}], "voteCount": 201345},
     {"name": [{"languageId": "en", "text": "Shannon L. Taylor"}], "voteCount": 188902}]}},
  {"name": [{"languageId": "en", "text": "Member, House of Representatives (2nd District)"}],
   "reportingStatus": {"reportingUnits": 1, "totalUnits": 8},
   "summaryResults": {"ballotOptions": [{"name": [{"languageId": "en", "text": "Jennifer Kiggans"}], "voteCount": 1}]}}],
  "totalRecordCount": 2}

TX_JSON = {"races": [
  {"officeName": "U. S. Representative District 35", "precinctsReported": 500, "precinctsTotal": 612,
   "candidates": [{"fullName": "Carlos De La Cruz", "party": "REP", "totalVotes": "88,120"},
                  {"fullName": "Johnny Garcia", "party": "DEM", "totalVotes": "84,993"}]},
  {"officeName": "U. S. Representative District 15", "precinctsReported": 10, "precinctsTotal": 400,
   "candidates": [{"fullName": "Monica De La Cruz", "party": "REP", "totalVotes": 9000},
                  {"fullName": "Bobby Pulido", "party": "DEM", "totalVotes": 9100}]}]}

def run(label, content, race):
    res = fr.extract(content.encode() if isinstance(content, str) else json.dumps(content).encode(), race, "test://" + label)
    print(label, "->", None if res is None else {"votes": [(c["name"], c["votes"]) for c in res.candidates],
          "precincts": (res.precincts_reporting, res.precincts_total, res.pct_reported)})
    return res

ok = True
r = run("FL-25", FL_HTML, R["FL-25"]); ok &= bool(r and r.candidates[0]["votes"] == 99870 and r.candidates[1]["votes"] == 101224 and r.precincts_total == 611)
r = run("MI-04", MI_HTML, R["MI-04"]); ok &= bool(r and r.candidates[0]["votes"] == 143882 and r.precincts_reporting == 220)
r = run("MT-01", MT_HTML, R["MT-01"]); ok &= bool(r and r.candidates[1]["votes"] == 145001 and r.precincts_total == 356)
r = run("VA-01", VA_JSON, R["VA-01"]); ok &= bool(r and r.candidates[0]["votes"] == 201345 and r.precincts_total == 212)
r = run("TX-35", TX_JSON, R["TX-35"]); ok &= bool(r and r.candidates[0]["votes"] == 88120 and r.candidates[1]["votes"] == 84993)
r = run("TX-15", TX_JSON, R["TX-15"]); ok &= bool(r and r.candidates[1]["votes"] == 9100)
r = run("TX-01 (absent)", TX_JSON, R["TX-01"]); ok &= (r is None)
print("ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
