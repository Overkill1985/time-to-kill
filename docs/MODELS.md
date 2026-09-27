# Model results

Every result lists sample sizes. "Market" means the no-vig probability from nflverse's
*reported* lines. Their timing isn't documented upstream; see DATA-SOURCES.md.

**Status: every NFL spread model is DEVELOPMENT, including the EPA and QB models.** None has a demonstrated edge over the
market. The 2022–2025 test seasons are **sealed and have not been scored**. No model is
registered yet. **The NBA spread models are DEVELOPMENT too** (below); their 2025 and 2026
test seasons are sealed.

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


## NBA spread (2026-09-26)

Data: ESPN's core API, one line per game per sportsbook (DATA-SOURCES.md): 11,592 games from
2017-18 to 2025-26, 11,589 with a closing line. Each game's benchmark is the most common home
spread across books, priced by the highest-priority book with real prices on both sides.
Openers are the books' own, and exist from 2023-24 only. Reproduce with `ttk backtest-nba`.

Splits (ESPN season = the year it ends): burn-in 2018, **train 2019–2022**, **validate
2023–2024**, test 2025–2026 (sealed).

### What was fitted on the training seasons

- **Elo:** K=10 (the grid was extended down to 5 because 10 had sat at its edge; 10 remained
  best), margin-of-victory on, 50% regression between seasons. Home court is rolling: 20 Elo
  per point of the prior three seasons' home margin, which gives 35–51 Elo by season. NBA home
  advantage varied by season (it dipped for 2022–2024), and the rolling value fit TRAIN better than every constant.
- **Key-number weights:** 0 gets 0.13 (NBA games can't end tied), 1
  gets 0.76, then 2–7 are near 1. NBA margins don't cluster the way the NFL's do.
- **Rest model:** margin = 0.16 + 0.042 × elo_diff + 0.029 × rest_diff − 2.20 × home
  back-to-back + 1.54 × away back-to-back. Rest days are capped at 4. A back-to-back is worth
  about 2 points: real signal.

### Validation, 2023–2024 (n=2,590 spread games with real prices on both sides)

| Model | Spread log loss | vs market (paired) | z |
|---|---|---|---|
| Market no-vig (close) | 0.6932 | — | — |
| Elo, normal margin | 0.7182 | +0.0249 | +5.7 (worse) |
| Elo, key numbers | 0.7178 | +0.0246 | +5.6 (worse) |
| Rest, key numbers | 0.7147 | +0.0215 | +5.1 (worse) |
| Market-anchored (Elo) | 0.6939 | +0.0007 | +1.2 |
| Market-anchored (rest) | 0.6939 | +0.0007 | +1.3 |

- **Margin RMSE:** Elo 13.66, rest 13.60, the closing line 13.18. Rest closes about an eighth of
  Elo's gap to the market.
- **Moneyline:** Elo log loss 0.6309 against the market's 0.6052 (n=2,629).
- **Pushes:** actual 1.52%, predicted 1.24% by the key-number model.
- **Betting every edge at the close loses about the vig** (ROI −4% to −7% at every threshold).
  The market-anchored models bet rarely: +0.8% ROI on 268 bets at edge ≥ 2%, which is noise.

### Betting the opener (2023-24, n=1,310 games with openers)

The model sees the opener; bets are placed at the opener and settled against results.
- **Price CLV** is reported only when the close stayed on the opening number. It is the bet's
  price against the close's no-vig probability, so a line that didn't move at −110 scores
  about −4.5% (the vig). Elo and rest: −4.6% (n≈200), meaning no movement toward the bet.
  Market-anchored: −7.6% (n=51), meaning prices moved against it.
- **Points versus the close**, where the line moved: Elo +0.12 (n=842), rest +0.21 (n=852),
  market-anchored +0.49 (n=168). Lines tended to move toward the models' sides, the
  market-anchored model most. This is the only encouraging number here. It isn't a price and
  isn't converted into one.
- **ROI at the opener:** Elo −8.8% to −13.8%, rest −8.4% to −10.4%, market-anchored −4.6% on
  every game and +5.2% on 52 bets at edge ≥ 4% (noise at that n).

### Verdicts

- No NBA model beats the closing line. Every one stays DEVELOPMENT, and the test seasons stay
  sealed.
- Rest and back-to-back are real signal that the market already prices.
- Worth pursuing: the market-anchored model's tendency to be on the side the line moves toward
  from the opener. The seasons after 2023-24 with openers are sealed test data, so a second look waits for
  live collection; injury and lineup news at the opener is the next input to test.

### What could actually beat the market (next, NFL)

Elo only knows past scores, and the market already knows those. An edge needs information that is timely or better processed:

- quarterback and injury status at the time of the bet;
- EPA and success rate from play-by-play;
- rest, travel and weather;
- **timestamped** odds, so a model can be tested against an earlier line and measured by closing-line value.

These plug into the market-anchored model as extra disagreement terms. Each is judged by the paired z against the market on validation. Only a model that clearly beats the market there gets promoted to PAPER, and only then are the test seasons scored, once.

**The benchmark is probably the hardest one available.** nflverse's reported lines are most likely closing lines, and closing lines already contain nearly all public information. Even a sound model will look no better than the close. The practical edge in betting usually comes from betting *before* the market has moved: early-week lines, beaten by a model that sees what the close will see. Testing that needs **timestamped odds history**: opening and intraday prices, plus the close for measuring closing-line value. That's the most valuable next data source. PropLine exposes odds history and closing lines; The Odds API's historical odds are a paid tier.
