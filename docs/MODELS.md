# Model results

Every result lists sample sizes. "Market" means the no-vig probability from nflverse's
*reported* lines. Their timing isn't documented upstream; see DATA-SOURCES.md.

**Status: every NFL spread model is DEVELOPMENT, including the EPA and QB models.** None has a demonstrated edge over the
market. The 2022–2025 test seasons are **sealed and have not been scored**. No model is
registered yet.

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

### What could actually beat the market (next)

Elo only knows past scores, and the market already knows those. An edge needs information that is timely or better processed:

- quarterback and injury status at the time of the bet;
- EPA and success rate from play-by-play;
- rest, travel and weather;
- **timestamped** odds, so a model can be tested against an earlier line and measured by closing-line value.

These plug into the market-anchored model as extra disagreement terms. Each is judged by the paired z against the market on validation. Only a model that clearly beats the market there gets promoted to PAPER, and only then are the test seasons scored, once.

**The benchmark is probably the hardest one available.** nflverse's reported lines are most likely closing lines, and closing lines already contain nearly all public information. Even a sound model will look no better than the close. The practical edge in betting usually comes from betting *before* the market has moved: early-week lines, beaten by a model that sees what the close will see. Testing that needs **timestamped odds history**: opening and intraday prices, plus the close for measuring closing-line value. That's the most valuable next data source. PropLine exposes odds history and closing lines; The Odds API's historical odds are a paid tier.
