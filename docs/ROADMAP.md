# Roadmap

Status as of 2026-09-26.

| Phase | Scope | Status |
|---|---|---|
| 1. Assessment | Tooling inventory, repo analysis, docs | **Done** |
| 2. Foundation | Schema + migrations, provider interfaces, odds ingestion, schedule/results, API shell | **Mostly done.** The ESPN schedule/results provider and cross-provider game linking are built and verified live. Remaining: a frontend shell, scheduled ingestion, and a first live run of The Odds API adapter (needs a key) |
| 3. Betting math | Odds conversions, no-vig, edge, EV, fair odds, Kelly, parlays, settlement, CLV, consensus, qualification | **Done** |
| 4. First model | NFL spread: historical data ingestion (nflverse), Elo baseline, then logistic regression, walk-forward validation, calibration, model registry | **In progress.** The history import is done (7,548 games). Built and validated: Elo, a key-number margin model (adopted: fixes push prediction), a season-by-season home field (tested, not adopted), a market-anchored model (no significant edge), and EPA and QB-change features from play-by-play (adopted; they close a third of Elo's margin-error gap to the market, but no edge against it). All stay DEVELOPMENT and the test seasons are still sealed (docs/MODELS.md). Next: timestamped odds history |
| 5. Monte Carlo | Reusable score simulation (seeded, reproducible), line sensitivity, maximum acceptable line | **Built, NFL.** The engine, presets and seeds; margin anchored to the validated model; totals around the market; rank copula; CLI, API and UI tab; same-game parlay joints. Validated: no gain over independence for spread × total (z +0.75). Still to do: other sports, once they have margin models |
| 6. Daily card | Qualified opportunities, filters, Why-Not view, data-quality scoring | **Started.** `ttk card` and `/api/card` cover NFL spreads, with qualification, Why-Not, data quality, uncertainty, bettable books, line movement and prediction snapshots. The first UI is at `/` (Today, Bet Tracker and Performance tabs). Still to do: filters, and moneyline/total markets once models exist |
| 7. Parlay Lab | Cross-sport slips, joint probability, correlation warnings, same-game simulation | **Started.** Built:

- cross-sport slips at one book, with manual prices;
- model or market leg probabilities;
- joint probability (flagged as independent);
- rule-based same-game correlation (LOW, MODERATE, HIGH or UNKNOWN);
- EV and per-leg diagnostics;
- saving, settlement with push repricing, and parlay performance;
- the UI tab.

Same-game NFL joint probabilities now come from Monte Carlo. Still to do: other sports |
| 8. Bet tracker / bankroll | Bets, settlement, ROI, CLV from closing snapshots, drawdown, bankroll limits | **Started.** Single bets are done: recording with model and market beliefs as of bet time, automatic settlement, CLV from the collector's closes, performance with sample sizes, CLI, API and UI. Still to do: parlays (Phase 7), bankroll limits and Kelly sizing |
| 9. Multi-sport | NFL, then NBA, CFB, NCAAB models in the governance order | **NBA started.** ESPN history 2017-18 to 2025-26 (per-book closes; the books' own openers from 2023-24); Elo, rest/back-to-back, lineup (box-score player availability) and market-anchored spread models; opener test. Lineups are the strongest signal so far (margin RMSE 13.61 → 13.46, close 13.15) but no model beats the close; all DEVELOPMENT, test seasons sealed (docs/MODELS.md). Live NFL/NBA injury collection started 2026-09-27. **Injury features built 2026-10-03** (expected missing value from the report at 24 h / 1 h before tip; walk-forward sit rates; `ttk injury-check`); the collector imports NBA box scores. Next: a walk-forward evaluation on the 2026-27 season with our own timestamped odds, once a few hundred games are played. **CFB started 2026-09-27:** ESPN history 2013–2025 (FBS and FCS), Elo with regression toward each team's recent level, rest, market-anchored, opener test. Elo trails the close by 2.3 points of margin error. **Preseason features added 2026-10-03** (CollegeFootballData: returning production, recruiting, transfers, coaching, preseason poll): margin error 17.58 → 17.20 (close 15.30). **In-season opponent-adjusted efficiency added 2026-10-03:** 17.18, almost nothing beyond Elo. No edge; DEVELOPMENT, 2025 sealed. Not yet tested: in-season quarterback changes (~220 CFBD calls); no historical college injury data exists. **NCAAB started 2026-09-28:** ESPN history 2014-15 to 2025-26 (71k games), Elo toward each team's recent level, rest, market-anchored, opener test. Elo trails the close by 3 points of margin error; no edge; DEVELOPMENT, 2025–2026 sealed |
| 10. Performance Lab | Threshold lab (52-60%), calibration drift, CLV trends | |
| 11. Advanced | Props, injury impact, alerts, movement analysis | |

## Next steps (Phase 2 remainder, then Phase 4)

1. **Live odds run:**
   - Set `TTK_ODDS_API_KEY`.
   - Run `ttk ingest-odds --sport NFL`.
   - Verify the stored snapshots against the book.
2. ~~Schedule and results provider~~ **Done 2026-09-26:**
   - ESPN covers all four sports.
   - Cross-provider linking is resolved by ESPN id, never guessed.
   - Follow-ups:
     - an NCAAB live run once the season starts (Nov);
     - an NBA live run once the season starts (Oct);
     - an unmatched-team review view.
3. **Scheduled ingestion:** a simple loop or OS task that respects API credits.
4. ~~NFL history~~ **Done 2026-09-26:** nflverse games from 1999 to 2026, with reported lines used for evaluation only.
   - Still to do: team game stats and EPA from play-by-play.
5. ~~Elo baseline~~ **Done 2026-09-26:** see docs/MODELS.md. The test seasons are still sealed.
6. ~~Beat the baseline~~ **Done 2026-09-26:**
   - The key-number margin model is adopted.
   - Season-by-season home field was not adopted.
   - The market-anchored model shows no edge (z −0.2).
7. **New information for the market-anchored model**, each judged by its paired z against the market on validation:
   - ~~quarterback status~~ and ~~EPA from play-by-play~~ **done 2026-09-26:** signal confirmed, no edge against the (probably closing) reported lines;
   - **timestamped odds history (highest priority):** opening, intraday and closing prices, so models can be tested against early lines and measured by closing-line value.
     - **Built 2026-09-26:** the PropLine adapter, `ttk collect-odds`, line history and CLV.
     - **Done 2026-09-26:** the PropLine key is configured and verified live, change-only storage is built, and the DraftKings/exchange name bug is fixed.
     - **Running since 2026-09-26 16:14:** the Windows scheduled task "Time-to-Kill odds collector" (see README). NFL, CFB and NBA (openers up to 90 days out) are polled every 15 minutes. NCAAB starts once it has games within 7 days.
     - After that, each week of collection adds opening-to-closing data. One NFL season is roughly 270 games;
   - injuries at bet time;
   - rest, travel and weather;
   - timestamped odds, so bets can be scored by closing-line value.
   - Only a model that clearly beats the market goes to PAPER, and only then are the test seasons scored.

## Open decisions

- Whether to build the PropLine REST adapter. It needs a PropLine key and confirmed endpoint docs.
- Frontend technology for Phase 6. A server-rendered minimal UI versus a small SPA.
