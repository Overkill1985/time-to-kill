# Time-to-Kill

Personal sports betting analytics for NFL, NBA, college football and men's college
basketball. Probability modeling, no-vig market comparison, expected value,
qualification rules, parlays, and performance tracking.

It never places wagers. It never says a bet "will win". It reports a model
probability, the no-vig market probability, the edge, EV, uncertainty and data
quality, and it is comfortable reporting **no qualified bets today**.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows; use .venv/bin on macOS/Linux
cp .env.example .env                               # add TTK_ODDS_API_KEY if you have one
.venv/Scripts/ttk migrate
```

## Use

```bash
ttk ingest-schedule --sport NFL  # ESPN games/status/scores; no key; --from/--to YYYY-MM-DD
ttk ingest-odds --sport NFL      # one snapshot; uses TTK_PROPLINE_API_KEY (or TTK_ODDS_API_KEY)
ttk collect-odds --loop-minutes 15  # keep polling sports with games this week (line history)
ttk serve                        # UI at http://127.0.0.1:8800, API docs at /docs
ttk import-nfl-history           # nflverse games 1999+, results, reported lines (~20 s)
ttk import-nfl-pbp               # play-by-play EPA aggregates (~15 MB per season)
ttk card [--date YYYY-MM-DD] [--why]  # the daily card; saves prediction snapshots
ttk bets add --game-id 7314 --market SPREAD --selection AWAY --line 2.5 --odds 102 --book fanduel --stake 50
ttk bets list | settle | summary  # the collector also settles bets and parlays on every pass
ttk simulate --game-id 7321 --preset detailed --seed 42  # Monte Carlo one NFL game
ttk backtest-nfl-elo             # tune on train, report validation; test stays sealed
ttk import-espn-history --sport NBA --from-season 2018 --to-season 2026  # ESPN games + book lines (hours; resumable)
ttk import-boxscores --sport NBA --from-season 2018 --to-season 2026  # player box scores (~1 h; resumable)
ttk backtest-nba                 # NBA: Elo, rest, lineups, market-anchored, betting the opener
ttk injury-check                 # NBA: how often players on the injury report actually sat
ttk import-espn-history --sport CFB --from-season 2013 --to-season 2025  # college history (hours)
ttk import-cfbd --from-season 2013 --to-season 2026  # college football preseason facts (TTK_CFBD_API_KEY)
ttk import-cfbd-games --from-season 2013 --to-season 2026  # per-game team efficiency (1 call a season)
ttk backtest-cfb                 # college football: Elo, rest, preseason, market-anchored, opener
ttk import-espn-history --sport NCAAB --from-season 2015 --to-season 2026  # ~12 h
ttk backtest-ncaab               # college basketball: Elo, rest, market-anchored, opener
```

Model results live in [docs/MODELS.md](docs/MODELS.md).

Set `TTK_BETTABLE_BOOKS` in `.env` (for example `draftkings,fanduel,betmgm`) so the card's best price and EV use only books you can actually bet at. Otherwise they include exchanges and prediction markets, whose quoted prices can exclude fees and may not be fillable.

### Background odds collection (Windows)

A scheduled task, **"Time-to-Kill odds collector"**, runs the collector at logon without a console window:

- **Command:** `.venv\Scripts\pythonw.exe -m ttk.cli collect-odds --loop-minutes 15 --log data\logs\collector.log`
- **Working directory:** this folder.
- **Triggers:** at logon, and a watchdog every 15 minutes that starts the collector if it isn't running. Windows' own "restart on failure" doesn't cover a program that exits with an error, which once left the collector down for 15 hours.
- **Settings:** no time limit; runs on battery; never starts a second copy (so the watchdog does nothing while it runs).
- **Inside the collector:** a failed pass (for example, the database busy with a bulk import) is logged and retried next pass; the database waits up to 2 minutes for a lock.
- **Each pass:**
  1. Refreshes the ESPN schedules (free), at most every 6 hours.
  2. Stores changes to ESPN's injury lists: NBA every pass, NFL hourly. There is no historical injury source, so this is the only record of what was known when.
  3. Imports box scores of newly finished NBA games, every 6 hours (player values, and who actually sat).
  4. Polls PropLine only for sports with games coming up: within 90 days for the NBA, whose books post lines months ahead, and 7 days for the other sports.

Manage it from PowerShell:

```powershell
Get-Content data\logs\collector.log -Tail 20 -Wait                      # watch it
Get-ScheduledTask -TaskName "Time-to-Kill odds collector"               # state
Stop-ScheduledTask -TaskName "Time-to-Kill odds collector"              # stop
Start-ScheduledTask -TaskName "Time-to-Kill odds collector"             # start / reload code
Unregister-ScheduledTask -TaskName "Time-to-Kill odds collector"        # remove
```

After pulling new code, restart the task so it loads the update.

Run `ingest-schedule` before `ingest-odds` so odds attach to ESPN's games. The
reverse order also works: an ESPN game adopts odds already stored for it.

## Develop

```bash
pytest
ruff check src tests migrations && ruff format --check src tests migrations
mypy
```

Schema changes: edit `src/ttk/db/models.py`, then
`alembic revision --autogenerate -m "..."`, review the file, and run `ttk migrate`.

See `docs/`: [ARCHITECTURE](docs/ARCHITECTURE.md), [ROADMAP](docs/ROADMAP.md),
[MODEL-GOVERNANCE](docs/MODEL-GOVERNANCE.md), [DATA-SOURCES](docs/DATA-SOURCES.md),
[TOOLING](docs/TOOLING.md).
