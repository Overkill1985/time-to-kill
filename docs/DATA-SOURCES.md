# Data sources

Every imported record carries `provider`, a source identifier, `source_timestamp`
(provider's time) and `observed_at` (our ingestion time). Odds are stored as
immutable snapshots.

| Provider | Data | Auth | Refresh | Limits / licensing | Fallback | Freshness expectation | Status |
|---|---|---|---|---|---|---|---|
| The Odds API v4 | Moneyline, spread, total for NFL, NCAAF, NBA, NCAAB across US books | `TTK_ODDS_API_KEY` | On demand: `ttk ingest-odds --sport X` | Free tier 500 credits/month; 1 credit per region per market per call (3 per sport per poll). Personal use | PropLine | Pregame odds <= 30 min old to qualify (`TTK_ODDS_MAX_AGE_MINUTES`) | **Adapter built and unit-tested with mocked HTTP. Not yet run against the live API** (no key configured) |
| PropLine | Same markets across 27 books incl. Pinnacle; history, closing lines, movement | PropLine API key (REST) | - | Per PropLine terms | The Odds API | Same | Normalizer verified on a real payload captured via MCP (`tests/fixtures/`). **REST adapter not built** - its endpoint must be confirmed from PropLine docs first |
| ESPN site API | **Schedule authority**: games, status, final scores, neutral site, season, ESPN team ids (NFL/NBA/CFB/NCAAB) | None (undocumented public API) | On demand: `ttk ingest-schedule --sport X` (default: 2 days back to 7 ahead) | Unofficial; may change without notice | NCAA API, sports-hub | Scores within an hour of final | **Built and run live** (NFL 16 games, CFB 236 games). Injuries not yet |
| nflverse | NFL play-by-play, EPA, snaps, injuries | None (GitHub release CSVs) | Daily | CC-BY 4.0 data | - | Previous week complete by Tuesday | Planned (Phase 4 features) |
| Open-Meteo | Weather for outdoor football | None | 4 h | Free non-commercial | - | Forecast <= 16 days out | Planned (NFL/CFB features) |

ESPN behaviors verified 2026-09-26 (`src/ttk/providers/espn.py`):

- NFL rejects date ranges (HTTP 400), so every sport is fetched one day at a time.
- College scoreboards need `groups`: CFB uses 80 (FBS) plus 81 (FCS); men's basketball uses 50 (Division I). FBS-vs-FCS games appear in both CFB groups and are de-duplicated.
- **`limit` above 500 is silently ignored and returns only 25 events.** The adapter uses 500 and flags any full page as `possibly_truncated_day`.
- Scheduled games report a score of `"0"`. Scores are only read once a game is in progress or final.

Cross-provider identity (`src/ttk/services/identity.py`, `src/ttk/teams.py`):

- ESPN ids are canonical for teams and games. Only ESPN writes status, scores and kickoff times.
- Team name resolution, in order:
  1. the ESPN id, if the provider supplies one;
  2. an exact alias seen before;
  3. the curated alias table;
  4. a normalized name that matches exactly one team.
  Anything else becomes an unmatched team, counted in `ingestion_runs.stats`. The resolver never guesses.
- Measured on real data, 2026-09-26: 0 of 230 PropLine college football names matched ESPN exactly. After resolution, **231 of 231 college football and 15 of 15 NFL PropLine games linked** to their ESPN games, with no unmatched teams.
- When a provider lists home and away the other way round, the link is marked `swapped` and that provider's HOME/AWAY selections are flipped. One real case, Prairie View A&M vs Grambling (neutral site), was detected correctly.

Normalization rules (`src/ttk/providers/odds_api_format.py`):

- DFS pick'em books (Underdog, PrizePicks, Sleeper, Dabble, Betr, ReBet) are excluded: they are not prices.
- Suspended markets, boosted/discounted prices, period (quarter/half) markets and team totals are skipped and **counted** in `ingestion_runs.skipped`.
- Alternate lines are kept. The "main" line is chosen at read time (most books, then closest to 50/50).
- A book's lines that are missing from its latest poll are treated as withdrawn.
