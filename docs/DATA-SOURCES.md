# Data sources

Every imported record carries `provider`, a source identifier, `source_timestamp`
(provider's time) and `observed_at` (our ingestion time). Odds are stored as
immutable snapshots.

| Provider | Data | Auth | Refresh | Limits / licensing | Fallback | Freshness expectation | Status |
|---|---|---|---|---|---|---|---|
| The Odds API v4 | Moneyline, spread, total for NFL, NCAAF, NBA, NCAAB across US books | `TTK_ODDS_API_KEY` | On demand: `ttk ingest-odds --sport X` | Free tier 500 credits/month; 1 credit per region per market per call (3 per sport per poll). Personal use | PropLine | Pregame odds <= 30 min old to qualify (`TTK_ODDS_MAX_AGE_MINUTES`) | **Adapter built and unit-tested with mocked HTTP. Not yet run against the live API** (no key configured) |
| PropLine | Same markets across 27 books incl. Pinnacle; history, closing lines, movement | PropLine API key (REST) | - | Per PropLine terms | The Odds API | Same | Normalizer verified on a real payload captured via MCP (`tests/fixtures/`). **REST adapter not built** - its endpoint must be confirmed from PropLine docs first |
| ESPN site API | Schedules, scores, injuries (NFL/NBA/CFB/NCAAB) | None (undocumented public API) | - | Unofficial; may change without notice | NCAA API, sports-hub | Scores within an hour of final | Planned (Phase 2 remainder) |
| nflverse | NFL play-by-play, EPA, snaps, injuries | None (GitHub release CSVs) | Daily | CC-BY 4.0 data | - | Previous week complete by Tuesday | Planned (Phase 4 features) |
| Open-Meteo | Weather for outdoor football | None | 4 h | Free non-commercial | - | Forecast <= 16 days out | Planned (NFL/CFB features) |

Normalization rules (`src/ttk/providers/odds_api_format.py`):

- DFS pick'em books (Underdog, PrizePicks, Sleeper, Dabble, Betr, ReBet) are excluded: they are not prices.
- Suspended markets, boosted/discounted prices, period (quarter/half) markets and team totals are skipped and **counted** in `ingestion_runs.skipped`.
- Alternate lines are kept. The "main" line is chosen at read time (most books, then closest to 50/50).
- A book's lines that are missing from its latest poll are treated as withdrawn.
