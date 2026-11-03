# Handoff / review request: 2026 Election-Night Bellwether Dashboard

**Purpose.** A personal, read-only dashboard that one user (an Illinois county election commission chairman who will be busy running his own jurisdiction's election on Nov. 3, 2026) can glance at on his phone to see unofficial returns in 20 hand-picked bellwether races, without watching TV. Not for profit, not official. Accuracy matters more than polish.

**Live:** https://taipan0319-byte.github.io/bellwether-2026/ (tabs: House / Senate / Governors)
**Code:** https://github.com/taipan0319-byte/bellwether-2026 (public)

We'd like a critical review: wrong assumptions, better data sources, failure modes we haven't considered, and anything that would make the numbers wrong rather than merely late.

## What's tracked

| Tab | Races |
|---|---|
| House (9) | FL-25 Moskowitz(D) v. Singer(R) · VA-01 Wittman(R) v. Taylor(D) · VA-02 Kiggans(R) v. Luria(D) · NC-11 Balkcom(R) v. Ager(D), open · MI-04 Huizenga(R) v. McCann(D) · TX-01 Moran(R) v. Prince(D) · TX-15 De La Cruz(R) v. Pulido(D) · TX-35 C. De La Cruz(R) v. Garcia(D), open · MT-01 Flint(R) v. Forstag(D), open |
| Senate (8) | ME Collins(R) v. Troy Jackson(D) · MI Rogers(R) v. El-Sayed(D) · MN Flanagan(D) v. Tafoya(R) · KS Marshall(R) v. Hamilton(D) · OH-special Husted(R) v. Brown(D) · GA Ossoff(D) v. M. Collins(R) · IA Hinson(R) v. Turek(D) · AK D. Sullivan(R) v. Peltola(D), RCV |
| Governors (4) | WI Crowley(D) v. Tiffany(R) · OH Ramaswamy(R) v. Acton(D) · NY Hochul(D) v. Blakeman(R) · FL Donalds(R) v. Jolly(D) |

Note: TX and FL districts use the 2025–26 mid-decade maps. ME Senate is ranked-choice (first-round only on election night). GA goes to a Dec. 1 runoff under 50%.

## Architecture

```
GitHub Actions cron (every 5 min from 5 PM CT Nov 3 → 6 AM CT Nov 4; every 30 min through Nov 6)
  └─ scripts/fetch_results.py  (Python; requests + BeautifulSoup; Playwright/Chromium for Cloudflare-fronted or JS-only sites)
       ├─ one adapter per state: fetches the STATE's official statewide results feed (never county sites)
       ├─ a shared "name-anchored" parser: finds each configured candidate's name in HTML or JSON and reads the vote
       │  count beside it; precincts-reporting parsed from nearby text; dedicated parsers where the feed is structured
       ├─ guards: a feed is used only if it is identifiably the Nov 3, 2026 general (TX/MT/MI/MN/NY/GA/IA) — before the
       │  states switch their portals over, nothing is shown rather than stale primary/2024 numbers
       ├─ data/manual_overrides.json (hand-entered numbers always win; set "called": "REP"/"DEM" when networks call)
       └─ commits data/results.json
GitHub Pages serves index.html (vanilla JS), which re-reads results.json every 60 s.
```

The dashboard never projects winners itself; "called" is manual.

## Data sources, per state (status as of Nov. 2, 9 PM CT)

| State | Source | Mechanism | Status |
|---|---|---|---|
| FL | floridaelectionwatch.gov (Division of Elections) | HTML contest pages | Live; Nov 3 contests present (zeros) |
| VA | enr.elections.virginia.gov (Enhanced Voting) | public JSON `…/api/elections/virginia/2026-November-General/ballot-items` | Live |
| NC | dl.ncsbe.gov `ENRS/2026_11_03/results_pct_20261103.zip` | precinct TSV, summed across all counties for the contest | Live (290 precincts in NC-11) |
| OH | liveresults.ohiosos.gov `Api/v1/Contest/GetContestDetailsObject` | JSON via headless browser (Cloudflare) | Live; Senate special + Governor present |
| IA | electionresults.iowa.gov (Clarity) `IA/127077/{ver}/json/en/summary.json` | JSON via headless browser | Live |
| TX | results.texas-election.com `static/data/election/{id}/{ver}/OfficeSummary.json` | JSON; election id from `ElectionConstants` | Parser verified on 2024; portal still on 2024 until SOS switches |
| MT | electionresults.mt.gov `ResultsSW.aspx?type=FED` | HTML via headless browser (Cloudflare JS challenge) | Verified on June primary; general not loaded yet |
| MI | mvic.sos.state.mi.us `VoteHistory/GetCountyVoteRecords?electionId=` | HTML; election id discovered by date | Verified on Aug primary; Nov 3 not listed yet. (mielections.us is unreachable from GitHub runners.) |
| GA | results.sos.ga.gov (Enhanced Voting) | `api/jurisdictions/Georgia` → election by date → ballot-items | Same platform as VA; Nov 3 not listed yet |
| MN | electionresults.sos.mn.gov | HTML | General not posted yet |
| KS | ent.sos.ks.gov | headless browser | Site currently returns Cloudflare error 1000 (dormant) |
| NY | results.elections.ny.gov | headless browser | Historical DB with "Latest Results"; untested for election night |
| ME, WI | none | — | No official statewide live site; manual entry from AP |

## Known weaknesses (please add to this list)

1. Five states (TX, MT, MI, GA, MN) are verified only against earlier elections; their Nov 3 pages can't be tested until the states load them (expected Election Day afternoon).
2. "% reporting" is inconsistent: FL contest pages give none; VA counts localities; NC's is an approximation (precincts with any votes ÷ total); FL/TX/VA centralize early/mail ballots as one unit, so precinct % understates the vote counted.
3. Name matching is fuzzy (last name + first three letters of first name) to survive "Last, First" and "Jennifer Capps Balkcom" variations. A same-surname candidate in the same contest would confuse it (none in these 20 races).
4. GitHub's cron can lag 5–15 min under load; manual "Run workflow" is the fallback.
5. For ME/WI/KS/NY the user is open to scraping a news outlet's AP-powered results page for personal use. Suggestions for the most stable public JSON source for those four would be welcome.
6. No alerting; the user must open the page.

## Questions for the reviewer

- Is there a better official feed for any state above (especially MI, KS, NY, WI, ME)?
- Any reason to distrust the state portals' statewide totals vs. AP on election night (known lags, known bugs)?
- What would you change in the "bellwether reading" logic (House: Dems winning/leading ≥5 of 9 ⇒ Democratic House signal; GOP holding ≥8 ⇒ GOP hold)?
- Anything in the repo you'd flag as likely to break at 7 PM ET?
