# HSL Statistics

Points leaders, standings, goalies, scores, schedules, player/team/game pages and calendar feeds for every Hockey Super League division (2026-27), built from the box scores on hockeysuperleague.ca.

- Site: https://replay88.github.io/hsl-scoring-race/ (`docs/`, GitHub Pages)
- Data: `docs/data/<division>.json`, one file per division, loaded by the page on demand; `docs/data/index.json` lists them.
- Calendars: `docs/cal/<division>/<team>.ics` (and `all.ics`), subscribable.
- Updater: `python3 -I tools/update.py` — runs on a schedule in `.github/workflows/update.yml`, or from the Actions tab ("Run workflow"). It reads the league calendar feeds, fetches any newly final box scores, and rebuilds everything. Set `PARSER` in `update.py` higher to force a re-read of cached games after a parser change.
