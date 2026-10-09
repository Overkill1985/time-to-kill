# Roadmap

Status as of 2026-10-08.

| Phase | Scope | Status |
|---|---|---|
| 1. Assessment | Tooling inventory, repo analysis, docs | **Done** |
| 2. Foundation | Schema + migrations, provider interfaces, odds ingestion, schedule/results, API shell | **Done except one item.** ESPN schedule/results, cross-provider game linking, the web UI, and scheduled ingestion (the Windows collector task, since 2026-09-26) are built and running. Remaining: a first live run of The Odds API adapter (needs a key; PropLine is the live source) |
| 3. Betting math | Odds conversions, no-vig, edge, EV, fair odds, Kelly, parlays, settlement, CLV, consensus, qualification | **Done** |
| 4. First model | NFL spread: historical data ingestion (nflverse), Elo baseline, then logistic regression, walk-forward validation, calibration, model registry | **In progress.** The history import is done (7,548 games). Built and validated: Elo, a key-number margin model (adopted: fixes push prediction), a season-by-season home field (tested, not adopted), a market-anchored model (no significant edge), and EPA and QB-change features from play-by-play (adopted; they close a third of Elo's margin-error gap to the market, but no edge against it). All stay DEVELOPMENT; the test seasons were scored once on 2026-10-05, no edge (docs/MODELS.md). Timestamped odds history has been collected since 2026-09-26; the NFL model is under forward test (Phase 9b) |
| 5. Monte Carlo | Reusable score simulation (seeded, reproducible), line sensitivity, maximum acceptable line | **Built, NFL.** The engine, presets and seeds; margin anchored to the validated model; totals around the market; rank copula; CLI, API and UI tab; same-game parlay joints. Validated: no gain over independence for spread × total (z +0.75). **All four sports since 2026-10-05:** college football, NBA and college basketball simulate from their frozen card models (margin centered on the market-anchored cover probability, no ties, TRAIN copula pairs from ESPN closing totals); validated against independence on their validation seasons (docs/MODELS.md) |
| 6. Daily card | Qualified opportunities, filters, Why-Not view, data-quality scoring | **Started.** Since 2026-10-04 the card prices every sport's spreads: NFL with its predictor, college football, NBA and college basketball with their frozen forward-tested market-anchored models (all DEVELOPMENT, so nothing qualifies; the web app builds them in the background and lists those sports as loading meanwhile). `ttk card` and `/api/card` cover with qualification, Why-Not, data quality, uncertainty, bettable books, line movement and prediction snapshots. The first UI is at `/` (Today, Bet Tracker and Performance tabs). **Filters 2026-10-05:** sport, classification, minimum edge and best-price book, in the Today tab (remembered per browser), `ttk card` and `/api/card`. Moneyline and totals models researched 2026-10-05/06: no edge against the market (moneylines from the spread models; a college basketball pace-and-efficiency totals model trailing the closing total by 0.7 points); NCAAB moneylines under forward test. Still to do: put them on the card if a forward test earns it |
| 7. Parlay Lab | Cross-sport slips, joint probability, correlation warnings, same-game simulation | **Started.** Built:

- cross-sport slips at one book, with manual prices;
- model or market leg probabilities;
- joint probability (flagged as independent);
- rule-based same-game correlation (LOW, MODERATE, HIGH or UNKNOWN);
- EV and per-leg diagnostics;
- saving, settlement with push repricing, and parlay performance;
- the UI tab.

Same-game joint probabilities come from Monte Carlo for all four sports (non-NFL once their models have loaded) |
| 8. Bet tracker / bankroll | Bets, settlement, ROI, CLV from closing snapshots, drawdown, bankroll limits | **Started.** Single bets are done: recording with model and market beliefs as of bet time, automatic settlement, CLV from the collector's closes, performance with sample sizes, CLI, API and UI. Parlays are tracked too (Phase 7). **Bankroll done 2026-10-05:** balance rebuilt from append-only deposits and settled results; limits per wager, per day, on open exposure and a stop below the peak, enforced when recording (an override needs a reason, kept on the wager); fractional-Kelly guidance capped by the limits, recommended only for QUALIFIED bets |
| 9. Multi-sport | NFL, then NBA, CFB, NCAAB models in the governance order | **NBA started.** ESPN history 2017-18 to 2025-26 (per-book closes; the books' own openers from 2023-24); Elo, rest/back-to-back, lineup (box-score player availability) and market-anchored spread models; opener test. Lineups are the strongest signal so far (margin RMSE 13.61 → 13.46, close 13.15) but no model beats the close; all DEVELOPMENT, test seasons sealed (docs/MODELS.md). Live NFL/NBA injury collection started 2026-09-27. **Injury features built 2026-10-03** (expected missing value from the report at 24 h / 1 h before tip; walk-forward sit rates; `ttk injury-check`); the collector imports NBA box scores. Next: a walk-forward evaluation on the 2026-27 season with our own timestamped odds, once a few hundred games are played. **CFB started 2026-09-27:** ESPN history 2013–2025 (FBS and FCS), Elo with regression toward each team's recent level, rest, market-anchored, opener test. Elo trails the close by 2.3 points of margin error. **Preseason features added 2026-10-03** (CollegeFootballData: returning production, recruiting, transfers, coaching, preseason poll): margin error 17.58 → 17.20 (close 15.30). **In-season opponent-adjusted efficiency added 2026-10-03:** 17.18, almost nothing beyond Elo. No edge; DEVELOPMENT, 2025 sealed. Not yet tested: in-season quarterback changes (~220 CFBD calls); no historical college injury data exists. **NCAAB started 2026-09-28:** ESPN history 2014-15 to 2025-26 (71k games), Elo toward each team's recent level, rest, market-anchored, opener test. Elo trails the close by 3 points of margin error. **Opponent-adjusted efficiency added 2026-10-03** (points per possession from ESPN team box totals): 14.31 → 12.51 (close 11.36), about 60% of the gap; still no demonstrated edge; DEVELOPMENT, 2025–2026 sealed. Next: forward tests on 2026-27 |
| 9b. Forward tests | Frozen models snapshotted before kickoff and scored on new games | **Started 2026-10-03:** frozen artifacts (verified against the backtest), append-only snapshots at 24 h / 1 h with the market we saw, `ttk forward-report`. NFL and college football first; **NBA added 2026-10-04** (lineup-prev baseline, and the injury model: at-tip lineup coefficients with the injury report's expected missing value at each snapshot); **college basketball added 2026-10-04** (adjusted efficiency, standalone and market-anchored; the collector imports team box totals). **NCAAB moneylines and totals, and NBA totals, added 2026-10-06** (moneylines: spread-implied and anchored, from the frozen spread model; totals: pace-and-efficiency, standalone and anchored; snapshots start with the season in November) |
| 9c. Test seasons | One-time scoring of frozen models on sealed seasons | **Done 2026-10-05:** no model beats the closing market on test; signal rankings held; the NCAAB large-edge pattern failed (docs/MODELS.md). Further changes judged by forward tests |
| 10. Performance Lab | Threshold lab (52-60%), calibration drift, CLV trends | **Built 2026-10-05** on forward-test data only: betting the model's side at probability thresholds 50–60% (record, ROI ± SE, break-even, CLV; under 30 bets flagged), calibration bins for model and market, weekly log loss vs market and CLV with running totals (`ttk lab`, Forward tests tab). Forward closing-line values are now stored once per finished snapshot (reports no longer replay the odds history). Still to do: the same views for real bets once there are some |
| 11. Advanced | Props, injury impact, alerts, movement analysis | **Alerts started 2026-10-04:** health alerts (collector or forward runner down, failing polls, dead runs, low quota) as Windows notifications and a log, from both long-running tasks. **Bet and movement alerts 2026-10-05:** one-time events from the collector: a pending bet's line 1.5+ points from your number, settlements, and steam (consensus main spread 1+ or total 1.5+ in 60 min, 3+ books, games within 24 h, NFL and NBA by default). **Props collection 2026-10-07:** NFL/NBA player-prop snapshots at 24 h and 1 h before kickoff from PropLine (sportsbooks only, roster-checked, `ttk props-report`); no prop model yet |

## Where things stand (2026-10-08)

Everything buildable without new data or decisions is built. What remains waits on the season or on you.

**Waiting on games** (forward tests; a z against the market is shown at 30 decided games per horizon):

- NFL spread: about 15 decided games per horizon now; 30 around Oct 19.
- NFL totals and wind: from Sunday Oct 11, outdoor games only (about 9 a week); 30 decided games per horizon around early November.
- College football spreads: the first full Saturday under forward test is Oct 10.
- NBA spreads and NBA player props: from the Oct 20 opener. NBA totals about Nov 1 (both teams need 5 games).
- College basketball spreads and moneylines: opening week in early November. Totals mid-November (the same 5-game rule).
- NFL player props: collected since 2026-10-07 (24 h and 1 h before kickoff). No prop model until there is history to validate on.

**Open decisions:**

- Whether to add The Odds API to the collector as a daily cross-check (about 180 of its 500 monthly credits for NFL and NBA). Run once by hand on 2026-10-08 (docs/DATA-SOURCES.md).
- Whether a prop model is worth building, once a few weeks of 24 h and 1 h snapshots exist.

**Rest, travel and weather (NFL, measured 2026-10-09):** rest and travel add nothing to the line. Wind does (the total under-reacts to it), so `nfl-total-wind` is under forward test on forecast wind from Week 5 (docs/MODELS.md).

**Not started:** weather for college football. NFL injuries are collected but not yet a model feature; NBA injuries are, through the injury model.

**Follow-ups:** an NBA live check after the opener and an NCAAB one after its opener; review `ttk props-report` (the off-roster names) after the first weekend.
