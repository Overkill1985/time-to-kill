"""Frozen spread models for forward tests (ESPN-history sports).

``freeze`` runs the validated pipeline (research/espn_models.sport_backtest: Elo
tuned and every model fitted on TRAIN seasons only, test seasons sealed) and
returns the fitted parameters as plain JSON. ``FrozenSpreadModel`` rebuilds the
predictor from those parameters alone; only *state* moves forward (Elo ratings,
team efficiency, injuries), through the latest finished game.

A frozen artifact is checked when made: the thawed model must reproduce the
backtest's own probabilities on validation games (``verify``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ttk.models.anchored import MarketAnchoredModel
from ttk.models.elo import EloParams, EloPrediction, run_elo
from ttk.models.margin import KeyNumberMarginModel, MarginModel
from ttk.models.metrics import score
from ttk.research.espn_models import (
    FeatureMarginModel,
    SportData,
    SportReport,
    feature_values,
    opener_test,
    sport_backtest,
)
from ttk.research.nfl_elo import (
    PricedSpread,
    _evaluate_candidate,
    priced_spreads,
    prior_home_field,
)

ARTIFACT_VERSION = 1


def freeze(data: SportData, feature_set: str) -> tuple[dict[str, Any], SportReport]:
    report = sport_backtest(data)
    if feature_set not in report.models:
        raise ValueError(f"{feature_set!r} is not fitted for {data.config.sport}")
    tuned = report.base.tuned
    model, anchored = report.models[feature_set], report.anchored[feature_set]
    p = tuned.params
    artifact: dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "sport": str(data.config.sport),
        "feature_set": feature_set,
        "splits": {
            "train": list(data.config.splits.train),
            "validate": list(data.config.splits.validate),
            "test": list(data.config.splits.test),
        },
        "elo": {
            "k": p.k,
            "home_field": p.home_field,
            "season_regression": p.season_regression,
            "margin_of_victory": p.margin_of_victory,
            "initial": p.initial,
            "regression_target": p.regression_target,
            "home_field_mode": tuned.home_field_mode,
            "home_field_per_point": tuned.home_field_per_point,
        },
        "margin": {
            "names": list(model.names),
            "intercept": model.intercept,
            "coefs": model.coefs,
            "sigma": model.key.base.sigma,
            "key_weights": {str(k): v for k, v in model.key.weights.items()},
            "n_train": model.n_train,
        },
        "anchored": {
            "intercept": anchored.intercept,
            "market_coef": anchored.market_coef,
            "disagreement_coef": anchored.disagreement_coef,
            "n_train": anchored.n_train,
        },
    }
    return artifact, report


@dataclass(frozen=True)
class FrozenView:
    expected_margin: float
    home_cover_key: float
    """Standalone feature model: P(home covers | no push) at the line."""
    push: float
    home_cover_anchored: float
    """Market-anchored: P(home covers | no push) given the market's no-vig."""
    features: dict[str, float]


class FrozenSpreadModel:
    def __init__(self, artifact: dict[str, Any], data: SportData) -> None:
        if artifact.get("artifact_version") != ARTIFACT_VERSION:
            raise ValueError("unknown artifact version")
        self.artifact = artifact
        self.data = data
        e = artifact["elo"]
        params = EloParams(
            k=e["k"],
            home_field=e["home_field"],
            season_regression=e["season_regression"],
            margin_of_victory=e["margin_of_victory"],
            initial=e["initial"],
            regression_target=e["regression_target"],
        )
        by_season = None
        if e["home_field_mode"] == "rolling":
            per_point = float(e["home_field_per_point"])
            by_season = {s: per_point * m for s, m in prior_home_field(data.games).items()}
        self.elo: dict[int, EloPrediction] = {
            pred.game_id: pred
            for pred in run_elo(data.games, params, home_field_by_season=by_season)
        }
        m = artifact["margin"]
        self.margin = FeatureMarginModel(
            tuple(m["names"]),
            float(m["intercept"]),
            {k: float(v) for k, v in m["coefs"].items()},
            KeyNumberMarginModel(
                MarginModel(0.0, 0.0, float(m["sigma"])),
                {int(k): float(v) for k, v in m["key_weights"].items()},
            ),
            int(m["n_train"]),
        )
        a = artifact["anchored"]
        self.anchored = MarketAnchoredModel(
            float(a["intercept"]),
            float(a["market_coef"]),
            float(a["disagreement_coef"]),
            int(a["n_train"]),
        )

    def view(
        self,
        game_id: int,
        home_line: float,
        market_home_cover: float,
        *,
        overrides: dict[str, float] | None = None,
    ) -> FrozenView | None:
        """``overrides`` replaces feature values known only at a later time with
        what is known now (e.g. ``missing_diff``, who sits at tip, by the injury
        report's expected missing value at the snapshot)."""
        pred = self.elo.get(game_id)
        if pred is None:
            return None
        values = {**feature_values(pred, self.data), **(overrides or {})}
        mu = self.margin.expected_margin(values)
        probs = self.margin.key.spread_at_mean(home_line, mu)
        anchored = self.anchored.home_cover_probability(market_home_cover, mu + home_line)
        used = {name: values[name] for name in self.margin.names}
        return FrozenView(mu, probs.home_cover_excluding_push, probs.push, anchored, used)


def verify(model: FrozenSpreadModel, report: SportReport, *, sample: int = 200) -> int:
    """The thawed model must match the backtest on validation games. Returns the
    number of games checked; raises if any probability differs."""
    feature_set = model.artifact["feature_set"]
    fitted, anchored = report.models[feature_set], report.anchored[feature_set]
    games = {g.game_id: g for g in model.data.games}
    rows = priced_spreads(
        report.base.tuned.run(model.data.games),
        games,
        model.data.closes,
        model.data.config.splits.validate,
    )[:sample]
    for r in rows:
        mu = fitted.expected_margin(feature_values(r.prediction, model.data))
        expected = anchored.home_cover_probability(r.market_home_cover, mu + r.home_line)
        view = model.view(r.game.game_id, r.home_line, r.market_home_cover)
        if view is None or abs(view.home_cover_anchored - expected) > 1e-9:
            raise AssertionError(f"frozen model differs from the backtest on game {r.game.game_id}")
    return len(rows)


def feature_set_at_tip(model: FrozenSpreadModel) -> bool:
    """True if the frozen model uses a feature known only at tip-off."""
    return model.artifact["feature_set"] in model.data.config.at_tip_only


def score_frozen(model: FrozenSpreadModel, window: tuple[int, int]) -> dict[str, Any]:
    """Score a frozen model, exactly as stored, on seasons ``window`` (its sealed
    test seasons): spread log loss against the closing market (paired), margin
    error, results at the closing price by edge, and betting the opener where
    openers exist. Plain data, for the one-time test report."""
    data = model.data
    variant = model.artifact.get("variant", "anchored")
    games = {g.game_id: g for g in data.games}
    predictions = list(model.elo.values())

    def cover(r: PricedSpread) -> tuple[float, float | None]:
        v = model.view(r.game.game_id, r.home_line, r.market_home_cover)
        assert v is not None
        p = v.home_cover_anchored if variant == "anchored" else v.home_cover_key
        return p, v.push

    rows = priced_spreads(predictions, games, data.closes, window)
    result = _evaluate_candidate(str(variant), rows, cover)
    errors, market_errors = [], []
    for p in predictions:
        g = games[p.game_id]
        if not (window[0] <= p.season <= window[1]) or not g.played:
            continue
        assert g.home_score is not None and g.away_score is not None
        actual = g.home_score - g.away_score
        values = feature_values(p, data)
        errors.append(model.margin.expected_margin(values) - actual)
        line = data.closes.get(p.game_id)
        if line is not None and line.home_spread is not None:
            market_errors.append(-line.home_spread - actual)
    openers = priced_spreads(predictions, games, data.opens, window)
    # A feature known only at tip-off (who actually played) isn't known when the
    # opener is posted: betting the opener with it would be leakage, not a test.
    at_tip = feature_set_at_tip(model)
    opener = (
        opener_test(str(variant), cover, openers, data.closes) if openers and not at_tip else None
    )
    v = result.vs_market
    return {
        "window": list(window),
        "spread_games": result.spread.n,
        "log_loss": result.spread.log_loss,
        "market_log_loss": score(
            [r.market_home_cover for r in rows if r.cover_margin != 0],
            [int(r.cover_margin > 0) for r in rows if r.cover_margin != 0],
        ).log_loss
        if any(r.cover_margin != 0 for r in rows)
        else None,
        "vs_market": v.mean_log_loss_diff,
        "vs_market_se": v.standard_error,
        "z": v.z,
        "margin_games": len(errors),
        "margin_rmse": math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else None,
        "market_rmse": math.sqrt(sum(e * e for e in market_errors) / len(market_errors))
        if market_errors
        else None,
        "betting": [
            {
                "min_edge": b.min_edge,
                "bets": b.bets,
                "wins": b.wins,
                "losses": b.losses,
                "pushes": b.pushes,
                "units": b.units,
                "roi": b.roi,
            }
            for b in result.betting
        ],
        "opener": None
        if opener is None
        else {
            "games": len(openers),
            "price_clv": opener.avg_price_clv,
            "price_clv_n": opener.price_clv_n,
            "points_vs_close": opener.avg_points_gained,
            "points_n": opener.moved_n,
            "betting": [
                {"min_edge": b.min_edge, "bets": b.bets, "units": b.units, "roi": b.roi}
                for b in opener.bets
            ],
        },
    }
