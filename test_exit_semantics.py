"""Tests de la convención de lado (largo/corto) y de las salidas del paper tracker.

El bug que fijan estos tests: el market making registra ``side="BOTH"`` y el
tracker lo trataba como corto. Consecuencias medidas sobre el historial real:

  * el take profit de esas posiciones se evaluaba al revés (``exit_mark <= target``)
    y el stop se disparaba al instante, porque su stop está POR DEBAJO de la entrada;
  * la salida se valoraba al ask en vez de al bid;
  * 39 cierres de MM quedaron etiquetados ``LOST`` con el precio a favor y PnL positivo.

La convención (``position_side.py``) es que BOTH y BUY_BUNDLE son largos, igual que
en strategies.py (target = entry + 0.02), live_execution.py (side="bid") y el propio
_close_trade (PnL como salida - entrada).
"""
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paper_tracker import PaperTradingEngine
from position_side import entry_order_side, exit_order_side, is_long_side
from strategy_promotion import evaluate_strategy
from strategy_ranking import breakeven_for_trade


class FakeMarket:
    """Mercado con libro real y quotes coherentes para ambos lados."""

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


class SideConventionTests(unittest.TestCase):
    """La tabla de lados es una sola y no se puede reinterpretar por módulo."""

    def test_both_is_long(self):
        self.assertTrue(is_long_side("BOTH"))
        self.assertTrue(is_long_side("BUY"))
        self.assertTrue(is_long_side("BUY_BUNDLE"))
        self.assertFalse(is_long_side("SELL"))
        self.assertFalse(is_long_side("SELL_BUNDLE"))
        print("[TEST Lados] BOTH/BUY_BUNDLE = largo; SELL/SELL_BUNDLE = corto")

    def test_order_sides_are_mirrors(self):
        self.assertEqual(entry_order_side("BOTH"), "BUY")
        self.assertEqual(exit_order_side("BOTH"), "SELL")
        self.assertEqual(entry_order_side("SELL"), "SELL")
        self.assertEqual(exit_order_side("SELL"), "BUY")
        print("[TEST Lados] BOTH abre comprando y cierra vendiendo")

    def test_breakeven_of_both_is_entry_price(self):
        # Antes devolvía 1 - entry (0.40 para una entrada a 0.60), lo que regalaba
        # 20 puntos de edge al market making en el criterio de promoción.
        self.assertAlmostEqual(
            breakeven_for_trade({"side": "BOTH", "entry_price": 0.60}), 0.60, places=4
        )
        self.assertAlmostEqual(
            breakeven_for_trade({"side": "BUY", "entry_price": 0.60}), 0.60, places=4
        )
        self.assertAlmostEqual(
            breakeven_for_trade({"side": "SELL", "entry_price": 0.60}), 0.40, places=4
        )
        print("[TEST Lados] breakeven de BOTH a 0.60 = 0.60 (antes 0.40)")



class ExitSemanticsTests(unittest.TestCase):
    def setUp(self):
        # El ciclo de precios llama al post-mortem IA y al gobernador, que escriben
        # estado REAL (trade_memory.json, strategy_governor.json). Se sustituyen
        # por stubs para que el test no toque el estado del bot en marcha.
        governor = mock.MagicMock()
        governor.is_globally_paused.return_value = False
        governor.is_paused.return_value = False
        governor.get_rules.return_value = {}
        self._stubs = mock.patch.dict(
            sys.modules,
            {
                "strategy_governor": SimpleNamespace(governor=governor),
                "ai_learning_engine": SimpleNamespace(ai_learning_engine=mock.MagicMock()),
                "quant_ml_engine": SimpleNamespace(quant_ml=mock.MagicMock()),
            },
        )
        self._stubs.start()
        self.addCleanup(self._stubs.stop)

        self.engine = PaperTradingEngine(
            storage_path=Path(tempfile.mkdtemp()) / "paper_trades.json",
            budget_mode="per_strategy",
        )
        self.engine.trades.clear()

    def _open_both_trade(self, entry: float = 0.50, target: float = 0.52, stop: float = 0.48):
        trade = {
            "trade_id": "MM:M1:Yes:BOTH",
            "strategy": "A: Market Making",
            "strategy_code": "MM",
            "market_id": "M1",
            "token": "Yes",
            "side": "BOTH",
            "entry_price": entry,
            "entry_fee_usd": 0.0,
            "target_price": target,
            "stop_loss": stop,
            "initial_stop_loss": stop,
            "peak_price": entry,
            "break_even_active": False,
            "trailing_stop_active": False,
            "trailing_stop_price": None,
            "shares": 100.0,
            "position_size_usd": 50.0,
            "confidence": 80.0,
            "status": "OPEN",
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "realized_pnl_usd": 0.0,
            "realized_pnl_pct": 0.0,
            "close_reason": None,
            "closed_at": None,
        }
        self.engine.trades[trade["trade_id"]] = trade
        return trade

    def test_exit_reference_is_bid_for_long_and_ask_for_short(self):
        market = FakeMarket(bid=0.60, ask=0.62)
        self.assertAlmostEqual(self.engine._exit_reference_price(market, "Yes", "BOTH"), 0.60)
        self.assertAlmostEqual(self.engine._exit_reference_price(market, "Yes", "BUY"), 0.60)
        self.assertAlmostEqual(self.engine._exit_reference_price(market, "Yes", "SELL"), 0.62)
        print("[TEST Salida] BOTH valora la salida al bid (0.60); SELL al ask (0.62)")

    def test_flat_market_keeps_both_position_open(self):
        """Con el precio plano no hay take profit ni stop: antes cerraba al instante."""
        trade = self._open_both_trade()
        self.engine.update_live_prices(FakeRegistry(FakeMarket(bid=0.49, ask=0.51)))
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNone(trade["close_reason"])
        print("[TEST Salida] BOTH con precio plano sigue OPEN (antes cerraba en el primer ciclo)")

    def test_rising_bid_takes_profit_for_both(self):
        """El bid por encima del objetivo cierra WON con beneficio real."""
        trade = self._open_both_trade()
        self.engine.update_live_prices(FakeRegistry(FakeMarket(bid=0.53, ask=0.55)))
        self.assertEqual(trade["status"], "WON")
        self.assertGreater(trade["realized_pnl_usd"], 0.0)
        self.assertIn("Take Profit alcanzado", trade["close_reason"])
        self.assertAlmostEqual(trade["exit_price"], 0.53, places=4)
        print(
            f"[TEST Salida] BOTH con el bid a 0.53 > 0.52 -> WON "
            f"PnL ${trade['realized_pnl_usd']} (antes lo etiquetaba LOST)"
        )

    def test_falling_bid_stops_out_for_both(self):
        trade = self._open_both_trade()
        self.engine.update_live_prices(FakeRegistry(FakeMarket(bid=0.47, ask=0.49)))
        self.assertEqual(trade["status"], "LOST")
        self.assertLess(trade["realized_pnl_usd"], 0.0)
        self.assertIn("Stop Loss", trade["close_reason"])
        print(f"[TEST Salida] BOTH con el bid a 0.47 < 0.48 -> LOST ${trade['realized_pnl_usd']}")

    def test_close_trade_books_long_pnl_for_both(self):
        trade = self._open_both_trade()
        self.assertTrue(
            self.engine._close_trade(trade, FakeMarket(bid=0.60, ask=0.62), "WON", "test")
        )
        self.assertEqual(trade["exit_price"], 0.60)
        self.assertGreater(trade["realized_pnl_usd"], 0.0)
        print(
            f"[TEST PnL] BOTH cierra vendiendo al bid 0.60 -> ${trade['realized_pnl_usd']} "
            f"(comision ${trade['exit_fee_usd']})"
        )

    def test_close_trade_books_short_pnl_when_price_falls(self):
        """Un corto gana cuando el precio baja: antes se contabilizaba al revés."""
        trade = self._open_both_trade()
        trade["trade_id"] = "MR:M1:Yes:SELL"
        trade["side"] = "SELL"
        trade["target_price"] = 0.45
        trade["stop_loss"] = 0.55
        trade["initial_stop_loss"] = 0.55
        self.assertTrue(
            self.engine._close_trade(trade, FakeMarket(bid=0.28, ask=0.30), "WON", "test")
        )
        self.assertEqual(trade["exit_price"], 0.30)
        self.assertGreater(trade["realized_pnl_usd"], 0.0)
        print(
            f"[TEST PnL] Comprar de vuelta a 0.30 desde 0.50 -> ${trade['realized_pnl_usd']} "
            "positivo (antes salia negativo)"
        )

    def test_both_signal_fills_on_the_ask(self):
        """La entrada de un BOTH se ejecuta comprando (ask), no vendiendo (bid)."""
        signal = {
            "signal_id": "sig_both",
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
            "urgency": "HIGH",  # urgencia alta: cruza el spread (taker) y es determinista
        }
        # Libro realista: el 0.40/0.60 que usaba este test (spread 0.20 sobre un
        # precio de 0.60) no es un mercado operable y ahora la puerta de spread lo
        # rechaza. Lo que se comprueba sigue siendo la base del fill, no su tamaño.
        market = FakeMarket(bid=0.58, ask=0.60)
        trade = self.engine.evaluate_and_record_signal(signal, market)
        self.assertIsNotNone(trade, "la entrada taker deberia cruzar el ask 0.60")
        # Igualdad con el ask (0.60) y no con el bid (0.58): un fill al bid dejaría
        # el PnL largo regalado desde el primer tick.
        self.assertAlmostEqual(trade["entry_price"], 0.60, places=2)
        print(
            f"[TEST Entrada] BOTH entra a {trade['entry_price']} (ask) y no a "
            f"{market.best_bid['Yes']} (bid)"
        )


class IntegrityRuleTests(unittest.TestCase):
    """La regla nueva del tablero: cierres cuya etiqueta contradice el precio."""

    @staticmethod
    def _trade(status: str, entry: float, exit_price: float, pnl: float, side: str = "BOTH"):
        return {
            "strategy_code": "MM",
            "status": status,
            "side": side,
            "entry_price": entry,
            "exit_price": exit_price,
            "realized_pnl_usd": pnl,
            "closed_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        }

    def _record(self, mislabelled: int, coherent: int):
        trades = [self._trade("LOST", 0.50, 0.60, 5.0) for _ in range(mislabelled)]
        trades += [self._trade("WON", 0.50, 0.60, 5.0) for _ in range(coherent)]
        trades += [self._trade("LOST", 0.50, 0.45, -5.0) for _ in range(coherent)]
        return trades

    def test_mislabelled_mm_record_is_sospechosa(self):
        row = evaluate_strategy("MM", self._record(6, 30), 16)
        self.assertFalse(row["integrity_ok"])
        self.assertEqual(row["state"], "SOSPECHOSA")
        self.assertIn("etiqueta contraria", " ".join(row["integrity_problems"]))
        print(
            f"[TEST Integridad] 6/66 cierres LOST con salida por encima de la entrada "
            f"-> {row['state']} (no se usa su win rate {row['win_rate_pct']}%)"
        )

    def test_coherent_record_is_not_flagged(self):
        row = evaluate_strategy("ok", self._record(0, 30), 16)
        self.assertTrue(row["integrity_ok"])
        self.assertNotEqual(row["state"], "SOSPECHOSA")
        print(f"[TEST Integridad] registro coherente -> integridad OK (estado {row['state']})")

    def test_short_with_falling_price_is_coherent(self):
        """En un corto, WON con la salida por debajo de la entrada es correcto."""
        trades = [self._trade("WON", 0.50, 0.40, 10.0, side="SELL") for _ in range(15)]
        trades += [self._trade("LOST", 0.50, 0.56, -6.0, side="SELL") for _ in range(10)]
        row = evaluate_strategy("mr", trades, 16)
        self.assertTrue(row["integrity_ok"])
        print("[TEST Integridad] corto ganador por debajo de la entrada -> no se marca")


if __name__ == "__main__":
    unittest.main()
