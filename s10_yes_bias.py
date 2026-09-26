"""S10 - Explotación del sesgo minorista hacia YES en mercados virales."""
from typing import List, Optional

from core_models import BaseStrategy, Market, Opportunity, Signal


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class YesBiasExploitation(BaseStrategy):
    name = "s10_yes_bias"
    tier = "S"
    strategy_id = 10
    required_data = []

    VIRAL_KEYWORDS = [
        "first", "ever", "historic", "record", "breakthrough", "revolutionary", "unprecedented",
    ]
    MIN_EDGE = 0.04

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        for m in markets:
            q_lower = m.question.lower()
            is_viral = any(kw in q_lower for kw in self.VIRAL_KEYWORDS)
            yes_price = None
            for token in m.tokens:
                if str(token.get("outcome", "")).lower() == "yes":
                    price = _to_float(token.get("price"))
                    yes_price = price if price > 0 else None
                    break

            if yes_price is not None and ((is_viral and yes_price > 0.25) or (yes_price > 0.35 and m.volume > 10000)):
                opportunities.append(Opportunity(
                    market_id=m.condition_id,
                    question=m.question,
                    market_price=yes_price,
                    category=m.category,
                    metadata={"tokens": m.tokens, "is_viral": is_viral},
                ))
        return opportunities

    def analyze(self, opportunity: Opportunity, **kwargs: object) -> Optional[Signal]:
        yes_price = opportunity.market_price
        no_price = 1.0 - yes_price
        estimated_no_prob = 0.78 if opportunity.metadata.get("is_viral") else 0.72
        edge = estimated_no_prob - no_price
        if edge < self.MIN_EDGE:
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
            confidence=0.75,
            strategy_name=self.name,
            metadata={"edge": edge, "viral_hype": opportunity.metadata.get("is_viral"), "recommendation": "BUY NO"},
        )