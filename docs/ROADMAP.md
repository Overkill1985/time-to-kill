# Roadmap

Status as of 2026-09-26.

| Phase | Scope | Status |
|---|---|---|
| 1. Assessment | Tooling inventory, repo analysis, docs | **Done** |
| 2. Foundation | Schema + migrations, provider interfaces, odds ingestion, schedule/results, API shell | **Mostly done.** The ESPN schedule/results provider and cross-provider game linking are built and verified live. Remaining: a frontend shell, scheduled ingestion, and a first live run of The Odds API adapter (needs a key) |
| 3. Betting math | Odds conversions, no-vig, edge, EV, fair odds, Kelly, parlays, settlement, CLV, consensus, qualification | **Done** |
| 4. First model | NFL spread: historical data ingestion (nflverse), Elo baseline, then logistic regression, walk-forward validation, calibration, model registry | Next |
| 5. Monte Carlo | Reusable score simulation (seeded, reproducible), line sensitivity, maximum acceptable line | |
| 6. Daily card | Qualified opportunities, filters, Why-Not view, data-quality scoring | |
| 7. Parlay Lab | Cross-sport slips, joint probability, correlation warnings, same-game simulation | |
| 8. Bet tracker / bankroll | Bets, settlement, ROI, CLV from closing snapshots, drawdown, bankroll limits | |
| 9. Multi-sport | NFL, then NBA, CFB, NCAAB models in the governance order | |
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
4. **NFL history:**
   - Import nflverse schedules, results and closing lines for 2018 onward. Closing lines are for evaluation only, never features.
   - Import team game stats.
5. **Elo baseline for the NFL spread:**
   - Model the margin distribution.
   - Derive P(cover) and P(push) at any line.
   - Run a walk-forward backtest against no-vig closing lines.

## Open decisions

- Whether to build the PropLine REST adapter. It needs a PropLine key and confirmed endpoint docs.
- Frontend technology for Phase 6. A server-rendered minimal UI versus a small SPA.
