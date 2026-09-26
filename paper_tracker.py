"""QUANT POLYMARKET - Paper Trading & Signal Track Record Engine
Monitorea en tiempo real las señales Top Sniper, simula entradas y evalúa PnL con Take Profit y Stop Loss.
"""
import json
import os
from decimal import Decimal
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    from config import (
        PAPER_MAX_EXPOSURE_USD,
        PAPER_MAX_OPEN_POSITIONS,
        PAPER_SLIPPAGE_BPS,
        PAPER_FEE_RATE,
        PAPER_ENFORCE_CAPITAL,
        PAPER_RESEARCH_BUDGET_PER_STRATEGY,
        PAPER_RESEARCH_MAX_OPEN_PER_STRATEGY,
        MIN_SIGNAL_ENTRY_PRICE,
        MAX_SIGNAL_RISK_REWARD,
        PAPER_MIN_LIQUIDITY_USD,
        PAPER_MIN_VOLUME_24H_USD,
        PAPER_MIN_OPEN_INTEREST,
        PAPER_MIN_CONFIDENCE,
        PAPER_ALLOW_NON_FLASH,
        PAPER_INITIAL_BALANCE,
        PAPER_MIN_TRADES_FOR_EMPIRICAL_WR,
        PAPER_ENTRY_DRIFT_PCT,
        PAPER_ENTRY_DRIFT_MIN_ABS,
        PAPER_RECORD_SCOPE,
        PAPER_LEGACY_SCOPE,
        PAPER_ZOMBIE_SCOPE,
        PAPER_ZOMBIE_HOURS,
    )
except ImportError:
    PAPER_MAX_EXPOSURE_USD = 1000.0
    PAPER_MAX_OPEN_POSITIONS = 12
    PAPER_SLIPPAGE_BPS = 5
    PAPER_FEE_RATE = 0.0
    PAPER_ENFORCE_CAPITAL = True
    PAPER_RESEARCH_BUDGET_PER_STRATEGY = 2000.0
    PAPER_RESEARCH_MAX_OPEN_PER_STRATEGY = 25
    MIN_SIGNAL_ENTRY_PRICE = 0.02
    MAX_SIGNAL_RISK_REWARD = 12.0
    PAPER_MIN_LIQUIDITY_USD = 500.0
    PAPER_MIN_VOLUME_24H_USD = 250.0
    PAPER_MIN_OPEN_INTEREST = 100.0
    PAPER_MIN_CONFIDENCE = 75.0
    PAPER_ALLOW_NON_FLASH = True
    PAPER_INITIAL_BALANCE = 1000.0
    PAPER_MIN_TRADES_FOR_EMPIRICAL_WR = 20
    PAPER_ENTRY_DRIFT_PCT = 0.15
    PAPER_ENTRY_DRIFT_MIN_ABS = 0.02
    PAPER_RECORD_SCOPE = "harness_v2"
    PAPER_LEGACY_SCOPE = "legacy_pre_harness_fix"
    PAPER_ZOMBIE_SCOPE = "harness_zombie"
    PAPER_ZOMBIE_HOURS = 6.0

from execution_model import book_levels, execution_model, normalize_levels

# Convención única de lado (largos vs cortos). Ver position_side.py: BOTH (market
# making) y BUY_BUNDLE son largos, y tratarlos como cortos invertía la referencia
# de salida (ask en vez de bid), la condición de take profit y la etiqueta
# WON/LOST: 39 cierres de MM quedaron etiquetados LOST con PnL positivo.
from position_side import entry_order_side, exit_order_side, is_long_side


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_float(val: Any, default: float = 0.0) -> float:
    if val in (None, ""):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def entry_geometry_valid(entry_price: float, stop_loss: float, target_price: float, side: Any) -> bool:
    """True si la geometría riesgo/beneficio es alcanzable desde la entrada real.

    Un largo necesita stop por debajo y objetivo por encima; un corto, al revés.
    Es la única invariante que separa una medición de un sesgo: cuando la señal
    calcula stop/objetivo sobre su precio y el fill llega a otro, el tracker abría
    la operación con el stop ya cruzado y la cerraba en el ciclo siguiente, con 0.0
    minutos de vida (100/108 de S20, 32/32 de S21, 35/36 de S23).
    """
    if entry_price <= 0 or stop_loss <= 0 or target_price <= 0:
        return False
    if is_long_side(side):
        return stop_loss < entry_price < target_price
    return target_price < entry_price < stop_loss


def closed_trades_of_scope(engine: Any) -> List[Dict[str, Any]]:
    """Cierres del alcance vigente de un tracker: lo único que puede decidir algo.

    Se usa desde ai_learning_engine y los endpoints, para que ni el aprendizaje ni
    la promoción se entrenen con el registro anterior (sesgado por el bug de lado y
    por los stops anclados al precio de la señal).
    """
    accessor = getattr(engine, "accounted_trades", None)
    trades = accessor() if callable(accessor) else list(getattr(engine, "trades", {}).values())
    return [t for t in trades if t.get("status") in ("WON", "LOST")]


def calculate_kelly_size(
    confidence: float,
    entry_price: float,
    target_price: float,
    stop_loss: float,
    side: str = "BUY",
    account_equity: float = 1000.0,
    fraction: float = 0.25,
    min_size_usd: float = 10.0,
    max_size_usd: float = 100.0,
) -> Dict[str, Any]:
    """Calcula el dimensionamiento dinámico de posición mediante Criterio de Kelly Fraccional (Quarter-Kelly) para portfolio de $1,000 USD.
    Fórmula: f* = (p * b - q) / b
    Donde:
      p = probabilidad de éxito (confidence / 100)
      q = 1 - p
      b = ratio beneficio/riesgo (reward / risk)
    En 75% de confianza: asigna ~1.5% - 2.5% de capital ($15 - $25 USD).
    En 96% - 98% de confianza: asigna ~8% - 10% de capital ($80 - $100 USD).
    """
    p = max(0.01, min(0.99, confidence / 100.0))
    q = 1.0 - p

    # El payoff se lee siempre como posición larga sobre el token salvo en los
    # lados genuinamente cortos (SELL/SELL_BUNDLE).
    if is_long_side(side):
        reward = max(0.005, target_price - entry_price)
        risk = max(0.005, entry_price - stop_loss)
    else:
        reward = max(0.005, entry_price - target_price)
        risk = max(0.005, stop_loss - entry_price)

    b = reward / risk if risk > 0 else 1.5
    f_star = (p * b - q) / b if b > 0 else 0.0

    if f_star <= 0:
        kelly_fraction = 0.005  # 0.5% capital mínimo prudencial
        size_usd = min_size_usd
    else:
        # Ponderación por convicción cuantitativa: 75% -> ~2.0%, 97% -> ~9.5%
        conviction_weight = max(0.15, min(1.0, (p - 0.65) / 0.32))
        base_fraction = f_star * fraction * conviction_weight
        kelly_fraction = max(0.015, min(0.10, base_fraction))
        calculated = account_equity * kelly_fraction
        size_usd = round(max(min_size_usd, min(max_size_usd, calculated)), 2)

    return {
        "size_usd": size_usd,
        "kelly_fraction_pct": round(kelly_fraction * 100, 2),
        "full_kelly_pct": round(max(0.0, f_star * 100), 2),
        "b_ratio": round(b, 2),
    }


def classify_time_horizon(market: Any) -> Dict[str, Any]:
    """Clasifica el horizonte temporal de un mercado en:
    - flash: ⚡ Flash (<48h)
    - short: 📅 Corto (<15d)
    - medium_long: 🗓️ Largo (>15d)
    """
    end_date_val = None
    if isinstance(market, dict):
        end_date_val = market.get("end_date") or market.get("end_date_iso") or market.get("endDate")
    else:
        end_date_val = getattr(market, "end_date", None) or getattr(market, "end_date_iso", None) or getattr(market, "endDate", None)

    if not end_date_val:
        return {"code": "medium_long", "label": "🗓️ Largo (>15d)", "hours_left": 720.0}

    try:
        if isinstance(end_date_val, (int, float)):
            end_dt = datetime.fromtimestamp(end_date_val, UTC)
        else:
            clean_str = str(end_date_val).replace("Z", "+00:00")
            if "T" in clean_str:
                end_dt = datetime.fromisoformat(clean_str)
            else:
                end_dt = datetime.strptime(clean_str[:10], "%Y-%m-%d").replace(tzinfo=UTC)

        now = utc_now()
        hours_left = max(0.0, (end_dt - now).total_seconds() / 3600.0)
        days_left = hours_left / 24.0

        if hours_left <= 48.0:
            return {"code": "flash", "label": "⚡ Flash (<48h)", "hours_left": round(hours_left, 1)}
        elif days_left <= 15.0:
            return {"code": "short", "label": "📅 Corto (<15d)", "hours_left": round(hours_left, 1)}
        else:
            return {"code": "medium_long", "label": "🗓️ Largo (>15d)", "hours_left": round(hours_left, 1)}
    except Exception:
        return {"code": "medium_long", "label": "🗓️ Largo (>15d)", "hours_left": 720.0}


class PaperTradingEngine:
    def __init__(self, storage_path: Optional[Path] = None, budget_mode: str = "global", budget_per_strategy: float = 2000.0, max_open_per_strategy: int = 25):
        self.storage_path = storage_path or (Path(__file__).resolve().parent / "paper_trades.json")
        self.initial_balance = float(PAPER_INITIAL_BALANCE)  # Equity simulada = bankroll real previsto
        self.position_size_usd = 25.0  # Fallback base ($25 USD)
        self.budget_mode = budget_mode  # "global" (realista) o "per_strategy" (research)
        self.budget_per_strategy = budget_per_strategy
        self.max_open_per_strategy = max_open_per_strategy
        self.trades: Dict[str, Dict[str, Any]] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    loaded = data.get("trades", {})
                    # Filtrar exclusivamente las que tengan confianza >= 75% (Ultra)
                    self.trades = {k: v for k, v in loaded.items() if to_float(v.get("confidence"), 0) >= 75.0}
                    # Migración de alcance: lo escrito antes del harness corregido no
                    # puede contar para las decisiones (lado mal etiquetado + stop
                    # anclado al precio de la señal). Se conserva para auditoría, con
                    # marca, fuera de win rate, Kelly, gobernador y promoción.
                    migrated = 0
                    for _trade in self.trades.values():
                        if not _trade.get("record_scope"):
                            _trade["record_scope"] = PAPER_LEGACY_SCOPE
                            migrated += 1
                    if migrated:
                        print(
                            f"[PaperTrading] {migrated} trades marcados como '{PAPER_LEGACY_SCOPE}' "
                            f"en {self.storage_path.name}: siguen visibles para auditoria pero "
                            "fuera de las estadisticas de decision."
                        )
            except Exception as e:
                # Si esto ocurre se empieza de cero y el track record desaparece,
                # asi que merece un aviso explicito y no un mensaje de paso.
                print(
                    f"[PaperTrading] ATENCION: historial ilegible en {self.storage_path} "
                    f"({type(e).__name__}: {e}). Se arranca sin track record."
                )

    def save_to_disk(self):
        """Guardado atomico: escribir sobre el archivo truncado a medias lo borraba.

        Antes se abria el archivo en modo "w" y se volcaba encima. Un reinicio del
        bot, un corte o una lectura concurrente en ese instante dejaban un JSON
        truncado, y al arrancar se perdia el historial entero (que es justo la
        base del gate de promocion). Se escribe a un temporal y se reemplaza con
        os.replace, que es atomico tambien en Windows.
        """
        payload = {"trades": self.trades, "updated_at": utc_now().isoformat()}
        tmp_path = self.storage_path.with_name(self.storage_path.name + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.storage_path)
        except Exception as e:
            print(f"[PaperTrading] Error guardando historial: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass

    @staticmethod
    def scope_of(trade: Dict[str, Any]) -> str:
        """Alcance de un trade. Sin marca se asume legado: nunca cuenta."""
        return str(trade.get("record_scope") or PAPER_LEGACY_SCOPE)

    def accounted_trades(self) -> List[Dict[str, Any]]:
        """Trades que cuentan para agregados (win rate, PnL, Kelly, promoción).

        Regla: entra el alcance vigente y entra cualquier posición abierta heredada
        (sigue siendo inventario real: su PnL flotante es dinero vivo y su capital
        está comprometido). Lo que no entra nunca es un cierre anterior al harness
        corregido: esos resultados los produjo el propio bug, no la estrategia, y
        mezclarlos hace que la muestra limpia arranque envenenada.
        """
        return [
            t for t in self.trades.values()
            if self.scope_of(t) == PAPER_RECORD_SCOPE or t.get("status") == "OPEN"
        ]

    def legacy_stats(self) -> Dict[str, Any]:
        """Resumen de lo excluido, para que la exclusión sea visible y auditable."""
        legacy = [t for t in self.trades.values() if self.scope_of(t) != PAPER_RECORD_SCOPE]
        closed = [t for t in legacy if t.get("status") in ("WON", "LOST")]
        wins = sum(1 for t in closed if t.get("status") == "WON")
        return {
            "scope": PAPER_RECORD_SCOPE,
            "legacy_scope": PAPER_LEGACY_SCOPE,
            "zombie_scope": PAPER_ZOMBIE_SCOPE,
            "legacy_trades": len(legacy),
            "legacy_closed": len(closed),
            "legacy_open": len(legacy) - len(closed),
            "legacy_win_rate_pct": round(wins / len(closed) * 100.0, 1) if closed else 0.0,
            "legacy_pnl_usd": round(sum(to_float(t.get("realized_pnl_usd"), 0.0) for t in closed), 2),
            "legacy_reason": (
                "medidos con el harness anterior (lado mal etiquetado y stop anclado al "
                "precio de la señal): se conservan para auditoría, no para decidir"
            ),
        }

    def evaluate_and_record_signal(self, signal: Dict[str, Any], market: Any) -> Optional[Dict[str, Any]]:
        """Evalúa si una señal califica como 'Top Sniper' y la registra en el track record con Kelly Sizing."""
        confidence = to_float(signal.get("confidence"), 0.0)
        entry_price = to_float(signal.get("entry_price"), 0.0)
        target_price = to_float(signal.get("target_price"), 0.0)
        stop_loss = to_float(signal.get("stop_loss"), 0.0)
        edge = to_float(signal.get("edge"), 0.0)
        side = str(signal.get("side") or "BUY").upper()

        # ===== Gobernador: pausas, drawdown global y reglas dinámicas =====
        try:
            from strategy_governor import governor
            sg_code = signal.get("strategy_code", "GEN")
            if governor.is_globally_paused():
                signal["governor_skipped"] = "global_paused"
                return None
            if governor.is_paused(sg_code):
                signal["governor_skipped"] = "paused"
                return None
            _rules = governor.get_rules(sg_code)
            if _rules.get("min_confidence") and confidence < to_float(_rules.get("min_confidence"), 0.0):
                signal["governor_skipped"] = "below_min_confidence"
                return None
            if _rules.get("max_entry_price") and entry_price > to_float(_rules.get("max_entry_price"), 0.0):
                signal["governor_skipped"] = "above_max_entry_price"
                return None
        except ImportError:
            pass

        if entry_price <= 0.01 or entry_price >= 0.99:
            return None
        if entry_price < MIN_SIGNAL_ENTRY_PRICE:
            return None

        # Si no tiene target o stop loss, fijarlo cuantitativamente
        # Mismos lados que strategies.py: largo sube al objetivo y baja al stop.
        if target_price <= 0 or target_price == entry_price:
            if is_long_side(side):
                target_price = min(0.99, entry_price + max(0.04, edge if edge > 0 else 0.05))
            else:
                target_price = max(0.01, entry_price - max(0.04, edge if edge > 0 else 0.05))

        if stop_loss <= 0 or stop_loss == entry_price:
            if is_long_side(side):
                stop_loss = max(0.01, entry_price * 0.92)
            else:
                stop_loss = min(0.99, entry_price * 1.08)

        # Enriquecer la señal con los valores de gestión de riesgo
        signal["stop_loss"] = f"{stop_loss:.4f}"
        signal["target_price"] = f"{target_price:.4f}"
        signal["entry_price"] = f"{entry_price:.4f}"

        # Cálculo de Ratio Riesgo/Beneficio
        risk = abs(entry_price - stop_loss)
        reward = abs(target_price - entry_price)
        # Se limita el R:R mostrado para no publicar ratios irreales en contratos
        # de bajo precio, que rompen la lectura del dashboard.
        rr_ratio = (reward / risk) if risk > 0 else 1.5
        signal["risk_reward_ratio"] = round(min(rr_ratio, MAX_SIGNAL_RISK_REWARD), 2)
        signal["recommended_order_type"] = "LIMIT (Maker)"
        signal["recommended_limit_price"] = f"{entry_price:.4f}"

        # Clasificación de Horizonte Temporal
        horizon_info = classify_time_horizon(market)
        signal["horizon"] = horizon_info["code"]
        signal["horizon_label"] = horizon_info["label"]
        signal["hours_to_resolve"] = horizon_info["hours_left"]

        # Dimensionamiento Dinámico Kelly con Multiplicador de Auto-Aprendizaje IA
        closed_pnl = sum(t.get("realized_pnl_usd", 0.0) for t in self.accounted_trades() if t.get("status") in ("WON", "LOST"))
        current_equity = max(200.0, self.initial_balance + closed_pnl)

        # Kelly con edge real: mezclar la confianza de la señal con el win rate empírico de la estrategia
        kelly_confidence = confidence
        _sg_code = signal.get("strategy_code", "GEN")
        _strat_closed = [t for t in self.accounted_trades() if t.get("strategy_code") == _sg_code and t.get("status") in ("WON", "LOST")]
        # Guarda de muestra: con pocos trades el win rate empírico es ruido y
        # mezclarlo con la confianza infla el sizing justo cuando no hay evidencia.
        if len(_strat_closed) >= PAPER_MIN_TRADES_FOR_EMPIRICAL_WR:
            _emp_wr = sum(1 for t in _strat_closed if t.get("status") == "WON") / len(_strat_closed) * 100.0
            kelly_confidence = min(99.0, 0.5 * confidence + 0.5 * _emp_wr)
            signal["empirical_win_rate_pct"] = round(_emp_wr, 1)
            signal["kelly_confidence_pct"] = round(kelly_confidence, 1)
            signal["empirical_sample_size"] = len(_strat_closed)
        else:
            signal["empirical_sample_size"] = len(_strat_closed)
            signal["empirical_note"] = (
                f"muestra insuficiente ({len(_strat_closed)}/{PAPER_MIN_TRADES_FOR_EMPIRICAL_WR})"
            )

        kelly_data = calculate_kelly_size(
            confidence=kelly_confidence,
            entry_price=entry_price,
            target_price=target_price,
            stop_loss=stop_loss,
            side=side,
            account_equity=current_equity,
            fraction=0.25,
            min_size_usd=10.0,
            max_size_usd=100.0,
        )
        
        # Multiplicador dinámico de IA y LinUCB Contextual Bandit
        conformal_res = None
        try:
            from ai_learning_engine import ai_learning_engine
            from quant_ml_engine import quant_ml
            strat_code = signal.get("strategy_code", "GEN")
            brier_mult = ai_learning_engine.get_strategy_multiplier(strat_code)
            
            # LinUCB Contextual Bandit
            ctx = quant_ml.bandit.get_current_context()
            ucb_score, bandit_mult = quant_ml.bandit.get_strategy_score_and_multiplier(strat_code, ctx)
            
            # Filtro de Conformal Prediction con garantía al 95%
            conformal_res = quant_ml.conformal.evaluate_signal(
                predicted_prob=confidence,
                market_price=entry_price,
                side=side,
            )
            signal["conformal_interval"] = {
                "p_lower": conformal_res.p_lower,
                "p_upper": conformal_res.p_upper,
                "quantile_q": conformal_res.quantile_q,
                "is_admissible": conformal_res.is_admissible,
                "edge_pct": conformal_res.edge_pct,
                "rejection_reason": conformal_res.rejection_reason,
            }
            signal["bandit_multiplier"] = bandit_mult
            signal["bandit_ucb_score"] = ucb_score

            # Multiplicador combinado regulado (Brier Score * LinUCB)
            strat_mult = round(min(max(brier_mult * bandit_mult, 0.40), 1.60), 2)
        except Exception:
            strat_mult = 1.0

        # Control Activo de Drawdown (Continuous Drawdown-Constrained Kelly):
        # Si el portfolio entra en racha negativa (drawdown > 0), el sizing se reduce suavemente
        # para blindar matemáticamente la cuenta de $1,000 USD contra rachas adversas.
        peak_equity = max(self.initial_balance, self.initial_balance + max(0.0, closed_pnl))
        current_dd = max(0.0, (peak_equity - current_equity) / peak_equity) if peak_equity > 0 else 0.0
        drawdown_factor = max(0.15, min(1.0, 1.0 - (current_dd / 0.08)))  # 8% max drawdown tolerance

        trade_size_usd = round(max(10.0, min(150.0, kelly_data["size_usd"] * strat_mult * drawdown_factor)), 2)
        signal["position_size_usd"] = trade_size_usd
        signal["kelly_fraction_pct"] = round(kelly_data["kelly_fraction_pct"] * strat_mult * drawdown_factor, 2)
        signal["full_kelly_pct"] = kelly_data["full_kelly_pct"]
        signal["ai_strategy_multiplier"] = strat_mult
        signal["drawdown_protection_factor"] = round(drawdown_factor, 2)
        signal["current_drawdown_pct"] = round(current_dd * 100.0, 2)

        # `_market_field` y no `getattr`: el auto-sniper de lead-lag pasa el mercado
        # como dict y `getattr(dict, "liquidity")` devuelve 0, así que TODOS los
        # snipes se descartaban como illíquidos mientras el dashboard anunciaba
        # EXECUTED_AUTO (0 trades de LL_SNIPER en el track record).
        liquidity = to_float(self._market_field(market, "liquidity", 0), 0.0)
        volume_24h = to_float(self._market_field(market, "volume_24h", 0), 0.0)
        open_interest = to_float(self._market_field(market, "open_interest", 0), 0.0)

        # Kalshi publica liquidity_dollars en 0.00 para la gran mayoria de mercados
        # y su volumen 24h rara vez alcanza miles. Exigir solo `liquidity` dejaba al
        # bot sin operar nunca, asi que se acepta cualquier indicador de actividad
        # real (volumen negociado, open interest vivo o profundidad del libro).
        book_depth = 0.0
        try:
            for side_rows in (getattr(market, "order_book", {}) or {}).values():
                rows = (side_rows.get("bids") or []) + (side_rows.get("asks") or [])
                for row in rows:
                    size = row.get("size") if isinstance(row, dict) else None
                    price = row.get("price") if isinstance(row, dict) else None
                    if size is None and isinstance(row, (list, tuple)) and len(row) >= 2:
                        price, size = row[0], row[1]
                    book_depth += to_float(size, 0.0) * to_float(price, 0.0)
        except Exception:
            book_depth = 0.0

        has_liquidity = (
            liquidity >= PAPER_MIN_LIQUIDITY_USD
            or volume_24h >= PAPER_MIN_VOLUME_24H_USD
            or open_interest >= PAPER_MIN_OPEN_INTEREST
            or book_depth >= PAPER_MIN_LIQUIDITY_USD
        )
        signal["liquidity_snapshot"] = {
            "liquidity": round(liquidity, 2),
            "volume_24h": round(volume_24h, 2),
            "open_interest": round(open_interest, 2),
            "book_depth_usd": round(book_depth, 2),
        }

        # Estrategias intradia de alta velocidad (rotacion rapida de capital).
        is_fast_strategy = signal.get("strategy_code") in ("S20", "S24", "S22", "BA", "LL_SNIPER")
        is_flash_horizon = horizon_info["code"] == "flash" or to_float(horizon_info.get("hours_left"), 999.0) <= 48.0
        horizon_allowed = is_flash_horizon or is_fast_strategy or PAPER_ALLOW_NON_FLASH

        if self.budget_mode == "per_strategy":
            # Modo Research: criterio relajado para que TODAS las estrategias operen
            is_sniper = (
                confidence >= 60.0
                and entry_price > 0.02
                and entry_price < 0.98
                and has_liquidity
            )
        else:
            # Modo Realista: exige conviccion alta y actividad real verificable.
            is_sniper = (
                confidence >= PAPER_MIN_CONFIDENCE
                and horizon_allowed
                and entry_price > 0.02
                and entry_price < 0.98
                and has_liquidity
            )
        signal["is_top_sniper"] = is_sniper

        if not is_sniper:
            return None

        trade_key = signal.get("dedupe_key") or f"{signal.get('strategy_code')}:{getattr(market, 'market_id', '')}:{signal.get('token')}:{side}"

        if trade_key in self.trades:
            return self.trades[trade_key]

        # ===== Bloque 1: límite de capital + ejecución con slippage/fees =====
        # Modo Realista (global): presupuesto único de $1,000.
        # Modo Research (per_strategy): presupuesto aislado por estrategia.
        total_open_exposure = 0.0
        if PAPER_ENFORCE_CAPITAL:
            open_trades = [t for t in self.trades.values() if t.get("status") == "OPEN"]
            strat_code = signal.get("strategy_code", "GEN")
            if self.budget_mode == "per_strategy":
                relevant = [t for t in open_trades if t.get("strategy_code") == strat_code]
                max_open = self.max_open_per_strategy
                max_exposure = self.budget_per_strategy
            else:
                relevant = open_trades
                max_open = PAPER_MAX_OPEN_POSITIONS
                max_exposure = PAPER_MAX_EXPOSURE_USD
            total_open_exposure = sum(to_float(t.get("position_size_usd"), 0.0) for t in relevant)
            if len(relevant) >= max_open:
                signal["capital_skipped"] = "max_positions"
                return None
            available = max_exposure - total_open_exposure
            if available < 10.0:
                signal["capital_skipped"] = "no_capital"
                return None
            if trade_size_usd > available:
                trade_size_usd = round(available, 2)
                signal["position_size_usd"] = trade_size_usd

        # ===== Ejecución realista: tick legal, profundidad, comisión y cola maker =====
        # El modelo anterior aplicaba slippage en puntos básicos y comisión cero,
        # que en un contrato de 3.5 centavos con tick de 1 centavo no representa
        # nada: el siguiente precio legal está un 14% más arriba.
        order_type = str(signal.get("recommended_order_type") or "").lower()
        urgency = str(signal.get("urgency") or "").upper()
        # Una señal urgente (latencia, noticias) cruza el spread: es taker y paga
        # comisión. Solo las señales tranquilas pueden aspirar a ser maker.
        is_maker = ("maker" in order_type) and urgency not in {"HIGH", "CRITICAL"}
        fill = execution_model.entry_fill_with_budget(
            market,
            signal.get("token", "Yes"),
            # El modelo de ejecución solo entiende BUY/SELL: sin normalizar, "BOTH"
            # se cruzaba como venta (bid) y luego se cerraba como compra (ask), es
            # decir ganaba el spread en las dos patas.
            entry_order_side(side),
            entry_price,
            trade_size_usd,
            is_maker=is_maker,
        )
        if not fill.filled:
            signal["execution_skipped"] = fill.reason
            return None

        exec_price = fill.avg_price
        shares = round(fill.contracts, 2)
        fee_usd = fill.fee_usd
        trade_size_usd = round(fill.notional_usd + fee_usd, 2)
        signal["position_size_usd"] = trade_size_usd
        signal["execution_price"] = round(exec_price, 4)
        signal["entry_fee_usd"] = fee_usd
        signal["slippage_ticks"] = fill.slippage_ticks
        signal["fill_model"] = execution_model.model
        signal["fill_is_maker"] = fill.is_maker
        signal["fill_partial"] = fill.partial
        signal["fill_book_synthetic"] = fill.book_synthetic
        signal["capital_exposure_usd"] = round(total_open_exposure + trade_size_usd, 2)

        # ===== Anclaje del riesgo al precio REAL de ejecución =====
        # La señal calculó stop y objetivo sobre SU precio (`entry_price`). Cuando el
        # fill llega a otro nivel (libro del outcome contrario, o señal ya caducada en
        # un mercado que se movió) el stop queda del lado equivocado de la entrada
        # real y la operación se cierra en el ciclo siguiente: así murieron 100/108
        # trades de S20, 35/36 de S23, 32/32 de S21 y 22/41 de S02, con 0.0 min de
        # vida media y win rate 0-8%. No es un resultado de la estrategia: es el
        # harness midiéndose a sí mismo.
        drift = abs(exec_price - entry_price)
        drift_limit = max(PAPER_ENTRY_DRIFT_MIN_ABS, PAPER_ENTRY_DRIFT_PCT * entry_price)
        if drift > drift_limit:
            signal["execution_skipped"] = (
                f"desvio_entrada: senal {entry_price:.4f} vs ejecucion {exec_price:.4f} "
                f"(>{drift_limit:.4f})"
            )
            return None

        if drift > 0:
            # Mismo diseño de riesgo, precio real: reescalar stop y objetivo por el
            # cociente preserva el R:R que la estrategia definió (es invariante a la
            # escala) pero lo ata al nivel al que se entró de verdad. Las bandas de
            # seguridad evitan que el redondeo deje el stop pegado a la entrada.
            scale = exec_price / entry_price
            stop_loss = round(max(0.01, min(0.99, stop_loss * scale)), 4)
            target_price = round(max(0.01, min(0.99, target_price * scale)), 4)
            _edge_margin = max(0.04, edge if edge > 0 else 0.05)
            if is_long_side(side):
                if stop_loss >= exec_price:
                    stop_loss = max(0.01, round(exec_price * 0.92, 4))
                if target_price <= exec_price:
                    target_price = min(0.99, round(exec_price + _edge_margin, 4))
            else:
                if stop_loss <= exec_price:
                    stop_loss = min(0.99, round(exec_price * 1.08, 4))
                if target_price >= exec_price:
                    target_price = max(0.01, round(exec_price - _edge_margin, 4))
            signal["entry_anchor_scale"] = round(scale, 4)
            signal["stop_loss"] = f"{stop_loss:.4f}"
            signal["target_price"] = f"{target_price:.4f}"

        # Invariante del harness: no se registra ninguna operación cuya geometría no
        # sea alcanzable respecto a la entrada real (largo con stop debajo y objetivo
        # encima; corto al revés). Una geometría imposible garantiza una pérdida.
        if not entry_geometry_valid(exec_price, stop_loss, target_price, side):
            signal["execution_skipped"] = (
                f"geometria_invalida: entrada {exec_price:.4f} stop {stop_loss:.4f} "
                f"objetivo {target_price:.4f} lado {side}"
            )
            return None

        # Spread en la entrada: forma parte del contexto con el que el bandit
        # aprende, porque un spread amplio cambia el resultado esperado.
        token_name = signal.get("token", "Yes")
        spread_at_entry = 0.0
        try:
            bid = to_float((self._market_field(market, "best_bid", {}) or {}).get(token_name), 0.0)
            ask = to_float((self._market_field(market, "best_ask", {}) or {}).get(token_name), 0.0)
            if ask > bid > 0:
                spread_at_entry = ask - bid
        except Exception:
            spread_at_entry = 0.0

        new_trade = {
            "trade_id": trade_key,
            "signal_id": signal.get("signal_id", ""),
            "opened_at": utc_now().isoformat(),
            "spread_at_entry": round(spread_at_entry, 4),
            "market_id": getattr(market, "market_id", ""),
            "market_question": getattr(market, "question", "Mercado Desconocido"),
            "category": getattr(market, "category", "General") or "General",
            "strategy": signal.get("strategy", "Quant Strategy"),
            "strategy_code": signal.get("strategy_code", "GEN"),
            "token": signal.get("token", "Yes"),
            "side": side,
            "order_type": "LIMIT",
            "entry_price": round(exec_price, 4),
            "entry_fee_usd": round(fee_usd, 4),
            "fill_model": execution_model.model,
            "fill_is_maker": fill.is_maker,
            "fill_partial": fill.partial,
            "fill_book_synthetic": fill.book_synthetic,
            "slippage_ticks": round(fill.slippage_ticks, 2),
            "signal_entry_price": entry_price,
            "entry_drift": round(drift, 4),
            "record_scope": PAPER_RECORD_SCOPE,
            "current_price": round(exec_price, 4),
            "target_price": round(target_price, 4),
            "stop_loss": round(stop_loss, 4),
            "initial_stop_loss": round(stop_loss, 4),
            # El pico arranca en el precio de EJECUCIÓN, no en el de la señal: si
            # arrancaba por encima, el break-even se activaba en el primer ciclo.
            "peak_price": round(exec_price, 4),
            "break_even_active": False,
            "trailing_stop_active": False,
            "trailing_stop_price": None,
            "shares": shares,
            "position_size_usd": trade_size_usd,
            "kelly_fraction_pct": kelly_data["kelly_fraction_pct"],
            "full_kelly_pct": kelly_data["full_kelly_pct"],
            "confidence": confidence,
            "edge_pct": round(edge * 100, 2),
            "risk_reward_ratio": round(rr_ratio, 2),
            "horizon": horizon_info["code"],
            "horizon_label": horizon_info["label"],
            "status": "OPEN",  # OPEN, WON, LOST
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "realized_pnl_usd": 0.0,
            "realized_pnl_pct": 0.0,
            "close_reason": None,
            "closed_at": None,
        }

        self.trades[trade_key] = new_trade
        self.save_to_disk()
        return new_trade

    record_signal = evaluate_and_record_signal

    def _market_field(self, market: Any, name: str, default: Any = None) -> Any:
        if isinstance(market, dict):
            return market.get(name, default)
        return getattr(market, name, default)

    def _exit_reference_price(self, market: Any, token_name: str, side: str) -> float:
        """Precio al que realmente se saldaría la posición.

        Para cerrar un largo hay que vender al BID; para cerrar un corto, comprar
        al ASK. Evaluar la salida contra el precio medio (como antes) daba el
        objetivo por alcanzado medio spread antes de tiempo. Los lados BOTH /
        BUY_BUNDLE son largos (ver LONG_SIDES): usar el ask para ellos daba la
        salida por buena al precio en el que se compra, no en el que se vende.
        """
        long_position = is_long_side(side)
        book = self._market_field(market, "order_book", {}) or {}
        side_books = book.get(token_name) or {}
        rows = side_books.get("bids" if long_position else "asks") or []
        levels = normalize_levels(rows, descending=long_position)
        if levels:
            return levels[0][0]
        quotes = self._market_field(
            market, "best_bid" if long_position else "best_ask", {}
        ) or {}
        return to_float(quotes.get(token_name), 0.0)

    def _close_trade(
        self,
        trade: Dict[str, Any],
        market: Any,
        status: str,
        reason: str,
        record_scope: Optional[str] = None,
    ) -> bool:
        """Cierra un trade ejecutando la salida contra el libro y cobrando su comisión.

        El PnL antiguo ignoraba el coste de salida: daba por bueno el precio
        objetivo sin comisión ni slippage, así que regalaba dinero dos veces, al
        entrar y al salir.
        """
        token_name = trade.get("token", "Yes")
        shares = to_float(trade.get("shares"), 0.0)
        entry = to_float(trade.get("entry_price"), 0.0)
        entry_fee = to_float(trade.get("entry_fee_usd"), 0.0)
        side = trade.get("side", "BUY")
        long_position = is_long_side(side)
        # Cerrar un largo es vender; cerrar un corto es comprar.
        exit_side = exit_order_side(side)
        sweep_limit = 0.01 if exit_side == "SELL" else 0.99

        fill = execution_model.exit_fill(
            market, token_name, exit_side, sweep_limit, shares, is_maker=False
        )
        executed = fill.contracts
        exit_price = fill.avg_price
        exit_fee = fill.fee_usd
        penalty_note = ""

        if not fill.filled or executed < shares - 1e-9:
            # El libro visible no cubre la posición. Un cierre real sigue llenando
            # barriendo más profundo y peor, así que se completa al peor precio
            # disponible con un tick de castigo en vez de dejar la posición zombi.
            bids, asks, _ = book_levels(market, token_name)
            levels = bids if exit_side == "SELL" else asks
            if not levels:
                trade["exit_blocked_reason"] = fill.reason or "libro vacío en la salida"
                return False
            tick = to_float(self._market_field(market, "tick_size", 0.01), 0.01) or 0.01
            worst = levels[-1][0]
            completed = worst - tick if exit_side == "SELL" else worst + tick
            completed = max(0.01, min(0.99, round(completed, 4)))
            remainder = shares - max(0.0, executed)
            if remainder > 0:
                existing_notional = max(0.0, executed) * exit_price
                exit_price = (existing_notional + remainder * completed) / shares
                exit_fee += execution_model.fee(remainder, completed, is_maker=False)
                executed = shares
                penalty_note = (
                    f" | cierre completado a {completed:.4f} por profundidad insuficiente"
                )

        if shares <= 0:
            return False

        entry_notional = shares * entry
        exit_notional = shares * exit_price
        if long_position:
            # Largo: se paga la entrada (notional + comisión) y se cobra la salida.
            basis = entry_notional + entry_fee
            realized = exit_notional - exit_fee - basis
        else:
            # Corto: se cobra la entrada y se paga la recompra, que es lo caro. Con
            # la fórmula del largo un corto ganador aparecía como pérdida.
            basis = entry_notional - entry_fee
            realized = basis - (exit_notional + exit_fee)
        realized_pct = (realized / basis * 100.0) if basis > 0 else 0.0

        trade["status"] = status
        trade["realized_pnl_usd"] = round(realized, 2)
        trade["realized_pnl_pct"] = round(realized_pct, 2)
        trade["unrealized_pnl_usd"] = 0.0
        trade["unrealized_pnl_pct"] = 0.0
        trade["closed_at"] = utc_now().isoformat()
        trade["close_reason"] = reason + penalty_note
        trade["exit_price"] = round(exit_price, 4)
        trade["exit_fee_usd"] = round(exit_fee, 4)
        trade["total_fees_usd"] = round(entry_fee + exit_fee, 4)
        if record_scope:
            # Un cierre forzado (mercado muerto) no pertenece a la estadística de la
            # estrategia: se conserva con su propia marca, fuera de win rate y Kelly.
            trade["record_scope"] = record_scope
        return True

    def _register_stale_market(self, trade: Dict[str, Any]) -> bool:
        """Marca un ciclo sin mercado ni precio y cierra la posición si ya es un zombi.

        Una posición abierta cuyo mercado dejó de cotizar no se puede valorar ni
        cerrar: se queda ocupando cupo y capital para siempre. Había 12 así, con
        hasta 23 h de antigüedad, y dejaban el tracker realista congelado en 12/12
        posiciones: ninguna señal nueva podía entrar. Se cierra al último precio
        conocido pagando su comisión (conservador: no se reclama ningún pago por
        resolución del mercado) y se marca con PAPER_ZOMBIE_SCOPE para que no
        contamine la estadística de la estrategia.

        Devuelve True si el trade cambió y hay que persistir.
        """
        if not trade.get("stale_since"):
            trade["stale_since"] = trade.get("opened_at") or utc_now().isoformat()
        try:
            since = datetime.fromisoformat(str(trade["stale_since"]).replace("Z", "+00:00"))
        except Exception:
            return False
        hours = (utc_now() - since).total_seconds() / 3600.0
        if hours < PAPER_ZOMBIE_HOURS:
            return False

        side = trade.get("side", "BUY")
        long_position = is_long_side(side)
        shares = to_float(trade.get("shares"), 0.0)
        entry = to_float(trade.get("entry_price"), 0.0)
        entry_fee = to_float(trade.get("entry_fee_usd"), 0.0)
        mark = to_float(trade.get("current_price"), 0.0) or entry
        exit_fee = execution_model.fee(shares, mark, is_maker=False) if shares > 0 and mark > 0 else 0.0

        if long_position:
            realized = shares * mark - exit_fee - (shares * entry + entry_fee)
            basis = shares * entry + entry_fee
        else:
            realized = (shares * entry - entry_fee) - (shares * mark + exit_fee)
            basis = shares * entry - entry_fee

        trade["status"] = "WON" if realized >= 0 else "LOST"
        trade["realized_pnl_usd"] = round(realized, 2)
        trade["realized_pnl_pct"] = round(realized / basis * 100.0, 2) if basis > 0 else 0.0
        trade["unrealized_pnl_usd"] = 0.0
        trade["unrealized_pnl_pct"] = 0.0
        trade["exit_price"] = round(mark, 4)
        trade["exit_fee_usd"] = round(exit_fee, 4)
        trade["total_fees_usd"] = round(entry_fee + exit_fee, 4)
        trade["closed_at"] = utc_now().isoformat()
        trade["close_reason"] = (
            f"🧟 Cierre forzado: mercado sin cotización viva durante {hours:.1f}h "
            f"(valorado al último precio {mark:.4f})"
        )
        trade["record_scope"] = PAPER_ZOMBIE_SCOPE
        print(
            f"[PaperTrading] Posición zombi cerrada {trade.get('trade_id')} "
            f"({trade.get('strategy_code')}): {hours:.1f}h sin mercado, PnL {realized:+.2f} USD"
        )
        return True

    def update_live_prices(self, market_registry):
        """Actualiza precios y ejecuta Trailing Stop dinámico y Break-even."""
        changed = False

        for trade in list(self.trades.values()):
            if trade.get("status") != "OPEN":
                continue

            market_id = trade.get("market_id")
            market = market_registry.get_market(market_id)
            if not market:
                # El mercado desapareció del registro (resuelto y fuera del
                # universo): la posición no se puede valorar ni cerrar. Si lleva
                # así lo suficiente es un zombi y libera su cupo y su capital.
                if self._register_stale_market(trade):
                    changed = True
                continue

            token_name = trade.get("token", "Yes")
            if isinstance(market, dict):
                mid_prices = market.get("mid_price", {})
                prices = market.get("prices", {})
            else:
                mid_prices = getattr(market, "mid_price", {}) or {}
                prices = getattr(market, "prices", {}) or {}

            current_p = mid_prices.get(token_name) if isinstance(mid_prices, dict) else None
            if current_p is None and isinstance(prices, dict):
                current_p = prices.get(token_name)

            if current_p is None:
                current_p = (mid_prices.get("Yes") if isinstance(mid_prices, dict) else None) or (prices.get("Yes") if isinstance(prices, dict) else None)

            curr_float = to_float(current_p, 0.0)
            if curr_float <= 0:
                # Mercado presente pero sin precio publicable: misma situación que
                # un mercado ausente (no se puede valorar la posición).
                if self._register_stale_market(trade):
                    changed = True
                continue

            # El mercado volvió a cotizar: la racha de silencio se reinicia.
            if trade.pop("stale_since", None) is not None:
                changed = True

            token_name = trade.get("token", "Yes")
            # El target/stop se evalúa contra el precio al que realmente se sale
            # (bid para un largo, ask para un corto), no contra el precio medio.
            exit_mark = self._exit_reference_price(market, token_name, trade.get("side", "BUY"))
            if exit_mark <= 0:
                exit_mark = curr_float
            trade["exit_reference_price"] = round(exit_mark, 4)
            entry = trade["entry_price"]
            target = trade["target_price"]
            shares = trade["shares"]
            side = trade["side"]
            # BOTH / BUY_BUNDLE son largos (ver LONG_SIDES). En la rama corta su
            # take profit era "el precio baja al objetivo" y su stop "el precio
            # sube al stop": con el stop por debajo de la entrada, el stop se
            # disparaba al instante y el cierre se etiquetaba mal.
            long_position = is_long_side(side)

            # Cálculo de PnL Flotante
            if long_position:
                pnl_usd = (curr_float - entry) * shares
                pnl_pct = ((curr_float - entry) / entry) * 100.0

                # Actualizar precio pico favorable
                peak = max(to_float(trade.get("peak_price", entry)), curr_float)
                trade["peak_price"] = round(peak, 4)
                peak_gain_pct = ((peak - entry) / entry) * 100.0

                # LÓGICA DE TRAILING STOP DINÁMICO (+4% BE, 2% Trailing)
                if peak_gain_pct >= 4.0:
                    # 1. Break-Even automático
                    if not trade.get("break_even_active"):
                        trade["break_even_active"] = True
                        be_price = round(entry * 1.002, 4)  # Break-even con mini-colchón
                        trade["stop_loss"] = max(to_float(trade.get("stop_loss")), be_price)

                    # 2. Trailing Stop persiguiendo precio pico a 2.0% de distancia
                    trailing_target = round(peak * 0.98, 4)
                    if trailing_target > to_float(trade.get("stop_loss")):
                        trade["stop_loss"] = trailing_target
                        trade["trailing_stop_active"] = True
                        trade["trailing_stop_price"] = trailing_target

                current_stop = to_float(trade.get("stop_loss"))

                # Verificación de Salida contra el precio real de salida (bid).
                if exit_mark >= target:
                    changed = self._close_trade(
                        trade,
                        market,
                        "WON",
                        f"🎯 Take Profit alcanzado (bid ${exit_mark:.3f} >= ${target:.3f})",
                    ) or changed
                elif exit_mark <= current_stop:
                    protected = exit_mark > entry
                    changed = self._close_trade(
                        trade,
                        market,
                        "WON" if protected else "LOST",
                        (
                            f"🎯 Trailing Stop activado (beneficio protegido a ${exit_mark:.3f})"
                            if protected
                            else f"🛑 Stop Loss (bid ${exit_mark:.3f} <= ${current_stop:.3f})"
                        ),
                    ) or changed

            else:  # Corto genuino (SELL / SELL_BUNDLE)
                pnl_usd = (entry - curr_float) * shares
                pnl_pct = ((entry - curr_float) / entry) * 100.0

                lowest = min(to_float(trade.get("peak_price", entry)), curr_float)
                trade["peak_price"] = round(lowest, 4)
                peak_gain_pct = ((entry - lowest) / entry) * 100.0

                if peak_gain_pct >= 4.0:
                    if not trade.get("break_even_active"):
                        trade["break_even_active"] = True
                        be_price = round(entry * 0.998, 4)
                        trade["stop_loss"] = min(to_float(trade.get("stop_loss")), be_price)

                    trailing_target = round(lowest * 1.02, 4)
                    if trailing_target < to_float(trade.get("stop_loss")):
                        trade["stop_loss"] = trailing_target
                        trade["trailing_stop_active"] = True
                        trade["trailing_stop_price"] = trailing_target

                current_stop = to_float(trade.get("stop_loss"))

                if exit_mark <= target:
                    changed = self._close_trade(
                        trade,
                        market,
                        "WON",
                        f"🎯 Take Profit alcanzado (ask ${exit_mark:.3f} <= ${target:.3f})",
                    ) or changed
                elif exit_mark >= current_stop:
                    protected = exit_mark < entry
                    changed = self._close_trade(
                        trade,
                        market,
                        "WON" if protected else "LOST",
                        (
                            f"🎯 Trailing Stop activado (beneficio protegido a ${exit_mark:.3f})"
                            if protected
                            else f"🛑 Stop Loss (ask ${exit_mark:.3f} >= ${current_stop:.3f})"
                        ),
                    ) or changed

            # LÓGICA DE TIME-STOP PARA ROTACIÓN CONTINUA DE CAPITAL (Regla B):
            # Si una posición abierta lleva >= 48 horas y está estancada (PnL < 1.5%),
            # se cierra a precio de mercado para liberar el capital y reasignarlo a señales Flash vivas.
            if trade.get("status") == "OPEN":
                opened_at_str = trade.get("opened_at")
                if opened_at_str:
                    try:
                        op_dt = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                        elapsed_hours = (utc_now() - op_dt).total_seconds() / 3600.0
                        if elapsed_hours >= 48.0 and abs(pnl_pct) < 2.0:
                            changed = self._close_trade(
                                trade,
                                market,
                                "WON" if pnl_usd >= 0 else "LOST",
                                (
                                    "⏱️ Time-Stop 48h (capital liberado tras "
                                    f"{elapsed_hours:.1f}h estancado)"
                                ),
                            ) or changed
                    except Exception:
                        pass

            if trade.get("status") == "OPEN":
                trade["unrealized_pnl_usd"] = round(pnl_usd, 2)
                trade["unrealized_pnl_pct"] = round(pnl_pct, 2)

        if changed:
            self.save_to_disk()
            try:
                from ai_learning_engine import ai_learning_engine
                from quant_ml_engine import quant_ml
                legacy_marked = False
                for tr in self.trades.values():
                    if tr.get("status") not in ("WON", "LOST") or tr.get("ai_post_mortem_done"):
                        continue
                    if self.scope_of(tr) != PAPER_RECORD_SCOPE:
                        # El aprendizaje ni el bandit pueden entrenarse con el registro
                        # sesgado: se marca como excluido (una sola vez) en vez de
                        # reprocesarlo en cada ciclo.
                        tr["ai_post_mortem_done"] = "excluded_legacy_scope"
                        legacy_marked = True
                        continue
                    ai_learning_engine.analyze_trade_post_mortem(tr)

                    # Actualización Online de LinUCB Contextual Bandit y Conformal Prediction
                    strat_code = tr.get("strategy_code", "GEN")
                    outcome = 1 if tr.get("status") == "WON" else 0
                    pnl = to_float(tr.get("realized_pnl_usd", 0.0), 0.0)
                    # Recompensa por PnL normalizado, no por acierto. Con ±1 el
                    # bandit escalaba capital hacia la estrategia con mejor
                    # hit-rate aunque perdiera dinero (9 ganancias de +$1 y una
                    # pérdida de -$500 puntuaban 9:1 a favor).
                    risk_budget = max(1.0, to_float(tr.get("position_size_usd"), 1.0))
                    reward = max(-1.0, min(1.0, pnl / risk_budget))
                    tr["bandit_reward"] = round(reward, 4)
                    ctx = quant_ml.bandit.get_context_for_trade(tr)
                    quant_ml.bandit.update_online(strat_code, ctx, reward)
                    quant_ml.conformal.record_ground_truth(to_float(tr.get("confidence", 80.0), 80.0), outcome)

                    tr["ai_post_mortem_done"] = True
                if legacy_marked:
                    self.save_to_disk()
            except Exception:
                pass

        # ===== Gobernador: circuit breaker + auto-tuning + drawdown global =====
        try:
            from strategy_governor import governor
            by_code = {}
            for t in self.accounted_trades():
                if t.get("status") in ("WON", "LOST"):
                    by_code.setdefault(t.get("strategy_code", "GEN"), []).append(t)
            for code, closed in by_code.items():
                budget = self.budget_per_strategy if self.budget_mode == "per_strategy" else self.initial_balance
                governor.evaluate_strategy(code, closed, budget)
            if self.budget_mode == "global":
                realized = sum(to_float(t.get("realized_pnl_usd", 0.0), 0.0) for t in self.accounted_trades() if t.get("status") in ("WON", "LOST"))
                governor.update_portfolio_drawdown(self.initial_balance + realized, self.initial_balance)
        except Exception:
            pass

    def get_summary(self) -> Dict[str, Any]:
        """Calcula métricas agregadas del track record incluyendo Sharpe, Drawdown y Equity Curve.

        Las métricas de decisión salen sólo del alcance vigente (`accounted_trades`):
        incluye el inventario abierto heredado, porque su capital está comprometido y
        su flotante es dinero vivo, pero deja fuera los cierres anteriores al harness
        corregido. Lo excluido se publica en `scope` para que la exclusión sea visible.
        """
        trades_list = list(self.trades.values())
        trades_list.sort(key=lambda x: x.get("opened_at", ""), reverse=True)

        accounted = self.accounted_trades()
        open_trades = [t for t in accounted if t.get("status") == "OPEN"]
        closed_trades = [t for t in accounted if t.get("status") in ("WON", "LOST")]
        winning_trades = [t for t in closed_trades if t.get("status") == "WON"]
        losing_trades = [t for t in closed_trades if t.get("status") == "LOST"]

        total_realized_pnl = sum(t.get("realized_pnl_usd", 0.0) for t in closed_trades)
        total_unrealized_pnl = sum(t.get("unrealized_pnl_usd", 0.0) for t in open_trades)
        current_equity = self.initial_balance + total_realized_pnl + total_unrealized_pnl

        closed_count = len(closed_trades)
        win_rate = (len(winning_trades) / closed_count * 100.0) if closed_count > 0 else 0.0

        gross_profit = sum(t.get("realized_pnl_usd", 0.0) for t in winning_trades)
        gross_loss = abs(sum(t.get("realized_pnl_usd", 0.0) for t in losing_trades))
        profit_factor = round((gross_profit / gross_loss), 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 1.0)
        total_roi_pct = ((current_equity - self.initial_balance) / self.initial_balance) * 100.0

        # Historial de curva de equidad cronológica
        chronological_closed = sorted(closed_trades, key=lambda x: x.get("closed_at") or x.get("opened_at") or "")
        equity_history = [
            {"time": "Inicio", "equity": round(self.initial_balance, 2), "pnl": 0.0, "trade": "Balance Inicial"}
        ]
        running_equity = self.initial_balance
        peak_equity = self.initial_balance
        max_drawdown_pct = 0.0

        for t in chronological_closed:
            pnl = to_float(t.get("realized_pnl_usd", 0.0))
            running_equity += pnl
            if running_equity > peak_equity:
                peak_equity = running_equity
            dd = ((peak_equity - running_equity) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

            t_date = str(t.get("closed_at", ""))[:16].replace("T", " ") or "Trade"
            equity_history.append({
                "time": t_date,
                "equity": round(running_equity, 2),
                "pnl": round(pnl, 2),
                "trade": f"{t.get('strategy_code', 'SIG')} {t.get('side', '')} {t.get('token', '')}",
            })

        if open_trades:
            if current_equity > peak_equity:
                peak_equity = current_equity
            dd = ((peak_equity - current_equity) / peak_equity) * 100.0 if peak_equity > 0 else 0.0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd
            equity_history.append({
                "time": "En vivo (Flotante)",
                "equity": round(current_equity, 2),
                "pnl": round(total_unrealized_pnl, 2),
                "trade": f"{len(open_trades)} posiciones abiertas",
            })

        # Ratio de Sharpe: solo se reporta con muestra suficiente. Antes se
        # devolvian constantes inventadas (2.5 con desviacion cero, 2.1 con un
        # unico trade), que son numeros falsos presentados como metricas.
        MIN_CLOSED_FOR_SHARPE = 10
        if closed_count >= MIN_CLOSED_FOR_SHARPE:
            returns = [to_float(t.get("realized_pnl_pct", 0.0)) / 100.0 for t in closed_trades]
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std_r = variance ** 0.5
            sharpe = round((mean_r / std_r) * (len(returns) ** 0.5), 2) if std_r > 0 else None
        else:
            sharpe = None

        if closed_count > 0:
            avg_win = (gross_profit / len(winning_trades)) if winning_trades else 0.0
            avg_loss = (gross_loss / len(losing_trades)) if losing_trades else 0.0
            expectancy_usd = round(((win_rate / 100.0) * avg_win) - (((100.0 - win_rate) / 100.0) * avg_loss), 2)
        else:
            expectancy_usd = 0.0

        return {
            "initial_balance": self.initial_balance,
            "current_equity": round(current_equity, 2),
            "total_pnl_usd": round(total_realized_pnl + total_unrealized_pnl, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "total_unrealized_pnl": round(total_unrealized_pnl, 2),
            "total_roi_pct": round(total_roi_pct, 2),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "expectancy_usd": expectancy_usd,
            "total_trades": len(accounted),
            "open_trades_count": len(open_trades),
            "winning_trades_count": len(winning_trades),
            "losing_trades_count": len(losing_trades),
            "equity_history": equity_history,
            "open_positions": open_trades,
            "history": closed_trades[:50],
            "all_trades": trades_list[:100],
            # Qué se excluye y por qué: la exclusión tiene que ser auditable, no un
            # borrado silencioso que haga parecer limpio lo que no lo era.
            "scope": self.legacy_stats(),
            "updated_at": utc_now().isoformat(),
        }

    def get_strategy_performance(self) -> Dict[str, Any]:
        """Calcula el desglose de PnL flotante, PnL realizado y ranking por estrategia."""
        catalog = {
            "S10": {
                "code": "S10",
                "name": "Yes-No Bias Exploitation",
                "tag": "S10: Yes Bias",
                "category": "Sesgo de Comportamiento",
                "icon": "⚖️",
                "description": "Explota la sobrevaloración del YES minorista apostando al NO con descuento cuantitativo.",
            },
            "WT": {
                "code": "WT",
                "name": "Whale & Smart Money Tracking",
                "tag": "WT: Whale Tracking",
                "category": "Flujo Institucional",
                "icon": "🐋",
                "description": "Sigue las posiciones y acumulación de billeteras ballena con win-rate comprobado >60%.",
            },
            "MR": {
                "code": "MR",
                "name": "Mean Reversion (Sobre-reacción)",
                "tag": "MR: Mean Reversion",
                "category": "Estadística Cuantitativa",
                "icon": "🔄",
                "description": "Entrada contra-tendencia tras movimientos abruptos de 1d/1w esperando reversión a la media.",
            },
            "S12": {
                "code": "S12",
                "name": "High Probability Harvesting",
                "tag": "S12: High Prob Harvesting",
                "category": "Cosecha de Rendimiento",
                "icon": "🌾",
                "description": "Cosecha de prima en contratos con probabilidad ultra-alta (85%-98%) cercanos al vencimiento.",
            },
            "S05": {
                "code": "S05",
                "name": "NegRisk Rebalancing Arbitrage",
                "tag": "S05: NegRisk Rebalancing",
                "category": "Arbitraje Matemático",
                "icon": "🧮",
                "description": "Arbitraje estructural cuando la suma de probabilidades del canasto supera $1.00.",
            },
            "S02": {
                "code": "S02",
                "name": "Weather NOAA Quantitative",
                "tag": "S02: Weather NOAA",
                "category": "Datos Externos / Clima",
                "icon": "⛅",
                "description": "Modelo de previsión meteorológica NOAA vs precios cotizados en Polymarket.",
            },
            "S03": {
                "code": "S03",
                "name": "Nothing Ever Happens (Status Quo)",
                "tag": "S03: Nothing Ever Happens",
                "category": "Sesgo Histórico",
                "icon": "🛡️",
                "description": "Monetiza el sesgo hacia noticias sensacionalistas comprando NO en eventos poco probables.",
            },
            "BA": {
                "code": "BA",
                "name": "Bundle Arbitrage (YES + NO)",
                "tag": "BA: Bundle Arbitrage",
                "category": "Arbitraje Libre de Riesgo",
                "icon": "⚡",
                "description": "Arbitraje libre de riesgo comprando YES y NO simultáneamente por debajo de $1.00.",
            },
            "MM": {
                "code": "MM",
                "name": "Market Making (Spread Capture)",
                "tag": "MM: Market Making",
                "category": "Provisión de Liquidez",
                "icon": "🏦",
                "description": "Captura de diferencial bid-ask y comisiones colocando órdenes límite pasivas.",
            },
            "FLB": {
                "code": "FLB",
                "name": "Favorite-Longshot Bias",
                "tag": "FLB: Favorite-Longshot",
                "category": "Sesgo de Probabilidad",
                "icon": "🎯",
                "description": "Venta de contratos 'longshot' sobrevalorados con expectativa matemática positiva.",
            },
            "S20": {
                "code": "S20",
                "name": "Oracle Delay Sniping (Descuento UMA)",
                "tag": "S20: Oracle Sniping",
                "category": "Caza Post-Evento / Liquidación",
                "icon": "🎯",
                "description": "Compra contratos con certeza casi total a descuento (0.88-0.97) esperando liquidación UMA a $1.00.",
            },
            "S21": {
                "code": "S21",
                "name": "Arbitraje Lógico Condicional",
                "tag": "S21: Arbitraje Condicional",
                "category": "Arbitraje Matemático Lógico",
                "icon": "📐",
                "description": "Explota incoherencias entre mercados causalmente vinculados (ej. escaleras de precios y prerrequisitos).",
            },
            "S22": {
                "code": "S22",
                "name": "Fast News Latency Sniping",
                "tag": "S22: Fast News Sniping",
                "category": "Latencia de Información & NLP",
                "icon": "⚡",
                "description": "Barre órdenes desactualizadas en el book ante noticias de impacto antes de que el mercado reajuste.",
            },
            "S23": {
                "code": "S23",
                "name": "Resolution Rules-Lawyer",
                "tag": "S23: Rules-Lawyer",
                "category": "Asimetría Contractual UMA",
                "icon": "📜",
                "description": "Monetiza el sesgo de lectura superficial comprando NO cuando la regla legal estricta no se puede cumplir.",
            },
            "S24": {
                "code": "S24",
                "name": "Order Flow Imbalance (OFI)",
                "tag": "S24: Order Flow Imbalance",
                "category": "Microestructura Cuantitativa",
                "icon": "🌊",
                "description": "Scalping rápido de 30s a 3m detectando absorciones violentas de liquidez y desbalance de profundidad.",
            },
        }

        trades_list = list(self.trades.values())
        # Los números por estrategia salen del alcance vigente (con su inventario
        # abierto), pero el catálogo se completa con todos los códigos vistos, para
        # que una estrategia con historial heredado siga apareciendo en el panel con
        # su muestra limpia a cero en lugar de desaparecer sin explicación.
        scoped_list = self.accounted_trades()
        codes_in_trades = set(t.get("strategy_code") for t in trades_list if t.get("strategy_code"))
        all_codes = list(catalog.keys())
        for c in codes_in_trades:
            if c and c not in all_codes:
                all_codes.append(c)

        strategy_stats = []
        total_floating_all = 0.0
        total_realized_all = 0.0

        for code in all_codes:
            meta = catalog.get(code, {
                "code": code,
                "name": f"Estrategia {code}",
                "tag": code,
                "category": "Algorítmica",
                "icon": "📈",
                "description": "Estrategia cuantitativa automatizada.",
            })

            strat_trades = [t for t in scoped_list if t.get("strategy_code") == code]
            open_trades = [t for t in strat_trades if t.get("status") == "OPEN"]
            closed_trades = [t for t in strat_trades if t.get("status") in ("WON", "LOST")]
            won_trades = [t for t in closed_trades if t.get("status") == "WON"]
            lost_trades = [t for t in closed_trades if t.get("status") == "LOST"]

            unrealized_pnl = sum(t.get("unrealized_pnl_usd", 0.0) for t in open_trades)
            realized_pnl = sum(t.get("realized_pnl_usd", 0.0) for t in closed_trades)
            total_pnl = round(realized_pnl + unrealized_pnl, 2)
            total_floating_all += unrealized_pnl
            total_realized_all += realized_pnl

            closed_count = len(closed_trades)
            win_rate = round((len(won_trades) / closed_count * 100.0), 1) if closed_count > 0 else 0.0

            gross_win = sum(t.get("realized_pnl_usd", 0.0) for t in won_trades)
            gross_loss = abs(sum(t.get("realized_pnl_usd", 0.0) for t in lost_trades))
            profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 1.0)

            if len(open_trades) > 0:
                status = "OPERATING"
                status_label = "🟢 Operando"
            elif closed_count > 0:
                status = "STANDBY"
                status_label = "🟡 Standby"
            else:
                status = "SCANNING"
                status_label = "🔍 Escaneando"

            strategy_stats.append({
                "code": code,
                "name": meta["name"],
                "tag": meta["tag"],
                "category": meta["category"],
                "icon": meta["icon"],
                "description": meta["description"],
                "status": status,
                "status_label": status_label,
                "unrealized_pnl_usd": round(unrealized_pnl, 2),
                "realized_pnl_usd": round(realized_pnl, 2),
                "total_pnl_usd": total_pnl,
                "win_rate_pct": win_rate,
                "profit_factor": profit_factor,
                "total_trades": len(strat_trades),
                "open_trades_count": len(open_trades),
                "closed_trades_count": closed_count,
                "won_count": len(won_trades),
                "lost_count": len(lost_trades),
                "open_positions": open_trades,
                "recent_closed": closed_trades[:15],
            })

        # Ordenar por PnL Total desc, luego PnL Realizado desc, luego flotante desc
        strategy_stats.sort(key=lambda s: (s["total_pnl_usd"], s["realized_pnl_usd"], s["unrealized_pnl_usd"], s["total_trades"]), reverse=True)

        for idx, s in enumerate(strategy_stats, start=1):
            s["rank"] = idx
            if idx == 1:
                s["rank_badge"] = "🥇 #1"
            elif idx == 2:
                s["rank_badge"] = "🥈 #2"
            elif idx == 3:
                s["rank_badge"] = "🥉 #3"
            else:
                s["rank_badge"] = f"#{idx}"

        best_strategy = strategy_stats[0] if strategy_stats else None
        strategies_with_closed = [s for s in strategy_stats if s["closed_trades_count"] > 0]
        best_winrate_strat = max(strategies_with_closed, key=lambda s: s["win_rate_pct"]) if strategies_with_closed else None

        return {
            "total_floating_pnl_usd": round(total_floating_all, 2),
            "total_realized_pnl_usd": round(total_realized_all, 2),
            "total_combined_pnl_usd": round(total_floating_all + total_realized_all, 2),
            "best_strategy_name": best_strategy["name"] if best_strategy else "N/A",
            "best_strategy_code": best_strategy["code"] if best_strategy else "N/A",
            "best_strategy_pnl": best_strategy["total_pnl_usd"] if best_strategy else 0.0,
            "best_winrate_name": best_winrate_strat["name"] if best_winrate_strat else "Sin posiciones cerradas",
            "best_winrate_pct": best_winrate_strat["win_rate_pct"] if best_winrate_strat else 0.0,
            "strategies": strategy_stats,
            "updated_at": utc_now().isoformat(),
        }

    def reset_track_record(self):
        """Reinicia el track record a cero."""
        self.trades.clear()
        self.save_to_disk()


# Instancia global (Modo Realista: presupuesto único de $1,000)
paper_tracker = PaperTradingEngine()

# Instancia del Modo Research (presupuesto aislado por estrategia)
paper_tracker_research = PaperTradingEngine(
    storage_path=Path(__file__).resolve().parent / "paper_trades_research.json",
    budget_mode="per_strategy",
    budget_per_strategy=PAPER_RESEARCH_BUDGET_PER_STRATEGY,
    max_open_per_strategy=PAPER_RESEARCH_MAX_OPEN_PER_STRATEGY,
)
