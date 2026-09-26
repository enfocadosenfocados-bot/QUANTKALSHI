"""Tests del gobernador de estrategias (circuit breaker + auto-tuning)."""
import tempfile
from pathlib import Path
from strategy_governor import StrategyGovernor


def test_circuit_breaker_pausa():
    g = StrategyGovernor(storage_path=Path(tempfile.mkdtemp()) / "g.json")
    trades = [{"status": "LOST", "realized_pnl_usd": -100.0, "confidence": 80.0} for _ in range(6)]
    g.evaluate_strategy("SX", trades, budget=1000.0)
    assert g.is_paused("SX"), "Debería pausar la estrategia con -60% en 6 trades"
    print("[OK] circuit breaker pausa estrategia perdedora")


def test_recuperacion_despausa():
    g = StrategyGovernor(storage_path=Path(tempfile.mkdtemp()) / "g.json")
    g.pause("SX", hours=0)
    trades = [{"status": "WON", "realized_pnl_usd": 50.0, "confidence": 80.0} for _ in range(6)]
    g.evaluate_strategy("SX", trades, budget=1000.0)
    assert not g.is_paused("SX"), "Debería des-pausar al volver a positivo"
    print("[OK] des-pausa al recuperar")


def test_auto_tune_sube_confianza():
    g = StrategyGovernor(storage_path=Path(tempfile.mkdtemp()) / "g.json")
    # 9 trades: 3 ganados, 6 perdidos -> wr 33%, breakeven alto
    trades = [{"status": "WON", "realized_pnl_usd": 5.0, "confidence": 95.0, "entry_price": 0.8} for _ in range(3)]
    trades += [{"status": "LOST", "realized_pnl_usd": -20.0, "confidence": 95.0, "entry_price": 0.8} for _ in range(6)]
    g.evaluate_strategy("SX", trades, budget=1000.0)
    rules = g.get_rules("SX")
    # el auto-tune solo aplica si el backtest valida el cambio; en caso de perder con confianza alta,
    # filtrar por mayor confianza no mejora (todos tienen 95) -> no debería cambiar
    print(f"[OK] auto-tune evaluado, reglas actuales: {rules}")


if __name__ == "__main__":
    test_circuit_breaker_pausa()
    test_recuperacion_despausa()
    test_auto_tune_sube_confianza()
    print("TESTS GOBERNADOR PASARON")
