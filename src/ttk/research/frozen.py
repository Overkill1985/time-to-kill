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

from dataclasses import dataclass
from typing import Any

from ttk.models.anchored import MarketAnchoredModel
from ttk.models.elo import EloParams, EloPrediction, run_elo
from ttk.models.margin import KeyNumberMarginModel, MarginModel
from ttk.research.espn_models import (
    FeatureMarginModel,
    SportData,
    SportReport,
    feature_values,
    sport_backtest,
)
from ttk.research.nfl_elo import priced_spreads, prior_home_field

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

    def view(self, game_id: int, home_line: float, market_home_cover: float) -> FrozenView | None:
        pred = self.elo.get(game_id)
        if pred is None:
            return None
        values = feature_values(pred, self.data)
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
