# NFL Alumni Tracker

A phone-first page showing every former college player (Iowa Hawkeyes to start) in this week's NFL games, with live scores, byes and roster status.

It's a static site. A GitHub Action refreshes the JSON files in `data/` on a schedule and commits them, then GitHub Pages serves the new data.

## Files

| Path | What it is |
|---|---|
| `index.html` | The whole site (HTML, CSS, JS in one file) |
| `schools.json` | Schools to track: names, college-name matches, colors |
| `data/week.json` | This week's games and scores (from the ESPN scoreboard) |
| `data/players/<school>.json` | That school's NFL players (Sleeper API, checked against ESPN team rosters) |
| `data/stats.json` | Box-score lines for tracked players (ESPN game summaries) |
| `manifest.json`, `icons/` | Lets the site be added to your phone's home screen like an app |
| `scripts/update.py` | Refreshes the data files. Python standard library only |
| `.github/workflows/update.yml` | Full refresh daily and Sunday morning. Every 15 min it also checks for live games and updates scores and stats when any are on, any day of the week |

## Setup (about 5 minutes)

1. Create a new **public** repo (`erikrocks/sattosun`) and push these files to it.
2. **Settings → Pages**: Source = "Deploy from a branch", branch `main`, folder `/ (root)`.
3. **Actions tab**: enable workflows, open "Update data" and click **Run workflow** once. That swaps the preview snapshot for live data.
4. Custom domain: `iahawks.com` (apex A/AAAA records to GitHub Pages, `www` CNAME to `erikrocks.github.io`), set under Settings → Pages. `hawks.eriksheridan.com` redirects to it.

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

`colleges` must match Sleeper's college string exactly (case-insensitive). Add more than one if a school appears under several names. Use `includeNames` for players who played there but finished at another school, and `excludeNames` to leave someone out. Commit, then run the workflow. A school picker appears once there's more than one school, and each school gets its own link: `/#iowa`, `/#iowa-state`.

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
