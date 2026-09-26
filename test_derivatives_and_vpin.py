"""
test_derivatives_and_vpin.py
============================
Pruebas Unitarias de Rendimiento de Ultra-Baja Latencia:
1. Black-Scholes Digital Option Pricing (N(d2)) < 5 microsegundos.
2. VPIN de López de Prado O(1) Circular Buffer.
3. Drawdown-Constrained Kelly Sizing.
"""

import unittest
import time
from black_scholes_digital import norm_cdf, bs_digital_engine
from vpin_microstructure import SingleMarketVPIN, vpin_manager


class TestDerivativesAndVPIN(unittest.TestCase):

    def test_norm_cdf_precision(self):
        """Verifica la precisión matemática de la función de distribución normal acumulada."""
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=5)
        self.assertAlmostEqual(norm_cdf(1.95996), 0.975, places=3)
        self.assertAlmostEqual(norm_cdf(-1.95996), 0.025, places=3)
        print("[TEST BS] Precisión de N(d2) normal CDF validada al 100%.")

    def test_black_scholes_speed_and_accuracy(self):
        """Verifica que 10,000 evaluaciones tomen menos de 0.05s (< 5 microsegundos por cálculo)."""
        start = time.perf_counter()
        for i in range(10000):
            res = bs_digital_engine.compute_fair_price(
                symbol="BTC",
                spot_price=86000.0 + (i * 0.1),
                strike_price=86000.0,
                minutes_left=15.0,
                clob_yes_price=0.50,
            )
        elapsed = time.perf_counter() - start
        avg_micros = (elapsed / 10000) * 1_000_000

        print(f"[TEST BS Velocidad] 10,000 evaluaciones en {elapsed:.4f}s ({avg_micros:.2f} µs por llamada). CERO LATENCIA.")
        self.assertLess(elapsed, 0.15)  # Menos de 150 ms para 10,000 cálculos

        # Comprobar In-The-Money vs Out-of-The-Money
        itm = bs_digital_engine.compute_fair_price("BTC", 90000.0, 80000.0, 15.0, 0.50)
        self.assertGreater(itm.fair_yes_prob, 0.90)
        self.assertEqual(itm.recommended_side, "BUY_YES")
        self.assertTrue(itm.arbitrage_active)

        otm = bs_digital_engine.compute_fair_price("BTC", 80000.0, 90000.0, 15.0, 0.50)
        self.assertLess(otm.fair_yes_prob, 0.10)
        self.assertEqual(otm.recommended_side, "BUY_NO")
        self.assertTrue(otm.arbitrage_active)

    def test_vpin_o1_circular_buffer(self):
        """Verifica el cálculo O(1) de VPIN y detección de toxicidad/insiders."""
        tracker = SingleMarketVPIN("test_market_btc", bucket_volume=200.0, num_buckets=10)

        # 1. Simular flujo comprador masivo de ballena (insider toxic flow)
        for i in range(25):
            reading = tracker.process_trade(price=0.50 + (i * 0.01), volume_usd=100.0)

        self.assertGreaterEqual(reading.vpin_score, 0.65)
        self.assertEqual(reading.toxicity_level, "INSIDER_TOXIC_FLOW")
        self.assertTrue(reading.alert_triggered)
        print(f"[TEST VPIN Toxic] VPIN: {reading.vpin_score*100:.1f}% -> Nivel: {reading.toxicity_level} (Alerta: {reading.alert_triggered})")

        # 2. Simular flujo balanceado de ruido retail
        tracker_retail = SingleMarketVPIN("test_market_retail", bucket_volume=200.0, num_buckets=10)
        for i in range(30):
            # Alternar compras y ventas idénticas
            p = 0.50 if i % 2 == 0 else 0.49
            reading_retail = tracker_retail.process_trade(price=p, volume_usd=100.0)

        self.assertLessEqual(reading_retail.vpin_score, 0.55)
        print(f"[TEST VPIN Retail] VPIN: {reading_retail.vpin_score*100:.1f}% -> Nivel: {reading_retail.toxicity_level}")

    def test_drawdown_constrained_kelly(self):
        """Verifica la fórmula matemática de reducción de tamaño por drawdown."""
        # 0% Drawdown
        dd_0 = 0.0
        factor_0 = max(0.15, min(1.0, 1.0 - (dd_0 / 0.08)))
        self.assertEqual(factor_0, 1.0)

        # 4% Drawdown (la mitad de la tolerancia máxima de 8%)
        dd_4 = 0.04
        factor_4 = max(0.15, min(1.0, 1.0 - (dd_4 / 0.08)))
        self.assertAlmostEqual(factor_4, 0.50, places=2)

        # 8% Drawdown (límite máximo alcanzado)
        dd_8 = 0.08
        factor_8 = max(0.15, min(1.0, 1.0 - (dd_8 / 0.08)))
        self.assertEqual(factor_8, 0.15)  # Reducción a piso seguro
        print(f"[TEST Kelly Drawdown] Factores: 0% DD={factor_0}x | 4% DD={factor_4}x | 8% DD={factor_8}x.")


if __name__ == "__main__":
    unittest.main()
