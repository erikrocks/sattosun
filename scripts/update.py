#!/usr/bin/env python3
"""Refresh the site's data files.

Writes:
  data/week.json               this week's NFL games and scores (ESPN scoreboard)
  data/players/<school>.json   every NFL player from that school (Sleeper, checked against ESPN team rosters)
  data/stats.json              box-score lines for tracked players (ESPN game summaries)

Standard library only, so it runs on a bare GitHub Actions runner.

  python scripts/update.py           full refresh: scores, rosters, stats
  python scripts/update.py --auto    only if a game is live or about to start: scores + stats
                                     (cheap no-op otherwise, so it can run every 15 minutes)
"""
import json, os, re, sys, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={}"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
ROSTER = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{}/roster"
ESPN_ABBR = {"WAS": "WSH"}            # our abbreviation -> ESPN's, where they differ
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
        games.append({
            "id": ev["id"],
            "kickoff": ev["date"],
            "state": state,
            "detail": st.get("shortDetail", "") if state != "pre" else "",
            "network": network,
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


def in_live_window(week, now):
    for g in (week or {}).get("games", []):
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
                    }
            if len(team) >= 40:              # a short list means a bad response, not a real roster
                rosters[t] = team
        except Exception as e:
            print(f"warning: ESPN roster for {t} failed: {e}", file=sys.stderr)
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
                             "status": espn_status(e) or "Active", "injury": None,
                             "college": e["college"], "espn_id": e["id"]})
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
        })
    out.sort(key=lambda x: (x["team"], x["name"]))
    return {"school": school_key, "updated": iso(now_utc()), "source": "Sleeper API", "players": out}


# ---------- stats ----------

def parse_summary(summary, tracked):
    """Return {norm_name: {"team": abbr, "cats": {category: {label: value}}}} for tracked names."""
    found = {}
    for tp in summary.get("boxscore", {}).get("players", []):
        team = norm(tp.get("team", {}).get("abbreviation"))
        for cat in tp.get("statistics", []):
            labels = cat.get("labels", [])
            for a in cat.get("athletes", []):
                key = norm_name(a.get("athlete", {}).get("displayName"))
                if key not in tracked:
                    continue
                entry = found.setdefault(key, {"team": team, "id": a.get("athlete", {}).get("id"), "cats": {}})
                entry["cats"][cat.get("name", "other")] = dict(zip(labels, a.get("stats", [])))
    return found


def update_stats(week, schools):
    tracked_by_team = {}
    for key in schools:
        for p in (read_json(f"data/players/{key}.json", {}) or {}).get("players", []):
            tracked_by_team.setdefault(p["team"], set()).add(norm_name(p["name"]))

    old = read_json("data/stats.json", {}) or {}
    old_games = old.get("games", {}) if old.get("week") == week.get("week") else {}
    games = {}
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
                              "players": parse_summary(get_json(SUMMARY.format(g["id"])), tracked)}
        except Exception as e:                           # one bad game shouldn't sink the run
            print(f"warning: stats for game {g['id']} failed: {e}", file=sys.stderr)
            if prev:
                games[g["id"]] = prev
    write_json("data/stats.json", {"season": week["season"], "week": week["week"],
                                   "updated": iso(now_utc()), "source": "ESPN game summaries", "games": games})
    print(f"stats: {len(games)} games")


# ---------- main ----------

def main():
    auto = "--auto" in sys.argv
    with open(os.path.join(ROOT, "schools.json")) as f:
        schools = json.load(f)["schools"]

    if auto and not in_live_window(read_json("data/week.json"), now_utc()):
        print("no game live or about to start; nothing to do")
        return

    week = build_week(get_json(SCOREBOARD))
    write_json("data/week.json", week)
    print(f'week {week["week"]}: {len(week["games"])} games')

    if not auto:
        players = get_json(SLEEPER_PLAYERS)
        rosters = fetch_rosters()
        for key, school in schools.items():
            data = reconcile(build_players(players, key, school), school, rosters)
            if len(data["players"]) < 3:   # guard against a bad feed wiping a school's list
                print(f"warning: only {len(data['players'])} players for {key}; keeping previous file", file=sys.stderr)
                continue
            write_json(f"data/players/{key}.json", data)
            print(f'{key}: {len(data["players"])} players')

    update_stats(week, schools)


if __name__ == "__main__":
    main()
