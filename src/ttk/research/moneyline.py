"""Moneylines from the validated spread models' margin distributions.

Every sport's spread model is a full distribution over the home margin, so it
also prices the moneyline: P(home wins | no tie) = mass above 0 over mass not at
0 (an NFL tie refunds a moneyline bet; the other sports cannot tie). Three
probabilities per game are compared with the closing moneyline market (no-vig):

- ``standalone``: centered on the feature model's expected margin;
- ``spread_implied``: centered where the distribution's cover probability at the
  closing spread equals the closing spread market's no-vig, so it carries no
  model opinion: the moneyline the spread market implies under this
  distribution. Its gap to the moneyline market is the disagreement between
  the two markets;
- ``anchored``: centered where the validated market-anchored spread model's
  cover probability holds at the closing spread (what the card would price).

Scored on VALIDATE seasons only: paired log loss against the moneyline market,
and results betting the side each probability favors at the closing moneyline
price by edge threshold.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ttk import betting_math as bm
from ttk.models.anchored import MarketAnchoredModel
from ttk.research.espn_models import feature_values
from ttk.research.frozen import FrozenSpreadModel

Pmf = Callable[[float], Mapping[int, float]]
EDGE_THRESHOLDS = (0.0, 0.02, 0.04, 0.06)
VARIANTS = ("standalone", "spread_implied", "anchored")


@dataclass(frozen=True)
class MlRow:
    game_id: int
    mu: float
    home_spread: float | None
    market_home_cover: float | None
    """Closing spread no-vig P(home covers)."""
    home_moneyline: float
    away_moneyline: float
    home_score: int
    away_score: int


def no_vig(a: float | None, b: float | None) -> float | None:
    """No-vig probability of side a; None unless both are real American prices."""
    if a is None or b is None or -100 < a < 100 or -100 < b < 100:
        return None
    return bm.no_vig_probabilities(
        [bm.american_to_decimal(a), bm.american_to_decimal(b)]
    ).probabilities[0]


def home_win_excluding_tie(pmf: Mapping[int, float]) -> float:
    win = sum(p for k, p in pmf.items() if k > 0)
    tie = pmf.get(0, 0.0)
    return win / (1 - tie) if tie < 1 else 0.5


def _cover(pmf: Mapping[int, float], home_line: float) -> float:
    win = sum(p for k, p in pmf.items() if k + home_line > 0)
    push = sum(p for k, p in pmf.items() if k + home_line == 0)
    return win / (1 - push) if push < 1 else 0.5


def center_for(pmf: Pmf, home_line: float, target: float) -> float:
    """The mean at which P(home covers | no push) at ``home_line`` is ``target``."""
    low, high = -60.0, 60.0
    for _ in range(40):
        mid = (low + high) / 2
        low, high = (mid, high) if _cover(pmf(mid), home_line) < target else (low, mid)
    return (low + high) / 2


def probabilities(row: MlRow, pmf: Pmf, anchored: MarketAnchoredModel) -> dict[str, float]:
    out = {"standalone": home_win_excluding_tie(pmf(row.mu))}
    if row.home_spread is not None and row.market_home_cover is not None:
        for variant in ("spread_implied", "anchored"):
            out[variant] = variant_probability(
                variant, pmf, anchored, row.mu, row.home_spread, row.market_home_cover
            )[0]
    return out


@dataclass
class Bets:
    min_edge: float
    bets: int = 0
    wins: int = 0
    units: float = 0.0

    @property
    def roi(self) -> float | None:
        return self.units / self.bets if self.bets else None


@dataclass
class VariantScore:
    name: str
    games: int = 0
    log_loss: float = 0.0
    market_log_loss: float = 0.0
    diff: float = 0.0
    se: float = 0.0
    bets: list[Bets] = field(default_factory=lambda: [Bets(e) for e in EDGE_THRESHOLDS])

    @property
    def z(self) -> float:
        return self.diff / self.se if self.se else 0.0


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


def evaluate(
    rows: Iterable[MlRow], pmf: Pmf, anchored: MarketAnchoredModel
) -> dict[str, VariantScore]:
    """Every variant on the same games: those with real moneyline prices on both
    sides, a spread market, and no tie."""
    diffs: dict[str, list[float]] = {v: [] for v in VARIANTS}
    scores = {v: VariantScore(v) for v in VARIANTS}
    for r in rows:
        market = no_vig(r.home_moneyline, r.away_moneyline)
        margin = r.home_score - r.away_score
        if market is None or margin == 0:
            continue
        probs = probabilities(r, pmf, anchored)
        if set(probs) != set(VARIANTS):
            continue
        y = int(margin > 0)
        for name, p in probs.items():
            s = scores[name]
            a, b = _ll(p, y), _ll(market, y)
            s.games += 1
            s.log_loss += a
            s.market_log_loss += b
            diffs[name].append(a - b)
            edge = p - market
            home = edge >= 0
            price = r.home_moneyline if home else r.away_moneyline
            won = (y == 1) == home
            for bet in s.bets:
                if abs(edge) < bet.min_edge:
                    continue
                bet.bets += 1
                bet.wins += won
                bet.units += bm.american_to_decimal(price) - 1 if won else -1.0
    for name, s in scores.items():
        d = diffs[name]
        if not d:
            continue
        n = len(d)
        s.log_loss /= n
        s.market_log_loss /= n
        s.diff = sum(d) / n
        s.se = math.sqrt(sum((x - s.diff) ** 2 for x in d) / (n - 1) / n) if n > 1 else 0.0
    return scores


def format_report(label: str, scores: Mapping[str, VariantScore]) -> Sequence[str]:
    lines = []
    for s in scores.values():
        if not s.games:
            lines.append(f"{label} {s.name}: no games")
            continue
        bets = "; ".join(
            f">= {b.min_edge:.0%}: {b.bets} bets ROI {b.roi:+.1%}"
            if b.roi is not None
            else f">= {b.min_edge:.0%}: 0 bets"
            for b in s.bets
        )
        lines.append(
            f"{label} {s.name}: n={s.games} log loss {s.log_loss:.4f} vs market "
            f"{s.market_log_loss:.4f} (diff {s.diff:+.5f}, z {s.z:+.2f}); {bets}"
        )
    return lines


def frozen_ml_rows(model: FrozenSpreadModel, window: tuple[int, int]) -> list[MlRow]:
    """Finished games in ``window`` with closing moneylines, for a frozen model."""
    games = {g.game_id: g for g in model.data.games}
    rows = []
    for game_id, pred in sorted(model.elo.items()):
        g = games[game_id]
        line = model.data.closes.get(game_id)
        if not (window[0] <= pred.season <= window[1]) or not g.played or line is None:
            continue
        if line.home_moneyline is None or line.away_moneyline is None:
            continue
        assert g.home_score is not None and g.away_score is not None
        rows.append(
            MlRow(
                game_id,
                model.margin.expected_margin(feature_values(pred, model.data)),
                line.home_spread,
                no_vig(line.home_spread_odds, line.away_spread_odds),
                line.home_moneyline,
                line.away_moneyline,
                g.home_score,
                g.away_score,
            )
        )
    return rows


def variant_probability(
    variant: str, pmf: Pmf, anchored: MarketAnchoredModel, mu: float, line: float, market: float
) -> tuple[float, float, float]:
    """(P(home wins | no tie), P(tie), center) for a frozen moneyline variant
    (``spread_implied`` or ``anchored``) at the main spread ``line`` whose
    market no-vig P(home covers) is ``market``."""
    if variant == "spread_implied":
        target = market
    elif variant == "anchored":
        target = anchored.home_cover_probability(market, mu + line)
    else:
        raise ValueError(f"unknown moneyline variant {variant!r}")
    center = center_for(pmf, line, target)
    dist = pmf(center)
    return home_win_excluding_tie(dist), dist.get(0, 0.0), center
