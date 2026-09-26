"""S02 - Arbitraje meteorológico con estimación NOAA simplificada."""
import math
import re
from typing import List, Optional

from core_models import BaseStrategy, Market, Opportunity, Signal


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _find_token(tokens: List[dict], outcome: str) -> str:
    for token in tokens:
        if str(token.get("outcome", "")).lower() == outcome.lower():
            return str(token.get("token_id") or token.get("tokenId") or "")
    return ""


class WeatherNOAA(BaseStrategy):
    name = "s02_weather_noaa"
    tier = "S"
    strategy_id = 2
    required_data = []

    WEATHER_KEYWORDS = [
        "temperature", "weather", "celsius", "fahrenheit", "rain", "snow",
        "degrees", "degree", "temp", "precipitation",
    ]
    MIN_EDGE = 0.05
    TEMP_SIGMA = 2.2  # Desviación estándar típica para forecast de temperatura

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        for m in markets:
            q_lower = m.question.lower()
            if any(kw in q_lower for kw in self.WEATHER_KEYWORDS) and m.active:
                yes_price = self._get_yes_price(m)
                if yes_price is not None:
                    opportunities.append(Opportunity(
                        market_id=m.condition_id,
                        question=m.question,
                        market_price=yes_price,
                        category="Weather",
                        metadata={"tokens": m.tokens, "volume": m.volume},
                    ))
        return opportunities

    def _get_yes_price(self, market: Market) -> Optional[float]:
        for token in market.tokens:
            if str(token.get("outcome", "")).lower() == "yes":
                price = _to_float(token.get("price"))
                return price if price > 0 else None
        return None

    @staticmethod
    def _normal_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def analyze(self, opportunity: Opportunity, forecast_temp: Optional[float] = None, **kwargs: object) -> Optional[Signal]:
        yes_price = opportunity.market_price
        no_price = 1.0 - yes_price

        # Extraer umbral de temperatura de la pregunta (ej: "exceed 75F")
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees?|f|fahrenheit|c|celsius)", opportunity.question.lower())
        if not match:
            return None

        target_temp = float(match.group(1))
        # Si no llega pronóstico externo en vivo, usa un estimador conservador base.
        mu = forecast_temp if forecast_temp is not None else target_temp - 2.0
        sigma = self.TEMP_SIGMA

        prob_exceed = 1.0 - self._normal_cdf((target_temp - mu) / sigma)
        yes_edge = prob_exceed - yes_price
        no_edge = (1.0 - prob_exceed) - no_price
        if yes_edge < self.MIN_EDGE and no_edge < self.MIN_EDGE:
            return None

        tokens = opportunity.metadata.get("tokens", [])
        buy_yes = yes_edge >= no_edge
        selected_outcome = "YES" if buy_yes else "NO"
        token_id = _find_token(tokens, selected_outcome)
        if not token_id:
            return None

        return Signal(
            market_id=opportunity.market_id,
            token_id=token_id,
            side="buy",
            estimated_prob=prob_exceed if buy_yes else (1.0 - prob_exceed),
            market_price=yes_price if buy_yes else no_price,
            confidence=0.88,
            strategy_name=self.name,
            metadata={
                "target_temp": target_temp,
                "forecast_mu": mu,
                "side_chosen": selected_outcome,
                "yes_edge": yes_edge,
                "no_edge": no_edge,
            },
        )