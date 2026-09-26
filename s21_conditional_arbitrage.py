"""S21 - Arbitraje logico de escaleras de umbral (adaptado a Kalshi).

En Polymarket la escalera vivia dentro de un mercado multiresultado. En Kalshi
cada umbral es un **mercado binario independiente** dentro del mismo evento
(p.ej. "BTC above $100,000?" y "BTC above $120,000?"), por lo que la coherencia
matematica se comprueba entre mercados hermanos del mismo `event_ticker`.

Regla logica:
- Para umbrales tipo "above / over / at least", P(YES) debe ser **no creciente**
  a medida que sube el umbral (es mas facil superar 100k que 120k).
- Para umbrales tipo "below / under / at most", P(YES) debe ser **no decreciente**.

Si se invierte el orden, hay una incoherencia explotable: se compra YES en el
umbral mas barato (el que deberia ser mas probable) o NO en el mas caro.
"""
import re
from typing import List, Optional

from core_models import BaseStrategy, Market, Opportunity, Signal


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kK])?")
UP_WORDS = ("above", "over", "at least", "greater than", "higher than", "exceed", ">")
DOWN_WORDS = ("below", "under", "at most", "less than", "lower than", "dip to", "fall to", "<")


def _extract_strike(text: str) -> Optional[float]:
    """Extraer el umbral numerico mas relevante del texto del mercado."""
    best: Optional[float] = None
    for match in NUMBER_RE.finditer(text or ""):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if match.group(2):
            value *= 1000.0
        if value >= 1.0:
            best = value if best is None else max(best, value)
    return best


def _direction(text: str) -> Optional[str]:
    lowered = (text or "").lower()
    if any(word in lowered for word in UP_WORDS):
        return "up"
    if any(word in lowered for word in DOWN_WORDS):
        return "down"
    return None


class ConditionalArbitrage(BaseStrategy):
    name = "s21_conditional_arbitrage"
    tier = "S"
    strategy_id = 21
    required_data = ["event_siblings"]

    MIN_EDGE = 0.03          # diferencia minima explotable entre umbrales
    MIN_PRICE = 0.03

    def _ladder(self, m: Market) -> Optional[dict]:
        """Construir la escalera de umbrales del evento (self + hermanos)."""
        if not m.event_siblings:
            return None

        legs: List[dict] = []

        own_text = f"{m.question} {getattr(m, 'strike_type', '')}"
        own_strike = _extract_strike(own_text)
        own_direction = _direction(m.question)
        own_yes = 0.0
        for token in m.tokens:
            if str(token.get("outcome", "")).lower() == "yes":
                own_yes = _to_float(token.get("price"))
                break
        if own_strike is not None and own_direction and own_yes >= self.MIN_PRICE:
            legs.append({
                "strike": own_strike,
                "direction": own_direction,
                "yes_price": own_yes,
                "question": m.question,
                "tokens": m.tokens,
                "is_self": True,
            })

        for sibling in m.event_siblings:
            text = f"{sibling.get('question') or ''} {sibling.get('subtitle') or ''}"
            strike = _extract_strike(text)
            direction = _direction(text)
            yes_price = _to_float(sibling.get("yes_price"))
            if strike is None or not direction or yes_price < self.MIN_PRICE:
                continue
            legs.append({
                "strike": strike,
                "direction": direction,
                "yes_price": yes_price,
                "question": sibling.get("question") or "",
                "tokens": sibling.get("tokens") or [],
                "is_self": False,
            })

        if len(legs) < 2:
            return None

        legs.sort(key=lambda leg: leg["strike"])
        return {"legs": legs}

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        opportunities: List[Opportunity] = []

        for m in markets:
            if not m.active:
                continue

            ladder = self._ladder(m)
            if not ladder:
                continue

            legs = ladder["legs"]
            self_leg = next((leg for leg in legs if leg["is_self"]), None)
            if not self_leg:
                continue

            for i in range(len(legs) - 1):
                low = legs[i]
                high = legs[i + 1]
                if low["direction"] != high["direction"]:
                    continue
                if low["strike"] == high["strike"]:
                    continue

                # "above": el umbral bajo debe ser >= al alto.
                # "below": el umbral bajo debe ser <= al alto.
                violator = None
                peer = None
                if low["direction"] == "up" and high["yes_price"] > low["yes_price"] + self.MIN_EDGE:
                    violator, peer = high, low
                elif low["direction"] == "down" and low["yes_price"] > high["yes_price"] + self.MIN_EDGE:
                    violator, peer = low, high

                if violator is None or not violator["is_self"]:
                    continue  # emite solo el mercado incoherente, una vez

                opportunities.append(Opportunity(
                    market_id=m.condition_id,
                    question=m.question,
                    market_price=violator["yes_price"],
                    category=m.category,
                    metadata={
                        "type": "ladder_inversion",
                        "strike": violator["strike"],
                        "strike_peer": peer["strike"],
                        "leg_price": violator["yes_price"],
                        "leg_peer_price": peer["yes_price"],
                        "leg_question": violator["question"],
                        "peer_question": peer["question"],
                        "direction": low["direction"],
                        "tokens": violator["tokens"],
                        "edge": round(abs(violator["yes_price"] - peer["yes_price"]), 4),
                    },
                ))
                break

        return opportunities

    def analyze(self, opportunity: Opportunity, **kwargs: object) -> Optional[Signal]:
        meta = opportunity.metadata
        entry_price = opportunity.market_price
        edge = _to_float(meta.get("edge"), 0.03)

        token_id = ""
        for token in meta.get("tokens") or []:
            if str(token.get("outcome", "")).lower() == "yes":
                token_id = str(token.get("token_id") or token.get("tokenId") or "")
                break
        if not token_id or entry_price <= 0:
            return None

        # La pata incoherente esta sobrevalorada: su valor razonable no puede
        # superar al de la pata con umbral mas facil.
        fair_price = min(0.97, max(0.02, _to_float(meta.get("leg_peer_price"), entry_price)))
        target_price = min(0.98, fair_price)

        reason = (
            f"Escalera de umbrales invertida: '{meta.get('leg_question')}' (umbral "
            f"{meta.get('strike')}) cotiza YES {entry_price:.3f} por encima de "
            f"'{meta.get('peer_question')}' (umbral {meta.get('strike_peer')}) a YES "
            f"{_to_float(meta.get('leg_peer_price')):.3f}. La relacion logica esta rota."
        )

        return Signal(
            market_id=opportunity.market_id,
            token_id=token_id,
            side="buy",
            estimated_prob=max(0.02, min(0.98, fair_price)),
            market_price=entry_price,
            confidence=0.90,
            strategy_name=self.name,
            metadata={
                "edge": edge,
                "arbitrage_type": "ladder_inversion",
                "recommendation": f"BUY NO @ {(1 - entry_price):.3f} (pata sobrevalorada de la escalera)",
                "trigger_reason": reason,
            },
        )
