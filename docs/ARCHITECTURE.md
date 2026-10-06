# Architecture

## Stack

Python 3.12+, FastAPI, SQLAlchemy 2 + Alembic (SQLite by default, Postgres-ready),
numpy/scipy/scikit-learn for models, pytest + ruff + mypy (strict).

## Layout

```
src/ttk/
  betting_math.py      The only place betting formulas live (odds, no-vig, EV, Kelly, parlays, CLV)
  consensus.py         Multi-book market consensus and line shopping (pure)
  qualification.py     QUALIFIED / LEAN / PASS / NO_BET with Why-Not checks (pure)
  domain.py            Enums shared across layers and stored as strings
  teams.py             Team-name normalization + curated ESPN-id aliases
  config.py            Settings from TTK_* env vars / .env
  providers/
    base.py            Normalized records (TeamRef, NormalizedGame, quotes) + provider Protocols
    odds_api_format.py Normalizer for Odds-API-shaped payloads (The Odds API, PropLine)
    the_odds_api.py    Odds HTTP adapter (The Odds API)
    propline.py        Odds HTTP adapter (PropLine; key in header; quota + Retry-After)
    espn.py            Schedule/results adapter (the schedule authority)
    nflverse.py        NFL history 1999+: results, reported lines, starting QBs
    nflverse_pbp.py    Play-by-play -> per-game team and QB EPA aggregates (streamed)
    espn_odds.py       ESPN core API historical lines per book (open/close); excludes non-markets
    espn_boxscore.py   ESPN game summaries -> player box-score lines (never the summary's injuries)
    espn_injuries.py   ESPN current injury lists (NFL, NBA)
    http_retry.py      GET with retries for transient 5xx (ESPN)
    cfbd.py            CollegeFootballData: preseason facts, each with known_at
  models/              Pure model code, no I/O
    elo.py             Sport-agnostic Elo run in time order (walk-forward by construction)
    margin.py          Normal + key-number margin models -> cover/push probabilities
    anchored.py        Market-anchored cover model (market no-vig + rating disagreement)
    nfl_features.py    Walk-forward EPA team strength (optionally opponent-adjusted) and QB-change features
    metrics.py         Brier, log loss, calibration tables (always with n)
    simulation.py      Monte Carlo: key-number margin x market total, rank copula, seeded
  research/
    nfl_elo.py         Tuning on train, spread candidates vs market (paired z), sealed test
    nfl_simulation.py  Copula pairs (train) and joint validation vs independence (shared core: SimRow)
    espn_simulation.py Same for the frozen CFB/NBA/NCAAB models: no-tie margins, per-sport dependence
    moneyline.py       Moneylines from the spread models' margin distributions; validation vs the ML market
    totals.py          College basketball totals: walk-forward pace and efficiency, calibration, validation
    espn_models.py     ESPN-history pipeline per SportConfig: feature-set margin models, market-anchored, opener test
    nba_model.py       NBA config: splits, grids, rest and lineup feature sets
    cfb_model.py       College football config: splits, grids, regression toward recent level
    ncaab_model.py     College basketball config: splits, grids, rest with back-to-backs
    cfb_preseason.py   Per-game preseason features known at kickoff (fading early-season changes)
    nba_lineups.py     Walk-forward player value and availability (missing at tip / last game)
    nba_injuries.py    Injury features at a horizon before tip-off; walk-forward sit rates
    frozen.py          Freeze a validated model's fitted parameters; rebuild and verify it
  db/
    models.py          ORM schema (changed only via migrations)
    session.py         Engine/session setup (SQLite pragmas)
  services/
    identity.py        Resolve provider teams/games to canonical rows (ESPN ids); never guesses
    runs.py            Audited ingestion runs (success or recorded failure)
    schedule_ingest.py ESPN -> games, status, scores (idempotent upsert)
    history_import.py  nflverse -> games + reported_lines + game_starters (idempotent)
    pbp_import.py      nflverse play-by-play -> team_game_stats, qb_game_stats (per season)
    espn_history_import.py  ESPN season schedule + per-book open/close lines (resumable)
    boxscore_import.py ESPN box scores -> player_game_stats, team_game_boxes (resumable)
    injury_ingest.py   Injury lists -> injury_reports change log; injuries_at(t)
    cfbd_import.py     CollegeFootballData -> team_season_features (dated) and team_game_stats (PPA)
    forward_test.py    Forward tests: snapshots at 24 h / 1 h before kickoff, scoring (CLV stored once final)
    performance_lab.py Forward-test threshold lab, calibration bins, weekly drift and CLV
    forward_models.py  The models under forward test (NFL predictor, frozen ESPN-sport models)
    health.py          Health summary: storage growth, collection gaps, dead runs, quotas, forward tests
    alerts.py          Health alerts each pass: Windows notification + log, once per problem
    bet_alerts.py      One-time events from the collector: your bet's line moved, settlements, steam
    repair.py          One-off data repairs, dry run first (merged ESPN games, malformed ESPN lines)
    odds_ingest.py     Provider -> immutable odds_snapshots (flips swapped HOME/AWAY)
    odds_state.py      Replays the odds change log: state at any time, last seen
    collector.py       Polling passes: schedules, injuries, NBA box scores, odds; quota-aware
    line_history.py    Opening/previous/current/closing per book; CLV at the bet's own line
    data_quality.py    Rule-based data quality and uncertainty, each with reasons
    nfl_spread_predictor.py  Live NFL spread probabilities from the validated artifact
    daily_card.py      The daily card, every sport (NFL predictor + frozen CardModels): evaluate, qualify, explain, snapshot
    bets.py            Bet tracker: record (beliefs as of bet time), settle, CLV, performance
    bankroll.py        Balance from append-only deposits + settled P/L, staking limits, Kelly guidance
    parlay_lab.py      Parlays: price at one book, correlation, joint prob (simulated same-game, all four sports), EV, save, settle
    simulation_service.py  One-game simulation summary: distributions, sensitivity, max acceptable line
    frozen_simulator.py    Monte Carlo from a frozen card model (CFB, NBA, NCAAB), anchored to the market
    market.py          Current market per game: per-book latest, pairing, consensus, main line
  api/app.py           FastAPI routes. Loopback only, and cross-site writes are refused (see Security)
  web/                 The browser UI (index.html, app.js, style.css): no build step, served at /
  cli.py               ttk migrate | ingest-schedule | ingest-odds | collect-odds | import-nfl-history | import-nfl-pbp | card | bets | serve
                           | import-espn-history | import-boxscores | import-team-boxes | import-cfbd | import-cfbd-games | bankroll | lab | injury-check | summary | alerts | score-test | forward-freeze | forward-freeze-ml | forward-snapshot | forward-run | forward-report | repair-merged-games | repair-espn-lines
                           | backtest-nfl-elo | backtest-nba | backtest-cfb | backtest-ncaab | backtest-totals | simulate
migrations/            Alembic.
                       - 0001: the schema and append-only triggers.
                       - 0002: ESPN team identity and swapped game links.
                       - 0003–0004: NFL history, play-by-play aggregates and starters.
                       - 0014: forward_predictions.market (SPREAD or MONEYLINE; added natively, triggers kept).
                       - 0013: forward_scores (append-only CLV of finished forward snapshots).
                       - 0012: bankroll_entries and bankroll_policies (append-only); bankroll columns on bets and parlays.
                       - 0011: forward_predictions (append-only) and model_versions.artifact.
                       - 0010: team_game_boxes (team box totals for possession efficiency).
                       - 0009: team_season_features (preseason facts with known_at).
                       - 0008: player box scores; injury change-log fields (added natively; triggers kept).
                       - 0005: change-only odds (a `withdrawn` flag and `book_observations`). Its column is added natively so the table's triggers survive.
                       On SQLite, migrations turn off foreign-key enforcement while batch mode rebuilds tables, then run the foreign-key integrity check.
tests/                 pytest; DB tests run the real migration on a temp SQLite file
```

## Principles

- **Pure core, thin edges.** Math, consensus and qualification are pure functions with
  no I/O. Providers do I/O and normalize. Services join them to the database.
- **One math module.** Nothing outside `betting_math.py` converts odds or computes EV.
- **History is immutable.** `odds_snapshots`, `book_observations`, `injury_reports` and `predictions` are append-only, enforced by database triggers. Re-runs add rows.
- **Odds are a change log.**
  - `odds_snapshots` stores a row only when a quote appears, changes price, or is withdrawn.
  - `book_observations` records when each book was seen in each poll.
  - `services/odds_state.py` rebuilds any book's prices at any time by replaying its changes.
  - Freshness comes from the last observation, not from the last price change.
  - On live NFL data, a poll one minute after the previous one wrote 629 rows for 36,323 quotes seen (98% fewer).
- **Provenance everywhere.** Every snapshot has provider, source timestamp, ingestion
  time and ingestion run. Predictions store their features and `inputs_as_of`.
- **No Claude at runtime.** MCP servers are used during development only (docs/TOOLING.md).
- **UTC only.** `UTCDateTime` stores UTC and rejects naive datetimes.
- **One game, one row.** ESPN is the schedule authority. Every other provider's games link to ESPN's rows by ESPN event id, or else by the same two teams within 24 hours. Ambiguous matches become new rows and are counted; they are never guessed.
- **Named constraints.** `Base.metadata` has a naming convention so migrations can alter constraints.

## Decision pipeline (target)

```
game -> data quality -> current odds (snapshots) -> market consensus (no-vig)
     -> features -> sport/market model -> calibration -> [Monte Carlo]
     -> model probability -> edge / EV -> uncertainty -> qualification
```

Built so far: every step, for NFL spreads, including Monte Carlo.
- Odds change log, market consensus, EPA/QB features, the market-anchored model, data quality and uncertainty, qualification, and the daily card.
- Other sports and markets are listed on the card as unmodeled.

## API (current)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status + last ingestion run |
| GET | `/api/games?sport=&include_started=` | Upcoming games |
| GET | `/api/games/{id}/market?all_lines=` | Consensus, best price and freshness per side |
| GET | `/api/games/{id}/line-history?market=&selection=` | Opening, previous, current and closing per book |
| GET | `/api/card?date=YYYY-MM-DD` | The daily card. Read-only: it never saves predictions (`ttk card` does). Optional filters on `entries` (summaries stay whole): `sport`, `classification` (repeatable), `min_edge` in points, `book`; `total_entries` is the unfiltered count |
| GET | `/api/sportsbooks` | Books in the data, flagged bettable or not |
| GET | `/api/bets?status=pending\|settled` | Tracked bets |
| POST | `/api/bets` | Record a bet (rejected at or after kickoff) |
| PATCH | `/api/bets/{id}` | Notes, or void a pending bet |
| POST | `/api/bets/settle` | Grade bets on finished games and record CLV |
| GET | `/api/bankroll` | Balance, peak, drawdown, open and today's stakes, limits, largest stake allowed now, entries |
| POST | `/api/bankroll/entries` | Deposit, withdrawal or signed adjustment (append-only) |
| PUT | `/api/bankroll/policy` | Save new staking limits (a new row; older rows are history) |
| POST | `/api/bankroll/guidance` | Kelly stake within the limits; 0 unless QUALIFIED |
| GET | `/api/performance?sport=&market=` | Record, ROI, units, CLV, edge, EV and drawdown, each with its sample size, plus a parlay summary |
| GET | `/api/games?date=&sport=` | Upcoming games (the date uses the card's US Eastern day) |
| GET | `/api/games/{id}/offers?book=` | Every line a book quotes for a game, with its main line flagged |
| POST | `/api/parlays/evaluate` | Analyze a slip. Read-only (it's a POST only because it takes a body) |
| POST | `/api/parlays` | Record a placed parlay, with its legs' beliefs as of bet time |
| GET | `/api/parlays` | Recorded parlays with their legs |
| GET | `/api/forward?sport=` | Forward-test scores per model and horizon, snapshot counts, latest snapshots |
| GET | `/api/lab?sport=&model=&horizon_hours=` | Performance Lab for one model and horizon (default: most finished games) |
| POST | `/api/simulations/run` | Monte Carlo for one game, any of the four sports (read-only; preset or iterations, and a seed; 503 while a non-NFL model loads) |

`POST /api/bets/settle` settles both single bets and parlays.

Once a deposit is recorded, `POST /api/bets` and `POST /api/parlays` refuse a stake over a bankroll
limit (per wager, per Eastern day, open exposure, uncommitted balance, or the stop below the peak)
with 422 unless `limit_override` gives a reason; the broken limits and the reason are kept on the
wager with `bankroll_at_bet`. Card entries carry `stake` guidance, which is 0 unless QUALIFIED.

## UI

Open `ttk serve`, then http://127.0.0.1:8800. It has six tabs:

- **Today:** the card, with model and market bars, stats, and a Why list per bet. *Track* prefills a bet from any entry, and *Add to parlay* puts it on the slip.
- **Parlay Lab:**
  - A slip priced at one book, with an optional price you enter per leg.
  - A leg picker (date, sport, game, then that book's lines).
  - Live analysis: odds, joint probability, fair odds, EV, correlation, and the strongest, weakest and costliest legs.
  - Suggested removals are buttons you choose to click; the lab never changes the slip itself. The slip is saved in your browser.
- **Simulator:** pick an NFL game, a preset and an optional seed. It shows score, margin and total distributions, spread and total sensitivity with the market line highlighted, the maximum acceptable lines, and the same-game joint table. *Simulate* on a card entry opens it.
- **Bet Tracker:** record a bet, list pending and settled bets (and parlays), settle finished games, and void a bet.
- **Performance:** summary tiles, filterable by sport and market.
- **Forward tests:** per model and horizon, decided games, log loss against the market at the snapshot (z shown only from 30 decided games), price CLV and points against our close, and results at edge >= 2%; plus snapshot counts and the latest snapshots with their results.

Plain HTML, CSS and ES modules, with no build step and no external requests. Every value is inserted with `textContent`, never raw HTML.

## Security

- **No authentication**, so the server binds to loopback only, and `TrustedHostMiddleware` rejects foreign `Host` headers (which also blocks DNS rebinding).
- **Cross-site writes are refused.** POST, PATCH, PUT and DELETE requests are rejected if their `Origin` isn't this app, or if they aren't `application/json`. Another website open in the browser can't make this API record or void bets.
- **Secrets stay out of the code:** they come from `.env` (gitignored) and are sent to providers in headers.
| POST | `/api/math/evaluate` | EV, edge, fair odds and Kelly for a probability and price |

## Relationship to nfl-parlay-advisor

This is a new codebase. From `Overkill1985/nfl-parlay-advisor` it keeps these ideas:
- Median-based consensus.
- Append-only snapshots.
- Loopback-only host guard.
- Credit-aware odds fetching.
- Migrations with a version number.

It drops the NFL-only player-prop engine, which had no statistical model. That project is left unchanged.
