# Data sources

Every imported record carries `provider`, a source identifier, `source_timestamp`
(provider's time) and `observed_at` (our ingestion time). Odds are stored as
immutable snapshots.

| Provider | Data | Auth | Refresh | Limits / licensing | Fallback | Freshness expectation | Status |
|---|---|---|---|---|---|---|---|
| The Odds API v4 | Moneyline, spread, total for NFL, NCAAF, NBA, NCAAB across US books | `TTK_ODDS_API_KEY` | On demand: `ttk ingest-odds --sport X` | Free tier 500 credits/month; 1 credit per region per market per call (3 per sport per poll). Personal use | PropLine | Pregame odds <= 30 min old to qualify (`TTK_ODDS_MAX_AGE_MINUTES`) | **Adapter built and unit-tested with mocked HTTP. Not yet run against the live API** (no key configured) |
| PropLine | Live moneyline, spread and total odds from up to 27 books, including Pinnacle | `TTK_PROPLINE_API_KEY`, sent in the `X-API-Key` header | `ttk collect-odds` every N minutes (see below) | Free: 1,000 requests/day, burst 10, 5/s. **Line history, closing lines and movement are paid** (Hobby $9/mo+); the archive starts April 2026 | The Odds API | Our own snapshots: opening = first poll, closing = last poll before kickoff | **REST adapter built** and tested against a real captured payload (mocked HTTP). **Not yet run live**; it needs your key |
| ESPN site API | **Schedule authority**: games, status, final scores, neutral site, season, ESPN team ids (NFL/NBA/CFB/NCAAB) | None (undocumented public API) | On demand: `ttk ingest-schedule --sport X` (default: 2 days back to 7 ahead) | Unofficial; may change without notice | NCAA API, sports-hub | Scores within an hour of final | **Built and run live** (NFL 16 games, CFB 236 games). Injuries not yet |
| nflverse `nfldata/games.csv` | NFL games 1999–present: results, neutral site, week, ESPN event ids, **reported** spread/total/moneyline | None | On demand: `ttk import-nfl-history` (idempotent, ~20 s) | See the upstream repo | ESPN for schedule | Updated through the season | **Built and imported**: 7,548 games, 7,340 line rows (5,359 with moneylines, from 2006) |
| nflverse play-by-play (`nflverse-data` release `pbp`) | Per-game team offense (EPA, success, dropbacks, rushes) and QB dropback EPA, aggregated from ~48k plays per season | None | On demand: `ttk import-nfl-pbp --from-season Y --to-season Y` (~15 MB download per season; raw plays are discarded) | See the upstream repo | - | Previous week complete by Tuesday | **Built** (1999–2026 imported) |
| nflverse games: starting QBs | `home_qb_id` / `away_qb_id` for every played game, and for upcoming games once listed | None | With `ttk import-nfl-history` | See the upstream repo | - | Known at kickoff | **Built** |
| ESPN core API odds | Per-game lines from real sportsbooks (spread + prices, total + prices, moneylines); **opening and closing** from 2023-24 on, closing only before | None (undocumented public API) | On demand: `ttk import-espn-history --sport NBA --from-season Y --to-season Y` (resumable, paced) | Unofficial; one request per game (~1,300 per NBA season) | - | Historical only | **Built** (NBA 2017-18 onward; CFB 2013 onward; NCAAB 2014-15 onward) |
| ESPN game summaries (box scores) | Per-player lines for final games: starter, minutes, points, shooting, rebounds, assists, steals, blocks, turnovers, fouls, +/-; healthy scratches with a reason | None (undocumented public API) | On demand: `ttk import-boxscores --sport NBA --from-season Y --to-season Y` (resumable, paced) | Unofficial; one request per game | - | Historical | **Built** (NBA 2017-18 onward) |
| ESPN injury lists | Current injury status per player (NFL, NBA): status, injury, comment, expected return, ESPN's update time | None (undocumented public API) | The collector: NBA every pass, NFL hourly; `injury_reports` stores changes only | Unofficial; NFL list is ~9 MB | - | Live only: **no history endpoint**, so history starts with our polling (2026-09-27) | **Built and running** |
| CollegeFootballData.com | College football preseason facts per team-season: roster talent (247 composite), returning production, recruiting classes, head coaches (hire dates), the transfer portal (transfer dates), polls | `TTK_CFBD_API_KEY` (bearer token; free at collegefootballdata.com) | On demand: `ttk import-cfbd --from-season Y --to-season Y` (~6 calls per season) | Free tier 1,000 calls a month; back-to-back calls get HTTP 429, so calls are 1 s apart and retried | - | Yearly, before the season | **Built** (2013–2026 imported 2026-10-03) |
| Open-Meteo | Weather for outdoor football | None | 4 h | Free non-commercial | - | Forecast <= 16 days out | Planned (NFL/CFB features) |

Timestamped odds history (verified 2026-09-26):

- **No free source exists.**
  - PropLine's history and closing endpoints return redacted data on the free tier (checked through the MCP: every price and point was `null` and flagged `redacted`). Its archive only starts in April 2026.
  - The Odds API's historical odds are a paid tier.
- **So Time-to-Kill builds its own history.** `ttk collect-odds --loop-minutes 15` polls sports that have games in the next 7 days. Every price is stored as an immutable snapshot, and `services/line_history.py` derives the opening, previous, current and closing prices, plus CLV.
- **Schedule first:** each pass refreshes ESPN schedules before polling odds, at most every 6 hours. Odds then attach to ESPN's teams and games, and curated aliases apply.
  - When odds arrived first on 2026-09-26, 8 college football games involving 7 teams ended up unlinked. Their odds rows are append-only, so they stay unlinked. The fix prevents it happening again: schedule first, and curated aliases now also adopt earlier unmatched teams.
- **How far ahead to poll** (`ODDS_LOOKAHEAD` in `services/collector.py`):
  - **NBA: 90 days.** On 2026-09-26 PropLine listed 41 NBA regular-season games from October 20 to December 25, with 9–16 books each.
  - **NFL and CFB: 7 days**, because PropLine only lists them about a week ahead.
  - An empty 8-day ESPN schedule doesn't stop polling a sport with a wider window.
- **Untagged team totals (FanDuel college football, 2026-09-26):** PropLine sent 977 FanDuel team-total markets with `team` empty, apparently wherever it couldn't match FanDuel's abbreviations ("J'ville St", "C Arkansas").
  - Their descriptions still show it ("Team Total Points - J'ville St", "Alternate Total Points (line 30.5) - J'ville St"), so the normalizer now skips them as `team_total_untagged`.
  - Before the fix they sat under the game-total key. A team total of about 30.5 looked like FanDuel's "main" game total, and colliding lines overwrote real game-total prices. College football duplicate collisions per poll fell from about 1,236 to 274.
  - The change log corrected itself on the next poll (1,905 withdrawals, 15,563 corrected prices). Rows from before the fix stay in history as they arrived.
  - NFL feeds were clean.
- **Quota:** one request per sport per poll. Four sports every 15 minutes is 384 requests a day, within PropLine's free 1,000. The collector stops when fewer than 20 requests remain.
- **Storage is change-only** (see ARCHITECTURE.md). A book that no longer lists a game has all its quotes for that game withdrawn. A book that pulled its line before kickoff has no closing price.
- **Live check, 2026-09-26:** PropLine's NFL feed has about 36,000 quotes per poll across 34 games and about 25 books, roughly 35 alternate spread lines per book per game.
  - Before the `side` fix, 3,669 outcomes were unparseable, including every DraftKings spread. After it, 2.
  - A poll repeated after one minute wrote 629 changes. The rate at 15-minute polls still needs measuring.
- **Our history is only as good as our polling.** The opening is our first poll, not the book's true open (`opening_at` shows when it was taken). The close is our last poll before kickoff (`close_minutes_before_kickoff` shows how close to kickoff that was).

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

ESPN core API odds, verified 2026-09-26 on NBA games 2017-18 through 2025-26 (`src/ttk/providers/espn_odds.py`):

- `sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{id}/competitions/{id}/odds` returns one item per provider. Every sampled game had at least one real sportsbook.
- Books vary by era: 2017-18 has CG Technology, Caesars, Unibet, Westgate, Wynn and a consensus; a 2025 sample had ESPN BET only.
- **Not every provider is a market.** Projection and picks sites (accuscore, numberfire, teamrankings, betegy, fantasy911), in-game "Live Odds" feeds and ESPN's "Opening" pseudo-provider (an opener, never a close; college football 2015) are excluded, at import and again when lines are read.
- **Malformed "ESPN BET" items (found 2026-09-27):** in retro-filled items (NBA 2022-23, CFB 2022-23) the `close` block held prices where the line belongs: a spread of −110 and a total of −115 (the over price), while `current` held the real line. 3,223 games were affected (NBA 1,377, CFB 1,842). No spread reaches 100 points and a total can't be negative or equal its own price, so such a `close` now falls back to `current` (a finished game's final line), and a malformed `open` is dropped. `ttk repair-espn-lines` re-fetched the affected games. The NBA results barely moved, because each game's benchmark is the most common line across books; the college ones changed a lot.
- **College football** (verified 2026-09-27): lines from 2013 (consensus only that year; mostly offshore books 2014–2016; US books from 2017). Books' own openers start with the 2023 season (part of it) and cover 2024 on. About half of all games have lines: books rarely list FCS-vs-FCS games. ESPN season year = the year a season starts.
- From 2023-24, `homeTeamOdds.open` / `.close` carry the home team's line ("+5.5") and prices, so **openers are real book openers**, not our first poll. Before that only the item-level `spread` exists; it is the home line (it agreed with the moneyline favorite in 142 of 146 sampled games).
- Stored in `reported_lines` as `espn:<book>` (close) and `espn-open:<book>` (open). Evaluation data only (MODEL-GOVERNANCE.md). Season years are ESPN's: the year a season ends (2025-26 = 2026).
- Season windows (`services/espn_history_import.py`): Sep 25 to Jun 30, except the 2019-20 bubble (to Oct 15 2020) and 2020-21 (Dec 1 2020 to Jul 25 2021). Preseason games are imported but excluded from models.

ESPN game summaries and injury lists, verified 2026-09-27 (`providers/espn_boxscore.py`, `providers/espn_injuries.py`):

- **Box scores:** healthy scratches are listed with `didNotPlay` and a reason ("COACH'S DECISION"). **Injured and inactive players are not listed at all**, so a regular missing from the box score is an absence. Minutes are whole numbers.
- **The summary's `injuries` block is not historical.** It shows the player's current status: a 2017-18 game lists injuries dated 2026. It is never read.
- **Injury lists** (`/injuries`): NBA ~70 entries (Out, Day-To-Day), NFL ~800 (Out, Doubtful, Questionable, Injured Reserve, and Active = cleared). CFB has a handful, NCAAB none; neither is polled. Each entry has ESPN's update time and an expected return date. The athlete id is only in the profile link.
- **Change log:** a row is written when a player's report changes, and a `cleared` row when the player leaves the list. An empty list while players are listed is treated as a feed glitch (counted, nothing cleared).

- **Men's college basketball** (verified 2026-09-27): lines from 2012-13 (consensus only that year); the books' own openers from 2023-24. Imported 2014-15 on: about 6,000 Division I games a season; about 3,700–4,000 had lines through 2017-18, 5,400–5,800 from 2021-22. ESPN season year = the year a season ends; NBA's COVID dates don't apply (2020-21 began November 25, 2020).

CollegeFootballData, verified 2026-10-03 (`providers/cfbd.py`):

- **Team ids are ESPN's** (133 of 133 FBS teams, 2023). Endpoints that carry only school names map through `/teams`; schools that match no ESPN team (mostly lower divisions and junior colleges in the transfer portal) are counted and skipped.
- **Regular-season week 1 is the preseason poll** (2023: LSU 5th and Florida State 8th, before they met).
- Coverage: recruiting from 2013, returning production from 2014, talent from 2015, the transfer portal from 2021 (the 2017 talent list is short: 157 teams). FCS teams are barely covered.

**Collector outage, 2026-09-27 14:37 to 2026-09-28 05:43 (EDT).** The collector crashed on "database is locked" while the college basketball import held the database, and Windows didn't restart it. No odds or injury history exists for that window; games that started in it have "closing" prices hours old (visible as `close_minutes_before_kickoff`). Since then: the database waits up to 2 minutes for a lock; a failed pass is logged and retried, not fatal; bulk imports commit every 10 games (odds) and a month at a time (schedules); and the scheduled task has a 15-minute watchdog.

**Transient errors:** every ESPN client (schedule, odds, box scores, injuries) retries 5xx responses and transport errors with backoff; a 502 stopped the first college import at 2016.

**Identity bug fixed 2026-09-27:** an ESPN event not yet linked could be matched by teams and time to *another* ESPN event's game. NBA teams can meet twice within 24 hours with home and away reversed (for example DAL-MEM on 2017-10-25, then MEM-DAL the next night). 66 NBA games 2017-18 to 2025-26 had merged; no other sport. Now an ESPN event can only adopt a game with no ESPN link. `ttk repair-merged-games` split the 66, dropped their ESPN lines and re-imported them.

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
