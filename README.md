# RimRunnerWyoming

Unofficial Wyoming HS boys basketball ratings. Not affiliated with the WHSAA or MaxPreps.
Status: **HELD (waiting on hosting)**. Preview: https://joshua-kpg.github.io/RimRunnerWyoming/ (GitHub Pages). No Firebase project yet
(the Google accounts are at their project quota) and Cloudflare Pages is at its project limit, so accounts, picks and follows save only in the
visitor's browser until the site's own Firebase project exists.

## Files
- `index.html` — the whole app (data embedded). `logos/` — small WebP school logos (from MaxPreps school profiles), served with the site.
- `data/results.json` — 2026-27 finals ledger written by the nightly updater.
- `firebase/` — Firestore rules and auth config for the site's future Firebase project (accounts, picks, follows).

## Teams
70 teams: every WHSAA boys basketball school, in the WHSAA 2026-27 classes (Culminating Event Structure 2026-27/2027-28):
4A 16, 3A 16, 2A 18, 1A 20. Conferences = the 16 WHSAA quadrants (4A/3A/2A/1A x Northwest, Southwest, Northeast, Southeast).
Ten Sleep is kept in 1A Northwest: the official WHSAA schedule lists its 2026-27 boys games (WyoPreps' "will not field a team in 2026" line
reads as fall volleyball). No Other (non-WHSAA) programs met the 8-game rule.

## Schedule
MaxPreps schedules plus the official WHSAA schedule (DragonFly): a WHSAA game between two listed teams is added when MaxPreps has none for
that pair within 3 days; when both list it, the WHSAA date and home team win. Regional/state tournament slots (1A/2A from Feb 25, 3A/4A from
Mar 4) are left out until real finals arrive (the nightly updater adds them).

## Season rule
WHSAA: first practice Nov 23, 2026; first contest Thu Dec 10, 2026. Games involving a WHSAA school before that date are left out (schedule
build and nightly updater); games between two non-WHSAA schools are kept. A school listed against itself is never a game. Regionals: 1A/2A
Feb 25-27, 3A/4A Mar 4-6; state: 1A/2A Mar 4-6, 3A/4A Mar 11-13, 2027 (Casper).

## Nightly scores
`.github/workflows/nightly-scores.yml` runs `scripts/update_scores.py` at 06:30 and 14:00 UTC (11:30 pm / 7:00 am Mountain standard time) and
on manual dispatch. Before `SEASON_START` in index.html (the first game on the schedules) the script makes no requests. From then on it
reads boys basketball finals for the last 3 days from the MaxPreps Wyoming scores-by-date page (team-page fallback), matches them to the schedule
by MaxPreps team id + date (WHSAA-only rows by team ids + date), records them in `data/results.json` and bakes scores and W-L records into
`index.html`. The page recomputes Elo from those finals. It commits only when data changed; GitHub Pages updates from the commit.
