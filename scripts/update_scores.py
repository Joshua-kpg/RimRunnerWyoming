#!/usr/bin/env python3
"""RimRunnerWyoming nightly score updater.

Pulls boys basketball finals for the last few days from MaxPreps, matches them to the
schedule embedded in index.html by MaxPreps team id + date (never by name), stores them
in data/results.json (the season ledger) and bakes them into index.html:
  * game rows -> status "final" + scores (+ result 1/0 for forfeits, so W/L decides)
  * team rows -> recordWins / recordLosses (all finals, incl. unlisted opponents)
The page itself recomputes Elo at load (applySeasonFade: K=20, 8-game fade from the
preseason rating, +30 Elo home edge, none on neutral floors), so ratings and Big Games
follow from the baked finals. Files are only rewritten when the data actually changed.

Source, in order of preference:
  1. State scores-by-date page  https://www.maxpreps.com/wy/basketball/scores/?date=M/D/YYYY
     (one request per date; contest id, both MaxPreps team ids, scores, Final/forfeit).
  2. Fallback when that page errors or its layout is unrecognised: the team schedule pages
     (__NEXT_DATA__) of only the teams with an unplayed game in the window.
New games (final between two listed teams with no scheduled game) are confirmed on the home
team's schedule page first (site, neutral, scrimmage check) before they are added.

Python 3.9+ standard library only.
"""
import argparse, base64, datetime as dt, json, os, re, sys, time, uuid
import urllib.request, urllib.error
from zoneinfo import ZoneInfo

UA = "RimRunnerWyoming-scores/1.0 (+https://joshua-kpg.github.io/RimRunnerWyoming/; nightly, a few requests)"
VHSL_FIRST_CONTEST = "2026-12-10"   # unused football-flag rule kept from the template (no football flags here)
WHSAA_FIRST_GAME = "2026-12-10"     # WHSAA basketball page: "First Contest: December 10th, 2026" (first practice Nov 23).
                                    # WHSAA members (site divisions 1-4: 4A, 3A, 2A, 1A) may not count a game before it; games between two non-WHSAA
                                    # schools still count (same rule as the schedule build, scripts/build_site.py). No December moratorium, so NO_PLAY never matches.
NO_PLAY = ("9999-12-31", "9999-12-31")
SCORES_URL = "https://www.maxpreps.com/wy/basketball/scores/?date={m}/{d}/{y}"
ET = ZoneInfo("America/Denver")   # Wyoming: Mountain time (dates are what matter here)
# Wyoming: every WHSAA team is in Wyoming, so no out-of-state member pairs need a team-page check (kept empty; logic from the Nevada updater).
OOS_TEAMS = set()
ZERO_ID = "00000000-0000-0000-0000-000000000000"
RESCHEDULE_DAYS = 21
GT = {0: "conference", 1: "non-conference", 2: "tournament", 3: "exhibition", 4: "playoff", 5: "conference-tournament"}

LOG_LINES = []
def log(*a):
    s = " ".join(str(x) for x in a)
    LOG_LINES.append(s)
    print(s, flush=True)

class Blocked(Exception):
    pass

# ---------------------------------------------------------------- HTTP
class Http:
    def __init__(self, delay, cache_dir=None, replay_dir=None, reuse_cache=False):
        self.delay, self.last, self.count = delay, 0.0, 0
        self.cache_dir, self.replay_dir, self.reuse = cache_dir, replay_dir, reuse_cache

    def _key(self, url):
        return re.sub(r"[^A-Za-z0-9]+", "_", url)[-180:] + ".html"

    def get(self, url):
        if self.replay_dir:                      # test mode: serve saved pages, no network
            p = os.path.join(self.replay_dir, self._key(url))
            if not os.path.exists(p):
                return 404, ""
            return 200, open(p, encoding="utf-8").read()
        if self.reuse and self.cache_dir and os.path.exists(os.path.join(self.cache_dir, self._key(url))):
            return 200, open(os.path.join(self.cache_dir, self._key(url)), encoding="utf-8").read()
        for attempt in range(3):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time(); self.count += 1
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", "replace"); code = r.status
            except urllib.error.HTTPError as e:
                code, body = e.code, ""
            except Exception as e:                # network hiccup: back off and retry
                log(f"  ! {url}: {e.__class__.__name__}: {e}")
                time.sleep(5 * (attempt + 1)); continue
            if code in (403, 429):
                raise Blocked(f"HTTP {code} from {url}")
            if code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            if self.cache_dir and code == 200:
                os.makedirs(self.cache_dir, exist_ok=True)
                open(os.path.join(self.cache_dir, self._key(url)), "w", encoding="utf-8").write(body)
            return code, body
        return 0, ""

# ---------------------------------------------------------------- index.html data
class Site:
    def __init__(self, path):
        self.path = path
        self.text = open(path, encoding="utf-8").read()
        L = self.text.split("\n")
        i = L.index("const DATA = {")
        assert L[i + 1] == "  teams: [", L[i + 1]
        j = L.index("  ],", i)
        assert L[j + 1] == "  games: [", L[j + 1]
        k = L.index("  ]", j + 1)
        assert L[k + 1] == "};", L[k + 1]
        rows = lambda a, b: [json.loads(x.strip().rstrip(",")) for x in L[a:b] if x.strip()]
        self.L, self.i, self.k = L, i, k
        self.teams = rows(i + 2, j)
        self.games = rows(j + 2, k)
        m = re.search(r'^const SEASON_START = "(\d{4}-\d\d-\d\d)"', self.text, re.M)
        self.season_start = m.group(1)
        self.mp2slug = {t["maxprepsId"]: t["id"] for t in self.teams}
        self.slug2team = {t["id"]: t for t in self.teams}

    def render(self):
        J = lambda o: "    " + json.dumps(o, ensure_ascii=False)
        block = ["const DATA = {", "  teams: ["] + [",\n".join(J(t) for t in self.teams)] + \
                ["  ],", "  games: ["] + [",\n".join(J(g) for g in self.games)] + ["  ]"]
        return "\n".join(self.L[:self.i] + block + self.L[self.k + 1:])

# ---------------------------------------------------------------- parsing
def dec_id(s):
    return str(uuid.UUID(bytes_le=base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))))

def to_int(s):
    s = (s or "").strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None

def parse_scores_page(html, date):
    """-> (status, finals, info). status: ok | no-games | layout."""
    m = re.search(r'href="[^"]*/scores/\?date=(\d+)/(\d+)/(\d{4})" class="btn btn-default active"', html)
    if not m:
        return "layout", [], "no active date button"
    shown = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    if shown != date:   # MaxPreps falls back to another day when the requested one has no games
        return "no-games", [], f"page shows {shown} instead"
    if 'class="c-s"' not in html or "contest-count" not in html:
        return "layout", [], "missing contest list"
    cnt = re.search(r'class="contest-count" >(\d+)', html)
    items = re.findall(r'<li class="c" data-teams="([^"]*)"[^>]*?data-contest-id="([0-9a-f-]{36})"\s*>(.*?)</div></li>', html, re.S)
    finals, seen = [], set()
    for teams, cid, body in items:
        if cid in seen:
            continue
        seen.add(cid)
        ids = teams.split(",")
        if len(ids) != 2:
            continue
        home, away = dec_id(ids[0]), dec_id(ids[1])     # data-teams = home, away
        if ZERO_ID in (home, away):
            continue                                     # TBA opponent
        st = re.search(r'data-contest-state="([^"]*)"', body)
        det = re.search(r'class="details" >([^<]*)', body)
        det = (det.group(1) if det else "").strip()
        lis = re.findall(r'<li data-result="(\d)"[^>]*>(.*?)</li>', body, re.S)
        if not st or st.group(1) != "boxscore" or len(lis) != 2:
            continue
        if not re.search(r"final|forfeit", det, re.I):
            continue
        # display order is away (top), home (bottom); confirm with the mascot image id when present
        sides = {}
        for idx, (res, b) in enumerate(lis):
            img = re.search(r"school-mascot/[0-9a-f]/[0-9a-f]/[0-9a-f]/([0-9a-f-]{36})", b)
            who = img.group(1) if img and img.group(1) in (home, away) else (away if idx == 0 else home)
            sc = re.search(r'class="score" >([^<]*)', b)
            sides[who] = (res, to_int(sc.group(1) if sc else None))
        if set(sides) != {home, away}:
            continue
        (hr, hs), (ar, as_) = sides[home], sides[away]
        forfeit = bool(re.search(r"forfeit", det, re.I))
        winner = "home" if hr == "2" else "away" if ar == "2" else None
        if winner is None and hs is not None and as_ is not None:
            winner = "home" if hs > as_ else "away" if as_ > hs else "tie"
        if winner is None or (not forfeit and (hs is None or as_ is None)):
            continue
        finals.append({"contestId": cid, "date": date.isoformat(), "homeMp": home, "awayMp": away,
                       "homeScore": hs, "awayScore": as_, "winner": winner, "forfeit": forfeit,
                       "neutral": None, "source": "scores-page"})
    info = f"{len(items)} contests listed" + (f" (header {cnt.group(1)})" if cnt else "")
    return "ok", finals, info

def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1))["props"]["pageProps"] if m else None

def team_page_contests(pp, owner):
    """Yield parsed contests from a MaxPreps team schedule page (positional arrays)."""
    for c in pp.get("contests") or []:
        t = c[0]
        if len(t) != 2:
            continue
        me = next((x for x in t if x[1] == owner), None)
        op = next((x for x in t if x[1] != owner), None)
        if not me or not op or not op[1] or me[7] or op[7]:
            continue
        yield {"contestId": c[1], "date": (c[11] or "")[:10], "timeFlag": c[10], "status": c[15],
               "note": (c[5] or "").strip(), "me": me, "op": op, "gameType": GT.get(me[12], me[12])}

def contest_to_final(x, owner):
    """Team-page contest -> final dict, or (None, reason)."""
    me, op = x["me"], x["op"]
    if x["status"] != 4:
        return None, "not final"
    if x["timeFlag"] == 4:
        return None, "date-TBA"
    if "scrimmage" in x["note"].lower() or x["gameType"] == "exhibition":
        return None, "scrimmage/exhibition"
    forfeit = bool(me[8] or op[8])
    res = me[5]
    if res not in ("W", "L", "T"):
        if me[6] is None or op[6] is None:
            return None, "no score"
        res = "W" if me[6] > op[6] else "L" if me[6] < op[6] else "T"
    if not forfeit and (me[6] is None or op[6] is None):
        return None, "no score"
    site = me[11]                         # 0 home, 1 away, 2 neutral
    if site == 1:
        home, away, hs, as_ = op, me, op[6], me[6]
        winner = {"W": "away", "L": "home", "T": "tie"}[res]
    else:
        home, away, hs, as_ = me, op, me[6], op[6]
        winner = {"W": "home", "L": "away", "T": "tie"}[res]
    return {"contestId": x["contestId"], "date": x["date"], "homeMp": home[1], "awayMp": away[1],
            "homeScore": hs, "awayScore": as_, "winner": winner, "forfeit": forfeit,
            "neutral": site == 2, "gameType": x["gameType"], "source": "team-page"}, ""

# ---------------------------------------------------------------- main logic
def daterange(end, days):
    return [end - dt.timedelta(days=i) for i in range(days - 1, -1, -1)]

def fetch_team_page(http, team, season_path):
    url = team["maxprepsUrl"].rstrip("/") + "/" + season_path
    code, html = http.get(url)
    if code != 200:
        log(f"  ! team page {team['id']}: HTTP {code}")
        return None
    pp = next_data(html)
    if pp is None:
        log(f"  ! team page {team['id']}: no __NEXT_DATA__")
    return pp

def collect(site, ledger, args, http):
    dates = daterange(args.end, args.days)
    dset = {d.isoformat() for d in dates}
    finals, fallback_dates = [], []
    if args.source in ("auto", "scores"):
        for d in dates:
            code, html = http.get(SCORES_URL.format(m=d.month, d=d.day, y=d.year))
            if code != 200:
                log(f"{d}: scores page HTTP {code}"); fallback_dates.append(d); continue
            st, fs, info = parse_scores_page(html, d)
            if st == "layout":
                log(f"{d}: scores page layout not recognised ({info})"); fallback_dates.append(d); continue
            log(f"{d}: {st}; {info}; {len(fs)} finals")
            finals += fs
        if args.source == "scores":
            fallback_dates = []
        # (Nevada logic) games between two out-of-state members -> home team's page; OOS_TEAMS is empty for Wyoming
        oos = sorted({g["homeId"] for g in site.games if g["date"] in dset and g["status"] != "final"
                      and g["homeId"] in OOS_TEAMS and g["awayId"] in OOS_TEAMS and g["homeId"] in site.slug2team})
        have = {f["contestId"] for f in finals}
        for s in oos:
            t = site.slug2team[s]
            pp = fetch_team_page(http, t, args.team_season_path)
            n = 0
            for x in team_page_contests(pp or {}, t["maxprepsId"]):
                if x["date"] in dset and x["contestId"] not in have:
                    f, why = contest_to_final(x, t["maxprepsId"])
                    if f:
                        have.add(x["contestId"]); finals.append(f); n += 1
            log(f"out-of-state team page {s}: {n} finals")
    else:
        fallback_dates = dates
    if fallback_dates:
        fd = {d.isoformat() for d in fallback_dates}
        if args.fallback_teams == "all":
            need = sorted(site.slug2team)
        else:
            need = sorted({s for g in site.games if g["date"] in fd and g["status"] != "final" for s in (g["homeId"], g["awayId"])})
        log(f"fallback: team pages for {len(need)} teams with unplayed games on {sorted(fd)}")
        need = need[:args.max_team_requests]
        got = set()
        for s in need:
            t = site.slug2team[s]
            pp = fetch_team_page(http, t, args.team_season_path)
            for x in team_page_contests(pp or {}, t["maxprepsId"]):
                if x["date"] in fd and x["contestId"] not in got:
                    f, why = contest_to_final(x, t["maxprepsId"])
                    if f:
                        got.add(x["contestId"]); finals.append(f)
        log(f"fallback: {len(got)} finals from team pages")
    return [f for f in finals if f["date"] in dset]

def merge(site, ledger, finals):
    """Add new / corrected finals to the ledger. Returns counts."""
    L = ledger["finals"]; c = {"new": 0, "corrected": 0, "dup": 0, "unlisted": 0, "ignored": 0}
    pairdate = {(frozenset((e["homeMp"], e["awayMp"])), e["date"]): k for k, e in L.items()}
    for f in finals:
        lh, la = f["homeMp"] in site.mp2slug, f["awayMp"] in site.mp2slug
        if not lh and not la:
            continue
        if f["date"] < site.season_start:
            c["ignored"] += 1; continue
        # a school listed against itself is never a real game (self-game guard, same as the build)
        if f["homeMp"] == f["awayMp"] or (lh and la and site.mp2slug[f["homeMp"]] == site.mp2slug[f["awayMp"]]):
            c["ignored"] += 1; continue
        # WHSAA members may not count a game before WHSAA_FIRST_GAME (2026-12-10); games between two non-WHSAA schools still count.
        if f["date"] < WHSAA_FIRST_GAME and any((site.slug2team.get(site.mp2slug.get(m)) or {}).get("division", 99) <= 4 for m in (f["homeMp"], f["awayMp"])):
            c["ignored"] += 1; continue
        # (Nebraska's December moratorium rule; NO_PLAY is empty for Wyoming.)
        if NO_PLAY[0] <= f["date"] <= NO_PLAY[1]:
            c["ignored"] += 1; continue
        if f["date"] < VHSL_FIRST_CONTEST and any((site.slug2team.get(site.mp2slug.get(m)) or {}).get("football") for m in (f["homeMp"], f["awayMp"])):
            c["ignored"] += 1; continue
        key = (frozenset((f["homeMp"], f["awayMp"])), f["date"])
        if key in pairdate and pairdate[key] != f["contestId"]:
            c["dup"] += 1; log(f"  dup: {f['contestId']} same pair+date as {pairdate[key]} (kept first)"); continue
        core = ("date", "homeMp", "awayMp", "homeScore", "awayScore", "winner", "forfeit")
        old = L.get(f["contestId"])
        if old and all(old.get(k) == f.get(k) for k in core):
            continue
        e = dict(old or {}); e.update({k: f[k] for k in core})
        if f.get("neutral") is not None:
            e["neutral"] = f["neutral"]
        e.setdefault("neutral", None); e["source"] = f["source"]
        if f.get("gameType"):
            e["gameType"] = f["gameType"]
        if f["source"] == "team-page":
            e["verified"] = True
        if not (lh and la):
            e["state"] = "unlisted"; c["unlisted"] += 0 if old else 1
        else:
            e.setdefault("state", "pending")
        if old:
            c["corrected"] += 1; log(f"  corrected: {f['contestId']} {old.get('homeScore')}-{old.get('awayScore')} -> {f['homeScore']}-{f['awayScore']}")
        else:
            c["new"] += 1
        L[f["contestId"]] = e; pairdate[key] = f["contestId"]
    return c

def verify_new(site, ledger, http, args, budget):
    """Confirm pending finals that have no scheduled game, on the home team's schedule page."""
    claimed = {e.get("gameId") for e in ledger["finals"].values() if e.get("gameId")}
    used = 0
    for cid, e in sorted(ledger["finals"].items(), key=lambda kv: kv[1]["date"]):
        if e.get("state") != "pending" or e.get("verified") or e.get("gameId"):
            continue
        if find_game(site, e, cid, claimed)[0] is not None:
            continue
        if used >= budget:
            log(f"  verify budget used up; {cid} stays pending"); continue
        owner = site.slug2team[site.mp2slug[e["homeMp"]]]
        used += 1
        pp = fetch_team_page(http, owner, args.team_season_path)
        x = next((x for x in team_page_contests(pp or {}, owner["maxprepsId"]) if x["contestId"] == cid), None)
        if not x:
            log(f"  new game {cid} not found on {owner['id']} schedule; stays pending"); continue
        f, why = contest_to_final(x, owner["maxprepsId"])
        if not f:
            if why in ("scrimmage/exhibition", "date-TBA"):
                e["state"] = "ignored"; e["reason"] = why; log(f"  new game {cid} ignored: {why}")
            else:
                log(f"  new game {cid} not final on team page yet ({why}); stays pending")
            continue
        e.update({"homeMp": f["homeMp"], "awayMp": f["awayMp"], "homeScore": f["homeScore"], "awayScore": f["awayScore"],
                  "winner": f["winner"], "forfeit": f["forfeit"], "neutral": f["neutral"], "gameType": f["gameType"], "verified": True})
    return used

def find_game(site, e, cid, claimed):
    h, a = site.mp2slug.get(e["homeMp"]), site.mp2slug.get(e["awayMp"])
    pair = {h, a}
    free = lambda g: g["id"] not in claimed
    same = [g for g in site.games if {g["homeId"], g["awayId"]} == pair and g["date"] == e["date"] and free(g)]
    if same:
        return same[0], "pair+date"
    byc = [g for g in site.games if g.get("maxprepsContestId") == cid and free(g)]
    if byc:
        return byc[0], "contest-id (rescheduled)"
    d0 = dt.date.fromisoformat(e["date"])
    near = [g for g in site.games if {g["homeId"], g["awayId"]} == pair and g["status"] != "final" and free(g)
            and abs((dt.date.fromisoformat(g["date"]) - d0).days) <= RESCHEDULE_DAYS]
    if near:
        near.sort(key=lambda g: abs((dt.date.fromisoformat(g["date"]) - d0).days))
        return near[0], "same pair, nearest unplayed date (rescheduled)"
    return None, ""

def apply(site, ledger):
    """Bake the ledger into site.games / site.teams. Idempotent."""
    gid = {g["id"]: g for g in site.games}
    claimed = {e.get("gameId") for e in ledger["finals"].values() if e.get("gameId")}
    m = re.match(r"(.*?)\d+$", site.games[0]["id"]) if site.games else None
    prefix = m.group(1) if m else "wy26-"
    stats = {"matched": 0, "rescheduled": 0, "added": 0, "pending": 0}
    for cid, e in sorted(ledger["finals"].items(), key=lambda kv: (kv[1]["date"], kv[0])):
        if e.get("state") in ("unlisted", "ignored"):
            continue
        g = gid.get(e.get("gameId"))
        if g is None:
            g, how = find_game(site, e, cid, claimed)
            if g is not None:
                if g["date"] != e["date"]:
                    log(f"  rescheduled: {g['id']} {g['awayId']} @ {g['homeId']} {g['date']} -> {e['date']} ({how})")
                    stats["rescheduled"] += 1
            elif e.get("verified"):
                n = 1 + sum(1 for x in site.games if x["id"].startswith(prefix + "x"))
                hs, as_ = site.mp2slug[e["homeMp"]], site.mp2slug[e["awayMp"]]
                g = {"id": f"{prefix}x{n:03d}", "date": e["date"], "homeId": hs, "awayId": as_, "homeScore": None,
                     "awayScore": None, "status": "scheduled", "predictedHome": 0, "predictedAway": 0, "homeWinPct": 0.5,
                     "source": "maxpreps-nightly", "neutral": bool(e.get("neutral")), "time": "",
                     "gameType": e.get("gameType") or "non-conference", "notes": "", "maxprepsContestId": cid}
                site.games.append(g); gid[g["id"]] = g
                log(f"  added new game {g['id']}: {as_} @ {hs} {e['date']} (not on the original schedule)")
                stats["added"] += 1
            else:
                stats["pending"] += 1
                log(f"  UNMATCHED (pending check): {cid} {e['date']} {site.mp2slug[e['awayMp']]} @ {site.mp2slug[e['homeMp']]}")
                continue
            e["gameId"] = g["id"]; claimed.add(g["id"]); e["state"] = "applied"
        # orient to the game's home team
        flip = site.mp2slug[e["homeMp"]] != g["homeId"]
        hs, as_ = (e["awayScore"], e["homeScore"]) if flip else (e["homeScore"], e["awayScore"])
        w = {"home": "away", "away": "home"}.get(e["winner"], e["winner"]) if flip else e["winner"]
        if e["forfeit"]:
            ok = (hs is not None and as_ is not None and ((w == "home" and hs > as_) or (w == "away" and as_ > hs)))
            if not ok:
                hs, as_ = (1, 0) if w == "home" else (0, 1)
            g["result"] = 1 if w == "home" else 0
            if "Forfeit" not in (g.get("notes") or ""):
                g["notes"] = ("Forfeit. " + (g.get("notes") or "")).strip()
        g["date"] = e["date"]; g["homeScore"] = hs; g["awayScore"] = as_; g["status"] = "final"
        stats["matched"] += 1
    site.games.sort(key=lambda g: (g["date"], g["id"]))
    # records: every final involving a listed team (ratings only use listed-vs-listed games)
    rec = {t["id"]: [0, 0] for t in site.teams}
    for e in ledger["finals"].values():
        if e.get("state") not in ("applied", "unlisted"):
            continue
        for mp, side in ((e["homeMp"], "home"), (e["awayMp"], "away")):
            s = site.mp2slug.get(mp)
            if s and e["winner"] in ("home", "away"):
                rec[s][0 if e["winner"] == side else 1] += 1
    for t in site.teams:
        t["recordWins"], t["recordLosses"] = rec[t["id"]]
    return stats

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--html", default="index.html")
    ap.add_argument("--ledger", default="data/results.json")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--end-date", help="last date to fetch (YYYY-MM-DD); default = today in Wyoming (Mountain time)")
    ap.add_argument("--source", choices=["auto", "scores", "teams"], default="auto")
    ap.add_argument("--team-season-path", default="schedule/", help='team page suffix ("25-26/schedule/" for replay tests)')
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between MaxPreps requests")
    ap.add_argument("--fallback-teams", choices=["window", "all"], default="window",
                    help="team-page fallback: only teams with an unplayed game in the window (default) or all teams (slow full sweep, ~1,800 requests)")
    ap.add_argument("--max-team-requests", type=int, default=500)
    ap.add_argument("--verify-budget", type=int, default=25)
    ap.add_argument("--offline", action="store_true", help="no fetching; just re-apply the ledger to the html")
    ap.add_argument("--cache-dir", help="save fetched pages here")
    ap.add_argument("--reuse-cache", action="store_true", help="tests: use pages already in --cache-dir")
    ap.add_argument("--replay-dir", help="read pages from here instead of the network (tests)")
    ap.add_argument("--log", default="nightly-log.txt")
    args = ap.parse_args()
    t0 = time.time()
    args.end = dt.date.fromisoformat(args.end_date) if args.end_date else dt.datetime.now(ET).date()
    site = Site(args.html)
    before_html = site.text
    ledger = json.load(open(args.ledger)) if os.path.exists(args.ledger) else {"finals": {}}
    ledger.setdefault("finals", {})
    before_ledger = json.dumps(ledger, sort_keys=True)
    http = Http(args.delay, args.cache_dir, args.replay_dir, args.reuse_cache)
    log(f"RimRunnerWyoming nightly scores: window {daterange(args.end, args.days)[0]}..{args.end} "
        f"(source {args.source}); {len(site.teams)} teams, {len(site.games)} games, season start {site.season_start}")
    rc = 0
    if not args.offline and not args.end_date and args.end.isoformat() < site.season_start:
        # Wyoming's first game on the MaxPreps schedules is SEASON_START (WHSAA first boys game day Thu Dec 10, 2026, unless two non-WHSAA schools play earlier): before that there is nothing to fetch.
        log(f"preseason: today {args.end} is before SEASON_START {site.season_start}; no MaxPreps requests this run")
        args.offline = True
    try:
        if not args.offline:
            finals = collect(site, ledger, args, http)
            c = merge(site, ledger, finals)
            log(f"merge: {c}")
            verify_new(site, ledger, http, args, args.verify_budget)
    except Blocked as b:
        log(f"BLOCKED: {b}. Stopping MaxPreps requests for this run (no workaround attempted). Applying what we already have.")
        rc = 3
    stats = apply(site, ledger)
    log(f"apply: {stats}; ledger finals {len(ledger['finals'])}")
    new_html = site.render()
    ledger_out = {"about": "RimRunnerWyoming 2026-27 finals ledger, written by scripts/update_scores.py. Keyed by MaxPreps contest id. "
                           "homeMp/awayMp are MaxPreps team ids. state: applied | unlisted (one team not in our list; counts for records only) "
                           "| pending (final between listed teams with no scheduled game yet; awaiting team-page check) | ignored.",
                  "finals": dict(sorted(ledger["finals"].items()))}
    changed = False
    if json.dumps(ledger_out, sort_keys=True) != before_ledger:
        os.makedirs(os.path.dirname(args.ledger) or ".", exist_ok=True)
        with open(args.ledger, "w") as fo:
            json.dump(ledger_out, fo, indent=1, sort_keys=True); fo.write("\n")
        changed = True
    if new_html != before_html:
        open(args.html, "w", encoding="utf-8").write(new_html)
        changed = True
    log(f"{'CHANGED' if changed else 'no data change'}; {http.count} MaxPreps requests; {time.time() - t0:.1f}s")
    if args.log:
        open(args.log, "w").write("\n".join(LOG_LINES) + "\n")
    return rc

if __name__ == "__main__":
    sys.exit(main())
