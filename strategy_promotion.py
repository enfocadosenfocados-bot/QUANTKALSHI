"""strategy_promotion.py — Puerta explícita de promoción de PAPER a LIVE.

Un ranking no es una puerta de promoción. Con 16 estrategias en paralelo, alguna
parecerá ganadora por puro azar, y elegirla para live es la forma más rápida de
llevarse la sorpresa que este trabajo intenta evitar. Este módulo convierte el
ranking en un estado por estrategia con criterios explícitos y las dos
correcciones que faltaban:

  1. Comparaciones múltiples (Bonferroni). El umbral de significancia se divide
     por el número de estrategias evaluadas, porque probar 16 hipótesis infla la
     probabilidad de encontrar una "ganadora" que solo era ruido.

  2. Holdout temporal. La estrategia debe sobrevivir en la parte más reciente de
     su propio historial, que el ajuste de umbrales y el bandit no han usado para
     aprender. Es lo más parecido a "datos que no ha visto" sin esperar semanas.

Además exige la muestra mínima que da potencia estadística suficiente para el
edge declarado, en lugar de un umbral fijo de trades.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from strategy_ranking import (
    breakeven_for_trade,
    max_drawdown,
    sample_size_for_edge,
    wilson_interval,
)
from position_side import is_long_side

Z95 = 1.96
BONFERRONI_ALPHA = 0.05
# Proporción final del historial reservada como holdout (no se usa para elegir).
HOLDOUT_FRACTION = 0.30
MIN_HOLDOUT_TRADES = 10
# Muestra mínima absoluta antes de opinar sobre una estrategia.
MIN_CLOSED_BASE = 20
# Trades que debe sostener el rendimiento en LIVE_PEQUENO antes de escalar.
LIVE_CONFIRM_TRADES = 25

STATES = (
    "SIN_DATOS",
    "CANDIDATA",
    "SOMBRA",
    "LIVE_PEQUENO",
    "LIVE",
    "DESCARTADA",
    "PAUSADA",
    "SOSPECHOSA",
)

# Orden de lectura del tablero: primero lo que exige una decisión hoy.
# No coincide con STATES a propósito: SOSPECHOSA va primero porque bloquea
# capital y apunta a un problema en el registro, no en la estrategia.
STATE_PRIORITY = (
    "SOSPECHOSA",
    "LIVE_PEQUENO",
    "LIVE",
    "SOMBRA",
    "CANDIDATA",
    "SIN_DATOS",
    "DESCARTADA",
    "PAUSADA",
)

# Un win rate perfecto en mercados binarios significa que el registro no
# describe una operación real (fills instantáneos al precio soñado).
IMPOSSIBLE_WIN_RATE = 0.99
MIN_TRADES_FOR_INTEGRITY = 20
# Un cierre etiquetado WON/LOST es un hecho sobre el precio, no una opinión: el
# take profit de un largo se alcanza con la salida por encima de la entrada y su
# stop se toca por debajo. Cuando la etiqueta contradice el precio de salida (o el
# signo del PnL: un stop nunca cierra con beneficio, porque las comisiones solo
# empeoran el resultado) el cierre no lo produjo el mercado sino un error de
# contabilidad de lados. Se tolera un residuo mínimo de incoherencias por libros
# finos (el cierre puede barrer la profundidad peor que el precio evaluado).
INCOHERENT_CLOSE_MAX_RATIO = 0.02
INCOHERENT_CLOSE_MIN_TRADES = 2

STATE_LABELS = {
    "SIN_DATOS": "Sin datos suficientes",
    "CANDIDATA": "Candidata (opera, sin evidencia)",
    "SOMBRA": "En sombra (significativa, sin holdout)",
    "LIVE_PEQUENO": "Apta para LIVE con tamaño mínimo",
    "LIVE": "Apta para LIVE (rendimiento sostenido)",
    "DESCARTADA": "Descartada (sin edge o falla el holdout)",
    "PAUSADA": "Pausada por el gobernador",
    "SOSPECHOSA": "Datos no fiables (estadísticas imposibles)",
}

STATE_ACTIONS = {
    "SIN_DATOS": "Seguir acumulando muestra",
    "CANDIDATA": "Observar",
    "SOMBRA": "Vigilar de cerca; aún no arriesgar capital",
    "LIVE_PEQUENO": "Probar en live con $1-5 por operación",
    "LIVE": "Escalar tamaño gradualmente",
    "DESCARTADA": "Filtrar; no asignar capital",
    "PAUSADA": "Esperar a que el circuit breaker la reactive",
    "SOSPECHOSA": "Revisar el registro de trades antes de creer cualquier métrica",
}

# Reglas de integridad: estadísticas que no pueden venir de una operación real.
# El modelo de ejecución viejo producía fill instantáneo al precio señalado, lo
# que fabricaba win rates del 100% y a la vez PnL negativo (contradictorio).
def _incoherent_closes(closed: List[Dict[str, Any]]) -> int:
    """Cuenta cierres cuya etiqueta contradice el precio de salida o el signo del PnL.

    Largo: WON exige salida por encima de la entrada y LOST el stop por debajo.
    Corto: al revés. Un LOST con PnL positivo es imposible en cualquier lado.
    """
    incoherent = 0
    for trade in closed:
        exit_price = _f(trade.get("exit_price"))
        entry = _f(trade.get("entry_price"))
        if exit_price <= 0 or entry <= 0:
            # Sin precio de salida no se puede juzgar la etiqueta.
            continue
        favorable_move = exit_price - entry
        if not is_long_side(trade.get("side")):
            favorable_move = -favorable_move
        won = trade.get("status") == "WON"
        if (not won and _f(trade.get("realized_pnl_usd")) > 0) or (won and favorable_move < 0):
            incoherent += 1
    return incoherent


def _integrity_problems(closed: List[Dict[str, Any]]) -> List[str]:
    problems: List[str] = []
    n = len(closed)
    wins = sum(1 for t in closed if t.get("status") == "WON")
    pnl = sum(_f(t.get("realized_pnl_usd")) for t in closed)
    if n >= MIN_TRADES_FOR_INTEGRITY and wins / n >= IMPOSSIBLE_WIN_RATE:
        problems.append(
            f"{wins}/{n} operaciones ganadoras ({wins / n:.1%}): en mercados "
            "binarios ningún edge real gana casi todo"
        )
    if n > 0 and wins == n and pnl <= 0:
        problems.append(
            f"ganó las {n} operaciones pero el PnL es ${pnl:.2f}: el registro "
            "de pérdidas no cuadra"
        )
    incoherent = _incoherent_closes(closed)
    if (
        n > 0
        and incoherent >= INCOHERENT_CLOSE_MIN_TRADES
        and incoherent / n >= INCOHERENT_CLOSE_MAX_RATIO
    ):
        problems.append(
            f"{incoherent}/{n} cierres ({incoherent / n:.1%}) con etiqueta contraria "
            "al precio de salida (pérdidas cerradas con beneficio o ganadoras "
            "cerradas por debajo de la entrada): el registro mezcla lados largos y "
            "cortos, así que su win rate y su edge no son fiables"
        )
    return problems


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def norm_cdf(z: float) -> float:
    """CDF normal estándar sin depender de scipy."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def one_sided_p_value(wins: int, n: int, p0: float) -> float:
    """P(X >= wins) bajo H0: p = p0, con corrección de continuidad.

    Es la probabilidad de ver un win rate al menos tan bueno como el observado si
    la estrategia no tuviera edge alguno. Un p-valor alto significa "esto se
    explica por azar".
    """
    if n <= 0:
        return 1.0
    p0 = min(0.999, max(0.001, p0))
    phat = wins / n
    se = math.sqrt(max(1e-12, p0 * (1.0 - p0) / n))
    z = (phat - p0 - 0.5 / n) / se
    return max(0.0, min(1.0, 1.0 - norm_cdf(z)))


def closed_trades_of(trades: List[Dict[str, Any]], code: str) -> List[Dict[str, Any]]:
    return [
        t
        for t in trades
        if t.get("strategy_code") == code and t.get("status") in ("WON", "LOST")
    ]


def holdout_split(
    closed: List[Dict[str, Any]], fraction: float = HOLDOUT_FRACTION
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Divide el historial por tiempo: ajuste primero, holdout al final.

    El holdout es lo más reciente porque un cambio de régimen de mercado se parece
    más al presente que al pasado remoto.
    """
    ordered = sorted(closed, key=lambda t: _ts(t.get("closed_at")))
    if not ordered:
        return [], []
    if len(ordered) == 1:
        return ordered, []
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * (1.0 - fraction))))
    return ordered[:cut], ordered[cut:]


def _stats(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(closed)
    wins = sum(1 for t in closed if t.get("status") == "WON")
    pnl = sum(_f(t.get("realized_pnl_usd")) for t in closed)
    breakevens = [breakeven_for_trade(t) for t in closed]
    breakeven = (sum(breakevens) / len(breakevens)) if breakevens else 0.5
    return {
        "trades": n,
        "wins": wins,
        "win_rate": (wins / n) if n else 0.0,
        "pnl_usd": round(pnl, 2),
        "breakeven": breakeven,
        "edge": ((wins / n) - breakeven) if n else 0.0,
    }


def evaluate_strategy(
    code: str,
    closed: List[Dict[str, Any]],
    strategies_tested: int,
    paused: bool = False,
    governor_rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Calcula el estado de promoción de una estrategia."""
    overall = _stats(closed)
    n = overall["trades"]
    alpha = BONFERRONI_ALPHA / max(1, strategies_tested)
    row: Dict[str, Any] = {
        "code": code,
        "closed_trades": n,
        "wins": overall["wins"],
        "win_rate_pct": round(overall["win_rate"] * 100.0, 1),
        "breakeven_pct": round(overall["breakeven"] * 100.0, 1),
        "edge_pct": round(overall["edge"] * 100.0, 1),
        "pnl_usd": overall["pnl_usd"],
        "bonferroni_alpha": round(alpha, 5),
        "strategies_tested": strategies_tested,
        "governor_rules": governor_rules or {},
    }

    if n == 0:
        row.update(
            {
                "state": "SIN_DATOS",
                "state_label": STATE_LABELS["SIN_DATOS"],
                "reason": "La estrategia no ha cerrado ninguna operación todavía.",
                "next_step": STATE_ACTIONS["SIN_DATOS"],
                "missing_trades": MIN_CLOSED_BASE,
                "significant": False,
                "holdout_passed": False,
            }
        )
        return row

    wins = overall["wins"]
    lo, hi = wilson_interval(wins, n)
    p_value = one_sided_p_value(wins, n, overall["breakeven"])
    sample_needed = sample_size_for_edge(overall["edge"], overall["breakeven"])
    significant = p_value < alpha and lo > overall["breakeven"]
    significantly_worse = p_value > 0.95 and hi < overall["breakeven"]
    integrity = _integrity_problems(closed)

    pnl_series: List[float] = []
    running = 0.0
    for trade in sorted(closed, key=lambda t: _ts(t.get("closed_at"))):
        running += _f(trade.get("realized_pnl_usd"))
        pnl_series.append(running)
    drawdown = max_drawdown(pnl_series)

    fit, holdout = holdout_split(closed)
    fit_stats = _stats(fit)
    holdout_stats = _stats(holdout)
    holdout_ok = (
        holdout_stats["trades"] >= MIN_HOLDOUT_TRADES
        and holdout_stats["pnl_usd"] > 0
        and holdout_stats["win_rate"] >= holdout_stats["breakeven"]
    )

    row.update(
        {
            "wilson_lower_pct": round(lo * 100.0, 1),
            "wilson_upper_pct": round(hi * 100.0, 1),
            "p_value": round(p_value, 4),
            "sample_required": sample_needed,
            "sample_progress_pct": round(min(100.0, n / max(1, sample_needed) * 100.0), 1),
            "max_drawdown_pct": drawdown,
            "fit_pnl_usd": fit_stats["pnl_usd"],
            "fit_trades": fit_stats["trades"],
            "holdout_trades": holdout_stats["trades"],
            "holdout_pnl_usd": holdout_stats["pnl_usd"],
            "holdout_win_rate_pct": round(holdout_stats["win_rate"] * 100.0, 1),
            "holdout_breakeven_pct": round(holdout_stats["breakeven"] * 100.0, 1),
            "holdout_passed": bool(holdout_ok),
            "significant": bool(significant),
            "integrity_ok": not integrity,
            "integrity_problems": integrity,
        }
    )

    if integrity:
        state = "SOSPECHOSA"
        reason = "Registro no fiable: " + "; ".join(integrity)
        next_step = STATE_ACTIONS["SOSPECHOSA"]
    elif paused:
        state = "PAUSADA"
        reason = "El gobernador la pausó por pérdidas (circuit breaker)."
        next_step = STATE_ACTIONS["PAUSADA"]
    elif n < MIN_CLOSED_BASE:
        state = "CANDIDATA"
        reason = f"Muestra insuficiente ({n}/{MIN_CLOSED_BASE} trades cerrados)."
        next_step = f"Faltan {MIN_CLOSED_BASE - n} trades para poder opinar"
    elif significantly_worse:
        state = "DESCARTADA"
        reason = (
            "El intervalo de confianza completo está por debajo del breakeven "
            f"({row['wilson_upper_pct']}% < {row['breakeven_pct']}%)."
        )
        next_step = STATE_ACTIONS["DESCARTADA"]
    elif significant and holdout_ok:
        state = "LIVE_PEQUENO"
        reason = (
            "Significativa tras corrección de Bonferroni y positiva en el holdout "
            f"({row['holdout_trades']} trades, ${row['holdout_pnl_usd']})."
        )
        next_step = STATE_ACTIONS["LIVE_PEQUENO"]
    elif significant and not holdout_ok:
        state = "SOMBRA"
        reason = (
            "Significativa en el periodo de ajuste pero el holdout no confirma "
            f"({row['holdout_trades']} trades, ${row['holdout_pnl_usd']})."
        )
        next_step = (
            f"Acumular {max(0, MIN_HOLDOUT_TRADES - row['holdout_trades'])} trades más "
            "en el holdout antes de arriesgar capital"
        )
    else:
        state = "CANDIDATA"
        reason = (
            f"Sin evidencia suficiente: p={row['p_value']} no supera el umbral "
            f"corregido {row['bonferroni_alpha']}."
        )
        next_step = (
            f"Faltan {max(0, sample_needed - n)} trades para detectar el edge observado"
        )

    row["state"] = state
    row["state_label"] = STATE_LABELS[state]
    row["reason"] = reason
    row["next_step"] = next_step
    row["missing_trades"] = max(0, max(MIN_CLOSED_BASE, sample_needed) - n)
    return row


def build_promotion_board(paper_tracker, governor=None) -> Dict[str, Any]:
    """Tablero de promoción para todas las estrategias del paper trading."""
    # El veredicto se calcula sólo con el alcance vigente: un cierre anterior al
    # harness corregido lo produjo un bug de medición (lado mal etiquetado y stop
    # anclado al precio de la señal), así que no puede contar ni a favor ni en contra
    # de una estrategia. La familia de comparaciones (Bonferroni) sí cuenta todo lo
    # probado, para no relajar nunca el listón de significancia.
    _scoped = getattr(paper_tracker, "accounted_trades", None)
    trades = list(_scoped() if callable(_scoped) else getattr(paper_tracker, "trades", {}).values()) or []
    seen: List[str] = []
    for trade in list(getattr(paper_tracker, "trades", {}).values()) or []:
        code = str(trade.get("strategy_code") or "GEN")
        if code not in seen:
            seen.append(code)
    strategies_tested = max(1, len(seen))

    def _paused(code: str) -> bool:
        try:
            return bool(governor and governor.is_paused(code))
        except Exception:
            return False

    def _rules(code: str) -> Dict[str, Any]:
        try:
            return governor.get_rules(code) if governor else {}
        except Exception:
            return {}

    rows = [
        evaluate_strategy(
            code,
            closed_trades_of(trades, code),
            strategies_tested,
            paused=_paused(code),
            governor_rules=_rules(code),
        )
        for code in sorted(seen)
    ]
    order = {name: index for index, name in enumerate(STATE_PRIORITY)}
    rows.sort(key=lambda r: (order.get(r["state"], 99), -_f(r.get("pnl_usd"))))

    counts: Dict[str, int] = {name: 0 for name in STATES}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1

    promotable = [
        r["code"] for r in rows if r["state"] in ("LIVE_PEQUENO", "LIVE")
    ]
    result = {
        "strategies_tested": len(seen),
        "bonferroni_alpha": round(BONFERRONI_ALPHA / max(1, strategies_tested), 5),
        "holdout_fraction": HOLDOUT_FRACTION,
        "min_closed_base": MIN_CLOSED_BASE,
        "min_holdout_trades": MIN_HOLDOUT_TRADES,
        "counts": counts,
        "promotable": promotable,
        "rows": rows,
        "note": (
            "Ninguna estrategia pasa a live solo por tener buen PnL: necesita "
            "significancia con corrección por comparaciones múltiples y confirmar "
            "en el holdout temporal."
        ),
    }
    if not rows:
        result["empty_reason"] = (
            "Todavía no hay trades cerrados: sin muestra no hay veredicto, y "
            "cualquier ganadora que aparezca ahora sería azar."
        )
    return result

