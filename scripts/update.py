#!/usr/bin/env python3
"""Refresh the site's data files.

Writes:
  data/week.json               this week's NFL games and scores (ESPN scoreboard)
  data/players/<school>.json   every NFL player from that school (Sleeper, checked against ESPN team rosters)
  data/stats.json              box-score lines for tracked players (ESPN game summaries)
  data/college/<school>.json   the school's own season: schedule, scores, box scores, season leaders, news (ESPN)
  data/weeks/<season>/<k>.json one file per finished NFL week (scores + stat lines), for the week picker
  data/season/<school>.json    each tracked player's NFL season: game-by-game lines, totals, leaderboards
  data/moves/<school>.json     roster moves, from comparing each day's roster with the day before
  data/status.json             when each part last refreshed, and any problems from the last run

Standard library only, so it runs on a bare GitHub Actions runner.

  python scripts/update.py           full refresh: scores, rosters, stats
  python scripts/update.py --college the school's own season only (rankings, record, schedule, news); the evening run
  python scripts/update.py --test-alert   fail on purpose, to check that failure notifications arrive
  python scripts/update.py --auto    only if a game is live or about to start: scores + stats
                                     (cheap no-op otherwise, so it can run every 15 minutes)

Exits non-zero if any feed failed, after writing whatever it could, so the workflow run fails and
someone gets told. A failed part keeps its previous file.
"""
import gzip, json, os, re, sys, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={}"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
ATHLETE = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{}"
ROSTER = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{}/roster"
ESPN_ABBR = {"WAS": "WSH"}            # our abbreviation -> ESPN's, where they differ
COLLEGE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
PROBLEMS = []                         # anything that should fail the run; reported at the end


def problem(msg):
    PROBLEMS.append(msg)
    print(f"ERROR: {msg}", file=sys.stderr)


RESERVE_TAGS = {"ir", "pup", "nfi"}   # Sleeper injury tags that mean a reserve list, not a game-day tag

TEAMS = ["ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN","DET","GB","HOU","IND","JAX","KC",
         "LV","LAC","LAR","MIA","MIN","NE","NO","NYG","NYJ","PHI","PIT","SF","SEA","TB","TEN","WAS"]
ALIAS = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "ARZ": "ARI", "OAK": "LV", "SD": "LAC", "STL": "LAR"}
SEASON_TYPE = {1: "pre", 2: "regular", 3: "post"}
LIVE_BEFORE = timedelta(minutes=20)   # start polling this long before kickoff
LIVE_AFTER = timedelta(hours=5)       # stop polling this long after kickoff (OT, delays)


def norm(abbr):
    abbr = (abbr or "").upper()
    return ALIAS.get(abbr, abbr)


def norm_name(name):
    """'T.J. Hockenson' and 'TJ Hockenson', 'Erick All Jr.' and 'Erick All' match. Same rule as index.html."""
    n = re.sub(r"[^a-z ]", "", (name or "").lower().replace("-", " "))
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return " ".join(n.split())


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nfl-alumni-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_json(rel, default=None):
    try:
        with open(os.path.join(ROOT, rel)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(rel, data):
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")


# ---------- games ----------

def watch_info(comp):
    """Where a game is shown: ([{"name": "Fox", "type": "TV" or "Streaming"}], "Ford Field, Detroit, MI")."""
    watch, seen = [], set()
    for b in (comp.get("geoBroadcasts") or comp.get("broadcasts") or []):
        names = [(b.get("media") or {}).get("shortName")] if b.get("media") else (b.get("names") or [])
        for n in names:
            if n and n not in seen:
                seen.add(n)
                kind = (b.get("type") or {}).get("shortName") if isinstance(b.get("type"), dict) else None
                watch.append({"name": n, "type": kind or "TV"})
    v = comp.get("venue") or {}
    addr = v.get("address") or {}
    venue = ", ".join(x for x in (v.get("fullName"), addr.get("city"), addr.get("state")) if x)
    return watch, venue


def build_week(sb):
    games = []
    for ev in sb.get("events", []):
        comp = ev["competitions"][0]
        sides = {}
        for c in comp["competitors"]:
            score = c.get("score")
            sides[c["homeAway"]] = {
                "abbr": norm(c["team"]["abbreviation"]),
                "score": int(score) if score not in (None, "") else None,
            }
        st = ev.get("status", {}).get("type", {})
        state = st.get("state", "pre")                     # pre | in | post
        broadcasts = comp.get("broadcasts") or []
        network = broadcasts[0]["names"][0] if broadcasts and broadcasts[0].get("names") else ""
        watch, venue = watch_info(comp)
        games.append({
            "id": ev["id"],
            "kickoff": ev["date"],
            "state": state,
            "detail": st.get("shortDetail", "") if state != "pre" else "",
            "network": network,
            "watch": watch if state != "post" else [],
            "venue": venue,
            "away": sides.get("away"),
            "home": sides.get("home"),
        })
    playing = {g[s]["abbr"] for g in games for s in ("away", "home") if g.get(s)}
    season = sb.get("season", {})
    return {
        "season": season.get("year"),
        "week": sb.get("week", {}).get("number"),
        "seasonType": SEASON_TYPE.get(season.get("type"), "regular"),
        "updated": iso(now_utc()),
        "source": "ESPN scoreboard",
        "games": games,
        "byes": [t for t in TEAMS if t not in playing] if season.get("type") == 2 and games else [],
    }


def in_live_window(data, now):
    """True if any game in a week.json or college file is live or about to start."""
    for g in (data or {}).get("games", []):
        if g.get("state") == "post":
            continue
        k = parse_time(g["kickoff"])
        if k - LIVE_BEFORE <= now <= k + LIVE_AFTER:
            return True
    return False


# ---------- players ----------

def status_label(p):
    s = (p.get("status") or "").strip()
    inj = (p.get("injury_status") or "").strip().lower()
    if inj in RESERVE_TAGS or "injured" in s.lower():
        return "Reserve"                     # Sleeper shows IR as status "Inactive" + injury "IR"
    if inj == "sus":
        return "Suspended"
    if not s or s.lower() == "active":
        return "Active"
    return s  # e.g. "Practice Squad", "Inactive"


def fetch_rosters():
    """ESPN roster per team: {team: {norm_name: {...}}}. A team that fails is left out, and then isn't checked."""
    rosters = {}
    for t in TEAMS:
        try:
            r = get_json(ROSTER.format(ESPN_ABBR.get(t, t)))
            team = {}
            for grp in r.get("athletes", []):
                for a in grp.get("items", []):
                    team[norm_name(a.get("displayName"))] = {
                        "id": a.get("id"),
                        "name": a.get("displayName"),
                        "status": (a.get("status") or {}).get("name") or "",
                        "college": (a.get("college") or {}).get("name") or "",
                        "pos": (a.get("position") or {}).get("abbreviation"),
                        "num": a.get("jersey"),
                        "injuries": [i.get("status") or "" for i in a.get("injuries", [])],
                        "exp": (a.get("experience") or {}).get("years"), "debut": a.get("debutYear"),
                    }
            if len(team) >= 40:              # a short list means a bad response, not a real roster
                rosters[t] = team
        except Exception as e:
            print(f"warning: ESPN roster for {t} failed: {e}", file=sys.stderr)
    if len(rosters) < len(TEAMS) - 3:        # a team or two can blip; more than that is a broken feed
        problem(f"ESPN rosters: only {len(rosters)} of {len(TEAMS)} teams loaded")
    return rosters


def espn_status(e):
    if "practice" in e["status"].lower():
        return "Practice Squad"
    if any("reserve" in i.lower() for i in e["injuries"]):
        return "Reserve"
    if "suspen" in e["status"].lower():
        return "Suspended"
    return None


def reconcile(data, school, rosters):
    """Check Sleeper's list against ESPN rosters: drop players no longer on the team (Sleeper keeps some
    retired players attached to their last team), pick up practice-squad status and ESPN ids, and add
    anyone ESPN lists from this school that Sleeper missed."""
    wanted = {c.strip().lower() for c in school["colleges"]}
    exclude = {norm_name(n) for n in school.get("excludeNames", [])}
    kept, seen = [], set()
    for p in data["players"]:
        team, key = rosters.get(p["team"]), norm_name(p["name"])
        seen.add((p["team"], key))
        if team is None:                     # roster fetch failed for this team: trust Sleeper
            kept.append(p)
            continue
        e = team.get(key)
        if not e:
            print(f'dropped {p["name"]} ({p["team"]}): not on ESPN roster')
            continue
        p["espn_id"] = e["id"] or p.get("espn_id")
        p["_exp"], p["_debut"] = e.get("exp"), e.get("debut")
        status = espn_status(e)
        if status and p["status"] != "Reserve":
            p["status"] = status
            if status == "Reserve":
                p["injury"] = None
        kept.append(p)
    for t, team in rosters.items():
        for key, e in team.items():
            if e["college"].strip().lower() in wanted and (t, key) not in seen and key not in exclude:
                print(f'added {e["name"]} ({t}): on ESPN roster, not in Sleeper')
                kept.append({"name": e["name"], "team": t, "pos": e["pos"] or "—",
                             "num": int(e["num"]) if (e["num"] or "").isdigit() else None,
                             "status": espn_status(e) or "Active",
                             "injury": next((i for i in e["injuries"] if i in ("Questionable", "Doubtful", "Out")), None),
                             "college": e["college"], "espn_id": e["id"], "_exp": e.get("exp"), "_debut": e.get("debut")})
    kept.sort(key=lambda x: (x["team"], x["name"]))
    data["players"] = kept
    data["source"] = "Sleeper API + ESPN rosters"
    return data


def build_players(all_players, school_key, school):
    wanted = {c.strip().lower() for c in school["colleges"]}
    include = {norm_name(n) for n in school.get("includeNames", [])}
    exclude = {norm_name(n) for n in school.get("excludeNames", [])}
    out = []
    for p in all_players.values():
        team = norm(p.get("team"))
        if team not in TEAMS:
            continue
        name = p.get("full_name") or f'{p.get("first_name","")} {p.get("last_name","")}'.strip()
        key = norm_name(name)
        college_hit = (p.get("college") or "").strip().lower() in wanted
        if key in exclude or not (college_hit or key in include):
            continue
        status = status_label(p)
        out.append({
            "name": name,
            "team": team,
            "pos": p.get("position") or (p.get("fantasy_positions") or ["—"])[0],
            "num": p.get("number"),
            "status": status,
            "injury": None if status == "Reserve" else (p.get("injury_status") or None),  # Q, D, Out ...
            "college": p.get("college"),
            "espn_id": p.get("espn_id"),
            "_rookie": (p.get("metadata") or {}).get("rookie_year"),
        })
    out.sort(key=lambda x: (x["team"], x["name"]))
    return {"school": school_key, "updated": iso(now_utc()), "source": "Sleeper API", "players": out}


def add_draft_info(data, old):
    """When each player came into the NFL, and so his last college season.
    Drafted players: the draft year (ESPN). Undrafted: Sleeper's rookie year, which matches the draft year for
    every drafted player we track. Draft details never change, so ESPN is asked once per player."""
    known = {str(p.get("espn_id")): p for p in (old or {}).get("players", []) if p.get("espn_id") and p.get("draftChecked")}
    looked_up = 0
    for p in data["players"]:
        rookie = p.pop("_rookie", None)
        p.pop("_exp", None), p.pop("_debut", None)
        prev = known.get(str(p.get("espn_id")))
        if prev:
            p["draft"], p["draftChecked"] = prev.get("draft"), True
        elif p.get("espn_id"):
            try:
                text = (get_json(ATHLETE.format(p["espn_id"])).get("athlete") or {}).get("displayDraft") or ""
                looked_up += 1
                m = re.match(r"(\d{4}): Rd (\d+), Pk (\d+)(?: \((\w+)\))?", text)
                p["draft"] = {"year": int(m.group(1)), "round": int(m.group(2)), "pick": int(m.group(3)),
                              "team": norm(m.group(4)) if m.group(4) else None} if m else None
                p["draftChecked"] = True
            except Exception as e:
                print(f'warning: draft lookup for {p["name"]} failed: {e}', file=sys.stderr)   # tried again next refresh
        entered = (p.get("draft") or {}).get("year") or (int(rookie) if str(rookie or "").isdigit() else None)
        if not entered and prev:
            entered = prev.get("entered")                 # Sleeper was down this run: keep what we had
        if entered:
            p["entered"], p["lastCollege"] = entered, entered - 1
    if looked_up:
        print(f"draft info: looked up {looked_up} players")
    return data


# ---------- stats ----------

def parse_summary(summary, tracked, by_id=None):
    """Return {norm_name: {"team": abbr, "cats": {category: {label: value}}}} for tracked names.
    by_id maps ESPN athlete id -> our name for players whose id we know; those are matched on id only."""
    found, by_id = {}, by_id or {}
    known = set(by_id.values())
    for tp in summary.get("boxscore", {}).get("players", []):
        team = norm(tp.get("team", {}).get("abbreviation"))
        for cat in tp.get("statistics", []):
            labels = cat.get("labels", [])
            for a in cat.get("athletes", []):
                key = by_id.get(str(a.get("athlete", {}).get("id")))
                if not key:
                    key = norm_name(a.get("athlete", {}).get("displayName"))
                    if key in known:                     # we know this player's id and it isn't him
                        continue
                if key not in tracked:
                    continue
                entry = found.setdefault(key, {"team": team, "id": a.get("athlete", {}).get("id"), "cats": {}})
                entry["cats"][cat.get("name", "other")] = dict(zip(labels, a.get("stats", [])))
    return found


def update_stats(week, schools):
    tracked_by_team, by_id = {}, {}
    for key in schools:
        for p in (read_json(f"data/players/{key}.json", {}) or {}).get("players", []):
            tracked_by_team.setdefault(p["team"], set()).add(norm_name(p["name"]))
            if p.get("espn_id"):
                by_id[str(p["espn_id"])] = norm_name(p["name"])

    old = read_json("data/stats.json", {}) or {}
    old_games = old.get("games", {}) if old.get("week") == week.get("week") else {}
    games = {}
    failed = 0
    for g in week["games"]:
        if g["state"] == "pre":
            continue
        teams = [g["away"]["abbr"], g["home"]["abbr"]]
        tracked = set().union(*(tracked_by_team.get(t, set()) for t in teams))
        if not tracked:
            continue
        prev = old_games.get(g["id"])
        if prev and prev.get("final"):
            games[g["id"]] = prev                       # finished game already captured
            continue
        try:
            games[g["id"]] = {"final": g["state"] == "post",
                              "players": parse_summary(get_json(SUMMARY.format(g["id"])), tracked, by_id)}
        except Exception as e:                           # one bad game shouldn't sink the run
            print(f"warning: stats for game {g['id']} failed: {e}", file=sys.stderr)
            failed += 1
            if prev:
                games[g["id"]] = prev
    if failed > 2:
        problem(f"ESPN box scores: {failed} games failed")
    write_json("data/stats.json", {"season": week["season"], "week": week["week"],
                                   "updated": iso(now_utc()), "source": "ESPN game summaries", "games": games})
    print(f"stats: {len(games)} games")



# ---------- finished weeks (the week picker) ----------

STYPE_NUM = {"pre": 1, "regular": 2, "post": 3}
POST_LABELS = {1: "Wild Card", 2: "Divisional", 3: "Conference", 4: "Pro Bowl", 5: "Super Bowl"}


def parse_summary_by_id(summary, by_id, by_name):
    """Like parse_summary, but for old weeks: a player is found wherever he actually played, matched on
    ESPN id (so a namesake on another team isn't picked up), and keyed by the name we list him under."""
    found = {}
    for tp in summary.get("boxscore", {}).get("players", []):
        team = norm(tp.get("team", {}).get("abbreviation"))
        for cat in tp.get("statistics", []):
            labels = cat.get("labels", [])
            for a in cat.get("athletes", []):
                ath = a.get("athlete", {})
                key = by_id.get(str(ath.get("id"))) or by_name.get(norm_name(ath.get("displayName")))
                if not key:
                    continue
                entry = found.setdefault(key, {"team": team, "id": ath.get("id"), "cats": {}})
                entry["cats"][cat.get("name", "other")] = dict(zip(labels, a.get("stats", [])))
    return found


def archive_weeks(current, schools):
    """Save every finished week of this season that isn't saved yet. Self-healing: a missing file is rebuilt."""
    season, now_type = current.get("season"), STYPE_NUM.get(current.get("seasonType"), 2)
    if not season or now_type < 2 or not current.get("week"):
        return
    by_id, by_name = {}, {}
    for key in schools:
        for p in (read_json(f"data/players/{key}.json", {}) or {}).get("players", []):
            if p.get("espn_id"):
                by_id[str(p["espn_id"])] = norm_name(p["name"])
            else:
                by_name[norm_name(p["name"])] = norm_name(p["name"])
    targets = [(2, w) for w in range(1, (current["week"] if now_type == 2 else 18) + 1)]
    if now_type == 3:
        targets += [(3, w) for w in range(1, current["week"] + 1)]
    folder, made = f"data/weeks/{season}", 0
    for stype, w in targets:
        rel = f"{folder}/{stype}-{w}.json"
        if (read_json(rel) or {}).get("complete"):
            continue
        is_current = (stype, w) == (now_type, current["week"])
        try:
            wk = current if is_current else build_week(get_json(f"{SCOREBOARD}?dates={season}&seasontype={stype}&week={w}"))
            if wk.get("week") != w or not wk["games"]:
                continue
            complete = all(g["state"] == "post" for g in wk["games"])
            if is_current and not complete:
                continue                                  # still in progress: the live view covers it
            stats = {g["id"]: {"final": True, "players": parse_summary_by_id(get_json(SUMMARY.format(g["id"])), by_id, by_name)}
                     for g in wk["games"] if g["state"] == "post"}
        except Exception as e:
            problem(f"saving week {stype}-{w} failed: {e}")
            continue
        label = f"Week {w}" if stype == 2 else POST_LABELS.get(w, f"Playoffs {w}")
        write_json(rel, {**wk, "key": f"{stype}-{w}", "label": label, "complete": complete, "stats": stats})
        made += 1
    weeks = []
    for name in os.listdir(os.path.join(ROOT, folder)) if os.path.isdir(os.path.join(ROOT, folder)) else []:
        d = read_json(f"{folder}/{name}") if name.endswith(".json") and name != "index.json" else None
        if d and d.get("key"):
            weeks.append({"key": d["key"], "label": d["label"]})
    weeks.sort(key=lambda x: tuple(int(n) for n in x["key"].split("-")))
    write_json("data/weeks/index.json", {"season": season, "weeks": weeks})
    print(f"weeks: {len(weeks)} saved ({made} new)")


# ---------- NFL season: player game logs, totals, leaderboards ----------

NFL_LEADERS = [  # (label, category, column to rank by, columns to show)
    ("Receiving yards", "receiving", "YDS", ["REC", "YDS", "TD"]),
    ("Rushing yards", "rushing", "YDS", ["CAR", "YDS", "TD"]),
    ("Passing yards", "passing", "YDS", ["YDS", "TD", "INT"]),
    ("Tackles", "defensive", "TOT", ["TOT", "SOLO", "TFL"]),
    ("Sacks", "defensive", "SACKS", ["SACKS"]),
    ("Passes defended", "defensive", "PD", ["PD"]),
    ("Interceptions", "interceptions", "INT", ["INT", "TD"]),
]
NO_SUM = {"AVG", "QBR", "RTG", "PCT"}


def add_stat(total, label, v):
    """Add one box-score value into a running total. Handles "13/26" and "2-15" pairs; LONG keeps the max."""
    v = str(v)
    if label in NO_SUM:
        return
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([/-])(-?\d+(?:\.\d+)?)", v)
    if m and label != "LONG":
        a, b = total.get(label, [0.0, 0.0, m.group(2)])[:2] if isinstance(total.get(label), list) else (0.0, 0.0)
        total[label] = [a + float(m.group(1)), b + float(m.group(3)), m.group(2)]
    elif label == "LONG":
        total[label] = max(total.get(label, 0.0), to_num(v))
    else:
        try:
            total[label] = total.get(label, 0.0) + float(v)
        except ValueError:
            pass


def show_total(v):
    return f"{fmt_num(v[0])}{v[2]}{fmt_num(v[1])}" if isinstance(v, list) else fmt_num(v)


def update_season(week, schools):
    """Build each school's season file from the saved weeks plus the week in progress. Local files only."""
    season = week.get("season")
    folder = os.path.join(ROOT, f"data/weeks/{season}")
    weeks = []
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        d = read_json(f"data/weeks/{season}/{name}") if name.endswith(".json") else None
        if d and d.get("key"):
            weeks.append(d)
    cur_key = f'{STYPE_NUM.get(week.get("seasonType"), 2)}-{week.get("week")}'
    if not any(w["key"] == cur_key for w in weeks) and STYPE_NUM.get(week.get("seasonType"), 2) >= 2:
        st = read_json("data/stats.json", {}) or {}
        label = f'Week {week.get("week")}' if week.get("seasonType") == "regular" else POST_LABELS.get(week.get("week"), "Playoffs")
        weeks.append({**week, "key": cur_key, "label": label, "stats": st.get("games", {}) if st.get("week") == week.get("week") else {}})
    weeks.sort(key=lambda w: tuple(int(n) for n in w["key"].split("-")))

    for key in schools:
        roster = {norm_name(p["name"]): p for p in (read_json(f"data/players/{key}.json", {}) or {}).get("players", [])}
        players = {}
        for w in weeks:
            games = {g["id"]: g for g in w.get("games", [])}
            for gid, gs in (w.get("stats") or {}).items():
                g = games.get(gid)
                if not g or g["state"] == "pre":
                    continue
                for k, e in (gs.get("players") or {}).items():
                    if k not in roster or not e.get("cats"):
                        continue
                    mine = "home" if g["home"]["abbr"] == e["team"] else "away"
                    us, them = g[mine], g["away" if mine == "home" else "home"]
                    res = ""
                    if g["state"] == "post" and us["score"] is not None and them["score"] is not None:
                        res = f'{"W" if us["score"] > them["score"] else "L" if us["score"] < them["score"] else "T"} {us["score"]}-{them["score"]}'
                    pl = players.setdefault(k, {"name": roster[k]["name"], "games": [], "totals": {}})
                    pl["games"].append({"key": w["key"], "label": w["label"], "team": e["team"], "opp": them["abbr"],
                                        "at": mine == "away", "result": res, "live": g["state"] == "in", "cats": e["cats"]})
                    for cat, row in e["cats"].items():
                        t = pl["totals"].setdefault(cat, {})
                        for l, v in row.items():
                            add_stat(t, l, v)
        for pl in players.values():
            pl["gp"] = len(pl["games"])
            pl["totals"] = {c: {l: show_total(v) for l, v in t.items()} for c, t in pl["totals"].items()}
        leaders = []
        for label, cat, by, show in NFL_LEADERS:
            rows = [(k, pl) for k, pl in players.items() if to_num(pl["totals"].get(cat, {}).get(by, 0)) > 0]
            rows.sort(key=lambda r: -to_num(r[1]["totals"][cat][by]))
            if rows:
                leaders.append({"label": label, "by": by, "players": [
                    {"key": k, "name": pl["name"], "team": roster[k]["team"], "value": pl["totals"][cat][by],
                     "line": " · ".join(f'{pl["totals"][cat].get(l, "0")} {l}' for l in show)} for k, pl in rows[:5]]})
        write_json(f"data/season/{key}.json", {"school": key, "season": season, "updated": iso(now_utc()),
                                               "through": weeks[-1]["label"] if weeks else None, "players": players, "leaders": leaders})
        print(f"season {key}: {len(players)} players with stats, {len(leaders)} leaderboards")


# ---------- roster moves ----------

def record_moves(key, old, new):
    """Compare yesterday's roster with today's and add what changed to data/moves/<school>.json."""
    if not old or not old.get("players") or str(old.get("source", "")).startswith("Snapshot"):
        return
    before = {norm_name(p["name"]): p for p in old["players"]}
    after = {norm_name(p["name"]): p for p in new["players"]}
    moves = []
    add = lambda p, kind, text: moves.append({"date": iso(now_utc())[:10], "name": p["name"], "team": p["team"], "pos": p.get("pos"),
                                              "type": kind, "text": text})
    for k, p in after.items():
        o = before.get(k)
        if not o:
            add(p, "added", f'Joined {p["team"]}' + (f' ({p["status"].lower()})' if p["status"] != "Active" else ""))
        elif o["team"] != p["team"]:
            add(p, "team", f'{o["team"]} → {p["team"]}' + (f' ({p["status"].lower()})' if p["status"] != "Active" else ""))
        elif o["status"] != p["status"]:
            a, b = o["status"], p["status"]
            text = ("Placed on the reserve list" if b == "Reserve" else "Activated from the reserve list" if a == "Reserve" and b == "Active"
                    else "Promoted from the practice squad" if a == "Practice Squad" and b == "Active"
                    else "Moved to the practice squad" if b == "Practice Squad" else f"{a} → {b}")
            add(p, "status", text)
    for k, o in before.items():
        if k not in after:
            add(o, "removed", f'No longer on the {o["team"]} roster')
    if len(moves) > 12:                       # that many in a day is a feed glitch, not news
        problem(f"{len(moves)} roster changes for {key} in one refresh; not recorded as moves, check the feeds")
        return
    if moves:
        rel = f"data/moves/{key}.json"
        log = (read_json(rel, {}) or {}).get("moves", [])
        write_json(rel, {"school": key, "updated": iso(now_utc()), "moves": (moves + log)[:150]})
        print(f"moves {key}: {len(moves)} new")
    elif not read_json(f"data/moves/{key}.json"):
        write_json(f"data/moves/{key}.json", {"school": key, "updated": iso(now_utc()), "since": iso(now_utc())[:10], "moves": []})


# ---------- college (the school's own games) ----------

LEADER_CATS = [  # (label, box category, column to rank by, columns to show)
    ("Passing", "passing", "YDS", ["YDS", "TD", "INT"]),
    ("Rushing", "rushing", "YDS", ["CAR", "YDS", "TD"]),
    ("Receiving", "receiving", "YDS", ["REC", "YDS", "TD"]),
    ("Tackles", "defensive", "TOT", ["TOT", "TFL", "SACKS"]),
    ("Sacks", "defensive", "SACKS", ["SACKS"]),
    ("Interceptions", "interceptions", "INT", ["INT", "TD"]),
]


def to_num(v):
    try:
        return float(str(v).split("/")[0])
    except ValueError:
        return 0.0


def fmt_num(n):
    return str(int(n)) if float(n).is_integer() else f"{n:.1f}"


def score_of(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("displayValue") if s.get("displayValue") not in (None, "") else s.get("value")
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def rank_of(c):
    r = (c.get("curatedRank") or {}).get("current") or c.get("rank")
    return r if isinstance(r, int) and 0 < r <= 25 else None


def college_game(ev, team_id):
    comp = ev["competitions"][0]
    us = next(c for c in comp["competitors"] if str(c["team"]["id"]) == str(team_id))
    opp = next(c for c in comp["competitors"] if c is not us)
    st = (comp.get("status") or ev.get("status") or {}).get("type", {})
    state = st.get("state", "pre")
    b = comp.get("broadcasts") or []
    network = ((b[0].get("media") or {}).get("shortName") or "") if b else ""
    side = lambda c: {"id": str(c["team"].get("id")), "abbr": c["team"].get("abbreviation"), "name": c["team"].get("location") or c["team"].get("displayName"),
                      "rank": rank_of(c), "score": score_of(c) if state != "pre" else None}
    watch, venue = watch_info(comp)
    return {"id": ev["id"], "kickoff": ev["date"], "state": state,
            "detail": st.get("shortDetail", "") if state != "pre" else "", "network": network,
            "watch": watch if state != "post" else [], "venue": venue,
            "where": "neutral" if comp.get("neutralSite") else us.get("homeAway", "home"),
            "homeAway": us.get("homeAway", "home"),
            "us": side(us), "opp": side(opp),
            "win": us.get("winner") if state == "post" else None}


def college_box(summary, team_id):
    """Box score for one game. "cats" is our side, "opp" the other team's:
    {category: {"labels": [...], "rows": [[name, v1, v2, ...], ...]}}. Plus the line score and scoring plays."""
    sides = {True: {}, False: {}}
    for tp in summary.get("boxscore", {}).get("players", []):
        cats = sides[str(tp.get("team", {}).get("id")) == str(team_id)]
        for cat in tp.get("statistics", []):
            rows = [[a.get("athlete", {}).get("displayName", "")] + list(a.get("stats", [])) for a in cat.get("athletes", [])]
            if rows:
                cats[cat.get("name", "other")] = {"labels": cat.get("labels", []), "rows": rows}
    out = {"cats": sides[True], "opp": sides[False], "lines": {}, "scoring": []}
    comps = ((summary.get("header") or {}).get("competitions") or [{}])[0].get("competitors", [])
    us_home = True
    for c in comps:
        mine = str(c.get("id")) == str(team_id)
        if mine:
            us_home = c.get("homeAway") == "home"
        out["lines"]["us" if mine else "opp"] = [l.get("displayValue") for l in c.get("linescores", [])]
    for sp in summary.get("scoringPlays", []):
        h, a = sp.get("homeScore"), sp.get("awayScore")
        out["scoring"].append({"q": (sp.get("period") or {}).get("number"), "clock": (sp.get("clock") or {}).get("displayValue"),
                               "us": str((sp.get("team") or {}).get("id")) == str(team_id),
                               "type": (sp.get("type") or {}).get("abbreviation"), "text": (sp.get("text") or "").strip(),
                               "score": [h, a] if us_home else [a, h]})
    return out


def season_leaders(boxes):
    """Top three per category, added up from every box score we hold."""
    out = []
    for label, cat, key, show in LEADER_CATS:
        totals = {}
        for b in boxes.values():
            c = (b.get("cats") or {}).get(cat)
            if not c:
                continue
            for row in c["rows"]:
                if row[0].strip().lower() in ("", "team"):
                    continue
                t = totals.setdefault(row[0], {})
                for l, v in zip(c["labels"], row[1:]):
                    if l in show:
                        t[l] = t.get(l, 0.0) + to_num(v)
        top = sorted((t for t in totals.items() if t[1].get(key, 0) > 0), key=lambda t: -t[1][key])[:3]
        if top:
            out.append({"label": label, "players": [
                {"name": n, "value": fmt_num(t[key]), "line": " · ".join(f"{fmt_num(t.get(l, 0))} {l}" for l in show)} for n, t in top]})
    return out


OTHER_SPORTS = re.compile(r"basketball|hoops|wrestl|volleyball|baseball|softball|gymnast|soccer|track and field|golf|tennis|"
                          r"swim|field hockey|rowing|cross country|women's|\bwbb\b|\bmbb\b", re.I)


def fetch_feed(url, name):
    """Headlines from an RSS or Atom feed: [{headline, url, published, source}]."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; iahawks-news/1.0)", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    root = ET.fromstring(raw)
    strip = lambda tag: tag.rsplit("}", 1)[-1]
    out = []
    for el in root.iter():
        if strip(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for ch in el:
            t = strip(ch.tag)
            if t == "link":
                if ch.get("href") and ch.get("rel", "alternate") == "alternate":
                    f["url"] = ch.get("href")
                elif (ch.text or "").strip():
                    f.setdefault("url", ch.text.strip())
            elif t in ("title", "pubDate", "published", "updated", "source") and (ch.text or "").strip():
                f.setdefault(t, ch.text.strip())
        when = f.get("pubDate") or f.get("published") or f.get("updated")
        if not (f.get("title") and when and re.match(r"https?://", f.get("url") or "")):
            continue
        try:
            dt = parsedate_to_datetime(when) if "," in when else parse_time(when)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        title, source = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", f["title"])), f.get("source") or name
        if f.get("source") and title.endswith(" - " + f["source"]):      # Google News puts "Headline - Outlet" in the title
            title = title[: -len(" - " + f["source"])]
        out.append({"headline": title, "url": f["url"], "published": iso(dt.astimezone(timezone.utc)), "source": source})
    return out


def gather_news(key, school, team_id):
    """Mix ESPN's team feed with the school's own outlets; keep what's about this team's football, newest first."""
    about = re.compile(school.get("newsMatch") or re.escape(school["name"]), re.I)
    items, ok = [], 0
    try:
        for a in get_json(f"{COLLEGE}/news?team={team_id}&limit=15").get("articles", []):
            url = ((a.get("links") or {}).get("web") or {}).get("href")
            if a.get("headline") and a.get("published") and re.match(r"https?://", url or ""):
                items.append({"headline": a["headline"], "url": url, "published": a["published"], "source": "ESPN",
                              "_text": f'{a["headline"]} {a.get("description") or ""}'})
        ok += 1
    except Exception as e:
        print(f"warning: ESPN news for {key} failed: {e}", file=sys.stderr)
    for feed in school.get("newsFeeds", []):
        try:
            got = fetch_feed(feed["url"], feed["name"])
            if feed.get("must"):                 # an all-sports feed: keep only headlines that say they're football
                got = [g for g in got if re.search(feed["must"], g["headline"], re.I)]
            items += got
            ok += 1
        except Exception as e:
            print(f'warning: news feed {feed["name"]} failed: {e}', file=sys.stderr)
    if not ok:
        problem(f"every news source for {key} failed")
        return []
    cutoff, seen, per_source, out = now_utc() - timedelta(days=10), set(), {}, []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        text = it.pop("_text", it["headline"])
        fp = re.sub(r"[^a-z0-9]", "", it["headline"].lower())[:50]
        if (not about.search(text) or OTHER_SPORTS.search(it["headline"]) or fp in seen
                or parse_time(it["published"]) < cutoff or per_source.get(it["source"], 0) >= 4):
            continue
        seen.add(fp)
        per_source[it["source"]] = per_source.get(it["source"], 0) + 1
        out.append(it)
    return out[:24]


POLLS = [("CFP", "cfp", "?polls=21"), ("AP", "ap", "?polls=1")]   # playoff committee first: it's the one that decides the bracket


def poll_ranks(team_id):
    """Where the team sits in each current poll: [{"poll": "CFP", "name": ..., "rank": 15 or None, "asOf": ...}]."""
    out = []
    for label, kind, query in POLLS:
        try:
            poll = next((p for p in get_json(f"{COLLEGE}/rankings{query}").get("rankings", []) if p.get("type") == kind), None)
        except Exception as e:
            print(f"warning: {label} rankings failed: {e}", file=sys.stderr)
            continue
        stamp = poll and (poll.get("lastUpdated") or poll.get("date"))
        if not stamp or now_utc() - parse_time(stamp) > timedelta(days=45):   # none yet this season, or last season's
            continue
        rank = next((r.get("current") for r in poll.get("ranks", []) if str(r.get("team", {}).get("id")) == str(team_id)), None)
        out.append({"poll": label, "name": poll.get("name"), "rank": rank, "asOf": stamp,
                    "week": (poll.get("occurrence") or {}).get("displayValue")})
    return out


def update_college(key, school, full):
    team_id = school.get("espnTeamId")
    if not team_id:
        return
    rel = f"data/college/{key}.json"
    old = read_json(rel, {}) or {}
    try:
        team = get_json(f"{COLLEGE}/teams/{team_id}")["team"]
        sched = get_json(f"{COLLEGE}/teams/{team_id}/schedule")
        events = list(sched.get("events", []))
        try:                                             # bowl / playoff games live under a separate season type
            post = get_json(f"{COLLEGE}/teams/{team_id}/schedule?seasontype=3")
            events += [e for e in post.get("events", []) if e["id"] not in {x["id"] for x in events}]
        except Exception:
            pass
        games = sorted((college_game(e, team_id) for e in events), key=lambda g: g["kickoff"])
    except Exception as e:
        problem(f"college schedule for {key} failed: {e}")
        return
    season = (sched.get("season") or {}).get("year")
    old_box = old.get("box", {}) if old.get("season") == season else {}
    box, failed = {}, 0
    for g in games:
        if g["state"] == "pre":
            continue
        prev = old_box.get(g["id"])
        if prev and prev.get("final") and prev.get("cats") and "opp" in prev:
            box[g["id"]] = prev
            continue
        try:
            box[g["id"]] = {"final": g["state"] == "post", **college_box(get_json(f"{COLLEGE}/summary?event={g['id']}"), team_id)}
        except Exception as e:
            print(f"warning: college box for game {g['id']} failed: {e}", file=sys.stderr)
            failed += 1
            if prev:
                box[g["id"]] = prev
    if failed > 2:
        problem(f"college box scores for {key}: {failed} games failed")

    news = old.get("news", [])
    if full or not news:
        fresh = gather_news(key, school, team_id)
        if fresh:
            news = fresh

    record = next((i.get("summary") for i in (team.get("record") or {}).get("items", []) if i.get("type") == "total"), None)
    ranked = [g["us"]["rank"] for g in games if g["state"] != "post"] or [g["us"]["rank"] for g in games[-1:]]
    polls = poll_ranks(team_id) if full else (old.get("team") or {}).get("polls", [])
    try:
        rooting = rooting_guide(school, team_id, school.get("name") or team.get("location") or "The team", old.get("rooting"))
    except Exception as e:                               # an extra, never worth losing the page over; keep the last one
        print(f"warning: rooting guide for {key} failed: {e}", file=sys.stderr)
        rooting = old.get("rooting")
    write_json(rel, {
        "school": key, "season": season, "updated": iso(now_utc()), "source": "ESPN college football",
        "team": {"id": str(team_id), "name": team.get("displayName"), "abbr": team.get("abbreviation"),
                 "record": record, "standing": team.get("standingSummary"),
                 "rank": polls[0]["rank"] if polls else (ranked[0] if ranked else None), "polls": polls},
        "games": games, "box": box, "leaders": season_leaders(box), "news": news, "rooting": rooting,
    })
    print(f"college {key}: {len(games)} games, {len(box)} box scores, {len(news)} news items")


# ---------- who to root for ----------
# Late in the season: which other results help the school reach its conference title game (every remaining
# combination of results run through the conference tiebreaker) and climb the playoff ranking.

MAX_OPEN_GAMES = 11                      # 2^11 combinations is the most we enumerate


def neutral_game(ev):
    comp = ev["competitions"][0]
    st = (comp.get("status") or ev.get("status") or {}).get("type", {})
    state = st.get("state", "pre")
    b = comp.get("broadcasts") or []
    side = lambda c: {"id": str(c["team"].get("id")), "abbr": c["team"].get("abbreviation"),
                      "name": c["team"].get("location") or c["team"].get("displayName"),
                      "rank": rank_of(c), "score": score_of(c) if state != "pre" else None}
    sides = {c.get("homeAway"): c for c in comp["competitors"]}
    away, home = sides.get("away") or comp["competitors"][0], sides.get("home") or comp["competitors"][-1]
    winner = next((str(c["team"]["id"]) for c in comp["competitors"] if c.get("winner")), None) if state == "post" else None
    title_game = (any("championship" in (n.get("headline") or "").lower() for n in comp.get("notes") or [])
                  or "championship" in ((comp.get("type") or {}).get("slug") or ""))
    return {"id": ev["id"], "kickoff": ev["date"], "state": state, "detail": st.get("shortDetail", "") if state != "pre" else "",
            "network": ((b[0].get("media") or {}).get("shortName") or "") if b else "",
            "away": side(away), "home": side(home), "winner": winner, "titleGame": title_game,
            "neutral": bool(comp.get("neutralSite"))}


def conference_finalists(results, teams, group_tied=False):
    """results: {frozenset((a, b)): winner}. Returns the top two as a list; an entry is a tuple when the
    published tiebreaker can't separate teams without the ratings service or a draw.

    Follows the Big Ten's 2024 championship-game tiebreaker document step by step. One thing it leaves open: when
    common opponents are themselves tied in the standings, whether to compare against them one at a time (in the
    order the tiebreaker puts them) or as a group. group_tied=True takes the group reading, which is the one that matches
    the Big Ten's own published clinch scenarios (checked against all 128 outcomes of the 2026 final week)."""
    from fractions import Fraction as F
    from itertools import combinations
    opp, W, N = {t: [] for t in teams}, dict.fromkeys(teams, 0), dict.fromkeys(teams, 0)
    for k, w in results.items():
        a, b = tuple(k)
        opp[a].append(b); opp[b].append(a); N[a] += 1; N[b] += 1; W[w] += 1
    pct = {t: F(W[t], N[t]) if N[t] else F(0) for t in teams}
    table = {}

    def rec(t, others):
        others = [o for o in others if o in opp[t]]
        return F(sum(results[frozenset((t, o))] == t for o in others), len(others)) if others else None

    def standings(depth):
        """Order of finish as slots of teams. Ties are broken with this same procedure; one level down, tied
        teams share a slot, which keeps the recursion finite."""
        if depth not in table:
            slots = []
            for p in sorted(set(pct.values()), reverse=True):
                grp = [t for t in teams if pct[t] == p]
                if len(grp) == 1 or depth > 1 or group_tied:
                    slots.append(grp)
                else:
                    slots += [list(x) if isinstance(x, tuple) else [x] for x in rank(grp, depth)]
            table[depth] = slots
        return table[depth]

    # Steps of the published procedure, each tagged with a sortable stage so a shrunken tie can carry on
    # "to the next step" instead of starting over.
    def steps(T, depth):
        pairs = [frozenset(p) for p in combinations(T, 2)]
        if len(T) == 2:                                            # A.1/A.2, B.1: head to head
            if pairs[0] in results:
                yield (0,), {t: int(results[pairs[0]] == t) for t in T}
        elif all(p in results for p in pairs):
            yield (0,), {t: rec(t, [o for o in T if o != t]) for t in T}
        else:                                                      # B.1(a): one team beat all the others
            for t in T:
                if all(results.get(frozenset((t, o))) == t for o in T if o != t):
                    yield (0,), {x: int(x == t) for x in T}
        common = [o for o in teams if o not in T and all(o in opp[t] for t in T)]
        if common:
            yield (1,), {t: rec(t, common) for t in T}             # A.3, B.2: all common opponents
            for i, slot in enumerate(standings(depth + 1)):        # A.4, B.3: one common opponent at a time, in order of finish
                these = [o for o in slot if o in common]
                if these:
                    yield (2, i), {t: rec(t, these) for t in T}
        yield (3,), {t: F(sum(W[o] for o in opp[t]), sum(N[o] for o in opp[t]) or 1) for t in T}   # A.5, B.4

    def rank(T, depth=0, after=None):
        """Rank tied teams. A clear No. 1 is selected and the rest revert to the beginning; two teams level at the
        top go to the two-team procedure; three or more still level move on to the next step together."""
        if len(T) <= 1:
            return list(T)
        for stage, m in steps(T, depth):
            if after is not None and stage <= after:
                continue
            best = max(m.values())
            top, rest = [t for t in T if m[t] == best], [t for t in T if m[t] != best]
            if not rest:
                continue
            if len(T) == 2 or len(top) == 1:
                return top + rank(rest, depth)
            if len(top) == 2:
                return rank(top, depth) + rank(rest, depth)
            return rank(top, depth, stage) + rank(rest, depth)
        return [tuple(sorted(T))]                                  # needs the ratings service or a draw

    out, filled = [], 0
    for p in sorted(set(pct.values()), reverse=True):
        for x in rank([t for t in teams if pct[t] == p]):
            size = len(x) if isinstance(x, tuple) else 1
            if filled + size <= 2:                                 # a tie that fits inside the top two is in, whatever its order
                out += list(x) if isinstance(x, tuple) else [x]
            else:
                out.append(x)                                      # straddles the cut: only the ratings service or a draw decides
            filled += size
            if filled >= 2:
                return out
    return out


STANDINGS = "https://site.api.espn.com/apis/v2/sports/football/college-football/standings?group={}"
P4_GROUPS = {"1": "ACC", "4": "Big 12", "5": "Big Ten", "8": "SEC"}   # their champions get automatic playoff bids
NOTRE_DAME = "87"
PLAYOFF_FIELD = 12                         # 4 power-conference champions + best of the rest + 7 more by ranking


def conference_members(group):
    st = get_json(STANDINGS.format(group))
    name = re.sub(r"\s+Conference$", "", st.get("shortName") or st.get("abbreviation") or st.get("name") or "conference")
    members = {}
    def walk(n):
        for e in (n.get("standings") or {}).get("entries", []):
            members[str(e["team"]["id"])] = e["team"].get("location") or e["team"].get("displayName")
        for c in n.get("children", []):
            walk(c)
    walk(st)
    return name, members


def span(c):
    """A count under the two readings of the tiebreaker: '102' when they agree, '85–102' when they don't."""
    a, b = c
    return str(a) if a == b else f"{min(a, b)}–{max(a, b)}"


def pct(p):
    if p is None:
        return "—"
    if p < 0.005:
        return "<1%" if p > 0 else "0%"
    if p > 0.995:
        return ">99%" if p < 1 else "100%"
    return f"{round(p * 100)}%"


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


ROW_KEYS = ("id", "kickoff", "state", "detail", "network", "away", "home", "winner")
LEVELS = {"must": 0, "big": 1, "split": 2, "helps": 3, "longshot": 4}


def finish_rows(rows, limit=None):
    out = sorted(rows.values(), key=lambda r: (LEVELS[r["level"]], -r.pop("weight", 0), r["kickoff"]))
    return out[:limit] if limit else out


# ---------- win probabilities ----------

FPI = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football/seasons/{}/powerindex?limit=400"
FPI_SD, FPI_HOME = 14.1, 2.4      # points: fit to ESPN's own matchup predictor (week 13 of 2026), for games it has no line on


def fpi_table(season):
    """ESPN's Football Power Index: each team's rating and its own conference-title odds. Returns (table, last updated)."""
    d = get_json(FPI.format(season))
    out, stamp = {}, ""
    for it in d.get("items", []):
        m = re.search(r"teams/(\d+)", (it.get("team") or {}).get("$ref", ""))
        if not m:
            continue
        v = {p["name"]: p.get("value") for p in it.get("predictives", [])}
        out[m.group(1)] = {"fpi": v.get("fpi") or 0.0, "conf": (v.get("probwinconf") or 0) / 100}
        stamp = max(stamp, it.get("lastUpdated") or "")
    return out, (parse_time(stamp) if stamp else None)


def fpi_home(fpi, away, home, neutral=False):
    """Chance the home team wins from FPI ratings alone. Teams FPI doesn't rate (FCS) count as far weaker."""
    from statistics import NormalDist
    fa, fh = (fpi.get(away) or {}).get("fpi", -25.0), (fpi.get(home) or {}).get("fpi", -25.0)
    return NormalDist().cdf((fh - fa + (0 if neutral else FPI_HOME)) / FPI_SD)


def market_home(summary, g):
    """Chance the home team wins from the moneylines ESPN shows, with the bookmaker's margin taken out."""
    implied = lambda m: 100 / (m + 100) if m > 0 else -m / (100 - m)
    for pc in summary.get("pickcenter") or []:
        h, a = (pc.get("homeTeamOdds") or {}), (pc.get("awayTeamOdds") or {})
        try:
            mh, ma = float(h["moneyLine"]), float(a["moneyLine"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(h.get("teamId") or g["home"]["id"]) != g["home"]["id"]:
            mh, ma = ma, mh
        ph, pa = implied(mh), implied(ma)
        return ph / (ph + pa)
    return None


def game_odds(g, fpi, need_pre=True):
    """(chance the home team wins now, chance it had before kickoff). Before kickoff that's the betting line ESPN shows
    (its matchup predictor when there's no line); during the game, ESPN's live win probability; after it, the result."""
    now = (1.0 if g["winner"] == g["home"]["id"] else 0.0) if g["winner"] else None
    pre = fpi_home(fpi, g["away"]["id"], g["home"]["id"], g.get("neutral"))
    if now is None or need_pre:
        try:
            s = get_json(f"{COLLEGE}/summary?event={g['id']}")
            pr = s.get("predictor") or {}
            home = pr.get("homeTeam") or {}
            if home.get("gameProjection") not in (None, ""):
                p = float(home["gameProjection"]) / 100
                pre = p if str(home.get("id")) == g["home"]["id"] else 1 - p
            line = market_home(s, g)                               # the betting line beats a ratings model when there is one
            if line is not None:
                pre = line
            wp = s.get("winprobability") or []
            if now is None and g["state"] == "in" and wp and wp[-1].get("homeWinPercentage") is not None:
                now = min(max(float(wp[-1]["homeWinPercentage"]), 0.001), 0.999)
        except Exception as e:
            print(f"warning: odds for game {g['id']}: {e}", file=sys.stderr)
    pre = min(max(pre, 0.01), 0.99)
    return (pre if now is None else now), pre


# ---------- conference title game ----------

def title_view(me, nickname, conf_name, members, sched, prob):
    """Every remaining combination of conference results, run through the tiebreaker under both readings and
    weighted by each game's odds (prob: event id -> chance the home team wins)."""
    from itertools import product
    conf_games = [g for g in sched.values() if g["away"]["id"] in members and g["home"]["id"] in members and not g["titleGame"]]
    played = {frozenset((g["away"]["id"], g["home"]["id"])): g["winner"] for g in conf_games if g["winner"]}
    open_games = sorted((g for g in conf_games if not g["winner"]), key=lambda g: g["kickoff"])
    if not (0 < len(open_games) <= MAX_OPEN_GAMES and len(played) > len(conf_games) / 2):
        return None, None, None
    name = lambda tid, g: (g["away"] if g["away"]["id"] == tid else g["home"])["name"]
    ids = [(g["away"]["id"], g["home"]["id"]) for g in open_games]
    ph = [min(max(prob.get(g["id"], 0.5), 0.001), 0.999) for g in open_games]
    teams = list(members)
    outcomes, weight = {}, {}
    for combo in product(*ids):
        res = dict(played)
        res.update({frozenset(pair): w for pair, w in zip(ids, combo)})
        f = conference_finalists(res, teams, group_tied=True)     # the reading the Big Ten's own clinch scenarios use
        outcomes[combo] = (f, f)
        w = 1.0
        for (a, h), p, x in zip(ids, ph, combo):
            w *= p if x == h else 1 - p
        weight[combo] = w
    mine = next((i for i, pair in enumerate(ids) if me in pair), None)
    pool = {c: o for c, o in outcomes.items() if mine is None or c[mine] == me}   # we win our own game
    total = len(pool)

    def count(cond, src=pool):
        """Combinations (not weighted) where we get there, under each reading: for 'clinched' and 'out'."""
        a = b = 0
        for c, (fa, fb) in src.items():
            if cond(c):
                a += me in fa; b += me in fb
        return a, b

    def chance(cond, src=pool, reading=None):
        """Chance we get there given cond. Averages the two readings unless one is asked for."""
        tot = hit = 0.0
        for c, (fa, fb) in src.items():
            if cond(c):
                w = weight[c]
                tot += w
                hit += w * ((me in fa) if reading == 0 else (me in fb) if reading == 1 else ((me in fa) + (me in fb)) / 2)
        return hit / tot if tot else 0.0

    every = lambda c: True
    good = count(every)
    lose = count(lambda c: c[mine] != me, outcomes) if mine is not None else (0, 0)
    overall = chance(every, outcomes)
    title, lines, rows = f"{conf_name} title game", [], {}

    def row(g, pick, level, why, weight, odds=None):
        rows[g["id"]] = {**{k: g[k] for k in ROW_KEYS}, "pick": pick, "level": level, "why": why, "weight": weight,
                         **({"chance": {t: round(p, 4) for t, p in odds.items()}} if odds else {})}

    if mine is not None:
        g = open_games[mine]
        opp = name(next(t for t in ids[mine] if t != me), g)
        win_c, lose_c = chance(every), chance(lambda c: c[mine] != me, outcomes)
        if good == (0, 0):
            lines.append(f"{nickname} can no longer reach the {title}.")
        elif good == (total, total) and lose == (total, total):
            lines.append(f"{nickname} is in the {title} whatever happens this week.")
        elif good == (total, total):
            lines.append(f"Beat {opp} and {nickname} is in the {title}."
                         + (" Lose and it is out." if lose == (0, 0) else f" Lose and its chance drops to {pct(lose_c)}."))
            row(g, me, "must", f"A win clinches a spot in the {title}.", 9, {me: 1.0, next(t for t in ids[mine] if t != me): lose_c})
        else:
            lines.append(f"If {nickname} beats {opp}, its chance of reaching the {title} is {pct(win_c)}."
                         + (" If it loses, it is out." if lose == (0, 0) else f" If it loses, {pct(lose_c)}."))
            row(g, me, "must" if lose == (0, 0) else "big", "Everything starts here.", 9,
                {me: win_c, next(t for t in ids[mine] if t != me): lose_c})
    elif good == (0, 0):
        lines.append(f"{nickname} can no longer reach the {title}.")
    elif good == (total, total):
        lines.append(f"{nickname} is in the {title} whatever happens in the remaining games.")
    else:
        lines.append(f"{nickname} gets there in {span(good)} of the {total} ways the remaining {conf_name} games can finish, "
                     f"or {pct(overall)} once each way is weighted by the odds of its games.")

    if 0 < sum(good) < 2 * total:
        for i, (a, b) in enumerate(ids):
            if i == mine:
                continue
            g = open_games[i]
            ca, cb = chance(lambda c: c[i] == a), chance(lambda c: c[i] == b)
            if abs(ca - cb) < 0.02:
                continue
            pick, other, cp, co = (a, b, ca, cb) if ca > cb else (b, a, cb, ca)
            flip = None                                            # does the better result change with another game?
            for j, pair in enumerate(ids):
                if j in (i, mine):
                    continue
                for w in pair:
                    o_ = chance(lambda c: c[i] == other and c[j] == w)
                    p_ = chance(lambda c: c[i] == pick and c[j] == w)
                    if o_ - p_ > 0 and (not flip or o_ - p_ > flip[0]):
                        flip = (o_ - p_, j, w, o_, p_)
            why, level = "", "big" if cp - co >= 0.25 else "helps"
            if flip and flip[0] >= 0.1:
                d, j, w, o_, p_ = flip
                hg = open_games[j]
                wn, ln = name(w, hg), name(next(t for t in ids[j] if t != w), hg)
                first = parse_time(hg["kickoff"]) < parse_time(g["kickoff"])
                timing = (" That game kicks off first, so you'll know which way to lean." if first
                          else " That game kicks off later, so you won't know yet.")
                if d >= 0.25:
                    level = "split"
                    why = (f"It depends on {hg['away']['name']} at {hg['home']['name']}. If {wn} wins, {nickname} wants {name(other, g)} here "
                           f"({pct(o_)} against {pct(p_)}). If {ln} wins, it wants {name(pick, g)}.{timing}"
                           + ("" if first else f" Until then, {name(pick, g)} winning is the better bet."))
                    if first:
                        pick = None
                else:
                    why = f"The exception: if {wn} beats {ln}, {nickname} is better off with {name(other, g)} winning ({pct(o_)} against {pct(p_)}).{timing}"
            row(g, pick, level, why, (cp - co) + (2 if level == "split" else 0), {a: ca, b: cb})

    note = ("Each remaining game is weighted by its odds: the betting line ESPN shows before kickoff (ESPN's matchup predictor "
            "where there's no line), ESPN's live win probability during the game. The finalists follow the Big Ten's published "
            "tiebreaker, read the way the conference's own clinch scenarios read it; ties it can only settle with a ratings "
            "service or a draw count as misses.")
    if mine is not None and sum(good):
        note += f" Chances for the other games assume {nickname} wins its own."

    def reach(t):
        """How often team t can still be a finalist if it wins / loses its open game: (wins, losses, per side)."""
        i = next((k for k, pair in enumerate(ids) if t in pair), None)
        hit = lambda f: any(t == x or (isinstance(x, tuple) and t in x) for x in f)
        if i is None:
            return None
        src = outcomes if t == me or mine is None else pool
        w = sum(1 for c, (fa, fb) in src.items() if c[i] == t and (hit(fa) or hit(fb)))
        l = sum(1 for c, (fa, fb) in src.items() if c[i] != t and (hit(fa) or hit(fb)))
        return w, l, len(src) // 2, open_games[i]

    view = {"label": f"{conf_name} title", "pct": round(overall, 4), "summary": lines, "games": finish_rows(rows), "note": note}
    return view, reach, {"open": [g["id"] for g in open_games], "outcomes": outcomes}


# ---------- playoff ----------

SIMS = 20000                       # simulated finishes of the season
DROP_LOSS, DROP_LOSS_RANKED, DROP_TITLE_LOSS = 6.0, 3.5, 1.5   # spots a loss costs in the committee's ranking (2024 drops)
BUMP_WIN_RANKED, BUMP_TITLE_WIN = 1.0, 1.0                     # spots a win over a ranked team / a title gains
RESHUFFLE = 1.0                    # the committee's own judgment, as random spots (standard deviation)
STALE_LOSS = 0.25                  # a loss FPI hasn't seen yet cuts a team's conference-title odds to this share


def playoff_chances(me, conf, conf_of, rank_of, fpi, fpi_stamp, games, odds, model):
    """Simulate the rest of the season SIMS times: this week's games with ESPN's odds, the conference title games, and
    the committee's final ranking (loosely, from the latest one). Returns (chance of making the field,
    {event: {team: chance if that team wins}}, chance of reaching our own conference title game)."""
    import random
    rnd = random.Random(12)                                        # same inputs, same answer
    by_conf = {}
    for t, c in conf_of.items():
        by_conf.setdefault(c, []).append(t)
    titles = {conf_of[g["away"]["id"]]: g for g in games
              if g["titleGame"] and conf_of.get(g["away"]["id"]) and conf_of.get(g["away"]["id"]) == conf_of.get(g["home"]["id"])}
    regular = [g for g in games if g not in titles.values()]
    open_regular = [g for g in regular if not g["winner"]]
    # FPI's conference-title odds are refreshed once a day; adjust them for results since then.
    seen = lambda g: g["winner"] and fpi_stamp and parse_time(g["kickoff"]) + timedelta(hours=4) < fpi_stamp
    team_game = {g[s]["id"]: g for g in regular if not seen(g) for s in ("away", "home")}

    def adjust(t, win):
        g = team_game.get(t)
        if not g:
            return 1.0
        p = odds[g["id"]][1] if t == g["home"]["id"] else 1 - odds[g["id"]][1]
        den = p + (1 - p) * STALE_LOSS
        return (1 if win[g["id"]] == t else STALE_LOSS) / den

    def choose(weights):
        tot = sum(weights.values())
        if tot <= 0:
            return None
        r = rnd.random() * tot
        for t, w in weights.items():
            r -= w
            if r <= 0:
                return t
        return t

    base = {t: float(r) for t, r in rank_of.items()}
    made = in_title = 0
    tally = {g["id"]: {g["away"]["id"]: [0, 0], g["home"]["id"]: [0, 0]} for g in open_regular}
    for _ in range(SIMS):
        win = {g["id"]: g["home"]["id"] if rnd.random() < odds[g["id"]][0] else g["away"]["id"] for g in regular}
        score = {t: b + rnd.gauss(0, RESHUFFLE) for t, b in base.items()}
        for g in regular:
            w = win[g["id"]]
            l = g["away"]["id"] if w == g["home"]["id"] else g["home"]["id"]
            if l in score:
                score[l] += DROP_LOSS_RANKED if w in rank_of else DROP_LOSS
            if w in score and l in rank_of:
                score[w] -= BUMP_WIN_RANKED
        champs = {}
        for c, mem in by_conf.items():
            w = l = None
            if c in titles:
                g = titles[c]
                w = g["winner"] or (g["home"]["id"] if rnd.random() < odds.get(g["id"], (0.5,))[0] else g["away"]["id"])
                l = g["away"]["id"] if w == g["home"]["id"] else g["home"]["id"]
            elif c == conf and model:
                pair = model["outcomes"][tuple(win[gid] for gid in model["open"])][rnd.random() < 0.5]   # either reading
                f = []
                for x in pair:
                    cands = list(x) if isinstance(x, tuple) else [x]
                    rnd.shuffle(cands)                             # a tie only a ratings service or draw can settle
                    f += [t for t in cands if t not in f]
                if len(f) >= 2:
                    a, h = f[:2]
                    in_title += me in (a, h)
                    w = h if rnd.random() < fpi_home(fpi, a, h, neutral=True) else a
                    l = a if w == h else h
            if w is None:                                          # title game not set yet: FPI's odds, adjusted
                w = choose({t: fpi[t]["conf"] * adjust(t, win) for t in mem if t in fpi and fpi[t]["conf"] > 0})
                if w is None:
                    w = min(mem, key=lambda t: score.get(t, 99))
                l = choose({t: fpi[t]["conf"] * adjust(t, win) for t in mem if t != w and t in fpi})   # the likeliest other finalists
            champs[c] = w
            if w in score:
                score[w] -= BUMP_TITLE_WIN
            if l in score:
                score[l] += DROP_TITLE_LOSS
        order = sorted(score, key=score.get)
        field = set(champs.values())
        best_rest = next((t for t in order if t not in conf_of and t != NOTRE_DAME), None)
        size = PLAYOFF_FIELD if best_rest else PLAYOFF_FIELD - 1   # else the bid goes to a team outside the top 25
        if best_rest:
            field.add(best_rest)
        if NOTRE_DAME in score and order.index(NOTRE_DAME) < PLAYOFF_FIELD:
            field.add(NOTRE_DAME)
        for t in order:
            if len(field) >= size:
                break
            field.add(t)
        hit = me in field
        made += hit
        for g in open_regular:
            cell = tally[g["id"]][win[g["id"]]]
            cell[0] += hit
            cell[1] += 1
    cond = {gid: {t: (h / n if n else None) for t, (h, n) in sides.items()} for gid, sides in tally.items()}
    return made / SIMS, cond, (in_title / SIMS if model else None)


def playoff_view(me, nickname, conf_name, group, members, sched, add_schedule, reach, sim):
    """Where the school stands for the 12-team playoff: the latest committee ranking, plus a simulation of the rest
    of the season (sim: dict with fpi, stamp, games, odds, model, get_odds)."""
    now = now_utc()
    try:
        poll = next((p for p in get_json(f"{COLLEGE}/rankings?polls=21").get("rankings", []) if p.get("type") == "cfp"), None)
    except Exception:
        return None
    stamp = poll and (poll.get("lastUpdated") or poll.get("date"))
    if not stamp or now - parse_time(stamp) > timedelta(days=10):
        return None
    ranked = sorted(((r["current"], str(r["team"]["id"]), r["team"].get("location") or r["team"].get("displayName"))
                     for r in poll.get("ranks", [])))
    rank_of, label = {t: r for r, t, _ in ranked}, {t: n for _, t, n in ranked}
    my_rank = rank_of.get(me)
    conf_of = {}
    for g, cname in P4_GROUPS.items():
        mem = members if g == str(group) else conference_members(g)[1]
        for t in mem:
            conf_of[t] = cname
    champ = {}                                                     # projected: each power conference's top-ranked team
    for r, t, _ in ranked:
        if conf_of.get(t) and conf_of[t] not in champ:
            champ[conf_of[t]] = t
    best_rest = next((t for r, t, _ in ranked if t not in conf_of and t != NOTRE_DAME), None)
    autos = set(champ.values()) | ({best_rest} if best_rest else set())
    spots = PLAYOFF_FIELD - len(P4_GROUPS) - 1
    others = [t for r, t, _ in ranked if t not in autos]
    if NOTRE_DAME in others and rank_of[NOTRE_DAME] <= PLAYOFF_FIELD and others.index(NOTRE_DAME) >= spots:
        spots -= 1                                                 # Notre Dame's own top-12 guarantee takes a spot
    lines, rows = [f"Win the {conf_name} title and {nickname} is in: the ACC, Big Ten, Big 12 and SEC champions get automatic bids."], {}
    ahead = []
    if not my_rank:
        lines.append(f"{nickname} isn't in the committee's top 25, so the title is its only realistic way in.")
    elif me in autos:
        lines.append(f"{nickname} is the top-ranked {conf_name} team at No. {my_rank}, so it holds the projected automatic bid for now.")
    else:
        ahead = others[:others.index(me)]
        pos = len(ahead) + 1
        if pos <= spots:
            lines.append(f"Otherwise it needs one of {spots} at-large spots. At No. {my_rank} in the committee's ranking it is "
                         f"{ordinal(pos)} in line, so it would be in today.")
        else:
            need = pos - spots
            lines.append(f"Otherwise it needs one of {spots} at-large spots. At No. {my_rank} in the committee's ranking it is "
                         f"{ordinal(pos)} in line, so {need} of the {len(ahead)} teams ahead of it {'needs' if need == 1 else 'need'} to fall behind it.")

    for t in set(ahead) | autos | {me}:
        try:
            add_schedule(t)
        except Exception as e:
            print(f"warning: schedule for team {t} failed: {e}", file=sys.stderr)

    def nxt(t):
        games = sorted((g for g in sched.values() if t in (g["away"]["id"], g["home"]["id"]) and not g["winner"]
                        and parse_time(g["kickoff"]) - now < timedelta(days=8)), key=lambda g: g["kickoff"])
        return games[0] if games else None

    def row(g, pick, level, why, weight):
        r = rows.get(g["id"])
        if r:                                                      # a second reason for the same result
            r["why"] += " " + why
            if LEVELS[level] < LEVELS[r["level"]]:
                r["level"] = level
            return
        rows[g["id"]] = {**{k: g[k] for k in ROW_KEYS}, "pick": pick, "level": level, "why": why, "weight": weight}

    mine = nxt(me)
    if mine:
        opp = next(s for s in (mine["away"], mine["home"]) if s["id"] != me)["name"]
        row(mine, me, "must", f"Start here. A loss to {opp} would all but end {nickname}'s at-large hopes"
                              + (f" and knock it out of the {conf_name} race." if reach and reach(me) and not reach(me)[1] else "."), 9)
    # Teams ahead of us in the at-large line: a loss could drop them behind.
    for t in ahead:
        g = nxt(t)
        if not g or me in (g["away"]["id"], g["home"]["id"]):
            continue
        foe = next(s for s in (g["away"], g["home"]) if s["id"] != t)
        if foe["id"] in ahead or foe["id"] in autos:
            continue                                               # both ahead of us: whoever loses, the line is the same length
        gap = my_rank - rank_of[t]
        why = (f"No. {rank_of[t]} {label[t]} is {gap} spot{'s' if gap > 1 else ''} ahead of {nickname} in the at-large line. "
               + ("A loss could drop it behind." if gap <= 6 else "A loss might not drop it far enough, but it would help."))
        row(g, foe["id"], "big" if gap <= 3 else "helps" if gap <= 6 else "longshot", why, 1 / gap)
    # Teams behind us that could still take an automatic bid away from a team ahead of us.
    for g in list(sched.values()):
        if not g["titleGame"] or g["winner"] or parse_time(g["kickoff"]) - now > timedelta(days=8):
            continue
        a, h = g["away"]["id"], g["home"]["id"]
        if me in (a, h) or not (conf_of.get(a) and conf_of.get(a) == conf_of.get(h)):
            continue
        hi_, lo_ = sorted((a, h), key=lambda t: rank_of.get(t, 99))
        ahead_of_us = lambda t: t in rank_of and (my_rank is None or rank_of[t] < my_rank)
        if ahead_of_us(hi_) and not ahead_of_us(lo_):              # an upset would add a team to the line ahead of us
            row(g, hi_, "big", f"If {label.get(lo_) or name_of(g, lo_)} wins, it takes the {conf_of[a]} automatic bid from behind {nickname}, "
                               f"and {label[hi_]} joins the at-large line ahead of it.", 2)
    if reach and my_rank and me not in autos:
        top = champ.get(P4_GROUPS.get(str(group)))
        for t in members:
            if t == me or rank_of.get(t, 99) < my_rank:
                continue
            info = reach(t)
            if not info or not info[0] or top is None:
                continue
            w, l, n, g = info
            foe = next(s for s in (g["away"], g["home"]) if s["id"] != t)
            if l or w / n < 0.03:
                continue                                           # gets there either way, or almost never
            row(g, foe["id"], "helps", f"{members[t]} is ranked behind {nickname}, but a win over {foe['name']} could put it in the "
                                       f"{conf_name} title game. Winning that would give it the automatic bid and put {label[top]} in "
                                       f"the at-large line ahead of {nickname}.", 0.5)

    # The simulation: chances overall, and which results move them.
    chance, title_chance = None, None
    if sim:
        involved = set(rank_of) | set(members) | {t for t, v in sim["fpi"].items() if conf_of.get(t) and v["conf"]}
        games = [g for g in sim["games"] if g["away"]["id"] in involved or g["home"]["id"] in involved]
        have = {g["id"] for g in games}
        games += [g for g in sched.values() if g["id"] in sim["model_ids"] and g["id"] not in have]
        odds = sim["get_odds"](games)
        chance, cond, title_chance = playoff_chances(me, P4_GROUPS.get(str(group)), conf_of, rank_of, sim["fpi"],
                                                     sim["stamp"], games, odds, sim["model"])
        sim["seen"] = [g["id"] for g in games]
        by_id = {g["id"]: g for g in games}
        moved = {}
        for gid, sides in cond.items():
            g = by_id[gid]
            a, h = g["away"]["id"], g["home"]["id"]
            if me in (a, h) or sides.get(a) is None or sides.get(h) is None:
                continue
            swing = abs(sides[a] - sides[h])
            if swing < 0.01:
                rows.pop(gid, None)                                # sounded like it mattered; the numbers say it doesn't
                continue
            pick = a if sides[a] > sides[h] else h
            old = rows.pop(gid, None)
            why = old["why"] if old and old["pick"] == pick else ""
            moved[gid] = {**{k: g[k] for k in ROW_KEYS}, "pick": pick, "why": why, "weight": swing,
                          "level": "big" if swing >= 0.08 else "helps" if swing >= 0.03 else "longshot",
                          "chance": {a: round(sides[a], 4), h: round(sides[h], 4)}}
        rows = {gid: r for gid, r in rows.items() if r["level"] == "must" or gid not in cond} | moved
    note = ("Chances come from playing out the rest of the season 20,000 times. This week's games use the betting line ESPN shows "
            "(its matchup predictor where there's none, its live win probability once a game starts). The Big Ten title game comes from the conference "
            "tiebreaker and is played out with ESPN's FPI ratings; other conference champions come from FPI's title odds. "
            "The committee is modeled loosely from its latest ranking: a loss costs a team about 6 spots (3 or 4 against a ranked "
            "team), a title-game loss 1 or 2, plus some random reshuffling. Treat it as an estimate, not a prediction of "
            "the committee." if sim else
            "Based on this week's committee ranking, which is redone every Tuesday, so a loss by a team ahead doesn't guarantee it "
            "drops behind.")
    note += (" The field is 12: the ACC, Big Ten, Big 12 and SEC champions, the highest-ranked team from the other conferences, "
             "and the next seven in the ranking.")
    view = {"label": "Playoff", "summary": lines, "games": finish_rows(rows, 10), "note": note}
    if chance is not None:
        view["pct"] = round(chance, 4)
    return view


def name_of(g, t):
    return (g["away"] if g["away"]["id"] == t else g["home"])["name"]


def final_line(g):
    w, l = (g["home"], g["away"]) if g["winner"] == g["home"]["id"] else (g["away"], g["home"])
    return f"{w['name']} {w['score']}, {l['name']} {l['score']}" if w.get("score") is not None else f"{w['name']} beat {l['name']}"


ROOTING_MODEL = 2                  # bump when the way the chances are worked out changes, so the log starts over


def rooting_guide(school, team_id, nickname, prev=None):
    group = school.get("conferenceGroup")
    if not group:
        return None
    me = str(team_id)
    conf_name, members = conference_members(group)
    if me not in members:
        return None
    sched = {}                                                     # event id -> game, across the whole conference

    def add_schedule(tid):
        for ev in get_json(f"{COLLEGE}/teams/{tid}/schedule?seasontype=2").get("events", []):   # regular season only: no bowls
            if ev["id"] not in sched:
                sched[ev["id"]] = neutral_game(ev)
    for tid in members:
        add_schedule(tid)

    # Odds for every game the chances depend on, fetched once.
    sb = get_json(f"{COLLEGE}/scoreboard?groups=80&limit=400")
    week_no = (sb.get("week") or {}).get("number")
    season = (sb.get("season") or {}).get("year") or now_utc().year
    week = []
    for ev in sb.get("events", []):
        try:
            week.append(neutral_game(ev))
        except (KeyError, IndexError):
            continue
    try:
        fpi, stamp = fpi_table(season)
    except Exception as e:
        print(f"warning: FPI failed: {e}", file=sys.stderr)
        fpi, stamp = {}, None
    cache = {}

    def get_odds(games):
        for g in games:
            if g["id"] not in cache:
                cache[g["id"]] = game_odds(g, fpi, need_pre=not g["winner"] or not stamp or parse_time(g["kickoff"]) > stamp - timedelta(hours=4))
        return {g["id"]: cache[g["id"]] for g in games}

    conf_open = [g for g in sched.values() if not g["winner"] and g["away"]["id"] in members and g["home"]["id"] in members]
    prob = {gid: o[0] for gid, o in get_odds(conf_open).items()}
    title, reach, model = title_view(me, nickname, conf_name, members, sched, prob)
    sim = {"fpi": fpi, "stamp": stamp, "games": week, "model": model, "model_ids": set((model or {}).get("open", [])),
           "get_odds": get_odds} if fpi else None
    try:
        playoff = playoff_view(me, nickname, conf_name, group, members, sched, add_schedule, reach, sim)
    except Exception as e:
        print(f"warning: playoff view failed: {e}", file=sys.stderr)
        playoff = None
    views = {k: v for k, v in (("playoff", playoff), ("title", title)) if v and (v["games"] or v["summary"])}
    if not views:
        return None
    out = {"updated": iso(now_utc()), "default": "playoff" if "playoff" in views else "title", "views": views, "week": week_no,
           "model": ROOTING_MODEL}

    # How the chances have moved as this week's games finished.
    cur = {k: views[k]["pct"] for k in views if views[k].get("pct") is not None}
    if cur:
        prev = prev or {}
        same = prev.get("week") == week_no and prev.get("model") == ROOTING_MODEL   # a new week or a new method starts over
        hist = list(prev.get("history") or []) if same else []
        tracked = {g["id"]: g for g in week + list(sched.values()) if g["id"] in set((sim or {}).get("seen", [])) | {x["id"] for x in conf_open}}
        listed = {g["id"] for v in (prev.get("views") or {}).values() for g in v.get("games", [])}
        newly = [tracked[gid] for gid in prev.get("open", []) if gid in tracked and tracked[gid]["winner"]]
        if not hist:
            hist.append({"at": out["updated"], "label": "Starting point", **cur})
        elif newly and same:
            moved = max(abs(cur.get(k, 0) - (hist[-1].get(k) or 0)) for k in cur)
            names = [final_line(g) for g in sorted(newly, key=lambda g: (g["id"] not in listed, g["kickoff"]))]
            if moved >= 0.005 or any(g["id"] in listed for g in newly):
                hist.append({"at": out["updated"], "label": "; ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else ""), **cur})
        out["history"] = hist[-40:]
        out["open"] = sorted(gid for gid, g in tracked.items() if not g["winner"])
    return out


# ---------- main ----------

def main():
    if "--test-alert" in sys.argv:           # prove the alerting works: record a fake problem and fail, touching nothing else
        status = read_json("data/status.json", {}) or {}
        status["problems"] = ["Test alert: this failure was triggered on purpose to check that notifications arrive. Nothing is wrong."]
        write_json("data/status.json", status)
        print("test alert: failing on purpose", file=sys.stderr)
        sys.exit(1)
    auto = "--auto" in sys.argv
    college_only = "--college" in sys.argv
    with open(os.path.join(ROOT, "schools.json")) as f:
        schools = json.load(f)["schools"]

    now = now_utc()
    nfl_live = in_live_window(read_json("data/week.json"), now)
    college_live = set()                     # the school's own game, or one in its "who to root for" list
    for k in schools:
        c = read_json(f"data/college/{k}.json") or {}
        rooting_games = [g for v in ((c.get("rooting") or {}).get("views") or {}).values() for g in v.get("games", [])]
        if in_live_window(c, now) or in_live_window({"games": rooting_games}, now):
            college_live.add(k)
    if auto and not nfl_live and not college_live:
        print("no game live or about to start; nothing to do")
        return

    status = read_json("data/status.json", {}) or {}
    stamp = lambda part: status.__setitem__(part, iso(now_utc()))

    if college_only:
        pass
    elif not auto or nfl_live:
        week = None
        try:
            week = build_week(get_json(SCOREBOARD))
            write_json("data/week.json", week)
            print(f'week {week["week"]}: {len(week["games"])} games')
            stamp("scores")
        except Exception as e:
            problem(f"NFL scoreboard failed: {e}")

        if not auto:
            try:
                try:
                    players = get_json(SLEEPER_PLAYERS)
                    if not isinstance(players, dict) or len(players) < 1000:
                        raise ValueError("response doesn't look like the player list")
                except Exception as e:           # carry on from ESPN rosters alone, but say so
                    problem(f"Sleeper player list failed ({e}); rosters built from ESPN only, without Sleeper's injury tags")
                    players = {}
                rosters = fetch_rosters()
                ok = True
                for key, school in schools.items():
                    data = reconcile(build_players(players, key, school), school, rosters)
                    data = add_draft_info(data, read_json(f"data/players/{key}.json"))
                    if len(data["players"]) < 3:   # guard against a bad feed wiping a school's list
                        problem(f"only {len(data['players'])} NFL players found for {key}; kept the previous file")
                        ok = False
                        continue
                    if players:                  # only when both sources answered, so an outage isn't logged as moves
                        record_moves(key, read_json(f"data/players/{key}.json"), data)
                    write_json(f"data/players/{key}.json", data)
                    print(f'{key}: {len(data["players"])} players')
                if ok:
                    stamp("rosters")
            except Exception as e:
                problem(f"roster refresh failed: {e}")

        if week:
            try:
                update_stats(week, schools)
            except Exception as e:
                problem(f"NFL box scores failed: {e}")
            if not auto:
                try:
                    archive_weeks(week, schools)
                except Exception as e:
                    problem(f"saving finished weeks failed: {e}")
            try:
                update_season(week, schools)
            except Exception as e:
                problem(f"building season stats failed: {e}")

    for key, school in schools.items():
        if not auto or key in college_live:
            before = len(PROBLEMS)
            update_college(key, school, full=not auto)
            if len(PROBLEMS) == before and school.get("espnTeamId"):
                stamp("college")

    status["lastRun"] = iso(now_utc())
    status["problems"] = PROBLEMS
    write_json("data/status.json", status)
    if PROBLEMS:
        print(f"{len(PROBLEMS)} problem(s); failing the run so it gets noticed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
