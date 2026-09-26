"""position_side.py — Una sola definición de qué lados son largos y qué lados son cortos.

El tracker de paper representa UNA posición por operación: larga o corta sobre el
token registrado. El market making (``side="BOTH"``) y los bundles de compra
(``"BUY_BUNDLE"``) son LARGOS, y eso no es una opinión:

  * ``strategies.py`` les asigna payoff direccional largo (``target = entry + 0.02``,
    ``stop = entry - 0.02``).
  * ``live_execution.py`` los envía como ``side="bid"`` (comprar YES).
  * ``paper_tracker._close_trade`` siempre contabilizó su PnL como
    ``(salida - entrada)``.

Esta tabla vive en un único módulo para que ningún consumidor decida por su
cuenta. Cuando ``paper_tracker`` los trataba como cortos, su take profit se
evaluaba al revés (con el stop por debajo de la entrada se disparaba al instante),
la salida se valoraba al ask en vez de al bid y 39 cierres de MM quedaron
etiquetados ``LOST`` con el precio a favor y PnL positivo; y ``strategy_ranking``
les calculaba el breakeven como ``1 - entry``, lo que inflaba su edge y su
p-valor justo en el criterio que decide la promoción a LIVE.
"""
from typing import Any

LONG_SIDES = frozenset({"BUY", "BOTH", "BUY_BUNDLE"})
SHORT_SIDES = frozenset({"SELL", "SELL_BUNDLE"})


def is_long_side(side: Any) -> bool:
    """True si el lado se gestiona como largo sobre el token (por defecto, largo)."""
    return str(side or "BUY").strip().upper() not in SHORT_SIDES


def entry_order_side(side: Any) -> str:
    """Lado real de la orden de apertura: se compra para abrir un largo."""
    return "BUY" if is_long_side(side) else "SELL"


def exit_order_side(side: Any) -> str:
    """Lado real de la orden de cierre: un largo se cierra vendiendo."""
    return "SELL" if is_long_side(side) else "BUY"
