# Data sources

Every imported record carries `provider`, a source identifier, `source_timestamp`
(provider's time) and `observed_at` (our ingestion time). Odds are stored as
immutable snapshots.

| Provider | Data | Auth | Refresh | Limits / licensing | Fallback | Freshness expectation | Status |
|---|---|---|---|---|---|---|---|
| The Odds API v4 | Moneyline, spread, total for NFL, NCAAF, NBA, NCAAB across US books | `TTK_ODDS_API_KEY` | On demand: `ttk ingest-odds --sport X` | Free tier 500 credits/month; 1 credit per region per market per call (3 per sport per poll). Personal use | PropLine | Pregame odds <= 30 min old to qualify (`TTK_ODDS_MAX_AGE_MINUTES`) | **Adapter built and unit-tested with mocked HTTP. Not yet run against the live API** (no key configured) |
| PropLine | Same markets across 27 books incl. Pinnacle; history, closing lines, movement | PropLine API key (REST) | - | Per PropLine terms | The Odds API | Same | Normalizer verified on a real payload captured via MCP (`tests/fixtures/`). **REST adapter not built** - its endpoint must be confirmed from PropLine docs first |
| ESPN site API | **Schedule authority**: games, status, final scores, neutral site, season, ESPN team ids (NFL/NBA/CFB/NCAAB) | None (undocumented public API) | On demand: `ttk ingest-schedule --sport X` (default: 2 days back to 7 ahead) | Unofficial; may change without notice | NCAA API, sports-hub | Scores within an hour of final | **Built and run live** (NFL 16 games, CFB 236 games). Injuries not yet |
| nflverse `nfldata/games.csv` | NFL games 1999–present: results, neutral site, week, ESPN event ids, **reported** spread/total/moneyline | None | On demand: `ttk import-nfl-history` (idempotent, ~20 s) | See the upstream repo | ESPN for schedule | Updated through the season | **Built and imported**: 7,548 games, 7,340 line rows (5,359 with moneylines, from 2006) |
| nflverse play-by-play (`nflverse-data` release `pbp`) | Per-game team offense (EPA, success, dropbacks, rushes) and QB dropback EPA, aggregated from ~48k plays per season | None | On demand: `ttk import-nfl-pbp --from-season Y --to-season Y` (~15 MB download per season; raw plays are discarded) | See the upstream repo | - | Previous week complete by Tuesday | **Built** (1999–2026 imported) |
| nflverse games: starting QBs | `home_qb_id` / `away_qb_id` for every played game, and for upcoming games once listed | None | With `ttk import-nfl-history` | See the upstream repo | - | Known at kickoff | **Built** |
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

nflverse games, verified 2026-09-26 (`src/ttk/providers/nflverse.py`):

- `result` is the home score minus the away score.
- `spread_line` is the home team's expected margin. The stored `home_spread` is its negative, so it's bookmaker-style: `-3.5` means home is favored.
- `gametime` is US Eastern.
- **The lines' timing (opening versus closing) and their source are not documented upstream.** They're stored in `reported_lines`. They benchmark models after the fact. A model may use one as an input only when the simulated bet is placed at that same line (the market-anchored model); see MODEL-GOVERNANCE.md. They're never an input for a bet placed at an earlier price, and never used as a closing line for closing-line value.
- Team codes map to ESPN franchise ids, verified against ESPN's own event records: OAK and LV → 13, STL and LA → 14, SD and LAC → 24, WAS → 28.
- Games link to ESPN rows by ESPN event id. ESPN outranks nflverse, so nflverse never overwrites a kickoff time or score that ESPN supplied.

nflverse play-by-play, verified 2026-09-26 on 1999 and 2025 (`src/ttk/providers/nflverse_pbp.py`):

- `game_id` matches `games.csv`, and player ids are NFL GSIS ids that match `games.csv` starting QBs.
- **Offensive plays:** pass or run plays with an EPA value, excluding two-point attempts. Penalty-only plays (`no_play`) are excluded.
- **Dropbacks:** `qb_dropback = 1`. The quarterback is the `id` column, which is also set on scrambles, where `passer_player_id` is empty.

Normalization rules (`src/ttk/providers/odds_api_format.py`):

- DFS pick'em books (Underdog, PrizePicks, Sleeper, Dabble, Betr, ReBet) are excluded: they are not prices.
- Suspended markets, boosted/discounted prices, period (quarter/half) markets and team totals are skipped and **counted** in `ingestion_runs.skipped`.
- Alternate lines are kept. The "main" line is chosen at read time (most books, then closest to 50/50).
- A book's lines that are missing from its latest poll are treated as withdrawn.
