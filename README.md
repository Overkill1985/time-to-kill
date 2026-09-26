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
ttk ingest-odds --sport NFL     # needs TTK_ODDS_API_KEY; ~3 API credits per call
ttk serve                        # http://127.0.0.1:8800/docs
```

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
