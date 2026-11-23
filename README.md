# IA Hawks

A phone-first site with two tabs at the bottom. **Saturday** is the school's own season (Iowa to start): record, ranking, last and next game, season leaders, schedule with two-sided box scores and scoring plays, and Iowa football headlines from several outlets. **Sunday** is every former player in this week's NFL games, with live scores, stat lines, byes and roster status, plus each player's season page, season leaderboards, a roster-moves feed and a week picker.

Live at https://iahawks.com.

It's a static site. A GitHub Action refreshes the JSON files in `data/` on a schedule and commits them, then GitHub Pages serves the new data.

## Files

| Path | What it is |
|---|---|
| `index.html` | The whole site (HTML, CSS, JS in one file) |
| `schools.json` | Schools to track: names, college-name matches, colors |
| `data/week.json` | This week's games and scores (from the ESPN scoreboard) |
| `data/players/<school>.json` | That school's NFL players (Sleeper API, checked against ESPN team rosters) |
| `data/stats.json` | Box-score lines for tracked players (ESPN game summaries) |
| `data/college/<school>.json` | The school's own season for the Saturday tab: schedule, scores, box scores, season leaders, news (ESPN) |
| `data/weeks/<season>/<type>-<week>.json` | One file per finished NFL week (scores and stat lines) for the week picker; `data/weeks/index.json` lists them. Missing weeks are rebuilt on the next full refresh |
| `data/season/<school>.json` | Each tracked player's NFL season (game log, totals) and the leaderboards, built from the saved weeks |
| `data/moves/<school>.json` | Roster moves: signings, releases, team changes and reserve or practice-squad moves, from comparing each morning's roster with the previous one |
| `about.html`, `404.html`, `og.png` | About page, not-found page, and the image shown when the link is shared |
| `data/status.json` | When each part last refreshed, and any problems from the last run |
| `manifest.json`, `icons/` | Lets the site be added to your phone's home screen like an app |
| `scripts/update.py` | Refreshes the data files. Python standard library only |
| `.github/workflows/update.yml` | Full refresh daily and Sunday morning. A college-only refresh each night around 9pm Central picks up evening rankings, record and news. Every 15 min it also checks for live NFL or college games and updates scores and stats when any are on, any day of the week |
| `scripts/check.py`, `.github/workflows/watchdog.yml` | Daily check that the live site is up and serving fresh data |

## Setup (about 5 minutes)

1. Create a new **public** repo (`erikrocks/sattosun`) and push these files to it.
2. **Settings → Pages**: Source = "Deploy from a branch", branch `main`, folder `/ (root)`.
3. **Actions tab**: enable workflows, open "Update data" and click **Run workflow** once. That swaps the preview snapshot for live data.
4. Custom domain: `iahawks.com` (apex A/AAAA records to GitHub Pages, `www` CNAME to `erikrocks.github.io`), set under Settings → Pages. `hawks.eriksheridan.com` redirects to it.

## Past weeks

Tap the week badge on the Sunday tab to look back at any finished week of the season. Players are shown with the team they actually played for that week (matched on ESPN id in that week's box scores), so a mid-season trade shows correctly. Roster status and injury tags aren't kept for past weeks.

## When something breaks

Nothing here needs babysitting, and it tells you when it needs attention:

- **A feed fails** (ESPN scoreboard, Sleeper, ESPN rosters, college schedule): the update run still writes everything it could, keeps the previous file for the part that failed, then fails. That opens a GitHub issue labelled `data-failure`, assigned to the repo owner, listing what broke. GitHub emails you for the issue and for the failed run.
- **Sleeper is down:** rosters are rebuilt from ESPN alone (no Sleeper injury tags) and the run is still flagged.
- **The site is down or stale** (stuck Pages deploy, domain problem, schedule not running): the daily watchdog reads the live site and opens a `site-stale` issue.
- Only one issue of each kind is open at a time, and each closes itself when the next full refresh or check passes.

To get these on your phone, install the GitHub app and leave notifications on for this repo. Email goes to the address on your GitHub account (Settings → Notifications; keep "Actions: failed workflows" on).

## Other things to know

- **Search** ignores punctuation and accents, matches from the start of a word, and understands teams, positions, jersey numbers, initials and one-letter typos. While searching, results cover the whole roster regardless of the selected chip.
- **Rankings:** the Saturday tile leads with the playoff committee's ranking once it exists for the season, with the AP poll beside it; before the committee starts, the AP poll leads. Both come from ESPN's rankings feed.
- **News sources** are listed per school in `schools.json` (`newsFeeds`). Headlines must match `newsMatch` and not mention another sport; a feed with `must` only keeps headlines matching that pattern. A feed that fails is skipped; the run only fails if every source fails.
- **Logos and headshots** load from ESPN's image servers; nothing is stored here. If one is missing the page hides it.
- **Visitor counts** use Cloudflare Web Analytics (no cookies). See them in the Cloudflare dashboard under Analytics → Web analytics → iahawks.com.
- **The site name and tagline** are in `schools.json` under `site`.
- **The share image** `og.png` is a 1200×630 screenshot; regenerate it if the name or tagline changes.

## Adding a school

Add an entry to `schools.json`:

```json
"iowa-state": {
  "name": "Iowa State",
  "nickname": "Cyclones",
  "short": "Clones",
  "colleges": ["Iowa State"],
  "theme": { "accent": "#F1BE48", "accentInk": "#000", "bg": "#0F0606", "card": "#1C1010", "line": "#3A2222" }
}
```

Add `"espnTeamId"` (the number in the team's ESPN college-football URL; Iowa is 2294) to get the Saturday tab; without it the school is NFL-only and the tab bar is hidden. `colleges` must match Sleeper's college string exactly (case-insensitive). Add more than one if a school appears under several names. Use `includeNames` for players who played there but finished at another school, and `excludeNames` to leave someone out. Commit, then run the workflow. A school picker appears once there's more than one school, and each school gets its own link: `/#iowa`, `/#iowa-state`. Tabs link too: `/#saturday`, `/#sunday`, `/#iowa/saturday`.

## Running locally

```
python scripts/update.py          # fetch fresh data
python -m http.server 8000        # then open http://localhost:8000
```

(Opening `index.html` straight from disk won't work, because browsers block `fetch` on `file://`.)

## How fresh is it?

- **On your own domain:** while a game is on, the page calls ESPN directly every 45 seconds, so scores and stat lines are close to live. The footer shows a green "Live" dot when that's working.
- **Fallback:** if the browser can't reach ESPN, the page re-reads its own data files every 2 minutes. Those come from the GitHub Action, which runs every 15 minutes (GitHub sometimes runs scheduled jobs late).
- **Claude preview:** outside calls are blocked there, so the preview only shows the snapshot.

## Notes

- "College" is the last school a player attended, so transfers count for the school they finished at.
- Sleeper keeps some retired players attached to their last team and doesn't mark practice squads, so each full refresh checks its list against ESPN's 32 team rosters: players ESPN doesn't list are dropped, practice-squad and reserve status come from ESPN, and anyone ESPN lists from the school that Sleeper missed is added.
- GitHub turns off scheduled workflows after 60 days with no repo activity. The daily data commit counts as activity, so this only bites if the feeds go quiet for two months (deep offseason). If it happens, re-enable it in the Actions tab.
- Game-day inactives aren't in the free data. Sleeper's injury tags (Q/D/Out/IR), refreshed Sunday morning, are the closest thing.
- Sleeper asks people to pull the full player list sparingly (about once a day). The workflow pulls rosters daily plus once Sunday morning; game-window runs only touch the scoreboard.
