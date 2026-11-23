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
    write_json(rel, {
        "school": key, "season": season, "updated": iso(now_utc()), "source": "ESPN college football",
        "team": {"id": str(team_id), "name": team.get("displayName"), "abbr": team.get("abbreviation"),
                 "record": record, "standing": team.get("standingSummary"),
                 "rank": polls[0]["rank"] if polls else (ranked[0] if ranked else None), "polls": polls},
        "games": games, "box": box, "leaders": season_leaders(box), "news": news,
    })
    print(f"college {key}: {len(games)} games, {len(box)} box scores, {len(news)} news items")


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
    college_live = {k for k in schools if in_live_window(read_json(f"data/college/{k}.json"), now)}
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
