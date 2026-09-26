# Model governance

A model is judged on out-of-sample calibration, EV and closing line value (CLV),
never on win rate alone. A 70% historical win rate created by leakage is worthless.

## Qualification (implemented: `src/ttk/qualification.py`)

Defaults (all configurable via `TTK_*` settings):

| Criterion | Default |
|---|---|
| Model probability | >= 56.0% |
| Edge vs consensus no-vig | >= +2.0 percentage points |
| Expected value | > 0% |
| Data quality | >= ACCEPTABLE |
| Odds age | <= 30 minutes |
| Model lifecycle status | ACTIVE |

Classification:

1. **NO_BET** if any of these holds:
   - the odds are stale, or timestamped in the future;
   - data quality is UNUSABLE;
   - uncertainty is INSUFFICIENT_DATA;
   - the model is RETIRED, or its health is DEGRADED or RETRAIN_REQUIRED.
2. **PASS** if the model does not favor the bet at this price: edge <= 0 or EV <= 0.
3. **QUALIFIED** if every criterion passes.
4. **LEAN** otherwise. The model favors the bet but a criterion fails. For example, it's a PAPER model, the probability is under 56%, the edge is under 2 points, or the data is POOR.

Every result carries the full check list (actual vs required), which is the Why-Not output.
Probability, edge and uncertainty are always reported separately. No
composite "confidence" score is ever presented as a probability.

## Lifecycle

`DEVELOPMENT -> (backtest) -> PAPER -> ACTIVE -> WATCH -> RETIRED`

- PAPER models write predictions but can never produce QUALIFIED.
- Drift health (`HEALTHY / WATCH / DEGRADED / RETRAIN_REQUIRED`) is tracked separately.
  DEGRADED or worse blocks recommendations entirely.
- Promotion to ACTIVE requires all of the following:
  - a walk-forward or season-holdout test on data never used for fitting or model selection;
  - Brier score and log loss better than the no-vig market baseline, or matching it while showing positive CLV;
  - a reliability curve within tolerance;
  - a sample size stated alongside every metric.

## No leakage

- A prediction for time T uses only data timestamped before T. `predictions.inputs_as_of` records the latest input time and is auditable.
- The following are never used as features for a pregame prediction:
  - closing lines;
  - season-end statistics;
  - later injury reports;
  - postgame stat revisions.
- Backtests rebuild features from snapshots as they stood at T. They never use current tables.
- **Market inputs:** a model may take a market price as an input only if the simulated bet is placed at that same price or a later one. Using a later line (such as the close) to decide a bet at an earlier price is leakage.
  - nflverse's reported lines have undocumented timing. So a market-anchored backtest bets at exactly the reported line it used as input.
  - They are never treated as closing lines for closing-line value.
- **Significance:** every model comparison against the market reports the paired per-game log-loss difference, with its standard error. A difference within about 2 standard errors is not an edge.

## Validation protocol

- Splits:
  - Train on seasons N-3 to N-1.
  - Validate on season N.
  - Test on season N+1, touched once.
  - Walk forward week by week within each season.
- Metrics: Brier score, log loss, calibration curve, ROI at the qualification thresholds, average CLV, and maximum drawdown.
- Baselines every model must beat:
  - the no-vig market;
  - home-team only;
  - Elo;
  - simple logistic regression.
- Ensembles only combine individually validated models. Their weights are fitted on validation data.

## Immutability

Predictions and odds snapshots are append-only (database triggers). A re-run writes
a new prediction row; old ones remain for audit and CLV analysis.
