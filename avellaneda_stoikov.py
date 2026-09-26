"""
avellaneda_stoikov.py
=====================
Motor Cuantitativo de Market Making Pasivo basado en el modelo institucional
de Avellaneda & Stoikov (2008) ("High-frequency trading in a limit order book")
adaptado a Mercados de Predicción (Polymarket CLOB).

Mecánica:
1. Cálculo analítico del Precio de Reserva (Reservation Price):
   r(s, q, t) = s - q * gamma * sigma^2 * (T - t)
   Donde s es el precio medio (o fair price), q es el inventario neto,
   gamma es la aversión al riesgo y sigma es la volatilidad.
2. Cálculo analítico del medio spread óptimo:
   delta* = (2 / gamma) * ln(1 + gamma / kappa)
   Bid = r - delta*/2, Ask = r + delta*/2
3. Protección Anti-Selección Adversa (Anti-Toxic Retreat):
   Si VPIN > 0.60 o la velocidad del Lead-Lag supera 0.25% en 10s,
   el motor cancela o ensancha inmediatamente las cotizaciones pasivas
   para evitar ser explotado por operadores informados o ballenas.
4. Gestión de inventario adaptada al portfolio de $1,000 USD.
"""

import math
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("avellaneda_stoikov")
logger.setLevel(logging.INFO)


@dataclass
class MMQuote:
    market_id: str
    symbol: str
    mid_price: float
    reservation_price: float
    optimal_bid: float
    optimal_ask: float
    half_spread: float
    inventory_q: int
    inventory_max: int
    is_active: bool
    status: str  # "QUOTING", "RETREAT_TOXIC", "MAX_INVENTORY_LONG", "MAX_INVENTORY_SHORT"
    timestamp: float


@dataclass
class MMTradeExecution:
    id: str
    market_id: str
    symbol: str
    side: str  # "BUY_BID" o "SELL_ASK"
    price: float
    contracts: int
    spread_captured: float
    timestamp: float


class AvellanedaStoikovEngine:
    def __init__(
        self,
        risk_aversion_gamma: float = 0.25,
        liquidity_kappa: float = 1.80,
        volatility_sigma: float = 0.45,
        time_horizon_hours: float = 4.0,
        max_inventory: int = 50,
        portfolio_capital: float = 1000.0,
    ):
        self.gamma = risk_aversion_gamma
        self.kappa = liquidity_kappa
        self.sigma = volatility_sigma
        self.T = time_horizon_hours / 24.0  # en fracción de día
        self.max_inventory = max_inventory
        self.portfolio_capital = portfolio_capital

        self.is_enabled = True
        self.inventory: Dict[str, int] = {}  # market_id -> q (positivo = net YES, negativo = net NO)
        self.quotes: Dict[str, MMQuote] = {}
        self.trade_history: List[MMTradeExecution] = []
        self.total_spread_pnl = 0.0
        self.total_volume_quoted = 0.0

    def compute_reservation_price(
        self,
        mid_price: float,
        inventory_q: int,
        time_left_fraction: Optional[float] = None,
    ) -> float:
        """
        Calcula el precio de reserva r(s, q, t) = s - q * gamma * sigma^2 * (T - t).
        Si el inventario q es positivo (largo), r disminuye para desincentivar compras y fomentar ventas.
        """
        tau = time_left_fraction if time_left_fraction is not None else self.T
        tau = max(0.01, tau)
        # s - q * gamma * sigma^2 * tau
        shift = inventory_q * self.gamma * (self.sigma ** 2) * tau
        r = mid_price - shift
        # Limitar dentro de [0.01, 0.99]
        return max(0.01, min(0.99, r))

    def compute_optimal_spread(self) -> float:
        """
        Calcula el spread óptimo asimétrico:
        delta* = (2 / gamma) * ln(1 + gamma / kappa)
        """
        try:
            arg = 1.0 + (self.gamma / self.kappa)
            delta = (2.0 / self.gamma) * math.log(arg)
            # Normalizar para contratos de predicción [0.02 a 0.12]
            return max(0.02, min(0.12, delta * 0.08))
        except Exception:
            return 0.04

    def quote_market(
        self,
        market_id: str,
        symbol: str,
        mid_price: float,
        vpin_toxicity: float = 0.20,
        crypto_velocity_10s: float = 0.0,
        time_left_hours: float = 4.0,
    ) -> MMQuote:
        """
        Genera las cotizaciones pasivas óptimas de Bid y Ask para un mercado.
        Aplica protección anti-selección adversa si VPIN o la velocidad de cripto son elevados.
        """
        now = time.time()
        tau = max(0.01, time_left_hours / 24.0)
        q = self.inventory.get(market_id, 0)

        # 1. Chequeo de Protección Anti-Selección Adversa (Toxic Retreat)
        is_toxic = (vpin_toxicity >= 0.60) or (abs(crypto_velocity_10s) >= 0.25)

        if not self.is_enabled:
            status = "DISABLED"
            is_active = False
            r_price = mid_price
            bid = round(max(0.01, mid_price - 0.05), 3)
            ask = round(min(0.99, mid_price + 0.05), 3)
            half_spread = 0.05
        elif is_toxic:
            # El mercado experimenta flujo agresivo de insiders o momentum violento:
            # RETREAT INMEDIATO: ensanchar el spread al triple y desactivar ejecución pasiva
            status = "RETREAT_TOXIC"
            is_active = False
            r_price = self.compute_reservation_price(mid_price, q, tau)
            half_spread = self.compute_optimal_spread() * 2.5
            bid = round(max(0.01, r_price - half_spread), 3)
            ask = round(min(0.99, r_price + half_spread), 3)
            logger.debug(f"🛡️ [MM RETREAT] {symbol} Protección activa: VPIN {vpin_toxicity*100:.1f}%, Vel {crypto_velocity_10s:.2f}%")
        else:
            r_price = self.compute_reservation_price(mid_price, q, tau)
            half_spread = self.compute_optimal_spread() / 2.0

            bid = round(max(0.01, r_price - half_spread), 3)
            ask = round(min(0.99, r_price + half_spread), 3)

            # Control de inventario en extremos
            if q >= self.max_inventory:
                status = "MAX_INVENTORY_LONG"
                bid = 0.01  # Deja de comprar
                is_active = True
            elif q <= -self.max_inventory:
                status = "MAX_INVENTORY_SHORT"
                ask = 0.99  # Deja de vender
                is_active = True
            else:
                status = "QUOTING"
                is_active = True

        quote = MMQuote(
            market_id=market_id,
            symbol=symbol,
            mid_price=round(mid_price, 3),
            reservation_price=round(r_price, 3),
            optimal_bid=bid,
            optimal_ask=ask,
            half_spread=round(half_spread, 3),
            inventory_q=q,
            inventory_max=self.max_inventory,
            is_active=is_active,
            status=status,
            timestamp=now,
        )
        self.quotes[market_id] = quote
        return quote

    def simulate_fill(
        self,
        market_id: str,
        side: str,  # "BUY_BID" o "SELL_ASK"
        contracts: int = 10,
    ) -> Optional[MMTradeExecution]:
        """
        Simula una ejecución pasiva completada en el Bid o Ask y ajusta el inventario y PnL.
        """
        quote = self.quotes.get(market_id)
        if not quote or not quote.is_active or quote.status == "RETREAT_TOXIC":
            return None

        current_q = self.inventory.get(market_id, 0)
        spread_captured = 0.0
        now = time.time()

        if side == "BUY_BID":
            fill_price = quote.optimal_bid
            self.inventory[market_id] = current_q + contracts
            # Ganancia estimada del half-spread
            spread_captured = (quote.mid_price - fill_price) * contracts
        else:
            fill_price = quote.optimal_ask
            self.inventory[market_id] = current_q - contracts
            spread_captured = (fill_price - quote.mid_price) * contracts

        self.total_spread_pnl += max(0.0, spread_captured)
        self.total_volume_quoted += fill_price * contracts

        exec_id = f"MM-{market_id}-{int(now * 1000)}"
        execution = MMTradeExecution(
            id=exec_id,
            market_id=market_id,
            symbol=quote.symbol,
            side=side,
            price=round(fill_price, 3),
            contracts=contracts,
            spread_captured=round(spread_captured, 3),
            timestamp=now,
        )
        self.trade_history.insert(0, execution)
        if len(self.trade_history) > 50:
            self.trade_history.pop()

        return execution

    def get_status(self) -> Dict[str, Any]:
        """Devuelve el estado completo del motor para el Dashboard."""
        active_quotes = [asdict(q) for q in self.quotes.values()]
        recent_trades = [asdict(t) for t in self.trade_history[:15]]
        total_inventory_exposure = sum(abs(v) for v in self.inventory.values())

        return {
            "is_enabled": self.is_enabled,
            "gamma_risk_aversion": self.gamma,
            "kappa_liquidity": self.kappa,
            "sigma_volatility": self.sigma,
            "max_inventory_limit": self.max_inventory,
            "portfolio_capital_base": self.portfolio_capital,
            "total_spread_pnl_usd": round(self.total_spread_pnl, 2),
            "total_volume_quoted_usd": round(self.total_volume_quoted, 2),
            "total_inventory_exposure": total_inventory_exposure,
            "active_market_quotes": active_quotes,
            "recent_mm_trades": recent_trades,
            "anti_toxic_protection": "ACTIVE (VPIN > 0.60 / Vel > 0.25%)",
        }


# Instancia singleton del motor Avellaneda-Stoikov
avellaneda_stoikov_engine = AvellanedaStoikovEngine()
