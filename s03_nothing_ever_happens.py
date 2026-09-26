"""S03 - Sesgo anti-dramatismo: comprar NO en eventos dramáticos sobrecomprados."""
from typing import List, Optional

from core_models import BaseStrategy, Market, Opportunity, Signal


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class NothingEverHappens(BaseStrategy):
    name = "s03_nothing_ever_happens"
    tier = "S"
    strategy_id = 3
    required_data = []

    DRAMATIC_KEYWORDS = [
        "war", "invade", "invasion", "crash", "collapse", "impeach", "resign",
        "fire", "default", "ban", "destroy", "overthrow", "assassin", "strike",
    ]
    MIN_YES_PRICE = 0.15
    MAX_YES_PRICE = 0.65
    BASE_NO_PROBABILITY = 0.70

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        for m in markets:
            q_lower = m.question.lower()
            if any(kw in q_lower for kw in self.DRAMATIC_KEYWORDS) and m.active:
                yes_price = self._get_yes_price(m)
                if yes_price is not None and self.MIN_YES_PRICE < yes_price < self.MAX_YES_PRICE:
                    opportunities.append(Opportunity(
                        market_id=m.condition_id,
                        question=m.question,
                        market_price=yes_price,
                        category=m.category,
                        metadata={"tokens": m.tokens},
                    ))
        return opportunities

    def _get_yes_price(self, market: Market) -> Optional[float]:
        for token in market.tokens:
            if str(token.get("outcome", "")).lower() == "yes":
                price = _to_float(token.get("price"))
                return price if price > 0 else None
        return None

    def analyze(self, opportunity: Opportunity, **kwargs: object) -> Optional[Signal]:
        yes_price = opportunity.market_price
        no_price = 1.0 - yes_price
        estimated_no_prob = self.BASE_NO_PROBABILITY
        edge = estimated_no_prob - no_price
        if edge < 0.05:
            return None

        no_token_id = ""
        for token in opportunity.metadata.get("tokens", []):
            if str(token.get("outcome", "")).lower() == "no":
                no_token_id = str(token.get("token_id") or token.get("tokenId") or "")
                break
        if not no_token_id:
            return None

        return Signal(
            market_id=opportunity.market_id,
            token_id=no_token_id,
            side="buy",
            estimated_prob=estimated_no_prob,
            market_price=no_price,
            confidence=0.72,
            strategy_name=self.name,
            metadata={"edge": edge, "recommendation": "BUY NO"},
        )