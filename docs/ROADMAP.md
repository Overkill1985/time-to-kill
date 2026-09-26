# Roadmap

Status as of 2026-09-26.

| Phase | Scope | Status |
|---|---|---|
| 1. Assessment | Tooling inventory, repo analysis, docs | **Done** |
| 2. Foundation | Schema + migrations, provider interfaces, odds ingestion, schedule/results, API shell | **Mostly done.** The ESPN schedule/results provider and cross-provider game linking are built and verified live. Remaining: a frontend shell, scheduled ingestion, and a first live run of The Odds API adapter (needs a key) |
| 3. Betting math | Odds conversions, no-vig, edge, EV, fair odds, Kelly, parlays, settlement, CLV, consensus, qualification | **Done** |
| 4. First model | NFL spread: historical data ingestion (nflverse), Elo baseline, then logistic regression, walk-forward validation, calibration, model registry | **In progress.** The history import is done (7,548 games). Built and validated: Elo, a key-number margin model (adopted: fixes push prediction), a season-by-season home field (tested, not adopted), a market-anchored model (no significant edge), and EPA and QB-change features from play-by-play (adopted; they close a third of Elo's margin-error gap to the market, but no edge against it). All stay DEVELOPMENT and the test seasons are still sealed (docs/MODELS.md). Next: timestamped odds history |
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
4. ~~NFL history~~ **Done 2026-09-26:** nflverse games from 1999 to 2026, with reported lines used for evaluation only.
   - Still to do: team game stats and EPA from play-by-play.
5. ~~Elo baseline~~ **Done 2026-09-26:** see docs/MODELS.md. The test seasons are still sealed.
6. ~~Beat the baseline~~ **Done 2026-09-26:**
   - The key-number margin model is adopted.
   - Season-by-season home field was not adopted.
   - The market-anchored model shows no edge (z −0.2).
7. **New information for the market-anchored model**, each judged by its paired z against the market on validation:
   - ~~quarterback status~~ and ~~EPA from play-by-play~~ **done 2026-09-26:** signal confirmed, no edge against the (probably closing) reported lines;
   - **timestamped odds history (highest priority):** opening, intraday and closing prices, so models can be tested against early lines and measured by closing-line value;
   - injuries at bet time;
   - rest, travel and weather;
   - timestamped odds, so bets can be scored by closing-line value.
   - Only a model that clearly beats the market goes to PAPER, and only then are the test seasons scored.

## Open decisions

- Whether to build the PropLine REST adapter. It needs a PropLine key and confirmed endpoint docs.
- Frontend technology for Phase 6. A server-rendered minimal UI versus a small SPA.
