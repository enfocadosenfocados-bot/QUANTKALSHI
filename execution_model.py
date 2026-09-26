"""execution_model.py — Modelo de ejecución realista para el paper trading.

Este módulo existe porque es la pieza que decide si una estrategia que gana en
simulación sigue ganando en real. El modelo anterior regalaba dinero por tres
vías a la vez:

  1. Comisión plana en cero (`PAPER_FEE_RATE = 0.0`). Kalshi cobra al taker
     ceil_a_centavo(rate * contratos * P * (1-P)) y ese redondeo a centavo no
     tiene piso: a 0.03 la comisión es 1 centavo, el 33% de la apuesta, y sube
     el breakeven de 3.0% a 4.0%.
  2. Slippage en puntos básicos. Con un tick de 1 centavo, 5 bps sobre 0.0350
     son 0.0000175: nada. El siguiente precio legal es 0.0400, un +14%.
  3. Fill maker instantáneo al toque. Un maker está al final de la cola y solo
     llena si el mercado viene a él.

Sustituye esos tres atajos por: precio ajustado al tick legal, consumo real de
niveles del libro con tope de profundidad, comisión por tramo maker/taker,
tamaño mínimo de orden y una latencia explícita entre la señal y la orden.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import (
    PAPER_DEPTH_SAFETY,
    PAPER_FILL_MODEL,
    PAPER_LATENCY_MS,
    PAPER_MAKER_FILL_PROBABILITY,
)
from fee_calibration import effective_fee_config
from position_side import is_long_side

CENT = 0.01
# Tamaño asumido cuando el libro llega vacío y solo hay quotes del market object.
# Se marca en el resultado (`book_synthetic`) para que el trade sea auditable.
SYNTHETIC_LEVEL_SIZE = 50.0
DEFAULT_TICK = 0.01

# ========== Comisión vigente ==========
# No son constantes de config: son el resultado de resolver la comisión MEDIDA en
# demo contra la documentada (ver `fee_calibration.py`). Se exponen como globales
# mutables y se refrescan con `reload_fee_calibration()` para poder recalibrar sin
# reiniciar el bot.
FEE_ROUNDING_STEPS: Dict[str, float] = {"cent": CENT, "micro": 0.0001}
FEE_TAKER_RATE: float = 0.07
FEE_MAKER_RATE: float = 0.0
FEE_ROUNDING_MODE: str = "cent"
FEE_ROUNDING_STEP: float = CENT
FEE_SOURCE: str = "documented"
FEE_NOTES: List[str] = []
FEE_CALIBRATION: Dict[str, Any] = {}


def _apply_fee_config(resolved: Dict[str, Any]) -> None:
    """Fija la comisión vigente a partir de la calibración ya resuelta."""
    global FEE_TAKER_RATE, FEE_MAKER_RATE, FEE_ROUNDING_MODE
    global FEE_ROUNDING_STEP, FEE_SOURCE, FEE_NOTES, FEE_CALIBRATION
    FEE_TAKER_RATE = float(resolved.get("taker_rate", FEE_TAKER_RATE))
    FEE_MAKER_RATE = float(resolved.get("maker_rate", FEE_MAKER_RATE))
    FEE_ROUNDING_MODE = str(resolved.get("rounding") or FEE_ROUNDING_MODE)
    FEE_ROUNDING_STEP = float(resolved.get("rounding_step") or FEE_ROUNDING_STEP)
    FEE_SOURCE = str(resolved.get("source") or FEE_SOURCE)
    FEE_NOTES = list(resolved.get("notes") or [])
    FEE_CALIBRATION = dict(resolved.get("calibration") or {})


def reload_fee_calibration(path: Any = None) -> Dict[str, Any]:
    """Relee `fee_calibration.json` y devuelve el estado del modelo de comisión."""
    _apply_fee_config(effective_fee_config(path))
    return fee_model_status()


def fee_model_status() -> Dict[str, Any]:
    return {
        "source": FEE_SOURCE,
        "taker_rate": FEE_TAKER_RATE,
        "maker_rate": FEE_MAKER_RATE,
        "rounding": FEE_ROUNDING_MODE,
        "rounding_step": FEE_ROUNDING_STEP,
        "measured_at": FEE_CALIBRATION.get("generated_at"),
        "notes": FEE_NOTES,
    }


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def ceil_to_step(value: float, step: float) -> float:
    """Redondeo hacia arriba al múltiplo de `step`, que es como cobra Kalshi.

    Se resta un épsilon antes del `ceil` para que un valor ya exacto (0.07 con
    step 0.01) no suba un escalón por error de coma flotante.
    """
    if value <= 0:
        return 0.0
    if step <= 0:
        return float(value)
    return math.ceil(value / step - 1e-9) * step


def ceil_to_cent(value: float) -> float:
    """Compatibilidad: redondeo al centavo (la precisión documentada)."""
    return round(ceil_to_step(value, CENT), 10)


def kalshi_fee_raw(contracts: float, price: float, rate: float) -> float:
    """Comisión SIN redondear: `rate * contratos * P * (1-P)`."""
    if contracts <= 0 or price <= 0 or price >= 1 or rate <= 0:
        return 0.0
    return rate * contracts * price * (1.0 - price)


def kalshi_order_fee(
    fills: Any,
    rate: Optional[float] = None,
    rounding: Optional[str] = None,
) -> float:
    """Comisión de UNA orden: acumula todos sus fills y redondea UNA vez.

    Kalshi cobra el pedido, no cada fill: lo que se acumula es
    `contratos * P * (1-P)` y el `ceil` se aplica al final. Redondear por fill
    cobra de más cuando la orden se llena en varias partes, y con precisión de
    centavo el error llega a ser de un centavo (varios puntos porcentuales del
    nocional) en cada fill adicional.
    """
    effective_rate = FEE_TAKER_RATE if rate is None else float(rate)
    mode = (rounding or FEE_ROUNDING_MODE).strip().lower()
    step = (
        FEE_ROUNDING_STEP
        if mode == FEE_ROUNDING_MODE
        else FEE_ROUNDING_STEPS.get(mode, CENT)
    )
    accumulated = 0.0
    for fill in fills or []:
        try:
            contracts, price = float(fill[0]), float(fill[1])
        except (TypeError, ValueError, IndexError):
            continue
        accumulated += kalshi_fee_raw(contracts, price, effective_rate)
    return round(ceil_to_step(accumulated, step), 10)


def kalshi_trading_fee(
    contracts: float,
    price: float,
    rate: Optional[float] = None,
    rounding: Optional[str] = None,
) -> float:
    """Comisión de un único fill. Para una orden con varios, `kalshi_order_fee`.

    Sin `rate`/`rounding` explícitos usa la comisión VIGENTE, que sale de
    `fee_calibration.py`: si el fichero medido en demo dice otra precisión u otra
    tasa, manda el dato medido. El `ceil` es lo que hace que los contratos
    baratos sean carísimos de operar: a 0.03 la comisión no baja de un centavo y
    supone el 33% de la apuesta.
    """
    return kalshi_order_fee([(contracts, price)], rate=rate, rounding=rounding)


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    """Ajusta un precio al tick legal del mercado.

    `up` al comprar (nunca pagar menos de lo legal), `down` al vender, `nearest`
    para referencias. Acotado al rango operable [0.01, 0.99].
    """
    step = tick if tick and tick > 0 else DEFAULT_TICK
    steps = price / step
    if direction == "up":
        steps = math.ceil(steps - 1e-9)
    elif direction == "down":
        steps = math.floor(steps + 1e-9)
    else:
        steps = round(steps)
    return round(min(0.99, max(CENT, steps * step)), 4)


@dataclass
class FillResult:
    """Resultado de intentar ejecutar una orden contra el libro."""

    filled: bool
    contracts: float = 0.0
    avg_price: float = 0.0
    fee_usd: float = 0.0
    notional_usd: float = 0.0
    slippage_ticks: float = 0.0
    is_maker: bool = False
    partial: bool = False
    reason: str = ""
    levels_consumed: int = 0
    depth_available: float = 0.0
    book_synthetic: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filled": self.filled,
            "contracts": round(self.contracts, 2),
            "avg_price": round(self.avg_price, 4),
            "fee_usd": round(self.fee_usd, 4),
            "notional_usd": round(self.notional_usd, 2),
            "slippage_ticks": round(self.slippage_ticks, 2),
            "is_maker": self.is_maker,
            "partial": self.partial,
            "reason": self.reason,
            "levels_consumed": self.levels_consumed,
            "depth_available": round(self.depth_available, 2),
            "book_synthetic": self.book_synthetic,
        }


def normalize_levels(
    raw_levels: Any, descending: bool = True
) -> List[Tuple[float, float]]:
    """Normaliza niveles del libro a [(precio, tamaño)] ordenados por agresividad."""
    out: List[Tuple[float, float]] = []
    for row in raw_levels or []:
        if isinstance(row, dict):
            price = row.get("price", row.get("price_dollars"))
            size = row.get("size", row.get("count", row.get("count_fp")))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, size = row[0], row[1]
        else:
            continue
        p, s = to_float(price), to_float(size)
        if p > 0 and s > 0:
            out.append((p, s))
    out.sort(key=lambda item: item[0], reverse=descending)
    return out


def _attr(market: Any, name: str, default: Any = None) -> Any:
    """Lee un atributo del mercado, que puede llegar como objeto o como dict."""
    if isinstance(market, dict):
        return market.get(name, default)
    return getattr(market, name, default)


def book_levels(
    market: Any, token: str
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]], bool]:
    """Niveles (bids, asks) para un outcome, con respaldo a las quotes del mercado.

    Devuelve también si el libro es sintético: el bot recibe quotes en el market
    object y a veces no tiene el libro completo, así que en vez de no operar
    nunca se usa un nivel único derivado de best_bid/best_ask y se marca el trade
    para poder auditarlo.
    """
    book = (_attr(market, "order_book", {}) or {}).get(token) or {}
    bids = normalize_levels(book.get("bids"), descending=True)
    asks = normalize_levels(book.get("asks"), descending=False)
    synthetic = False

    def _quote(name: str) -> float:
        return to_float((_attr(market, name, {}) or {}).get(token), 0.0)

    if not bids:
        best_bid = _quote("best_bid")
        if best_bid > 0:
            bids = [(best_bid, SYNTHETIC_LEVEL_SIZE)]
            synthetic = True
    if not asks:
        best_ask = _quote("best_ask")
        if best_ask > 0:
            asks = [(best_ask, SYNTHETIC_LEVEL_SIZE)]
            synthetic = True
    return bids, asks, synthetic


def simulate_taker_fill(
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    side: str,
    limit_price: float,
    contracts_requested: float,
    tick: float = DEFAULT_TICK,
    depth_safety: float = PAPER_DEPTH_SAFETY,
    min_order_size: float = 1.0,
    book_synthetic: bool = False,
    reason_prefix: str = "",
) -> FillResult:
    """Consume niveles del libro hasta llenar la orden o agotar el precio límite.

    Un BUY levanta asks; un SELL cruza bids. El tamaño se limita por la
    profundidad disponible (con margen de seguridad, porque no todo lo mostrado
    es ejecutable en nuestra dirección).
    """
    is_buy = is_long_side(side)
    wanted = float(contracts_requested)
    levels = asks if is_buy else bids
    total_depth = sum(size for _, size in levels)

    if wanted < min_order_size:
        return FillResult(
            filled=False,
            reason=f"{reason_prefix}tamaño {wanted} por debajo del mínimo {min_order_size}",
            depth_available=total_depth,
            book_synthetic=book_synthetic,
        )
    if not levels:
        return FillResult(
            filled=False,
            reason=f"{reason_prefix}libro vacío en el lado {'ask' if is_buy else 'bid'}",
            depth_available=0.0,
            book_synthetic=book_synthetic,
        )

    best_price = levels[0][0]
    filled = 0.0
    spent = 0.0
    consumed = 0
    # Tramos que consume ESTA orden. La comisión se acumula sobre todos ellos y se
    # redondea una sola vez al final, que es exactamente como la cobra Kalshi.
    legs: List[Tuple[float, float]] = []
    for price, size in levels:
        if is_buy and price > limit_price + 1e-9:
            break
        if not is_buy and price < limit_price - 1e-9:
            break
        take = min(size * depth_safety, wanted - filled)
        if take <= 1e-9:
            break
        filled += take
        spent += take * price
        legs.append((take, price))
        consumed += 1
        if filled >= wanted - 1e-9:
            break

    if filled <= 0:
        return FillResult(
            filled=False,
            reason=(
                f"{reason_prefix}sin liquidez dentro del límite (mejor "
                f"{'ask' if is_buy else 'bid'} {best_price:.4f} vs límite {limit_price:.4f})"
            ),
            depth_available=total_depth,
            book_synthetic=book_synthetic,
        )

    avg_price = spent / filled
    slippage_ticks = (
        (avg_price - limit_price) / tick if is_buy else (limit_price - avg_price) / tick
    )
    fee = kalshi_order_fee(legs, rate=FEE_TAKER_RATE)
    return FillResult(
        filled=True,
        contracts=filled,
        avg_price=round(avg_price, 4),
        fee_usd=fee,
        notional_usd=filled * avg_price,
        slippage_ticks=max(0.0, slippage_ticks),
        is_maker=False,
        partial=filled < wanted - 1e-9,
        reason=f"{reason_prefix}fill taker en {consumed} nivel(es)",
        levels_consumed=consumed,
        depth_available=total_depth,
        book_synthetic=book_synthetic,
    )


def simulate_maker_fill(
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    side: str,
    limit_price: float,
    contracts_requested: float,
    tick: float = DEFAULT_TICK,
    depth_safety: float = PAPER_DEPTH_SAFETY,
    min_order_size: float = 1.0,
    fill_probability: float = PAPER_MAKER_FILL_PROBABILITY,
    book_synthetic: bool = False,
    rng: Optional[random.Random] = None,
    reason_prefix: str = "",
    fee_rate: Optional[float] = None,
) -> FillResult:
    """Orden pasiva: solo llena si el libro cruza su nivel, y con probabilidad.

    Es la corrección más importante del modelo. Antes, toda señal etiquetada
    "LIMIT (Maker)" se llenaba al instante al toque, lo que infla justo a la
    estrategia que más órdenes pasivas genera (market making). Aquí un maker
    situado dentro del spread casi nunca llena en el mismo ciclo.

    La comisión del maker se cobra con la tasa VIGENTE
    (`fee_calibration.FEE_MAKER_RATE`). El fee schedule de Kalshi aplica el
    acumulador de comisiones tanto a maker como a taker, así que cobrar cero es un
    supuesto pendiente de medir, no un hecho: `fee_rate` permite fijarlo de forma
    explícita en tests y en la sonda de calibración.
    """
    is_buy = is_long_side(side)
    wanted = float(contracts_requested)
    levels = asks if is_buy else bids
    total_depth = sum(size for _, size in levels)

    if wanted < min_order_size:
        return FillResult(
            filled=False,
            reason=f"{reason_prefix}tamaño {wanted} por debajo del mínimo {min_order_size}",
            depth_available=total_depth,
            book_synthetic=book_synthetic,
        )
    if not levels:
        return FillResult(
            filled=False,
            reason=f"{reason_prefix}libro vacío para evaluar el fill pasivo",
            depth_available=0.0,
            book_synthetic=book_synthetic,
        )

    rng = rng or random
    best_opposite = levels[0][0]
    # El libro cruza nuestro nivel: alguien está dispuesto a pagar lo que pedimos.
    crossed = (is_buy and best_opposite <= limit_price + 1e-9) or (
        not is_buy and best_opposite >= limit_price - 1e-9
    )
    if not crossed and rng.random() > fill_probability:
        return FillResult(
            filled=False,
            reason=(
                f"{reason_prefix}maker sin fill: el mejor "
                f"{'ask' if is_buy else 'bid'} ({best_opposite:.4f}) no alcanza "
                f"el límite {limit_price:.4f}"
            ),
            depth_available=total_depth,
            is_maker=True,
            book_synthetic=book_synthetic,
        )

    available = total_depth * depth_safety
    filled = min(wanted, available)
    if filled <= 0:
        return FillResult(
            filled=False,
            reason=f"{reason_prefix}maker sin profundidad disponible",
            is_maker=True,
            depth_available=total_depth,
            book_synthetic=book_synthetic,
        )

    price = round_to_tick(limit_price, tick, "up" if is_buy else "down")
    fee = kalshi_order_fee(
        [(filled, price)], rate=FEE_MAKER_RATE if fee_rate is None else fee_rate
    )
    return FillResult(
        filled=True,
        contracts=filled,
        avg_price=price,
        fee_usd=fee,
        notional_usd=filled * price,
        slippage_ticks=0.0,
        is_maker=True,
        partial=filled < wanted - 1e-9,
        reason=f"{reason_prefix}fill maker {'por cruce' if crossed else 'probabilístico'}",
        levels_consumed=1,
        depth_available=total_depth,
        book_synthetic=book_synthetic,
    )


class ExecutionModel:
    """Punto de entrada del modelo de ejecución del paper trading.

    `model="tick"`   -> fill realista: tick legal, profundidad, cola maker,
                        comisión maker/taker y tamaño mínimo (por defecto).
    `model="legacy"` -> comportamiento anterior (fill instantáneo, comisión 0).
                        Se conserva solo para poder comparar resultados, nunca
                        para decidir qué estrategia pasa a live.

    Sobre la latencia: no se duerme el pipeline. La señal trae un precio, y el
    fill se evalúa contra el libro **actual**, no contra el snapshot de la señal.
    Si el mercado se movió, la orden límite ya no cruza y no llena: ese es
    exactamente el efecto que la latencia produce en real, y es lo que hoy hace
    que las estrategias de latencia parezcan rentables sin serlo.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        depth_safety: Optional[float] = None,
        maker_fill_probability: Optional[float] = None,
        latency_ms: Optional[int] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.model = (model or PAPER_FILL_MODEL or "tick").strip().lower()
        self.depth_safety = PAPER_DEPTH_SAFETY if depth_safety is None else depth_safety
        self.maker_fill_probability = (
            PAPER_MAKER_FILL_PROBABILITY
            if maker_fill_probability is None
            else maker_fill_probability
        )
        self.latency_ms = PAPER_LATENCY_MS if latency_ms is None else int(latency_ms)
        # RNG inyectable para que los tests sean deterministas.
        self._rng = rng or random.Random()

    @property
    def is_realistic(self) -> bool:
        return self.model != "legacy"

    def fee(self, contracts: float, price: float, is_maker: bool = False) -> float:
        """Comisión de un único tramo, con la tasa vigente (medida o documentada)."""
        rate = FEE_MAKER_RATE if is_maker else FEE_TAKER_RATE
        return kalshi_trading_fee(contracts, price, rate)

    def entry_fill(
        self,
        market: Any,
        token: str,
        side: str,
        limit_price: float,
        contracts: float,
        is_maker: bool = True,
    ) -> FillResult:
        """Fill de entrada. Por defecto pasivo, como recomiendan las señales."""
        return self._fill(market, token, side, limit_price, contracts, is_maker, "entrada: ")

    def exit_fill(
        self,
        market: Any,
        token: str,
        side: str,
        limit_price: float,
        contracts: float,
        is_maker: bool = False,
    ) -> FillResult:
        """Fill de salida. Por defecto agresivo: hay que salir del riesgo.

        Además, el objetivo de un trade no se alcanza cuando lo toca el precio
        medio sino cuando lo toca el lado que cruzarías, así que la salida se
        evalúa contra el libro pidiendo liquidez real.
        """
        return self._fill(market, token, side, limit_price, contracts, is_maker, "salida: ")

    def _fill(
        self,
        market: Any,
        token: str,
        side: str,
        limit_price: float,
        contracts: float,
        is_maker: bool,
        reason_prefix: str,
    ) -> FillResult:
        if not self.is_realistic:
            return self._legacy_fill(side, limit_price, contracts)
        bids, asks, synthetic = book_levels(market, token)
        tick = to_float(_attr(market, "tick_size", DEFAULT_TICK), DEFAULT_TICK) or DEFAULT_TICK
        min_size = to_float(_attr(market, "min_order_size", 0), 0.0) or 1.0
        if is_maker:
            return simulate_maker_fill(
                bids,
                asks,
                side,
                limit_price,
                contracts,
                tick=tick,
                depth_safety=self.depth_safety,
                min_order_size=min_size,
                fill_probability=self.maker_fill_probability,
                book_synthetic=synthetic,
                rng=self._rng,
                reason_prefix=reason_prefix,
            )
        return simulate_taker_fill(
            bids,
            asks,
            side,
            limit_price,
            contracts,
            tick=tick,
            depth_safety=self.depth_safety,
            min_order_size=min_size,
            book_synthetic=synthetic,
            reason_prefix=reason_prefix,
        )

    def entry_fill_with_budget(
        self,
        market: Any,
        token: str,
        side: str,
        limit_price: float,
        budget_usd: float,
        is_maker: bool = True,
    ) -> FillResult:
        """Llena respetando el presupuesto: precio * contratos + comisión <= budget.

        La comisión es escalonada (`ceil` a centavo), así que no basta con dividir
        el presupuesto por el precio: se ajusta el tamaño de forma iterativa hasta
        que el coste total entra en el presupuesto.
        """
        budget = float(budget_usd)
        price = max(float(limit_price), CENT)
        if budget <= 0:
            return FillResult(filled=False, reason="presupuesto agotado")

        contracts = budget / price
        fill = self.entry_fill(market, token, side, limit_price, contracts, is_maker)
        for _ in range(4):
            if not fill.filled:
                return fill
            cost = fill.contracts * fill.avg_price + fill.fee_usd
            if cost <= budget + 1e-9:
                return fill
            reduction = (cost - budget) / max(fill.avg_price, CENT) * 1.05
            contracts = fill.contracts - reduction
            if contracts < 1.0:
                return FillResult(
                    filled=False,
                    reason="presupuesto insuficiente una vez descontada la comisión",
                )
            fill = self.entry_fill(market, token, side, limit_price, contracts, is_maker)
        return fill

    def _legacy_fill(self, side: str, limit_price: float, contracts: float) -> FillResult:
        """Modelo anterior, conservado solo para comparar resultados."""
        price = max(CENT, min(0.99, float(limit_price)))
        return FillResult(
            filled=contracts > 0,
            contracts=contracts,
            avg_price=round(price, 4),
            fee_usd=0.0,
            notional_usd=contracts * price,
            slippage_ticks=0.0,
            is_maker=False,
            partial=False,
            reason="modelo legacy: fill instantáneo sin comisión ni profundidad",
        )

    def status(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "realistic": self.is_realistic,
            "fee_taker_rate": FEE_TAKER_RATE,
            "fee_maker_rate": FEE_MAKER_RATE,
            "fee_source": FEE_SOURCE,
            "fee_rounding": FEE_ROUNDING_MODE,
            "fee_rounding_step": FEE_ROUNDING_STEP,
            "fee_measured_at": FEE_CALIBRATION.get("generated_at"),
            "depth_safety": self.depth_safety,
            "maker_fill_probability": self.maker_fill_probability,
            "latency_ms": self.latency_ms,
            "note": (
                "El fill se evalúa contra el libro actual, no contra el snapshot de "
                "la señal: si el mercado se movió, la orden no cruza y no llena."
            ),
        }


# Calibración al arranque: si existe `fee_calibration.json` (medido con dinero
# ficticio en el exchange demo), el simulador arranca ya con las comisiones reales
# y no con las documentadas.
_apply_fee_config(effective_fee_config())

execution_model = ExecutionModel()
