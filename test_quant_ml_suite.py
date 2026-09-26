"""
test_quant_ml_suite.py
======================
Pruebas Unitarias Rigurosas para la Suite Cuantitativa de Machine Learning Avanzado.
"""

import unittest
from quant_ml_engine import (
    mat_inv_4x4,
    vec_dot,
    LinUCBContextualBandit,
    ConformalPredictor,
    BetaCalibrator,
    OrderFlowImbalanceEngine,
    CombinatorialArbitrageScanner,
    quant_ml,
)


class TestQuantMLSuite(unittest.TestCase):

    def test_pure_python_linear_algebra(self):
        """Verifica la inversión de matrices 4x4 por Gauss-Jordan y producto punto."""
        A = [
            [2.0, 0.0, 0.0, 0.0],
            [0.0, 4.0, 0.0, 0.0],
            [0.0, 0.0, 5.0, 0.0],
            [0.0, 0.0, 0.0, 10.0],
        ]
        A_inv = mat_inv_4x4(A)
        self.assertAlmostEqual(A_inv[0][0], 0.5)
        self.assertAlmostEqual(A_inv[1][1], 0.25)
        self.assertAlmostEqual(A_inv[2][2], 0.2)
        self.assertAlmostEqual(A_inv[3][3], 0.1)

        u = [1.0, 2.0, 3.0, 4.0]
        v = [2.0, 0.5, -1.0, 1.0]
        self.assertAlmostEqual(vec_dot(u, v), 1.0 * 2.0 + 2.0 * 0.5 - 3.0 * 1.0 + 4.0 * 1.0)
        print("[TEST LinAlg] Gauss-Jordan 4x4 y Dot Product validados al 100%.")

    def test_linucb_contextual_bandit(self):
        """Verifica la extracción de contexto, cálculo de UCB y actualización recursiva."""
        bandit = LinUCBContextualBandit(alpha=0.5)
        context = bandit.get_current_context(btc_velocity_10s=0.25, avg_spread=0.03, vol_liq_ratio=0.18, current_hour_utc=14)
        self.assertEqual(len(context), 4)

        ucb, mult = bandit.get_strategy_score_and_multiplier("S20", context)
        self.assertGreaterEqual(mult, 0.50)
        self.assertLessEqual(mult, 1.75)

        # Simular trade ganador (+1.0 reward)
        initial_trades = bandit.trade_counts["S20"]
        bandit.update_online("S20", context, reward=1.0)
        self.assertEqual(bandit.trade_counts["S20"], initial_trades + 1)

        new_ucb, new_mult = bandit.get_strategy_score_and_multiplier("S20", context)
        self.assertGreaterEqual(new_mult, mult)  # El multiplicador debe aumentar tras recompensa positiva
        print(f"[TEST LinUCB] Multiplicador S20: {mult}x -> {new_mult}x tras ganar trade.")

    def test_conformal_prediction_guarantee(self):
        """Verifica el cálculo de cuantiles conformes y el filtro estricto de admisión."""
        cp = ConformalPredictor(alpha=0.05)
        q = cp.compute_quantile()
        self.assertGreater(q, 0.0)
        self.assertLess(q, 0.30)

        # 1. Señal con ventaja masiva (CLOB a 0.40, Predicción 0.85): Debe ADMITIRSE
        res_admit = cp.evaluate_signal(predicted_prob=0.85, market_price=0.40, side="BUY")
        self.assertTrue(res_admit.is_admissible)
        self.assertGreater(res_admit.edge_pct, 10.0)
        self.assertLess(res_admit.market_price, res_admit.p_lower)

        # 2. Señal ruidosa (CLOB a 0.80, Predicción 0.82): Debe RECHAZARSE por estar dentro del intervalo conforme
        res_reject = cp.evaluate_signal(predicted_prob=0.82, market_price=0.80, side="BUY")
        self.assertFalse(res_reject.is_admissible)
        self.assertIn("Ruido estadístico", res_reject.rejection_reason)
        print(f"[TEST Conformal] Quantile q={q:.4f} | Admitted Edge={res_admit.edge_pct}% | Rejected={res_reject.rejection_reason[:45]}...")

    def test_beta_calibration(self):
        """Verifica la suavidad y monotonicidad estricta de la calibración Beta."""
        calibrator = BetaCalibrator()
        p1 = calibrator.calibrate(0.05)
        p2 = calibrator.calibrate(0.50)
        p3 = calibrator.calibrate(0.95)

        self.assertLess(p1, p2)
        self.assertLess(p2, p3)
        self.assertGreater(p1, 0.01)
        self.assertLess(p3, 0.99)
        print(f"[TEST Beta Calibration] Raw [0.05, 0.50, 0.95] -> Calibrados: [{p1}, {p2}, {p3}]")

    def test_order_flow_imbalance_engine(self):
        """Verifica el cálculo de OFI a partir de deltas de libro L2."""
        ofi = OrderFlowImbalanceEngine()
        bids = [{"price": 0.50, "size": 1000.0}, {"price": 0.49, "size": 800.0}]
        asks = [{"price": 0.51, "size": 200.0}, {"price": 0.52, "size": 300.0}]

        state = ofi.process_orderbook_snapshot("poly_test_market", bids, asks)
        self.assertEqual(state.direction, "BUY_PRESSURE")
        self.assertGreater(state.ofi_ratio, 0.25)
        print(f"[TEST OFI] Dirección: {state.direction} | OFI Ratio: {state.ofi_ratio} | Imbalance: {state.depth_imbalance_pct}%")

    def test_combinatorial_arbitrage_scanner(self):
        """Verifica la detección de arbitraje combinatorio libre de riesgo."""
        scanner = CombinatorialArbitrageScanner()
        outcomes = [
            {"name": "Resultado A", "best_ask": 0.35, "no_ask": 0.65},
            {"name": "Resultado B", "best_ask": 0.30, "no_ask": 0.70},
            {"name": "Resultado C", "best_ask": 0.28, "no_ask": 0.72},
        ]
        # Suma de asks = 0.35 + 0.30 + 0.28 = 0.93 (< 0.98 -> Arbitraje de 7%)
        opp = scanner.scan_multi_outcome_event("Ganador Elecciones Especiales", outcomes)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.arbitrage_type, "MUTUALLY_EXCLUSIVE_LONG")
        self.assertGreater(opp.net_roi_pct, 5.0)
        print(f"[TEST Combinatorial Arb] ROI: {opp.net_roi_pct}% | Costo: ${opp.total_cost} | Payout: ${opp.guaranteed_payout}")


if __name__ == "__main__":
    unittest.main()
