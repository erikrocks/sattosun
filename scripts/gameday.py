#!/usr/bin/env python3
"""Keep the data fresh through a day of games (run by .github/workflows/gameday.yml).

GitHub starts frequent scheduled runs late and skips most of them (an "every 15 minutes" schedule lands a
few times a day), so a single run that does land stays up: while any tracked game is on it refreshes and
commits every few minutes, and it waits for a kickoff that's coming up instead of exiting.

Tracked games: this week's NFL games, the school's own games, and the games in its "Who to root for" list.

  python scripts/gameday.py            loop until nothing is on or coming up soon
  python scripts/gameday.py --dry-run  say what it would do now, change nothing

Writes continue=true to $GITHUB_OUTPUT when it stops for the job's time limit with games still to come,
so the workflow can start a fresh run. Exits 1 if the last refresh reported a problem.
"""
import json, os, subprocess, sys, time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update as U

EVERY = 10 * 60                     # seconds between refreshes while a game is on (Pages builds are capped per hour)
LOOKAHEAD = timedelta(hours=6)      # wait for a kickoff this close rather than exit
BUDGET = 5 * 3600 + 30 * 60         # GitHub stops a job at 6 hours; hand over before that


def tracked_games():
    out = list((U.read_json("data/week.json") or {}).get("games", []))
    with open(os.path.join(U.ROOT, "schools.json")) as f:
        schools = json.load(f)["schools"]
    for key in schools:
        c = U.read_json(f"data/college/{key}.json") or {}
        out += c.get("games", [])
        out += [g for v in ((c.get("rooting") or {}).get("views") or {}).values() for g in v.get("games", [])]
    return [g for g in out if g.get("state") != "post" and g.get("kickoff")]


def next_window(now):
    """Start of the next game window that hasn't opened yet, or None."""
    starts = [U.parse_time(g["kickoff"]) - U.LIVE_BEFORE for g in tracked_games()]
    future = [s for s in starts if s > now]
    return min(future) if future else None


def git(*args):
    return subprocess.run(["git", *args], cwd=U.ROOT).returncode


def commit():
    git("add", "data")
    if git("diff", "--cached", "--quiet") == 0:
        return
    git("commit", "-q", "-m", f"Update data {U.iso(U.now_utc())[:16]}Z")
    for _ in range(3):                                  # another run may have pushed meanwhile; fresh data wins
        if git("pull", "--rebase", "-q", "-X", "theirs") == 0 and git("push", "-q") == 0:
            return
        time.sleep(15)
    print("warning: couldn't push this refresh; the next one will try again", file=sys.stderr)


def say(msg):
    print(f"[{U.iso(U.now_utc())}] {msg}", flush=True)


def output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def main():
    dry = "--dry-run" in sys.argv
    started, failed = time.monotonic(), False
    while True:
        now = U.now_utc()
        on = U.in_live_window({"games": tracked_games()}, now)
        nxt = None if on else next_window(now)
        if time.monotonic() - started > BUDGET:
            more = on or (nxt is not None and nxt - now <= LOOKAHEAD)
            say("time limit reached" + ("; handing over to a fresh run" if more else ""))
            output("continue", "true" if more else "false")
            break
        if on:
            if dry:
                say("a game is on: would refresh and commit now, then every %d minutes" % (EVERY // 60))
                break
            say("a game is on: refreshing")
            failed = subprocess.run([sys.executable, os.path.join(U.ROOT, "scripts", "update.py"), "--auto"], cwd=U.ROOT).returncode != 0
            commit()
            time.sleep(EVERY)
        elif nxt and nxt - now <= LOOKAHEAD:
            wait = max(30, (nxt - now).total_seconds())
            say(f"next game window opens at {U.iso(nxt)}; waiting {wait / 60:.0f} minutes")
            if dry:
                break
            time.sleep(min(wait, max(60, BUDGET - (time.monotonic() - started) + 1)))
        else:
            say("nothing on and nothing within %d hours; done" % (LOOKAHEAD.total_seconds() // 3600)
                + (f" (next window {U.iso(nxt)})" if nxt else ""))
            output("continue", "false")
            break
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
