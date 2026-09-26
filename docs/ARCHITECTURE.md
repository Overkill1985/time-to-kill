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
    the_odds_api.py    Odds HTTP adapter
    espn.py            Schedule/results adapter (the schedule authority)
  db/
    models.py          ORM schema (changed only via migrations)
    session.py         Engine/session setup (SQLite pragmas)
  services/
    identity.py        Resolve provider teams/games to canonical rows (ESPN ids); never guesses
    runs.py            Audited ingestion runs (success or recorded failure)
    schedule_ingest.py ESPN -> games, status, scores (idempotent upsert)
    odds_ingest.py     Provider -> immutable odds_snapshots (flips swapped HOME/AWAY)
    market.py          Current market per game: per-book latest, pairing, consensus, main line
  api/app.py           FastAPI routes (loopback-only via TrustedHostMiddleware)
  cli.py               ttk migrate | ingest-schedule | ingest-odds | serve
migrations/            Alembic. 0001 creates the schema + append-only triggers. 0002 adds ESPN team identity and swapped game links.
                       On SQLite, migrations turn off foreign-key enforcement while batch mode rebuilds tables, then run the foreign-key integrity check.
tests/                 pytest; DB tests run the real migration on a temp SQLite file
```

## Principles

- **Pure core, thin edges.** Math, consensus and qualification are pure functions with
  no I/O. Providers do I/O and normalize. Services join them to the database.
- **One math module.** Nothing outside `betting_math.py` converts odds or computes EV.
- **History is immutable.** `odds_snapshots`, `injury_reports` and `predictions` are
  append-only, enforced by database triggers. Re-runs add rows.
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

Built so far: odds snapshots, market consensus, edge/EV and qualification.
Not built yet: features, models, calibration, simulation, data-quality scoring.

## API (current)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status + last ingestion run |
| GET | `/api/games?sport=&include_started=` | Upcoming games |
| GET | `/api/games/{id}/market?all_lines=` | Consensus, best price and freshness per side |
| POST | `/api/math/evaluate` | EV, edge, fair odds and Kelly for a probability and price |

## Relationship to nfl-parlay-advisor

This is a new codebase. From `Overkill1985/nfl-parlay-advisor` it keeps these ideas:
- Median-based consensus.
- Append-only snapshots.
- Loopback-only host guard.
- Credit-aware odds fetching.
- Migrations with a version number.

It drops the NFL-only player-prop engine, which had no statistical model. That project is left unchanged.
