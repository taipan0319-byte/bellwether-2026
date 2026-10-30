# 2026 Bellwether Watch

Election-night dashboard for seven U.S. House races tracked as bellwethers for control of Congress:

| Race | Matchup | Polls close (CT) | Source |
|---|---|---|---|
| FL-25 | Jared Moskowitz (D, inc.) vs. Scott Singer (R) | 6:00 PM | Florida Division of Elections |
| VA-01 | Rob Wittman (R, inc.) vs. Shannon Taylor (D) | 6:00 PM | Virginia Dept. of Elections |
| MI-04 | Bill Huizenga (R, inc.) vs. Sean McCann (D) | 7:00 PM | Michigan SOS |
| TX-01 | Nathaniel Moran (R, inc.) vs. Yolanda Prince (D) | 7:00 PM | Texas SOS |
| TX-15 | Monica De La Cruz (R, inc.) vs. Bobby Pulido (D) | 7:00 PM | Texas SOS |
| TX-35 | Carlos De La Cruz (R) vs. Johnny Garcia (D) — open seat | 7:00 PM | Texas SOS |
| MT-01 | Aaron Flint (R) vs. Sam Forstag (D) — open seat | 9:00 PM | Montana SOS |

**Live dashboard:** `https://taipan0319-byte.github.io/bellwether-2026/` (after Pages is enabled — see setup).

## How it works

```
GitHub Actions (every 5 min on election night)
   └─ scripts/fetch_results.py
        ├─ pulls each state's official election-night site
        ├─ name-anchored parser reads votes next to each candidate (HTML or JSON)
        ├─ applies data/manual_overrides.json  (hand-entered numbers always win)
        └─ commits data/results.json
GitHub Pages serves index.html, which re-reads results.json every 60 s
```

* `data/races.json` — the seven races, candidates, ratings, poll-close times, and the regex that identifies each contest on its state's site.
* `data/results.json` — machine-written. Don't edit by hand; use overrides instead.
* `data/manual_overrides.json` — the election-night safety valve (below).
* `scripts/fetch_results.py` — fetcher/parser. `--probe` saves every raw document to `probe/` for inspection.
* `scripts/test_extract.py` — offline tests of the parser against synthetic pages.

## One-time setup

1. **Enable Pages:** Settings → Pages → Source: *GitHub Actions*. The `pages.yml` workflow deploys on every push to `main`.
2. **Allow Actions to push:** Settings → Actions → General → Workflow permissions → *Read and write permissions*.
3. **Dry-run the fetcher:** Actions → *Update election results* → Run workflow → check *probe*. Download the `probe-*` artifact to see exactly what each state site returned. Before Nov 3 most contests will show zeros or not exist yet — that's expected. The purpose is to confirm the URLs resolve and the contest names match `contest_pattern`.

## Election-night runbook

* The schedule starts polling at 5 PM CT Nov 3 and runs every 5 minutes until 6 AM CT Nov 4, then every 30 minutes through Nov 6. GitHub's cron can lag 5–15 minutes under load; use **Run workflow** to force an update.
* Each race card shows a **Feed** line: `ok`, `error` (last good numbers kept), or `Manual entry`.
* **If a state feed breaks:** open `data/manual_overrides.json` in the GitHub web editor (works on a phone), copy the `_example` block into `overrides`, type in the numbers from AP/NYT/the state site, commit. The next run (or a manual run) will use them. Set `"called": "REP"` or `"DEM"` when a network calls the race — the dashboard never projects on its own.
* To return a race to the automatic feed, set `"enabled": false` or delete its entry.

## Local use

```bash
pip install requests beautifulsoup4 lxml
python3 scripts/test_extract.py            # parser sanity tests
python3 scripts/fetch_results.py --dry-run # fetch without writing
python3 -m http.server 8000                # open http://localhost:8000
```

## Caveats

* Returns are **unofficial** until canvassed. Texas counties post early-vote totals first, then Election Day; Montana counties must post to the state site before releasing elsewhere; Virginia continues to add provisional/same-day ballots for days.
* Texas and Florida are using mid-decade maps adopted in 2025–26 (TX map allowed by the Supreme Court pending litigation). District numbers in state feeds follow the new maps.
* Precinct counts can lie about "percent counted" where early/mail ballots are tallied as a single central precinct (FL, VA, TX). Treat the reporting bar as a rough guide.
