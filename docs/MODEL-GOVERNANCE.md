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

**Pushes:** the model probability is P(win | no push), which is on the same footing as a no-vig market price. The probability and edge thresholds compare those two. EV uses win = p × (1 − P(push)), and a push returns the stake.

**Daily card:**
- It uses exactly the validated artifact, fitted on the training seasons. Nothing is refitted on the validation or test seasons, so the test seasons stay sealed.
- Ratings and features run through the latest completed game.
- The first saved `ttk card` run registers the model as DEVELOPMENT, so the card can never show QUALIFIED until someone promotes the model in the registry.
- Best price and EV use only `TTK_BETTABLE_BOOKS`. The market probability always uses every book.
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
- **Starters (known at kickoff):** QB features use the quarterback who actually started. This is known once inactives are announced, about 90 minutes before kickoff. A model using it is valid only for bets placed after starters are known, and the bet tracker must record the bet time to hold it to that.
- **NBA lineups (known at tip-off):** "missing at tip" uses who actually played, from the box score. Lineups are confirmed about 30 minutes before tip-off, so a model using it is scored against the close only, never at the opener. Its early-line counterpart uses the team's *previous* game ("who sat out last time"), which is known before the opener.
- **Preseason facts** (`team_season_features`) carry `known_at`, when each became public: a coach's hire date, a transfer's date, February 15 for recruiting classes, August 1 for the roster composites and August 22 for the preseason AP poll. A fact is used only for games after it; an undated fact is never used. End-of-season ratings (SP+, final polls) are never features for their own season.
- **NBA injury features** use the report as we observed it at a stated horizon before tip-off (24 h, 1 h), and sit rates learned only from earlier finished games. The 1-hour feature assumes the bet is placed an hour before tip (score it against the close); the 24-hour one is an opener-time proxy. A game before injury tracking began has no injury feature.
- **Injury reports:** there is no free source of past injury status as of a given time. ESPN's game summaries show each player's *current* status even for old games (a 2017 game shows 2026 dates), so they are never used. Injury status comes only from our own polling (`injury_reports`, append-only, stamped with `observed_at`), queried with `injuries_at(t)`.
- **Significance:** every model comparison against the market reports the paired per-game log-loss difference, with its standard error. A difference within about 2 standard errors is not an edge.

## Forward tests

A forward test scores a model on games that had not been played when it was frozen.

- **Frozen artifact.** The model's fitted parameters (fitted on TRAIN, exactly as validated) are stored on its `model_versions` row (`artifact`) when it is frozen, and checked: the thawed model must reproduce the backtest's probabilities on validation games. Only *state* moves forward afterwards (Elo ratings, efficiency, injuries) through the latest finished game. A changed model is a new version.
- **Snapshots** (`forward_predictions`, append-only) are taken once per model, game and horizon: 24 hours before kickoff (when the game first comes inside that window) and 1 hour before. Each records the main spread, the all-book consensus no-vig probability and the best bettable prices **as we saw them**, and is refused if those odds are more than 2 hours old.
- **Scoring** uses finished games only: paired log loss against the market at the snapshot, price CLV against our own close at the same number, points versus the closing main line, and results at the snapshot's best price, each with its sample size.
- **Live substitution** (NBA "injury" models). A frozen model may replace a feature known only at tip-off with its best estimate at the snapshot, and only if the artifact says so (`live_substitution`). The NBA at-tip lineup model's `missing_diff` (who actually sat, fitted on TRAIN) becomes the injury report's expected missing value as observed at the snapshot, re-read on every snapshot pass. Its validation scores were earned with the real value, so its forward record is the only evidence for the substituted version; it is always reported next to the no-injury baseline (`lineup-prev`).
- **Status.** Forward-tested models stay DEVELOPMENT while they are being tested: their snapshots are records, never bets. Promotion to PAPER needs a forward record that beats the market at the snapshot by the significance standard above.

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
