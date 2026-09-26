"""strategy_governor.py — Gobernador cuantitativo de estrategias.

Centraliza la auto-protección y auto-mejora del portfolio:
  1. Circuit breaker: pausa una estrategia si pierde más de X% en N trades cerrados.
  2. Reglas dinámicas por estrategia: restricciones activas (min_confidence, max_entry_price, etc.).
  3. Límite de drawdown global: frena nuevas entradas si el portfolio cae más de Y%.
  4. Auto-tuning con validación por backtest: solo aplica un cambio de umbral si habría
     mejorado el PnL histórico (walk-forward simple).
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_PATH = Path(__file__).resolve().parent / "strategy_governor.json"

CIRCUIT_BREAKER_MAX_LOSS_PCT = 5.0    # % del presupuesto perdido que dispara la pausa
CIRCUIT_BREAKER_MIN_TRADES = 5        # trades cerrados mínimos para evaluar
# Horas de pausa del circuit breaker. Configurable porque 24h dejaba al bot sin# parte de sus estrategias durante un dia completo; 3h permite re-evaluarlas.
try:
    from config import GOVERNOR_PAUSE_HOURS as CIRCUIT_BREAKER_COOLDOWN_H
except Exception:
    CIRCUIT_BREAKER_COOLDOWN_H = 3.0
PORTFOLIO_MAX_DRAWDOWN_PCT = 15.0     # drawdown global que frena nuevas entradas
TUNE_MIN_TRADES = 8                   # trades cerrados mínimos para auto-tune
TUNE_CONF_STEP = 5.0                  # paso de ajuste del umbral de confianza
TUNE_MIN_CONF = 50.0
TUNE_MAX_CONF = 95.0


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


class StrategyGovernor:
    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or STATE_PATH
        self.paused: Dict[str, float] = {}          # code -> pause_until_ts
        self.rules: Dict[str, Dict[str, Any]] = {}  # code -> reglas dinámicas
        self.global_paused_until: float = 0.0
        self._load()

    def _load(self):
        if self.storage_path.exists():
            try:
                d = json.loads(self.storage_path.read_text(encoding="utf-8"))
                self.paused = {k: float(v) for k, v in (d.get("paused") or {}).items()}
                self.rules = d.get("rules") or {}
                self.global_paused_until = float(d.get("global_paused_until", 0.0))
            except Exception:
                pass

    def _save(self):
        try:
            self.storage_path.write_text(json.dumps({
                "updated_at": time.time(),
                "paused": self.paused,
                "rules": self.rules,
                "global_paused_until": self.global_paused_until,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def is_paused(self, code: str) -> bool:
        return self.paused.get(code, 0.0) > time.time()

    def is_globally_paused(self) -> bool:
        return self.global_paused_until > time.time()

    def get_rules(self, code: str) -> Dict[str, Any]:
        return self.rules.get(code) or {}

    def set_rule(self, code: str, key: str, value: Any):
        self.rules.setdefault(code, {})[key] = value
        self._save()

    def pause(self, code: str, hours: float = CIRCUIT_BREAKER_COOLDOWN_H):
        self.paused[code] = time.time() + hours * 3600.0
        self._save()

    def unpause(self, code: str):
        self.paused.pop(code, None)
        self._save()

    def update_portfolio_drawdown(self, equity: float, peak: float):
        """Frena globalmente si el drawdown supera el umbral."""
        if peak <= 0:
            return
        dd = (peak - equity) / peak * 100.0
        if dd >= PORTFOLIO_MAX_DRAWDOWN_PCT and not self.is_globally_paused():
            self.global_paused_until = time.time() + CIRCUIT_BREAKER_COOLDOWN_H * 3600.0
            self._save()

    def evaluate_strategy(self, code: str, closed_trades: List[Dict[str, Any]], budget: float):
        """Circuit breaker + auto-tuning sobre los trades cerrados de una estrategia."""
        if not closed_trades or budget <= 0:
            return
        realized = sum(_f(t.get("realized_pnl_usd", t.get("pnl"))) for t in closed_trades)
        loss_pct = abs(realized) / budget * 100.0 if realized < 0 else 0.0

        if len(closed_trades) >= CIRCUIT_BREAKER_MIN_TRADES and loss_pct >= CIRCUIT_BREAKER_MAX_LOSS_PCT:
            self.pause(code)
        elif realized >= 0 and code in self.paused:
            self.unpause(code)

        self._auto_tune(code, closed_trades)

    def _auto_tune(self, code: str, closed_trades: List[Dict[str, Any]]):
        if len(closed_trades) < TUNE_MIN_TRADES:
            return
        wins = sum(1 for t in closed_trades if t.get("status") == "WON")
        wr = wins / len(closed_trades) * 100.0
        be = sum(_f(t.get("entry_price"), 0.5) for t in closed_trades) / len(closed_trades) * 100.0

        current_conf = _f(self.get_rules(code).get("min_confidence"), 0.0)
        if current_conf <= 0:
            current_conf = 60.0

        proposed: Optional[float] = None
        if wr < be and current_conf < TUNE_MAX_CONF:
            proposed = current_conf + TUNE_CONF_STEP  # más selectivo
        elif wr > be + 10.0 and current_conf > TUNE_MIN_CONF:
            proposed = current_conf - TUNE_CONF_STEP  # más agresivo
        if proposed is None:
            return

        # Backtest: solo aplicar si filtrar por el nuevo umbral habría mejorado el PnL realizado
        filtered = [t for t in closed_trades if _f(t.get("confidence"), 0.0) >= proposed]
        if len(filtered) >= 3:
            pnl_all = sum(_f(t.get("realized_pnl_usd", t.get("pnl"))) for t in closed_trades)
            pnl_filt = sum(_f(t.get("realized_pnl_usd", t.get("pnl"))) for t in filtered)
            if pnl_filt > pnl_all:
                self.set_rule(code, "min_confidence", round(proposed, 1))

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "paused": {k: round(v - now, 0) for k, v in self.paused.items() if v > now},
            "global_paused": self.is_globally_paused(),
            "rules": self.rules,
        }


governor = StrategyGovernor()
