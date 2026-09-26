"""S12 - Cosecha de contratos YES de alta probabilidad cercanos a resolución."""
from datetime import datetime, timezone
from typing import List, Optional

from core_models import BaseStrategy, Market, Opportunity, Signal


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class HighProbabilityHarvesting(BaseStrategy):
    name = "s12_high_prob_harvesting"
    tier = "A"
    strategy_id = 12
    required_data = []

    BUY_MIN_PRICE = 0.93
    BUY_MAX_PRICE = 0.985
    MAX_DAYS_TO_EXPIRY = 30

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        now = datetime.now(timezone.utc)
        for m in markets:
            if not m.active:
                continue

            yes_price = None
            for token in m.tokens:
                if str(token.get("outcome", "")).lower() == "yes":
                    price = _to_float(token.get("price"))
                    yes_price = price if price > 0 else None
                    break

            if yes_price is not None and self.BUY_MIN_PRICE <= yes_price <= self.BUY_MAX_PRICE:
                days_left = 15.0  # fallback si Gamma no envía fecha
                if m.end_date_iso:
                    try:
                        end_dt = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        delta = (end_dt - now).total_seconds() / 86400.0
                        days_left = max(0.5, delta)
                    except Exception:
                        pass

                if days_left <= self.MAX_DAYS_TO_EXPIRY:
                    opportunities.append(Opportunity(
                        market_id=m.condition_id,
                        question=m.question,
                        market_price=yes_price,
                        category=m.category,
                        metadata={"tokens": m.tokens, "days_left": days_left},
                    ))
        return opportunities

    def analyze(self, opportunity: Opportunity, **kwargs: object) -> Optional[Signal]:
        yes_price = opportunity.market_price
        days_left = max(0.5, _to_float(opportunity.metadata.get("days_left"), 10.0))
        profit_pct = (1.0 - yes_price) / yes_price
        annualized_yield = profit_pct * (365.0 / days_left)

        token_id = ""
        for token in opportunity.metadata.get("tokens", []):
            if str(token.get("outcome", "")).lower() == "yes":
                token_id = str(token.get("token_id") or token.get("tokenId") or "")
                break
        if not token_id:
            return None

        return Signal(
            market_id=opportunity.market_id,
            token_id=token_id,
            side="buy",
            estimated_prob=0.99,
            market_price=yes_price,
            confidence=0.90,
            strategy_name=self.name,
            metadata={
                "days_left": round(days_left, 1),
                "profit_pct": f"{profit_pct * 100:.2f}%",
                "annualized_yield": f"{annualized_yield * 100:.1f}%",
            },
        )