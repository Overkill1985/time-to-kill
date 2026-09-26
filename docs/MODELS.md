# Model results

Every result lists sample sizes. "Market" means the no-vig probability from nflverse's
*reported* lines. Their timing isn't documented upstream; see DATA-SOURCES.md.

**Status: every NFL spread model is DEVELOPMENT.** None has a demonstrated edge over the
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

### What could actually beat the market (next)

Elo only knows past scores, and the market already knows those. An edge needs information that is timely or better processed:

- quarterback and injury status at the time of the bet;
- EPA and success rate from play-by-play;
- rest, travel and weather;
- **timestamped** odds, so a model can be tested against an earlier line and measured by closing-line value.

These plug into the market-anchored model as extra disagreement terms. Each is judged by the paired z against the market on validation. Only a model that clearly beats the market there gets promoted to PAPER, and only then are the test seasons scored, once.
