import logging
from typing import Dict, Tuple

class Aggregator:
    """Aggregates raw strategy scores into a final confidence score (0-100) and direction.

    Expected `raw_scores` format:
      {
        "TREND": {"score": 72.5, "direction": "BUY"},
        "MEAN_REV": {"score": 20.0, "direction": "SELL"},
        ...
      }

    `weights` is a dict mapping strategy name -> weight (floats). If weights do not sum to 1,
    they are normalized. Missing weights default to 1.0 (equal importance).
    """
    def __init__(self, weights: Dict[str, float] | None):
        self.log = logging.getLogger(self.__class__.__name__)
        self.weights = dict(weights) if weights else {}
        total = sum(self.weights.values())
        if total <= 0:
            # leave weights empty; we'll treat missing weights as 1.0 when aggregating
            self.log.debug("No valid weights provided; using equal weighting for strategies.")
            self.weights = {}
        else:
            # normalize to sum-to-1 for interpretability
            self.weights = {k: float(v) / total for k, v in self.weights.items()}
            s = sum(self.weights.values())
            if abs(s - 1.0) > 1e-9:
                self.log.warning(f"Normalized weights sum to {s:.12f}; adjusting to sum 1.")

    def calculate_final_score(self, raw_scores: dict) -> Tuple[float, str]:
        """Return (score 0-100, direction) based on weighted strategy signals."""
        buy_momentum = 0.0
        sell_momentum = 0.0
        buy_weight = 0.0
        sell_weight = 0.0

        if not raw_scores:
            return 0.0, "NEUTRAL"

        for strat, val in raw_scores.items():
            # support both numeric scores and dicts with score+direction
            score = 0.0
            direction = "NEUTRAL"
            if isinstance(val, dict):
                score = float(val.get("score", 0) or 0)
                direction = str(val.get("direction", "NEUTRAL")).upper()
            else:
                try:
                    score = float(val)
                except Exception:
                    score = 0.0
                direction = "NEUTRAL"

            w = self.weights.get(strat, 1.0) if self.weights else 1.0

            if direction.startswith("BUY"):
                buy_momentum += w * score
                buy_weight += w
            elif direction.startswith("SELL"):
                sell_momentum += w * score
                sell_weight += w
            # NEUTRAL contributes nothing

        # compute average (0-100) for buy/sell sides
        normalized_buy = (buy_momentum / buy_weight) if buy_weight > 0 else 0.0
        normalized_sell = (sell_momentum / sell_weight) if sell_weight > 0 else 0.0

        if normalized_buy > normalized_sell:
            final_direction = "BUY"
            final_score = normalized_buy - normalized_sell
        elif normalized_sell > normalized_buy:
            final_direction = "SELL"
            final_score = normalized_sell - normalized_buy
        else:
            final_direction = "NEUTRAL"
            final_score = 0.0

        # final_score should be in 0-100 already (difference of two 0-100 averages)
        final_score = max(0.0, min(100.0, final_score))

        return final_score, final_direction
