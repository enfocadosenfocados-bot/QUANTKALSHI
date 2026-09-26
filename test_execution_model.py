"""Tests del modelo de ejecución realista (execution_model.py).

Cubre las tres vías por las que el simulador anterior regalaba dinero:
comisión cero, slippage en puntos básicos y fill maker instantáneo. Si estos
tests pasan, las ganadoras del paper trading son comparables con lo que pasaría
en real.
"""
import random
import unittest

from execution_model import (
    ExecutionModel,
    kalshi_trading_fee,
    round_to_tick,
    simulate_maker_fill,
    simulate_taker_fill,
)

# Tabla medida contra la fórmula documentada de Kalshi:
# fee = ceil_a_centavo(0.07 * contratos * P * (1-P)).
FEE_TABLE = {
    0.01: 0.01, 0.02: 0.01, 0.03: 0.01, 0.05: 0.01, 0.10: 0.01,
    0.20: 0.02, 0.35: 0.02, 0.50: 0.02, 0.65: 0.02, 0.80: 0.02,
    0.90: 0.01, 0.97: 0.01, 0.99: 0.01,
}


class TestFeeMath(unittest.TestCase):
    def test_fee_per_contract_matches_measured_table(self):
        for price, expected in FEE_TABLE.items():
            with self.subTest(price=price):
                self.assertAlmostEqual(kalshi_trading_fee(1, price), expected, places=9)
        print(f"[TEST Fee] {len(FEE_TABLE)}/{len(FEE_TABLE)} precios coinciden con la tabla medida")

    def test_cheap_contracts_pay_enormous_relative_fee(self):
        """El ceil a centavo impide que la comision baje de 1 centavo."""
        cheap = kalshi_trading_fee(1, 0.03) / 0.03
        rich = kalshi_trading_fee(1, 0.50) / 0.50
        self.assertGreater(cheap, 0.30)
        self.assertLess(rich, 0.05)
        print(f"[TEST Fee] 0.03 -> {cheap * 100:.0f}% del precio | 0.50 -> {rich * 100:.0f}%")

    def test_fee_scales_with_contracts(self):
        self.assertAlmostEqual(kalshi_trading_fee(100, 0.50), 1.75, places=9)
        self.assertGreater(kalshi_trading_fee(100, 0.50), kalshi_trading_fee(10, 0.50))

    def test_no_fee_at_extremes(self):
        self.assertEqual(kalshi_trading_fee(10, 1.0), 0.0)
        self.assertEqual(kalshi_trading_fee(10, 0.0), 0.0)
        self.assertEqual(kalshi_trading_fee(0, 0.5), 0.0)


class TestTickRounding(unittest.TestCase):
    def test_a_tick_is_14_percent_on_a_cheap_contract(self):
        """5 bps sobre 0.035 no representa nada; el siguiente tick legal es +14%."""
        self.assertEqual(round_to_tick(0.0350, 0.01, "up"), 0.04)
        self.assertEqual(round_to_tick(0.0350, 0.01, "down"), 0.03)
        legacy = 0.0350 * (1 + 5 / 10000)
        self.assertLess(legacy, 0.0351)
        print("[TEST Tick] 0.0350 -> up 0.04 (+14%) | down 0.03 | legacy 5bps = 0.035018")

    def test_clamped_to_operable_range(self):
        self.assertEqual(round_to_tick(1.5, 0.01, "up"), 0.99)
        self.assertEqual(round_to_tick(0.0, 0.01, "down"), 0.01)


class TestTakerFill(unittest.TestCase):
    def setUp(self):
        self.bids = [(0.33, 100.0), (0.32, 100.0)]
        self.asks = [(0.35, 100.0), (0.36, 100.0), (0.40, 500.0)]

    def test_walks_levels_and_respects_limit(self):
        fill = simulate_taker_fill(self.bids, self.asks, "BUY", 0.36, 250.0, depth_safety=1.0)
        self.assertTrue(fill.filled)
        self.assertEqual(fill.contracts, 200.0)
        self.assertAlmostEqual(fill.avg_price, 0.355, places=4)
        self.assertEqual(fill.levels_consumed, 2)
        self.assertTrue(fill.partial)
        self.assertGreater(fill.fee_usd, 0.0)
        print(f"[TEST Taker] 250@0.36 -> {fill.contracts:.0f} a {fill.avg_price:.4f} (fee ${fill.fee_usd:.2f})")

    def test_no_fill_when_price_not_reachable(self):
        fill = simulate_taker_fill(self.bids, self.asks, "BUY", 0.30, 50.0, depth_safety=1.0)
        self.assertFalse(fill.filled)
        self.assertIn("sin liquidez", fill.reason)
        print("[TEST Taker] limite inalcanzable -> sin fill (no se inventa el precio)")

    def test_depth_caps_size(self):
        fill = simulate_taker_fill([], [(0.50, 2.0)], "BUY", 0.60, 100.0, depth_safety=0.5)
        self.assertTrue(fill.filled)
        self.assertEqual(fill.contracts, 1.0)
        self.assertTrue(fill.partial)
        print(f"[TEST Taker] 100 contra profundidad 2 -> solo {fill.contracts:.1f} (parcial)")

    def test_rejects_below_min_order_size(self):
        fill = simulate_taker_fill(self.bids, self.asks, "BUY", 0.40, 0.148, min_order_size=1.0)
        self.assertFalse(fill.filled)
        self.assertIn("mínimo", fill.reason)
        print("[TEST Taker] 0.148 contratos rechazado por min_order_size")

    def test_sell_crosses_bids(self):
        # Con límite 0.32 sí puede barrer el nivel de 0.33 y el de 0.32; el VWAP
        # refleja el coste real de salir de una posición grande.
        fill = simulate_taker_fill(self.bids, self.asks, "SELL", 0.32, 150.0, depth_safety=1.0)
        self.assertTrue(fill.filled)
        self.assertEqual(fill.contracts, 150.0)
        self.assertEqual(fill.levels_consumed, 2)
        self.assertAlmostEqual(fill.avg_price, (100 * 0.33 + 50 * 0.32) / 150, places=4)
        print(f"[TEST Taker SELL] 150 con limite 0.32 -> VWAP {fill.avg_price:.4f} en 2 niveles")

    def test_sell_cannot_reach_below_limit(self):
        # Con límite 0.33 no se puede vender a 0.32: solo llena el primer nivel.
        fill = simulate_taker_fill(self.bids, self.asks, "SELL", 0.33, 150.0, depth_safety=1.0)
        self.assertEqual(fill.contracts, 100.0)
        self.assertTrue(fill.partial)
        print("[TEST Taker SELL] limite 0.33 no alcanza 0.32 -> solo 100 contratos (parcial)")


class TestMakerFill(unittest.TestCase):
    def setUp(self):
        self.bids = [(0.33, 100.0)]
        self.asks = [(0.35, 100.0)]

    def test_no_instant_fill_inside_the_spread(self):
        """La correccion clave: el maker ya no llena al toque."""
        fill = simulate_maker_fill(
            self.bids, self.asks, "BUY", 0.34, 100.0,
            fill_probability=0.0, rng=random.Random(7),
        )
        self.assertFalse(fill.filled)
        self.assertTrue(fill.is_maker)
        self.assertIn("maker sin fill", fill.reason)
        print("[TEST Maker] dentro del spread -> SIN fill (antes llenaba al instante)")

    def test_fills_when_book_crosses(self):
        fill = simulate_maker_fill(
            self.bids, self.asks, "BUY", 0.36, 100.0,
            fill_probability=0.0, rng=random.Random(7),
        )
        self.assertTrue(fill.filled)
        self.assertEqual(fill.avg_price, 0.36)
        self.assertEqual(fill.fee_usd, 0.0)
        print("[TEST Maker] libro cruzado -> fill a 0.36 con comision 0 (maker)")

    def test_probability_controls_fill_rate(self):
        rng = random.Random(1234)
        fills = sum(
            1 for _ in range(400)
            if simulate_maker_fill(
                self.bids, self.asks, "BUY", 0.34, 100.0,
                fill_probability=0.35, rng=rng,
            ).filled
        )
        self.assertGreater(fills, 90)
        self.assertLess(fills, 200)
        print(f"[TEST Maker] 400 intentos con p=0.35 -> {fills} fills ({fills / 4:.0f}%)")


class TestExecutionModelDeterminism(unittest.TestCase):
    class _Market:
        market_id = "M1"
        tick_size = 0.01
        min_order_size = 1
        best_bid = {"Yes": 0.34}
        best_ask = {"Yes": 0.35}
        order_book = {
            "Yes": {
                "bids": [{"price": 0.34, "size": 500.0}],
                "asks": [{"price": 0.35, "size": 500.0}],
            }
        }

    def test_same_book_and_seed_give_same_result(self):
        """Determinismo: sin esto el track record no es reproducible."""
        first = ExecutionModel(rng=random.Random(42))
        second = ExecutionModel(rng=random.Random(42))
        results = [
            (
                model.entry_fill(
                    self._Market(), "Yes", "BUY", 0.345, 100.0, is_maker=True
                ).to_dict(),
                model.entry_fill(
                    self._Market(), "Yes", "BUY", 0.36, 100.0, is_maker=False
                ).to_dict(),
            )
            for model in (first, second)
        ]
        self.assertEqual(results[0], results[1])
        print("[TEST Determinismo] mismo libro + misma semilla -> resultado identico")

    def test_exit_fill_charges_fee_and_slips(self):
        model = ExecutionModel(rng=random.Random(3))
        fill = model.exit_fill(self._Market(), "Yes", "SELL", 0.01, 400.0, is_maker=False)
        self.assertTrue(fill.filled)
        self.assertGreater(fill.fee_usd, 0.0)
        self.assertLessEqual(fill.avg_price, 0.34)
        print(
            f"[TEST Exit] venta de 400 -> {fill.contracts:.0f} a "
            f"{fill.avg_price:.4f} (fee ${fill.fee_usd:.2f})"
        )

    def test_legacy_model_is_reachable_for_comparison(self):
        legacy = ExecutionModel(model="legacy")
        self.assertFalse(legacy.is_realistic)
        fill = legacy.entry_fill(self._Market(), "Yes", "BUY", 0.35, 100.0, is_maker=True)
        self.assertTrue(fill.filled)
        self.assertEqual(fill.fee_usd, 0.0)
        print("[TEST Legacy] modelo anterior accesible solo para comparar (comision 0)")

    def test_budget_cap_includes_fee(self):
        """El coste total (nocional + comision) nunca supera el presupuesto."""
        model = ExecutionModel(rng=random.Random(11))
        fill = model.entry_fill_with_budget(
            self._Market(), "Yes", "BUY", 0.35, 50.0, is_maker=False
        )
        self.assertTrue(fill.filled)
        self.assertLessEqual(fill.notional_usd + fill.fee_usd, 50.0 + 1e-9)
        print(
            f"[TEST Budget] presupuesto $50 -> {fill.contracts:.0f} contratos, "
            f"coste ${fill.notional_usd + fill.fee_usd:.2f}"
        )

    def test_synthetic_book_is_flagged(self):
        """Sin libro completo se usa una quote, pero el trade queda marcado."""
        class NoBook:
            market_id = "M2"
            tick_size = 0.01
            min_order_size = 1
            best_bid = {"Yes": 0.40}
            best_ask = {"Yes": 0.42}
            order_book = {}

        fill = ExecutionModel(rng=random.Random(5)).entry_fill(
            NoBook(), "Yes", "BUY", 0.42, 10.0, is_maker=False
        )
        self.assertTrue(fill.filled)
        self.assertTrue(fill.book_synthetic)
        print("[TEST Book] libro ausente -> quote de respaldo marcada como sintetica")


if __name__ == "__main__":
    unittest.main()
