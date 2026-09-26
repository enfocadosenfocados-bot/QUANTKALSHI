"""Tests de regresión del harness de medición corregido (B1, B2 y B3).

El paper trading sólo sirve para decidir si una estrategia pasa a dinero real si
mide a la estrategia y no al propio simulador. Aquí se fijan los tres defectos que
lo rompían:

  B1. El stop y el objetivo se anclaban al precio de la SEÑAL mientras el PnL se
      calculaba con el precio de EJECUCIÓN. Cuando el libro se movía entre la señal
      y la orden, el stop quedaba del lado equivocado de la entrada real y la
      posición moría en el ciclo siguiente (100/108 trades de S20, 32/32 de S21,
      35/36 de S23, con 0.0 minutos de vida media). Ahora el riesgo se reescala al
      precio al que se entró de verdad y ninguna operación se registra con
      geometría inalcanzable.
  B2. Una posición cuyo mercado dejó de cotizar no se podía valorar ni cerrar:
      seguía ocupando cupo hasta congelar el tracker realista entero (12/12
      posiciones, hasta 23 h). Ahora se cierra como zombi al último precio conocido
      y se marca con su propio alcance para no contaminar a la estrategia.
  B3. El registro anterior al harness corregido (lado mal etiquetado) volvía
      incoherentes las métricas de MM (359/1220) y S21 (2/24). Se conserva para
      auditoría, pero queda fuera de win rate, Kelly, gobernador, bandit,
      promoción y ranking.

Lo que se comprueba es la invariante, no un número: ninguna operación se registra
con el stop del lado equivocado de su entrada real, ninguna posición muere por un
error de contabilidad y ningún cierre producido por el bug decide nada.
"""
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from config import (
    PAPER_LEGACY_SCOPE,
    PAPER_RECORD_SCOPE,
    PAPER_ZOMBIE_HOURS,
    PAPER_ZOMBIE_SCOPE,
)
from paper_tracker import PaperTradingEngine, closed_trades_of_scope, entry_geometry_valid
from strategy_promotion import build_promotion_board, evaluate_strategy
from strategy_ranking import build_strategy_ranking


class FakeMarket:
    """Mercado con libro real y quotes coherentes para el lado que se evalúa."""

    def __init__(self, bid: float, ask: float, market_id: str = "M1"):
        self.market_id = market_id
        self.question = "Will event occur?"
        self.ticker = "TICK"
        self.liquidity = 50000.0
        self.volume_24h = 100000.0
        self.open_interest = 1000.0
        self.category = "Politics"
        self.tick_size = 0.01
        self.min_order_size = 1
        self.end_date_iso = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        mid = round((bid + ask) / 2, 4)
        self.best_bid = {"Yes": bid}
        self.best_ask = {"Yes": ask}
        self.mid_price = {"Yes": mid}
        self.prices = {"Yes": mid}
        self.order_book = {
            "Yes": {
                "bids": [{"price": bid, "size": 5000.0}],
                "asks": [{"price": ask, "size": 5000.0}],
            }
        }


class FakeRegistry:
    def __init__(self, market):
        self.market = market

    def get_market(self, market_id):
        return self.market


class MissingRegistry:
    """Registro donde el mercado ya no existe: es el caso del zombi."""

    def get_market(self, market_id):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(hours_ago: float = 0.0) -> str:
    return (_utc_now() - timedelta(hours=hours_ago)).isoformat()


class HarnessStubMixin:
    """Sustituye gobernador/IA/ML por stubs: los tests no tocan estado del bot."""

    def _patch_engines(self):
        governor = mock.MagicMock()
        governor.is_globally_paused.return_value = False
        governor.is_paused.return_value = False
        governor.get_rules.return_value = {}
        stubs = mock.patch.dict(
            sys.modules,
            {
                "strategy_governor": SimpleNamespace(governor=governor),
                "ai_learning_engine": SimpleNamespace(ai_learning_engine=mock.MagicMock()),
                "quant_ml_engine": SimpleNamespace(quant_ml=mock.MagicMock()),
            },
        )
        stubs.start()
        self.addCleanup(stubs.stop)

    @staticmethod
    def _signal(**overrides):
        """Señal larga de market making: entrada 0.60, objetivo 0.62, stop 0.58."""
        signal = {
            "signal_id": "sig_test",
            "dedupe_key": "MM:M1:Yes:BOTH",
            "strategy": "A: Market Making",
            "strategy_code": "MM",
            "token": "Yes",
            "side": "BOTH",
            "confidence": 80.0,
            "entry_price": "0.6000",
            "target_price": "0.6200",
            "stop_loss": "0.5800",
            "edge": 0.02,
            "urgency": "HIGH",  # urgencia alta = taker: fill determinista contra el libro
        }
        signal.update(overrides)
        return signal

    @staticmethod
    def _open_trade(
        trade_id: str,
        strategy_code: str = "S20",
        side: str = "BOTH",
        entry: float = 0.50,
        shares: float = 100.0,
        hours_open: float = 0.0,
        record_scope: str = PAPER_RECORD_SCOPE,
        status: str = "OPEN",
    ):
        """Trade abierto con la geometría que deja la entrada ya reescalada."""
        long_position = side in ("BOTH", "BUY", "BUY_BUNDLE")
        return {
            "trade_id": trade_id,
            "signal_id": "sig_" + trade_id,
            "opened_at": _iso(hours_open),
            "closed_at": None,
            "strategy": "Test Strategy",
            "strategy_code": strategy_code,
            "market_id": trade_id.split(":")[1] if ":" in trade_id else "M1",
            "market_question": "Will event occur?",
            "token": "Yes",
            "side": side,
            "entry_price": entry,
            "entry_fee_usd": 0.10,
            "signal_entry_price": entry,
            "entry_drift": 0.0,
            "record_scope": record_scope,
            "current_price": entry,
            "target_price": round(entry + 0.10, 4) if long_position else round(entry - 0.10, 4),
            "stop_loss": round(entry - 0.05, 4) if long_position else round(entry + 0.05, 4),
            "initial_stop_loss": round(entry - 0.05, 4) if long_position else round(entry + 0.05, 4),
            "peak_price": entry,
            "break_even_active": False,
            "trailing_stop_active": False,
            "trailing_stop_price": None,
            "shares": shares,
            "position_size_usd": round(shares * entry, 2),
            "confidence": 80.0,
            "status": status,
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "realized_pnl_usd": 0.0,
            "realized_pnl_pct": 0.0,
            "close_reason": None,
        }


class EntryAnchorTests(HarnessStubMixin, unittest.TestCase):
    """B1: el riesgo se ancla al precio real de ejecución, no al de la señal."""

    def setUp(self):
        self._patch_engines()
        self.engine = PaperTradingEngine(
            storage_path=Path(tempfile.mkdtemp()) / "paper_trades.json",
            budget_mode="per_strategy",
        )
        self.engine.trades.clear()

    def test_geometry_invariant_covers_both_sides(self):
        # Largo: stop debajo, objetivo encima.
        self.assertTrue(entry_geometry_valid(0.50, 0.45, 0.55, "BOTH"))
        self.assertTrue(entry_geometry_valid(0.50, 0.45, 0.55, "BUY"))
        # Corto: al revés.
        self.assertTrue(entry_geometry_valid(0.50, 0.55, 0.45, "SELL"))
        self.assertTrue(entry_geometry_valid(0.50, 0.55, 0.45, "SELL_BUNDLE"))
        # Geometría invertida: el stop ya está cruzado al abrir.
        self.assertFalse(entry_geometry_valid(0.50, 0.55, 0.52, "BOTH"))
        self.assertFalse(entry_geometry_valid(0.50, 0.45, 0.55, "SELL"))
        # Precios degenerados no describen ninguna operación.
        self.assertFalse(entry_geometry_valid(0.0, 0.45, 0.55, "BOTH"))
        self.assertFalse(entry_geometry_valid(0.50, 0.0, 0.55, "BOTH"))
        print("[TEST Anclaje] invariante de geometría correcta en los 4 cuadrantes")

    def test_stop_and_target_follow_the_fill_price(self):
        """El libro bajó entre la señal (0.60) y la orden: se entra a 0.56."""
        signal = self._signal()
        market = FakeMarket(bid=0.55, ask=0.56)
        trade = self.engine.evaluate_and_record_signal(signal, market)

        self.assertIsNotNone(trade, "la señal debe operar: el desvío (0.04) entra en la tolerancia")
        self.assertAlmostEqual(trade["entry_price"], 0.56, places=4)
        # La señal pedía un stop de 0.58 y la entrada real es 0.56: sin reescalar, el
        # stop quedaba POR ENCIMA de la entrada y cerraba en el ciclo siguiente.
        self.assertLess(trade["stop_loss"], trade["entry_price"])
        self.assertGreater(trade["target_price"], trade["entry_price"])
        self.assertTrue(
            entry_geometry_valid(
                trade["entry_price"], trade["stop_loss"], trade["target_price"], trade["side"]
            )
        )
        self.assertEqual(trade["signal_entry_price"], 0.60)
        self.assertAlmostEqual(trade["entry_drift"], 0.04, places=4)
        self.assertAlmostEqual(signal["entry_anchor_scale"], 0.9333, places=4)
        self.assertEqual(trade["record_scope"], PAPER_RECORD_SCOPE)
        # El pico arranca en la ejecución: si arrancara en la señal (0.60) el
        # break-even se activaba en el primer ciclo.
        self.assertAlmostEqual(trade["peak_price"], 0.56, places=4)

        # Y sobrevive el primer ciclo de precios con el mercado donde entró.
        self.engine.update_live_prices(FakeRegistry(market))
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNone(trade["close_reason"])
        print(
            f"[TEST Anclaje] señal 0.60/stop 0.58 -> fill {trade['entry_price']} y "
            f"stop {trade['stop_loss']} (debajo): sigue OPEN tras el primer ciclo"
        )

    def test_mirror_token_signal_is_rejected_by_entry_drift(self):
        """La señal valora el outcome contrario: el mercado cotiza a 0.68/0.70.

        Es el caso de S20 y S23: la señal emitía una entrada a 0.30 (precio del otro
        lado de 0.5) y el libro del token que se iba a operar estaba a 0.70. Antes se
        registraba la entrada a 0.70 con el stop de la señal y moría al instante.
        """
        signal = self._signal(
            dedupe_key="MR:M1:Yes:SELL",
            strategy="G: Mean Reversion",
            strategy_code="MR",
            side="SELL",
            entry_price="0.3000",
            target_price="0.2800",
            stop_loss="0.3200",
        )
        market = FakeMarket(bid=0.68, ask=0.70)
        trade = self.engine.evaluate_and_record_signal(signal, market)

        self.assertIsNone(trade, "no se puede registrar una señal que cotiza el outcome contrario")
        self.assertIn("desvio_entrada", signal.get("execution_skipped", ""))
        self.assertEqual(self.engine.trades, {})
        # La geometría reescalada sería formalmente válida (target 0.6347 < 0.68 <
        # stop 0.7253), así que el filtro que decide aquí es el desvío: la tesis de
        # la señal ya no describe este mercado.
        self.assertTrue(entry_geometry_valid(0.68, 0.7253, 0.6347, "SELL"))
        print(
            f"[TEST Anclaje] señal corta a 0.30 contra libro 0.68/0.70 -> rechazada "
            f"({signal['execution_skipped']})"
        )

    def test_invalid_geometry_is_never_registered(self):
        """Con desvío cero no hay reescalado: el stop invertido se rechaza igual."""
        signal = self._signal(
            entry_price="0.5000",
            target_price="0.5200",
            stop_loss="0.5500",  # stop por ENCIMA de la entrada larga
        )
        market = FakeMarket(bid=0.50, ask=0.50)
        trade = self.engine.evaluate_and_record_signal(signal, market)

        self.assertIsNone(trade)
        self.assertIn("geometria_invalida", signal.get("execution_skipped", ""))
        self.assertEqual(self.engine.trades, {})
        print(
            f"[TEST Anclaje] señal con stop 0.55 sobre entrada 0.50 -> rechazada "
            f"({signal['execution_skipped']})"
        )


class ZombiePositionTests(HarnessStubMixin, unittest.TestCase):
    """B2: una posición sin mercado vivo se cierra y deja de ocupar cupo."""

    def setUp(self):
        self._patch_engines()
        self.engine = PaperTradingEngine(
            storage_path=Path(tempfile.mkdtemp()) / "paper_trades.json",
            budget_mode="per_strategy",
        )
        self.engine.trades.clear()

    def test_stale_position_is_closed_and_marked_zombie(self):
        trade = self._open_trade("S20:M1:Yes:BOTH", hours_open=12.0)
        self.engine.trades[trade["trade_id"]] = trade

        self.engine.update_live_prices(MissingRegistry())

        self.assertIn(trade["status"], ("WON", "LOST"))
        self.assertEqual(trade["record_scope"], PAPER_ZOMBIE_SCOPE)
        self.assertIn("mercado sin cotización viva", trade["close_reason"])
        self.assertGreater(trade["exit_fee_usd"], 0.0)
        # No se reclama ningún pago por resolución: se valora al último precio,
        # pagando la comisión de salida, así que el cierre es una pérdida pequeña.
        self.assertLess(trade["realized_pnl_usd"], 0.0)

        summary = self.engine.get_summary()
        # El cierre del zombi lo produjo el harness (no la estrategia): fuera de la
        # estadística, pero visible para auditoría.
        self.assertEqual(summary["total_trades"], 0)
        self.assertEqual(summary["win_rate_pct"], 0.0)
        self.assertEqual(summary["scope"]["legacy_closed"], 1)
        self.assertEqual(summary["scope"]["legacy_open"], 0)
        self.assertEqual(len(summary["all_trades"]), 1)
        print(
            f"[TEST Zombi] 12h sin mercado -> cierre forzado al último precio "
            f"({trade['realized_pnl_usd']:+.2f} USD) y fuera del win rate"
        )

    def test_fresh_position_survives_silent_cycles(self):
        trade = self._open_trade("S20:M1:Yes:BOTH", hours_open=0.05)
        self.engine.trades[trade["trade_id"]] = trade

        self.engine.update_live_prices(MissingRegistry())
        self.assertEqual(trade["status"], "OPEN", "un ciclo sin mercado no cierra la posición")
        self.assertIn("stale_since", trade)

        # El reloj del zombi arranca en el primer ciclo silencioso: con 7h acumuladas
        # (por encima de PAPER_ZOMBIE_HOURS) sí se cierra.
        trade["stale_since"] = _iso(7.0)
        self.engine.update_live_prices(MissingRegistry())
        self.assertIn(trade["status"], ("WON", "LOST"))
        self.assertEqual(trade["record_scope"], PAPER_ZOMBIE_SCOPE)
        print(
            f"[TEST Zombi] a los 5 minutos sigue OPEN; con 7h (> {PAPER_ZOMBIE_HOURS}h) "
            "se cierra y libera cupo"
        )

    def test_frozen_realistic_tracker_unfreezes_after_zombie_cleanup(self):
        realistic = PaperTradingEngine(
            storage_path=Path(tempfile.mkdtemp()) / "paper_trades.json",
            budget_mode="global",
        )
        realistic.trades.clear()
        for index in range(12):
            trade = self._open_trade(f"S20:M{index}:Yes:BOTH", hours_open=23.0)
            realistic.trades[trade["trade_id"]] = trade

        def _new_signal():
            return self._signal(
                dedupe_key="S10:M1:Yes:BOTH",
                strategy="B: Value",
                strategy_code="S10",
                entry_price="0.5000",
                target_price="0.5200",
                stop_loss="0.4800",
            )

        # 12/12 posiciones abiertas y ningún mercado que las cierre: el tracker
        # realista no podía aceptar nada nuevo.
        blocked = _new_signal()
        self.assertIsNone(
            realistic.evaluate_and_record_signal(blocked, FakeMarket(bid=0.49, ask=0.50))
        )
        self.assertEqual(blocked.get("capital_skipped"), "max_positions")

        realistic.update_live_prices(MissingRegistry())
        zombies = [
            t for t in realistic.trades.values() if t.get("record_scope") == PAPER_ZOMBIE_SCOPE
        ]
        self.assertEqual(len(zombies), 12)

        trade = realistic.evaluate_and_record_signal(
            _new_signal(), FakeMarket(bid=0.49, ask=0.50)
        )
        self.assertIsNotNone(trade, "tras cerrar los zombis el tracker vuelve a operar")
        summary = realistic.get_summary()
        self.assertEqual(summary["open_trades_count"], 1)
        self.assertEqual(summary["scope"]["legacy_closed"], 12)
        print(
            f"[TEST Zombi] tracker realista congelado en 12/12 -> limpia y acepta "
            f"{trade['strategy_code']} a {trade['entry_price']}"
        )


class RecordScopeTests(HarnessStubMixin, unittest.TestCase):
    """B3: el registro anterior al harness se conserva pero no decide nada."""

    def setUp(self):
        self._patch_engines()

    def _engine(self):
        engine = PaperTradingEngine(
            storage_path=Path(tempfile.mkdtemp()) / "paper_trades.json",
            budget_mode="per_strategy",
        )
        engine.trades.clear()
        return engine

    @staticmethod
    def _closed(trade_id, status, entry, exit_price, pnl, scope, opened_hours=1.0):
        return {
            "trade_id": trade_id,
            "strategy_code": "MM",
            "market_id": "M1",
            "token": "Yes",
            "side": "BOTH",
            "entry_price": entry,
            "exit_price": exit_price,
            "realized_pnl_usd": pnl,
            "record_scope": scope,
            "confidence": 80.0,
            "opened_at": _iso(opened_hours + 1.0),
            "closed_at": _iso(opened_hours),
            "status": status,
        }

    def _contaminated_mm_history(self):
        """El registro de MM: 6/66 cierres LOST con la salida por encima.

        Son los cierres que el bug de lado generó (valoraban la pata contraria) y que
        hacían que MM apareciera como SOSPECHOSA con 359/1220 incoherencias.
        """
        trades = [
            self._closed(f"MM:legacyL{i}", "LOST", 0.50, 0.60, 5.0, PAPER_LEGACY_SCOPE)
            for i in range(6)
        ]
        trades += [
            self._closed(f"MM:legacyW{i}", "WON", 0.50, 0.60, 5.0, PAPER_LEGACY_SCOPE)
            for i in range(30)
        ]
        trades += [
            self._closed(f"MM:legacyX{i}", "LOST", 0.50, 0.45, -5.0, PAPER_LEGACY_SCOPE)
            for i in range(30)
        ]
        return trades

    def _clean_mm_history(self):
        trades = [
            self._closed(f"MM:cleanW{i}", "WON", 0.50, 0.60, 5.0, PAPER_RECORD_SCOPE)
            for i in range(4)
        ]
        trades += [
            self._closed(f"MM:cleanX{i}", "LOST", 0.50, 0.45, -5.0, PAPER_RECORD_SCOPE)
            for i in range(6)
        ]
        return trades

    def test_load_marks_legacy_trades_and_excludes_them(self):
        path = Path(tempfile.mkdtemp()) / "paper_trades.json"
        payload = {
            "trades": {
                "MM:M1:Yes:BOTH": self._closed(
                    "MM:M1:Yes:BOTH", "LOST", 0.50, 0.45, -10.0, None
                ),
                "SM:M2:Yes:BOTH": self._open_trade(
                    "SM:M2:Yes:BOTH", strategy_code="SM", record_scope=None
                ),
                "MM:M3:Yes:BOTH": self._closed(
                    "MM:M3:Yes:BOTH", "WON", 0.50, 0.60, 5.0, PAPER_RECORD_SCOPE
                ),
            },
            "updated_at": _iso(0.0),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        engine = PaperTradingEngine(storage_path=path, budget_mode="per_strategy")

        # La migración alcanza todo lo escrito sin marca y respeta lo ya marcado.
        self.assertTrue(all(t.get("record_scope") for t in engine.trades.values()))
        self.assertEqual(engine.trades["MM:M1:Yes:BOTH"]["record_scope"], PAPER_LEGACY_SCOPE)
        self.assertEqual(engine.trades["SM:M2:Yes:BOTH"]["record_scope"], PAPER_LEGACY_SCOPE)
        self.assertEqual(engine.trades["MM:M3:Yes:BOTH"]["record_scope"], PAPER_RECORD_SCOPE)

        # Sólo cuenta el cierre del alcance vigente; la posición abierta heredada
        # sigue siendo inventario (capital comprometido y flotante vivo).
        self.assertEqual(
            {t["trade_id"] for t in engine.accounted_trades()},
            {"MM:M3:Yes:BOTH", "SM:M2:Yes:BOTH"},
        )
        self.assertEqual(
            [t["trade_id"] for t in closed_trades_of_scope(engine)], ["MM:M3:Yes:BOTH"]
        )

        summary = engine.get_summary()
        self.assertEqual(summary["total_trades"], 2)
        self.assertEqual(summary["open_trades_count"], 1)
        self.assertEqual(summary["winning_trades_count"], 1)
        self.assertEqual(summary["win_rate_pct"], 100.0)
        self.assertEqual(summary["total_realized_pnl"], 5.0)
        scope = summary["scope"]
        self.assertEqual(scope["legacy_trades"], 2)
        self.assertEqual(scope["legacy_closed"], 1)
        self.assertEqual(scope["legacy_open"], 1)
        self.assertEqual(scope["legacy_pnl_usd"], -10.0)
        print(
            f"[TEST Registro] migración: {scope['legacy_closed']} cierre legado "
            f"({scope['legacy_pnl_usd']:+.2f} USD) fuera del win rate {summary['win_rate_pct']}%"
        )

    def test_promotion_ignores_legacy_registers(self):
        engine = self._engine()
        legacy = self._contaminated_mm_history()
        for trade in legacy + self._clean_mm_history():
            engine.trades[trade["trade_id"]] = trade

        board = build_promotion_board(engine)
        row = next(r for r in board["rows"] if r["code"] == "MM")
        self.assertEqual(row["closed_trades"], 10, "sólo el alcance vigente entra en el veredicto")
        self.assertNotEqual(row["state"], "SOSPECHOSA")
        self.assertEqual(row["state"], "CANDIDATA")
        # La familia de comparaciones no se relaja: cuenta todo lo probado.
        self.assertEqual(board["strategies_tested"], 1)

        # El detector de incoherencias sigue vivo: con el mismo registro sin filtrar
        # por alcance, el tablero marca SOSPECHOSA (es lo que pasaba con MM).
        closed_legacy = [t for t in legacy if t.get("status") in ("WON", "LOST")]
        control = evaluate_strategy("MM", closed_legacy, 1)
        self.assertEqual(control["state"], "SOSPECHOSA")
        self.assertFalse(control["integrity_ok"])
        print(
            f"[TEST Registro] MM: {len(closed_legacy)} cierres legados -> {control['state']}; "
            f"con el alcance vigente -> {row['state']} ({row['closed_trades']} trades)"
        )

    def test_ranking_ignores_legacy_registers(self):
        engine = self._engine()
        for trade in self._contaminated_mm_history() + self._clean_mm_history():
            engine.trades[trade["trade_id"]] = trade

        ai_stub = SimpleNamespace(
            get_status=lambda: {"strategy_metrics": {}, "reflections": []},
            strategy_weights={},
        )
        ml_stub = SimpleNamespace(get_status=lambda: {"bandit_strategies": {}})
        ranking = build_strategy_ranking(engine, ai_stub, ml_stub, mode="realistic")

        self.assertEqual(ranking["total_closed_trades"], 10)
        row = next(r for r in ranking["strategies"] if r["code"] == "MM")
        self.assertEqual(row["closed_trades_count"], 10)
        # Los 66 cierres legados daban MM un 40% de acierto y +30 USD de PnL falso:
        # el ranking limpio reporta las 10 operaciones del harness y su pérdida real.
        self.assertEqual(row["realized_pnl_usd"], -10.0)
        print(
            f"[TEST Registro] ranking MM con {row['closed_trades_count']} cierres limpios: "
            f"{row['realized_pnl_usd']:+.2f} USD (legado descartado)"
        )


if __name__ == "__main__":
    unittest.main()
