"""S05 - Arbitraje de canasta en eventos mutuamente excluyentes (Kalshi).

Adaptacion a Kalshi: aqui cada *mercado* es binario (YES/NO), pero un **evento**
`mutually_exclusive` agrupa N mercados que representan resultados posibles de un
mismo suceso (brackets de temperatura, candidatos de una nominacion, etc.).
La suma de los precios YES de todos los hijos deberia ser 1.00.

- Suma > 1 + umbral -> canasta sobrevalorada: comprar NO en el hijo mas caro.
- Suma < 1 - umbral -> canasta infravalorada: comprar YES en el hijo mas barato.

En Polymarket esto vivia dentro de un unico mercado con `outcomes` multiples; en
Kalshi se resuelve a nivel de evento.
"""
from typing import List, Optional

from core_models import BaseStrategy, Market, Opportunity, Signal


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _token_id(token: dict) -> str:
    return str(token.get("token_id") or token.get("tokenId") or "")


class NegRiskRebalancing(BaseStrategy):
    name = "s05_negrisk_rebalancing"
    tier = "S"
    strategy_id = 5
    required_data = ["event_siblings"]

    MIN_OUTCOMES = 3        # la canasta necesita al menos 3 resultados posibles
    MIN_OVERPRICE = 0.02    # suma > 1.02 -> arbitraje de venta
    MIN_UNDERPRICE = 0.02   # suma < 0.98 -> arbitraje de compra
    MIN_COMPONENT_PRICE = 0.02

    @staticmethod
    def _yes_price(market: Market) -> float:
        for token in market.tokens:
            if str(token.get("outcome", "")).lower() == "yes":
                return _to_float(token.get("price"))
        return 0.0

    def _basket(self, m: Market) -> Optional[dict]:
        """Construir la canasta del evento si el mercado pertenece a uno exclusivo."""
        if not m.event_siblings or not getattr(m, "mutually_exclusive", False):
            return None

        components: List[dict] = []
        own_yes = self._yes_price(m)
        if own_yes > 0:
            components.append({
                "market_id": m.condition_id,
                "question": m.question,
                "subtitle": "",
                "yes_price": own_yes,
                "tokens": m.tokens,
                "is_self": True,
            })

        for sibling in m.event_siblings:
            price = _to_float(sibling.get("yes_price"))
            if price <= 0:
                continue
            components.append({
                "market_id": sibling.get("market_id") or sibling.get("condition_id") or "",
                "question": sibling.get("question") or "",
                "subtitle": sibling.get("subtitle") or "",
                "yes_price": price,
                "tokens": sibling.get("tokens") or [],
                "is_self": False,
            })

        if len(components) < self.MIN_OUTCOMES:
            return None

        priced = [c for c in components if c["yes_price"] >= self.MIN_COMPONENT_PRICE]
        if len(priced) < self.MIN_OUTCOMES:
            return None

        total = sum(c["yes_price"] for c in priced)
        return {"components": priced, "total": total}

    def scan(self, markets: List[Market]) -> List[Opportunity]:
        opportunities: List[Opportunity] = []
        for m in markets:
            if not m.active:
                continue

            basket = self._basket(m)
            if not basket:
                continue

            components = basket["components"]
            total = basket["total"]
            self_component = next((c for c in components if c["is_self"]), None)
            if not self_component:
                continue

            most_expensive = max(components, key=lambda c: c["yes_price"])
            cheapest = min(components, key=lambda c: c["yes_price"])

            # Un unico mercado del evento emite la senal para no duplicar el mismo
            # arbitraje una vez por cada hijo.
            if total > 1.0 + self.MIN_OVERPRICE and most_expensive["is_self"]:
                opportunities.append(Opportunity(
                    market_id=self_component["market_id"],
                    question=self_component["question"],
                    market_price=total,
                    category=m.category,
                    metadata={
                        "direction": "OVERPRICED",
                        "components": components,
                        "total_yes": total,
                        "overprice": total - 1.0,
                        "target_tokens": most_expensive["tokens"],
                        "target_outcome": "No",
                        "target_yes_price": most_expensive["yes_price"],
                    },
                ))
            elif total < 1.0 - self.MIN_UNDERPRICE and cheapest["is_self"]:
                opportunities.append(Opportunity(
                    market_id=self_component["market_id"],
                    question=self_component["question"],
                    market_price=total,
                    category=m.category,
                    metadata={
                        "direction": "UNDERPRICED",
                        "components": components,
                        "total_yes": total,
                        "underprice": 1.0 - total,
                        "target_tokens": cheapest["tokens"],
                        "target_outcome": "Yes",
                        "target_yes_price": cheapest["yes_price"],
                    },
                ))
        return opportunities

    def analyze(self, opportunity: Opportunity, **kwargs: object) -> Optional[Signal]:
        meta = opportunity.metadata
        components = meta.get("components", [])
        if len(components) < self.MIN_OUTCOMES:
            return None

        direction = meta.get("direction")
        total = _to_float(meta.get("total_yes"))
        target_outcome = str(meta.get("target_outcome") or "No")
        target_yes_price = _to_float(meta.get("target_yes_price"))

        token_id = ""
        target_price = 0.0
        for token in meta.get("target_tokens") or []:
            if str(token.get("outcome", "")).lower() == target_outcome.lower():
                token_id = _token_id(token)
                target_price = _to_float(token.get("price"))
                break
        if not token_id or target_price <= 0:
            return None

        if direction == "OVERPRICED":
            overprice = _to_float(meta.get("overprice"))
            estimated = min(0.98, target_price + overprice)
            confidence = 0.90
            reason = (
                f"Canasta del evento sumo {total:.4f} (> 1.00): {overprice * 100:.2f}% de "
                f"sobreprecio repartido entre {len(components)} resultados. Se compra NO en el "
                f"hijo mas caro (YES {target_yes_price:.3f}) buscando el rebalanceo."
            )
        elif direction == "UNDERPRICED":
            underprice = _to_float(meta.get("underprice"))
            estimated = min(0.98, target_price + underprice)
            confidence = 0.88
            reason = (
                f"Canasta del evento sumo solo {total:.4f} (< 1.00): {underprice * 100:.2f}% de "
                f"descuento entre {len(components)} resultados. Se compra YES en el hijo mas "
                f"barato (YES {target_yes_price:.3f})."
            )
        else:
            return None

        edge = estimated - target_price
        if edge < 0.015:
            return None

        return Signal(
            market_id=opportunity.market_id,
            token_id=token_id,
            side="buy",
            estimated_prob=estimated,
            market_price=target_price,
            confidence=confidence,
            strategy_name=self.name,
            metadata={
                "edge": round(edge, 4),
                "basket_total": round(total, 4),
                "basket_size": len(components),
                "direction": direction,
                "recommendation": f"BUY {target_outcome} @ {target_price:.3f} (rebalanceo de canasta)",
                "trigger_reason": reason,
            },
        )
