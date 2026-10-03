# Model results

Every result lists sample sizes. "Market" means the no-vig probability from nflverse's
*reported* lines. Their timing isn't documented upstream; see DATA-SOURCES.md.

**Status: every NFL spread model is DEVELOPMENT, including the EPA and QB models.** None has a demonstrated edge over the
market. The 2022–2025 test seasons are **sealed and have not been scored**. No model is
registered yet. **The NBA, college football and college basketball spread models are
DEVELOPMENT too** (below); their test seasons (NBA and NCAAB 2025-2026, CFB 2025) are sealed.

## NFL spread (2026-09-26)

- **Code:**
  - `src/ttk/models/` (`elo.py`, `margin.py`, `anchored.py`, `metrics.py`)
  - `src/ttk/research/nfl_elo.py`
- **Command:** `ttk backtest-nfl-elo [--report file.json] [--register]`
- **Protocol:**
  - Splits: burn-in 1999–2001, train 2002–2017, validate 2018–2021, test 2022–2025 (sealed).
  - Everything is fitted on the training seasons only.
  - A single pass through games in date order, so each prediction uses only games already played.
  - Spread models are compared on the same games: those with real prices on both sides (from 2006).

### What was fitted on the training seasons

- **Elo:** K=20, season regression 0.50, margin-of-victory on, home field a constant 50 Elo points.
  - A season-by-season home field (a scale times the average home margin over the previous 3 seasons) was also searched. It did **not** improve training log loss, so tuning kept the constant.
- **Normal margin model:** margin = 0.28 + 0.0463·elo_diff, σ 13.69.
- **Key-number weights:** how often each final margin actually occurs, relative to the normal model.

  | Margin | 0 (tie) | 1 | 3 | 4 | 6 | 7 | 10 | 14 |
  |---|---|---|---|---|---|---|---|---|
  | Weight | 0.20 | 0.73 | **2.72** | 0.99 | 1.21 | **1.86** | 1.32 | 1.34 |

- **Market-anchored model:** logit P(cover) = −0.047 − 0.101·logit(market) + 0.0105·disagreement in points (n=2,987).
  - Each point of Elo disagreement moves the cover probability by only about 0.26 percentage points.
  - The market term is near zero because spread prices barely move away from 50/50, so there's little in them to fit.

### Validation, 2018–2021 (n=1,065 decided spread games)

| Model | Cover log loss | vs market (SE) | z | Pushes predicted (actual 2.11%) |
|---|---|---|---|---|
| Market (no-vig) | **0.6925** | – | – | – |
| `elo_normal` | 0.7132 | +0.0207 (0.0067) | **+3.1, significantly worse** | 1.46% |
| `elo_key_numbers` | 0.7155 | +0.0230 (0.0072) | **+3.2, significantly worse** | **2.19%** |
| `market_anchored` | 0.6922 | −0.0003 (0.0013) | −0.2, no difference | – |

Moneyline, for reference (n=1,083): Elo 0.640, market **0.611**, home-win baseline 0.694.

Spread bets at the reported prices, taking the side with the larger model edge:

| Model | Minimum edge | Bets | W-L-P | ROI |
|---|---|---|---|---|
| `elo_normal` | ≥2% | 886 | 425-442-19 | −4.4% |
| `elo_key_numbers` | ≥2% | 903 | 441-441-21 | −2.8% |
| `market_anchored` | ≥0% | 1,088 | 534-531-23 | −0.3% |
| `market_anchored` | ≥2% | 367 | 182-176-9 | +2.9% (95% bootstrap CI −7.2% to +13.1%) |

### Verdicts

- **Key numbers:** keep this margin model. It fixes push prediction (2.19% predicted vs 2.11% actual, where the normal model said 1.46%). Accurate pushes matter for EV and Kelly sizing on whole-number lines. It doesn't improve cover prediction, because Elo's expected margin is the weak input.
- **Season-by-season home field:** not adopted. It didn't help on the training seasons.
- **Elo-only spread models:** significantly worse than the market (z ≈ +3). They must never produce QUALIFIED bets.
- **Market-anchored:** indistinguishable from the market. The +2.9% ROI is within noise. It's the right *shape* for future models: start from the market and let new information move it. But Elo carries no spread information the market doesn't already have.

### EPA and quarterback features (2026-09-26)

- **Data:** nflverse play-by-play for 1999–2026, aggregated to 15,000 team-games plus per-game quarterback dropback EPA. Starting QBs come from `games.csv`; they're known at kickoff (see MODEL-GOVERNANCE.md).
- **Code:** `src/ttk/models/nfl_features.py`. Features are computed in game order, so each game's features come only from earlier games, the same as Elo. Tests prove a game's features can't see its own stats.
- **Features (home minus away, in points):**
  - `epa_net_diff_pts`: offense EPA/play minus defense EPA/play allowed, weighted toward recent games, × 62 plays.
  - `qb_change_diff_pts`: today's starter's shrunken EPA per dropback, minus the team's recent QB level, × 36 dropbacks.
- **Settings tuned on the training seasons:** team half-life 16 games, season carryover 0.6, QB prior 300 dropbacks, chosen from a 12-setting grid by training margin error.
- **Margin regression (training seasons):** margin = 0.89 + 0.0336·elo_diff + 0.230·epa_net_diff_pts + **1.005·qb_change_diff_pts**, σ 13.57.
  - The QB term moves the margin almost exactly point for point, which is what a well-scaled feature should do.
  - EPA takes over part of Elo's weight (Elo's coefficient drops from 0.046 to 0.034).

Validation, 2018–2021:

| Model | Cover log loss | vs market (SE) | z |
|---|---|---|---|
| Market (no-vig) | **0.6925** | – | – |
| `features_key_numbers` | 0.7117 | +0.0192 (0.0056) | +3.5, significantly worse |
| `market_anchored_features` | 0.6929 | +0.0004 (0.0013) | +0.3, no difference |

Margin error, RMSE in points, on validation games with a reported line:

| | All games (n=1,088) | Games with a QB-change swing of 3+ points (n=114) |
|---|---|---|
| Market line | **13.09** | **12.06** |
| EPA + QB features | 13.37 | 12.45 |
| Elo | 13.53 | 12.99 |

**Verdict:** adopt the features as the best non-market predictor, but they're still DEVELOPMENT.
- They close about a third of the gap between Elo's margin error and the market's, and the most where they should: games with a quarterback change.
- The market prices quarterback changes too, and better. Betting the feature model's disagreements loses (−6.5% at a 2% minimum edge). Anchored to the market, it's back to break-even (+0.7%, n=382, within noise).

### First live card (Sunday 2026-09-27, generated Saturday evening)

- **The slate:** 14 NFL games, all modeled; **NO QUALIFIED BETS**, as intended, because the model is DEVELOPMENT.
- **Using every book, 13 sides came out LEAN.** Most of the EV came from exchange and prediction-market prices (novig, Polymarket, Kalshi at +106 to +115 on spreads). Restricted to DraftKings, FanDuel, BetMGM and Fanatics: 5 LEANs, top EV +5.3%.
- **The remaining "edges" come from the model.** The anchored model's market coefficient is about −0.1, so it largely ignores small price skews. It shows ~51% where the no-vig market is ~48%. Validation found no edge (z −0.2), so these LEANs are informational only.

### Monte Carlo engine (2026-09-26)

- **Code:** `src/ttk/models/simulation.py` (the engine), `src/ttk/research/nfl_simulation.py` (fitting and validation), `src/ttk/services/simulation_service.py`.
- **Command:** `ttk simulate --game-id N [--preset quick|detailed|research] [--seed S]`; also `POST /api/simulations/run` and the UI's Simulator tab.
- **Presets:** 10,000, 50,000 or 100,000 runs. A seed makes a run exactly reproducible (tested).
- **How a game is simulated:**
  - **Margin:** drawn from the key-number margin distribution. With a spread market, it's centered where the validated market-anchored model's cover probability holds at the main line, so the simulation adds shape, key numbers and joint structure, but no unvalidated opinion. For Ravens −3.5, the simulation gives 51.6% against the card's 51.3%.
  - **Total:** the market's main total plus historical residuals (actual total minus the reported line, 2002–2017), centered on their median. A market total is a 50/50 point, so the simulated over at the line is 49.7–49.9%. Uncentered, training-era totals ran +0.8 over.
  - **Linking margin and total:** each run takes one historical game's pair: where the favorite's margin landed in its predicted distribution (by rank, jittered so it's exactly uniform), plus its total residual.
  - **Why the rank jitter:** a test showed that reusing the raw historical values baked that one sample's noise into every simulation, about ±0.4 points on key numbers. Scores are whole numbers, non-negative, and consistent with the margin.
- **Validation, 2018–2021 (n=1,053):** does modeled dependence beat independence for "home covers × over"?
  - Categorical log loss: simulation 1.4051, independence 1.4044; difference +0.0006 (SE 0.0009, z +0.75).
  - **No improvement.** Side and total in one game are nearly independent: the favorite/over association in training is +0.054.
  - Both overestimate "covers and over" (0.26, against 0.24 observed), because those seasons' totals went under more often (47.7% over).
- **Verdict: adopted as the joint-probability and distribution engine.** Its value is where legs share the margin: a moneyline and a spread on the same team are nested, and multiplying them as if independent is badly wrong. It also gives line sensitivity, key-number push rates and the maximum acceptable line. It is not a source of spread-versus-total edge.
- **In the Parlay Lab:** a same-game NFL group gets the simulation's lift, P(all legs) ÷ the product of each leg's probability in the same simulated games. The lift multiplies the displayed leg probabilities, and the result is capped at the weakest leg's probability.


## NBA spread (2026-09-27)

Data: ESPN's core API, one line per game per sportsbook, and ESPN box scores
(DATA-SOURCES.md): 11,648 regular-season and playoff games from 2017-18 to 2025-26. Each
game's benchmark is the most common home spread across books, priced by the highest-priority
book with real prices on both sides. Openers are the books' own, and exist from 2023-24 only.
Reproduce with `ttk backtest-nba`.

Splits (ESPN season = the year it ends): burn-in 2018, **train 2019–2022**, **validate
2023–2024**, test 2025–2026 (sealed).

**Numbers changed slightly on 2026-09-27** after 66 wrongly merged games were split and
1,377 malformed ESPN BET lines (2023) were re-fetched (DATA-SOURCES.md). None of the
verdicts changed.

### What was fitted on the training seasons (n=4,955 games)

- **Elo:** K=10 (the grid was extended down to 5 because 10 had sat at its edge; 10 remained
  best), margin-of-victory on, 50% regression between seasons. Home court is rolling: 20 Elo
  per point of the prior three seasons' home margin, which gives 34–52 Elo by season. NBA home
  advantage varied by season (it dipped for 2022–2024), and the rolling value fit TRAIN better
  than every constant.
- **Key-number weights:** 0 gets 0.13 (NBA games can't end tied), 1 gets 0.76, then 2–7 are
  near 1. NBA margins don't cluster the way the NFL's do.
- **Rest:** margin = 0.15 + 0.042 × elo_diff + 0.017 × rest_diff − 2.25 × home back-to-back +
  1.49 × away back-to-back. Rest days are capped at 4. A back-to-back is worth about 2 points.
- **Lineups** (`research/nba_lineups.py`, walk-forward from box scores): each player's value is
  a recency-weighted Game Score; each team's "missing value" sums the value of its regular
  rotation players who didn't play, weighted by how regular they were.
  - **At tip-off** (who actually played; close only): −0.171 points per unit of missing value
    (home minus away). A star worth ~18 missing costs about 3 points.
  - **Previous game** (known before the opener): −0.080 per unit. Absences partly carry over
    to the next game.

### Validation, 2023–2024 (n=2,602 spread games with real prices on both sides)

| Model | Spread log loss | vs market (paired) | z | Margin RMSE |
|---|---|---|---|---|
| Market no-vig (close) | 0.6932 | — | — | 13.15 |
| Elo, key numbers | 0.7181 | +0.0248 | +5.7 (worse) | 13.67 |
| Rest, key numbers | 0.7152 | +0.0219 | +5.2 (worse) | 13.61 |
| Lineups at tip, key numbers | 0.7114 | +0.0181 | +5.4 (worse) | **13.46** |
| Lineups previous game, key numbers | 0.7133 | +0.0200 | +4.9 (worse) | 13.56 |
| Market-anchored (Elo, rest, either lineup) | 0.6939 | +0.0006 to +0.0007 | +1.1 to +1.3 | — |

- **Lineups are the strongest signal so far.** Who played closes about a third of the rest
  model's margin-error gap to the closing line (13.61 → 13.46, market 13.15); the previous
  game's absences close about a tenth.
- **The close already has it.** No model beats the closing line, and anchoring any of them to
  the market adds nothing (z about +1).
- **Betting against the close with lineups loses more** (ROI −7% to −9%, against −5% to −6%
  for rest): where the lineup model disagrees with the close, the close knows something it
  doesn't (minutes limits, late scratches, matchups).
- **Moneyline:** Elo log loss 0.6316 against the market's 0.6050 (n=2,635).
- **Pushes:** actual 1.44%, predicted 1.21% by the key-number model.

### Betting the opener (2023-24, n=1,314 games with openers)

Bets are placed at the opener and settled against results. Lineups at tip-off are **excluded**
here: they aren't known when the opener is posted.
- **Price CLV** is reported only when the close stayed on the opening number. It is the bet's
  price against the close's no-vig probability, so a line that didn't move at −110 scores
  about −4.5% (the vig). Every key-number model: about −4.7% (n≈200), i.e. no movement.
  Market-anchored: about −7.6% (n≈52), i.e. prices moved against it.
- **Points versus the close**, where the line moved (positive = the line moved toward the
  bet's side):

  | Model | Points | n |
  |---|---|---|
  | Elo, key numbers | +0.13 | 849 |
  | Rest, key numbers | +0.22 | 853 |
  | Lineups previous game, key numbers | **+0.31** | 847 |
  | Market-anchored (any) | +0.45 to +0.50 | 169–182 |

  Adding last game's absences moves the key-number model's picks further toward where the line
  goes. This isn't a price and isn't converted into one.
- **ROI at the opener** is still negative: lineups previous game −6.3% on every game (rest
  −8.8%, Elo −9.2%), market-anchored about −4.5%. The small positive results at edge ≥ 4–6%
  (10–57 bets) are noise.

### Verdicts

- No NBA model beats the closing line. Every one stays DEVELOPMENT, and the test seasons stay
  sealed.
- Rest and lineups are real signal that the market prices by the close.
- The early-line lead to follow: models with more information pick the side the line moves
  toward (+0.13 → +0.22 → +0.31 points). The real test is **injury status at the opener**,
  which exists only from our own polling (started 2026-09-27); the 2026-27 season, from
  October, will be the first with it. The later seasons with openers are sealed test data.

## Men's college basketball spread (2026-09-28)

Data: ESPN (DATA-SOURCES.md), Division I, 2014-15 to 2025-26: 70,991 regular-season and
postseason games, 58,762 with a closing line. The books' own openers exist from 2023-24.
Line check: 102 of 50,076 closing lines of 2+ points favour a different team than the
moneyline (0.2%). Reproduce with `ttk backtest-ncaab`.

Splits (ESPN season = the year it ends): burn-in 2015–2016, **train 2017–2022**, **validate
2023–2024**, test 2025–2026 (sealed).

### What was fitted on the training seasons (n=34,044 games)

- **Elo:** K=30, margin-of-victory on, 50% regression each offseason toward the team's own
  recent level (which beat one global mean: power conferences and low-majors are different
  tiers). Home court is rolling: 15 Elo per point of the prior three seasons' home margin
  (109–128 Elo by season, about 4 points). Both optima are inside their grids.
- **Key-number weights:** close to 1 everywhere except 0 (ties are impossible).
- **Rest:** margin = 1.27 + 0.037 × elo_diff − 1.26 × rest_diff − 4.04 × home back-to-back +
  2.58 × away back-to-back (rest capped at 7 days). Back-to-backs are mostly conference
  tournaments, where the team that played the day before is usually the lower seed, so the
  flags partly measure strength that Elo misses rather than fatigue alone.

### Validation, 2023–2024 (n=11,414 spread games with real prices on both sides)

| Model | Spread log loss | vs market (paired) | z | Margin RMSE |
|---|---|---|---|---|
| Market no-vig (close) | 0.6931 | — | — | 11.36 |
| Elo, key numbers | 0.7249 | +0.0318 | +12.0 (worse) | 14.31 |
| Rest, key numbers | 0.7296 | +0.0365 | +12.9 (worse) | 14.06 |
| Market-anchored (Elo) | 0.6933 | +0.0002 | +1.2 | — |
| Market-anchored (rest) | 0.6934 | +0.0003 | +1.2 | — |

- **Elo is 3 points of margin error behind the close**, the widest gap of any sport here:
  with ~360 teams and few games between most of them, results alone rate teams poorly.
- **Moneyline:** Elo log loss 0.5648 against the market's 0.5401 (n=11,177).
- **Pushes:** actual 0.99%, predicted 0.81%.
- **Betting at the close loses** about the vig at every threshold (−4% to −6%).
- **A pattern to watch, not an edge:** the market-anchored model's rare large edges won
  55–33 at edge ≥ 4% in validation (and 40–19 in training), but that is 88 bets at a
  threshold chosen among many, the same model loses 3.4% on its 693 bets at edge ≥ 2%, and
  its per-game score is worse than the market's. Its large edges come from lopsided prices at
  a spread; whether that is signal or a pricing quirk of the benchmark book is untested.

### Betting the opener (2023-24, n=5,755 games with openers)

- **Price CLV** where the close stayed on the opening number: Elo and rest −4.6% (n≈1,280),
  about the vig. Market-anchored −1.9% (n=123) and −2.7% (n=187): prices moved toward it
  somewhat, on small samples.
- **Points versus the close**, where the line moved: Elo +0.07 (n=3,623), rest +0.06
  (n=3,696), market-anchored −0.16 and −0.10 (n=263, 494). No consistent direction.
- **ROI at the opener:** Elo −5.9%, rest −4.4%, market-anchored −2.8% (every game).

### Verdicts

- No college basketball model beats the closing line. Every one stays DEVELOPMENT, and
  2025–2026 stay sealed.
- The gap to the market is widest here; team ratings need information beyond scores (tempo
  and efficiency, rosters, schedule strength across conferences).

## College football spread (2026-09-27)

Data: ESPN (DATA-SOURCES.md), FBS and FCS, 2013 to 2025: 20,991 regular-season and postseason
games, 15,370 with a closing line (books rarely list FCS-vs-FCS games). The books' own openers
exist for part of 2023 and all of 2024-2025. Reproduce with `ttk backtest-cfb`.

Splits (ESPN season = the year it starts): burn-in 2013–2014, **train 2015–2022**, **validate
2023–2024**, test 2025 (sealed). 2026 is the live season.

**The first run was wrong and is not reported.** 1,842 college games (mostly 2022–2023) had
malformed ESPN BET lines, a price stored as the spread (DATA-SOURCES.md). It showed up as an
impossible 34–0 record for the market-anchored model at edge ≥ 6%, and a closing-line margin
error of 17.27. After the re-fetch: 2–1, and 15.30.

### What was fitted on the training seasons (n=12,305 games)

- **Elo:** K=40, margin-of-victory on, 50% regression each offseason **toward the team's own
  recent level** (its average of the last three season-end ratings), which beat regression to
  one global mean: FCS teams are a weaker tier. Home field is rolling: 10 Elo per point of the
  prior three seasons' home margin (59–74 Elo by season). The first grid had both optima on an
  edge; it was widened (K 20–80, 4–15 Elo per point) and the same values won inside it.
- **Key-number weights:** 3 gets 2.56 and 7 gets 2.34, as in the NFL.
- **Rest** (days since the last game, capped at 14, for byes and short weeks) adds nothing:
  the fitted coefficient is small and margin error doesn't change.
- **Preseason** (added 2026-10-03; CollegeFootballData, `research/cfb_preseason.py`): each
  fact is used only for games after it became public. Fitted per unit, home minus away:
  - returning production share +20.8 points per unit (a team returning 20 percentage points
    more of last season's production: about +4 points early in the season);
  - four-year recruiting average +1.2 per standard deviation, transfer-portal balance +1.4
    per SD, preseason AP poll +0.9 per 1,000 points, a new head coach −0.8;
  - talent composite and returning passing share ≈ 0 once those are in (talent is built
    from recruiting).
  The "change" features fade with games played (half weight after 4). 11,464 of 12,305
  training games and 3,256 of 3,330 validation games have preseason facts; most of the rest
  are FCS teams, which the source barely covers.
- **In-season efficiency** (added 2026-10-03; CollegeFootballData per-game PPA, the NFL's
  walk-forward EPA features in `models/nfl_features.py`): team offense minus defense per
  play from earlier games, **opponent-adjusted** (each game judged against the opponent's
  rating going in) because college schedules range from the SEC to FCS. Raw efficiency
  adds nothing to Elo; adjusted, +0.15 points per point of efficiency edge. A grid over its
  memory settings on TRAIN only (36 combinations) moved TRAIN margin error by 0.02 points,
  so the NFL settings stay.

### Validation, 2023–2024 (n=3,017 spread games with real prices on both sides)

| Model | Spread log loss | vs market (paired) | z | Margin RMSE |
|---|---|---|---|---|
| Market no-vig (close) | 0.6935 | — | — | 15.30 |
| Elo, key numbers | 0.7431 | +0.0496 | +7.9 (worse) | 17.58 |
| Rest, key numbers | 0.7453 | +0.0518 | +8.1 (worse) | 17.58 |
| Efficiency, raw | 0.7453 | +0.0518 | +8.2 (worse) | 17.57 |
| Efficiency, opponent-adjusted | 0.7422 | +0.0487 | +8.0 (worse) | 17.49 |
| Preseason, key numbers | 0.7310 | +0.0375 | +6.8 (worse) | 17.20 |
| Preseason + adjusted efficiency | 0.7305 | +0.0370 | +6.8 (worse) | **17.18** |
| Market-anchored (Elo, rest or preseason) | 0.6941–0.6942 | +0.0006 to +0.0007 | +1.4 to +1.6 | — |

- **Elo is far from the college market**, much further than in the NFL or NBA: 2.3 points of
  margin error behind the close. Results alone miss what the market knows (roster turnover,
  transfers, quarterback changes, coaching).
- **Preseason information closes about a sixth of that gap** (17.58 → 17.20; close 15.30) and
  cuts the log-loss deficit from +0.050 to +0.038. Real signal, still far from the close.
- **In-season efficiency adds almost nothing on top** (17.20 → 17.18): Elo, updating fast
  (K=40), already learns most of what per-play efficiency shows. Unlike the NFL, where EPA
  closed a third of Elo's gap.
- **Moneyline:** Elo log loss 0.5606 against the market's 0.5169 (n=2,031).
- **Pushes:** actual 1.53%, predicted 0.79%. The key-number weights are fit around Elo's
  means, which are too far off for the push rate to come out right.
- **Betting at the close loses** at every threshold: Elo −2.5% to −4.8% ROI, market-anchored
  −5.7% (3,064 games) and −9.0% on 170 bets at edge ≥ 2%.

### Betting the opener (n=2,313 games with openers, 2023–2024)

- **Price CLV** where the close stayed on the opening number: Elo and rest −5.3% (n≈435),
  which is about the vig, so no movement toward the bet. Market-anchored −7.0% (n=13).
- **Points versus the close**, where the line moved: Elo −0.15 (n=1,581), rest −0.16
  (n=1,578), preseason −0.06 (n=1,563), preseason + efficiency −0.05 (n=1,573). **College lines move away from the Elo side**, the
  opposite of the NBA; preseason information removes most of that. Market-anchored +0.49 to
  +0.73, but on only 34–51 games.
- **ROI at the opener:** Elo −3.5%, rest −4.0%, preseason −4.2%, market-anchored −6.3% to
  −7.2% (every game).

### Verdicts

- No college model comes near the closing line. Every one stays DEVELOPMENT, and 2025 stays
  sealed.
- Preseason information (returning production, recruiting, transfers, coaching, the
  preseason poll) is real signal, but the market prices it: the preseason model is still
  2 points of margin error behind the close. Team efficiency adds almost nothing beyond Elo.
- Not yet tested: in-season quarterback changes (needs ~220 CollegeFootballData calls, a
  quarter of the monthly quota). There is no historical college injury source.

### What could actually beat the market (next, NFL)

Elo only knows past scores, and the market already knows those. An edge needs information that is timely or better processed:

- quarterback and injury status at the time of the bet;
- EPA and success rate from play-by-play;
- rest, travel and weather;
- **timestamped** odds, so a model can be tested against an earlier line and measured by closing-line value.

These plug into the market-anchored model as extra disagreement terms. Each is judged by the paired z against the market on validation. Only a model that clearly beats the market there gets promoted to PAPER, and only then are the test seasons scored, once.

**The benchmark is probably the hardest one available.** nflverse's reported lines are most likely closing lines, and closing lines already contain nearly all public information. Even a sound model will look no better than the close. The practical edge in betting usually comes from betting *before* the market has moved: early-week lines, beaten by a model that sees what the close will see. Testing that needs **timestamped odds history**: opening and intraday prices, plus the close for measuring closing-line value. That's the most valuable next data source. PropLine exposes odds history and closing lines; The Odds API's historical odds are a paid tier.
