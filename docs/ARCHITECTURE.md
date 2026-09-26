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
  models/              Pure model code, no I/O
    elo.py             Sport-agnostic Elo run in time order (walk-forward by construction)
    margin.py          Normal + key-number margin models -> cover/push probabilities
    anchored.py        Market-anchored cover model (market no-vig + rating disagreement)
    nfl_features.py    Walk-forward EPA team strength and QB-change features
    metrics.py         Brier, log loss, calibration tables (always with n)
  research/
    nfl_elo.py         Tuning on train, spread candidates vs market (paired z), sealed test
  db/
    models.py          ORM schema (changed only via migrations)
    session.py         Engine/session setup (SQLite pragmas)
  services/
    identity.py        Resolve provider teams/games to canonical rows (ESPN ids); never guesses
    runs.py            Audited ingestion runs (success or recorded failure)
    schedule_ingest.py ESPN -> games, status, scores (idempotent upsert)
    history_import.py  nflverse -> games + reported_lines + game_starters (idempotent)
    pbp_import.py      nflverse play-by-play -> team_game_stats, qb_game_stats (per season)
    odds_ingest.py     Provider -> immutable odds_snapshots (flips swapped HOME/AWAY)
    odds_state.py      Replays the odds change log: state at any time, last seen
    collector.py       Polling passes: sports with upcoming games, quota-aware
    line_history.py    Opening/previous/current/closing per book; CLV at the bet's own line
    data_quality.py    Rule-based data quality and uncertainty, each with reasons
    nfl_spread_predictor.py  Live NFL spread probabilities from the validated artifact
    daily_card.py      The daily card: evaluate, qualify, explain, snapshot predictions
    market.py          Current market per game: per-book latest, pairing, consensus, main line
  api/app.py           FastAPI routes (loopback-only via TrustedHostMiddleware)
  cli.py               ttk migrate | ingest-schedule | ingest-odds | collect-odds | import-nfl-history | import-nfl-pbp | card
                           | backtest-nfl-elo | serve
migrations/            Alembic.
                       - 0001: the schema and append-only triggers.
                       - 0002: ESPN team identity and swapped game links.
                       - 0003–0004: NFL history, play-by-play aggregates and starters.
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

Built so far: every step except Monte Carlo, for NFL spreads.
- Odds change log, market consensus, EPA/QB features, the market-anchored model, data quality and uncertainty, qualification, and the daily card.
- Other sports and markets are listed on the card as unmodeled.

## API (current)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status + last ingestion run |
| GET | `/api/games?sport=&include_started=` | Upcoming games |
| GET | `/api/games/{id}/market?all_lines=` | Consensus, best price and freshness per side |
| GET | `/api/games/{id}/line-history?market=&selection=` | Opening, previous, current and closing per book |
| GET | `/api/card?date=YYYY-MM-DD` | The daily card. Read-only: it never saves predictions (`ttk card` does) |
| POST | `/api/math/evaluate` | EV, edge, fair odds and Kelly for a probability and price |

## Relationship to nfl-parlay-advisor

This is a new codebase. From `Overkill1985/nfl-parlay-advisor` it keeps these ideas:
- Median-based consensus.
- Append-only snapshots.
- Loopback-only host guard.
- Credit-aware odds fetching.
- Migrations with a version number.

It drops the NFL-only player-prop engine, which had no statistical model. That project is left unchanged.
