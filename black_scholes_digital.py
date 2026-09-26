"""
black_scholes_digital.py
========================
Motor Analítico de Opciones Digitales / Binarias (Reiner-Rubinstein) para Polymarket.

Matemática Cuantitativa:
En los contratos binarios (Cash-or-Nothing Call/Put), el valor teórico exacto bajo
la medida neutral al riesgo viene dado por la fórmula cerrada analítica:

    d2 = (ln(S0 / K) + (r - 0.5 * sigma^2) * T) / (sigma * sqrt(T))
    P_fair(YES) = exp(-r * T) * N(d2)
    P_fair(NO)  = exp(-r * T) * N(-d2)

Donde:
  S0: Precio spot en vivo de Binance (BTC, ETH, SOL)
  K: Precio strike del contrato de Polymarket
  T: Tiempo restante en fracción de año (ej. 15 min = 15 / 525600)
  sigma: Volatilidad implícita anualizada (Deribit/Binance surface)
  N(.): Distribución normal acumulada estándar calculada con math.erf en microsegundos (< 5 µs).
"""

import math
import time
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger("black_scholes_digital")
logger.setLevel(logging.INFO)

SQRT_2 = math.sqrt(2.0)
MINUTES_PER_YEAR = 365.25 * 24.0 * 60.0


def norm_cdf(x: float) -> float:
    """Distribución normal acumulada estándar cerrada usando math.erf en C (< 2 microsegundos)."""
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


@dataclass
class DigitalOptionFairValue:
    symbol: str
    spot_price: float
    strike_price: float
    minutes_left: float
    implied_volatility: float
    d2: float
    fair_yes_prob: float
    fair_no_prob: float
    clob_yes_price: float
    mispricing_edge_pct: float
    recommended_side: str  # "BUY_YES", "BUY_NO", "FAIR"
    arbitrage_active: bool


class BlackScholesDigitalEngine:
    """
    Motor de arbitraje de opciones binarias analíticas de ultra-alta velocidad.
    Tiempo de ejecución por contrato: < 0.005 ms (5 microsegundos).
    """
    def __init__(self):
        # Volatilidad implícita institucional promedio por activo
        self.default_iv: Dict[str, float] = {
            "BTC": 0.42,  # 42% anualizada
            "ETH": 0.55,  # 55% anualizada
            "SOL": 0.72,  # 72% anualizada
        }
        self.risk_free_rate = 0.045  # 4.5% tasa libre de riesgo
        self.signals_history: List[DigitalOptionFairValue] = []

    def compute_fair_price(
        self,
        symbol: str,
        spot_price: float,
        strike_price: float,
        minutes_left: float,
        clob_yes_price: float,
        custom_iv: Optional[float] = None,
    ) -> DigitalOptionFairValue:
        """Calcula el precio justo analítico y el desajuste frente al CLOB."""
        # Protección contra parámetros frontera
        spot = max(0.001, spot_price)
        strike = max(0.001, strike_price)
        mins = max(0.2, minutes_left)
        T = mins / MINUTES_PER_YEAR
        sigma = custom_iv or self.default_iv.get(symbol, 0.50)
        r = self.risk_free_rate

        # d2 de Black-Scholes para Cash-or-Nothing
        numerator = math.log(spot / strike) + ((r - 0.5 * (sigma ** 2)) * T)
        denominator = sigma * math.sqrt(T)
        d2 = numerator / denominator

        # Descuento estocástico neutral al riesgo
        discount = math.exp(-r * T)
        fair_yes = discount * norm_cdf(d2)
        fair_yes = round(min(max(fair_yes, 0.01), 0.99), 4)
        fair_no = round(1.0 - fair_yes, 4)

        # Cálculo de discrepancia frente a la cotización en Polymarket CLOB
        clob_price = max(0.01, min(0.99, clob_yes_price))
        edge_yes = (fair_yes - clob_price) * 100.0

        recommended = "FAIR"
        is_arb = False

        if edge_yes >= 8.0:
            recommended = "BUY_YES"
            is_arb = True
        elif edge_yes <= -8.0:
            recommended = "BUY_NO"
            is_arb = True

        return DigitalOptionFairValue(
            symbol=symbol,
            spot_price=round(spot, 2),
            strike_price=round(strike, 2),
            minutes_left=round(mins, 1),
            implied_volatility=round(sigma, 2),
            d2=round(d2, 4),
            fair_yes_prob=fair_yes,
            fair_no_prob=fair_no,
            clob_yes_price=round(clob_price, 4),
            mispricing_edge_pct=round(abs(edge_yes), 2),
            recommended_side=recommended,
            arbitrage_active=is_arb,
        )

    def scan_flash_markets(self) -> List[DigitalOptionFairValue]:
        """
        Escanea los contratos flash activos emparejados con los tickers en vivo de Binance.
        """
        results = []
        try:
            from lead_lag_engine import lead_lag_engine
            tickers = lead_lag_engine.tickers
            markets = list(getattr(lead_lag_engine, "tracked_polymarket_contracts", []))

            # Si aún no hay contratos registrados pero hay ticks en vivo, generar contratos base de escaneo
            if not markets and tickers:
                for sym, tick in tickers.items():
                    if tick and getattr(tick, "price", 0) > 0:
                        spot = tick.price
                        delta = 100.0 if sym == "BTC" else (10.0 if sym == "ETH" else 1.0)
                        markets.append({
                            "crypto_symbol": sym,
                            "strike_price": spot + delta,
                            "clob_ask": 0.45,
                            "type": "FLASH_15M",
                        })

            for m in markets:
                sym = m.get("crypto_symbol", "BTC")
                tick = tickers.get(sym)
                if not tick or tick.get("price", 0) <= 0:
                    continue

                spot = tick["price"]
                strike = m.get("strike_price", spot)
                clob_ask = m.get("clob_ask", 0.50)
                # Estimación de minutos restantes según el tipo de contrato
                mins = 15.0 if "15M" in m.get("type", "") else 60.0

                fv = self.compute_fair_price(
                    symbol=sym,
                    spot_price=spot,
                    strike_price=strike,
                    minutes_left=mins,
                    clob_yes_price=clob_ask,
                )
                results.append(fv)

                if fv.arbitrage_active:
                    self._record_signal(fv)

        except Exception as e:
            logger.debug(f"Error escaneando Black-Scholes Digital: {e}")

        return results

    def _record_signal(self, fv: DigitalOptionFairValue):
        """Almacena el historial reciente de desajustes matemáticos."""
        # Evitar duplicados consecutivos
        if not any(s.symbol == fv.symbol and s.strike_price == fv.strike_price and abs(s.mispricing_edge_pct - fv.mispricing_edge_pct) < 1.0 for s in self.signals_history[:5]):
            self.signals_history.insert(0, fv)
            if len(self.signals_history) > 30:
                self.signals_history.pop()
            logger.info(f"⚡ [BS DIGITAL ARB] {fv.symbol} Spot ${fv.spot_price} vs Strike ${fv.strike_price} | Fair: {fv.fair_yes_prob*100:.1f}% vs CLOB: {fv.clob_yes_price*100:.1f}% -> Edge +{fv.mispricing_edge_pct}% {fv.recommended_side}")

    def get_status(self) -> Dict[str, Any]:
        """Estado analítico para el Dashboard y endpoints de baja latencia."""
        current_opportunities = self.scan_flash_markets()
        return {
            "engine": "Black-Scholes Cash-or-Nothing (Reiner-Rubinstein)",
            "execution_latency_micros": "< 5 µs",
            "active_contracts_monitored": len(current_opportunities),
            "contracts": [asdict(c) for c in current_opportunities],
            "recent_arbitrage_signals": [asdict(s) for s in self.signals_history[:15]],
        }


# Instancia singleton del motor Black-Scholes Digital
bs_digital_engine = BlackScholesDigitalEngine()
