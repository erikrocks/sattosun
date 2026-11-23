#!/usr/bin/env python3
"""Watchdog: is the public site serving fresh data?

Reads the data files from the live site (not the repo), so it also catches a stuck Pages deploy
or a broken domain. Prints each problem and exits non-zero if there are any.

  python scripts/check.py https://iahawks.com
"""
import json, sys, urllib.request
from datetime import datetime, timedelta, timezone

MAX_AGE = timedelta(hours=30)        # the full refresh runs daily; allow for GitHub running it late


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nfl-alumni-tracker-watchdog/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    site = (sys.argv[1] if len(sys.argv) > 1 else "https://iahawks.com").rstrip("/")
    now, problems = datetime.now(timezone.utc), []
    bust = f"?t={int(now.timestamp())}"

    def age_check(label, stamp):
        if not stamp:
            problems.append(f"{label}: never refreshed")
            return
        age = now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if age > MAX_AGE:
            problems.append(f"{label}: last refreshed {age.total_seconds() / 3600:.0f} hours ago ({stamp})")

    try:
        if b"<title>" not in fetch(site + "/" + bust):
            problems.append("home page loaded but doesn't look like the site")
    except Exception as e:
        problems.append(f"home page failed to load: {e}")

    try:
        status = json.loads(fetch(site + "/data/status.json" + bust))
        for part in ("scores", "rosters", "college"):
            age_check(part, status.get(part))
        for p in status.get("problems", []):
            problems.append(f"last update reported: {p}")
    except Exception as e:
        problems.append(f"data/status.json failed to load: {e}")

    try:
        schools = json.loads(fetch(site + "/schools.json" + bust))["schools"]
        for key in schools:
            n = len(json.loads(fetch(f"{site}/data/players/{key}.json{bust}")).get("players", []))
            if n < 3:
                problems.append(f"{key}: only {n} NFL players listed")
    except Exception as e:
        problems.append(f"player data failed to load: {e}")

    for p in problems:
        print(f"PROBLEM: {p}")
    if problems:
        sys.exit(1)
    print(f"{site} is fresh")


if __name__ == "__main__":
    main()
