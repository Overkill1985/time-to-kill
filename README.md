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
ttk card [--date YYYY-MM-DD] [--why]  # the daily card, all sports; saves prediction snapshots
ttk card --sport NBA --only LEAN --min-edge 2 --book fanduel  # filtered (each flag repeatable but --min-edge)
ttk bets add --game-id 7314 --market SPREAD --selection AWAY --line 2.5 --odds 102 --book fanduel --stake 50
ttk bets list | settle | summary  # the collector also settles bets and parlays on every pass
ttk bankroll deposit 1000        # turns on staking limits; also withdraw, adjust (signed)
ttk bankroll policy --max-stake 0.02 --stop 0.2   # limits as fractions; `ttk bankroll` shows them
ttk backtest-totals [--sport NBA]  # basketball totals model (TRAIN tuning, VALIDATE report)
ttk simulate --game-id 7321 --preset detailed --seed 42  # Monte Carlo one game (NFL, CFB, NBA, NCAAB)
ttk backtest-nfl-elo             # tune on train, report validation (--final-test: the test seasons, once)
ttk import-espn-history --sport NBA --from-season 2018 --to-season 2026  # ESPN games + book lines (hours; resumable)
ttk import-boxscores --sport NBA --from-season 2018 --to-season 2026  # player box scores (~1 h; resumable)
ttk backtest-nba                 # NBA: Elo, rest, lineups, market-anchored, betting the opener
ttk injury-check                 # NBA: how often players on the injury report actually sat
ttk forward-freeze --sport CFB --feature-set inseason  # freeze a validated model for forward tests
ttk forward-freeze-ml --sport NCAAB --label eff  # moneyline models from the frozen card spread model
ttk forward-freeze-totals [--sport NBA]  # basketball totals models (backtest, freeze, verify, register)
ttk forward-run --loop-minutes 30 --refresh-inputs  # snapshots 24 h / 1 h before kickoff; refresh inputs every 6 h
ttk forward-report               # score forward snapshots on finished games
ttk lab --horizon 1              # Performance Lab: results by model probability, calibration, weekly drift
ttk score-test --sport NBA       # frozen models on their sealed test seasons (once; done 2026-10-05)
ttk summary [--days 7] [--write FILE]  # health: storage, collection gaps, quotas, forward tests
ttk teams-unmatched              # provider team names not linked to ESPN, with their games
ttk link-team --team-id 1909 --espn-team-id 337 [--apply]  # link one (a dry run without --apply)
ttk alerts [--test-notify]       # run the health alert checks once (and send a test notification)
ttk import-espn-history --sport CFB --from-season 2013 --to-season 2025  # college history (hours)
ttk import-cfbd --from-season 2013 --to-season 2026  # college football preseason facts (TTK_CFBD_API_KEY)
ttk import-cfbd-games --from-season 2013 --to-season 2026  # per-game team efficiency (1 call a season)
ttk backtest-cfb                 # college football: Elo, rest, preseason, market-anchored, opener
ttk import-espn-history --sport NCAAB --from-season 2015 --to-season 2026  # ~12 h
ttk import-team-boxes --sport NCAAB --from-season 2015 --to-season 2024  # team totals (~9 h; resumable)
ttk backtest-ncaab               # college basketball: Elo, rest, efficiency, market-anchored, opener
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
  3. Imports box scores of newly finished NBA games (player values, and who actually sat) and team box totals of college basketball games (possession efficiency), every 6 hours.
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

### Health alerts

Both tasks run the alert checks on every pass, so each watches the other: no successful odds poll for 45 minutes, the last 3 odds polls failed, a run left RUNNING for 6 hours (its process died), PropLine requests below 100, or the forward-test runner silent for an hour. Each problem is shown once as a Windows notification and written to `data/logs/alerts.log`, re-sent every 6 hours while it lasts, and reported once when it clears. Set `TTK_ALERTS_NOTIFY=false` to keep only the log. After the computer wakes from sleep, a short "no odds poll" alert and its resolution are expected.

### Bet and line-movement alerts

The odds collector also sends one-time events on every pass (same notification and log, remembered in `data/alerts/events.json`):

- **Your bet moved:** a pending spread or total bet's consensus main line is 1.5 points or more from your number before kickoff, your way or against you.
- **Settled:** a bet or parlay settled in the last 24 hours, with result, P/L and CLV.
- **Steam:** a game in `TTK_ALERTS_STEAM_SPORTS` (default `NFL,NBA`; empty for none) kicking off within 24 hours whose consensus main spread moved 1 point or more, or total 1.5 or more, in 60 minutes across at least 3 books. On the 2026 NFL weeks so far no game moved more than half a point on the spread in any hour, so this is rare by design.

These describe the market and your own bets; none is a bet signal. `ttk alerts` lists the events current right now without sending them.

### Forward tests (Windows)

A second task, **"Time-to-Kill forward tests"**, runs `ttk forward-run --loop-minutes 30 --refresh-inputs --log data\logs\forward.log` with the same settings and triggers (logon, plus a 15-minute watchdog). Every 30 minutes it snapshots each forward-tested model for games 24 hours and 1 hour from kickoff (docs/MODEL-GOVERNANCE.md, "Forward tests"). Every 6 hours it rebuilds the models after refreshing their slow inputs: this season's nflverse NFL games and play-by-play (~15 MB) and one CollegeFootballData call. Manage it like the collector (`Get-Content data\logs\forward.log -Tail 20 -Wait`, `Stop-ScheduledTask` / `Start-ScheduledTask -TaskName "Time-to-Kill forward tests"`), and see the scores with `ttk forward-report` or the **Forward tests** tab of the UI. The same task writes a weekly health summary (`ttk summary`) to `data/reports/weekly/<Monday>.md`, the first time it runs after 8 AM on Monday.

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
