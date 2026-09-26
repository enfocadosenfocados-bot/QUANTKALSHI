"""Tests de la puerta de promoción PAPER -> LIVE (strategy_promotion.py).

Lo que se valida aquí es que el sistema se resista a elegir ganadoras por azar:
corrección por comparaciones múltiples, muestra mínima con potencia, holdout
temporal y descarte de estrategias sin edge.
"""
import unittest
from datetime import UTC, datetime, timedelta

from strategy_promotion import (
    build_promotion_board,
    evaluate_strategy,
    holdout_split,
    one_sided_p_value,
)

ENTRY = 0.50  # breakeven implícito = 0.50 para BUY


def make_trades(n: int, wins: int, code: str = "TEST") -> list:
    """Trades cerrados con timestamps crecientes: los primeros son WON."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    trades = []
    for i in range(n):
        won = i < wins
        trades.append(
            {
                "strategy_code": code,
                "status": "WON" if won else "LOST",
                "entry_price": ENTRY,
                "side": "BUY",
                "shares": 100,
                "realized_pnl_usd": 50.0 if won else -50.0,
                "closed_at": (base + timedelta(hours=i)).isoformat(),
            }
        )
    return trades


def make_mixed_trades(n: int, one_loss_in: int = 4, code: str = "TEST") -> list:
    """Trades con victorias y derrotas repartidas de forma uniforme.

    Necesario para el holdout: si todas las pérdidas cayeran al final, el holdout
    sería negativo por construcción y ninguna estrategia podría promoverse.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    trades = []
    for i in range(n):
        won = (i % one_loss_in) != (one_loss_in - 1)
        trades.append(
            {
                "strategy_code": code,
                "status": "WON" if won else "LOST",
                "entry_price": ENTRY,
                "side": "BUY",
                "shares": 100,
                "realized_pnl_usd": 50.0 if won else -50.0,
                "closed_at": (base + timedelta(hours=i)).isoformat(),
            }
        )
    return trades


class TestStatistics(unittest.TestCase):
    def test_p_value_high_when_no_edge(self):
        p = one_sided_p_value(50, 100, 0.50)
        self.assertGreater(p, 0.4)
        print(f"[TEST Stats] 50/100 sin edge -> p={p:.3f} (alto: se explica por azar)")

    def test_p_value_low_with_strong_edge(self):
        p = one_sided_p_value(90, 100, 0.50)
        self.assertLess(p, 1e-9)
        print(f"[TEST Stats] 90/100 vs breakeven 0.50 -> p={p:.2e} (significativo)")

    def test_holdout_is_the_most_recent_part(self):
        trades = make_trades(100, 70)
        fit, holdout = holdout_split(trades)
        self.assertEqual(len(fit) + len(holdout), 100)
        self.assertGreater(len(holdout), 0)
        self.assertLess(
            max(t["closed_at"] for t in fit), min(t["closed_at"] for t in holdout)
        )
        print(
            f"[TEST Holdout] {len(fit)} ajuste + {len(holdout)} holdout "
            "(el holdout es lo mas reciente)"
        )


class TestPromotionStates(unittest.TestCase):
    def test_no_trades_is_sin_datos(self):
        row = evaluate_strategy("S99", [], 16)
        self.assertEqual(row["state"], "SIN_DATOS")
        print("[TEST Estado] sin trades -> SIN_DATOS")

    def test_small_sample_stays_candidate(self):
        row = evaluate_strategy("S99", make_trades(5, 5), 16)
        self.assertEqual(row["state"], "CANDIDATA")
        self.assertIn("Muestra insuficiente", row["reason"])
        print(f"[TEST Estado] 5/5 trades -> CANDIDATA ({row['reason']})")

    def test_strong_edge_with_holdout_becomes_live_pequeno(self):
        # 120 trades con 75% de acierto repartido (no un win rate perfecto).
        row = evaluate_strategy("S99", make_mixed_trades(120, 4), 16)
        self.assertEqual(row["state"], "LIVE_PEQUENO")
        self.assertTrue(row["significant"])
        self.assertTrue(row["holdout_passed"])
        print(
            f"[TEST Estado] {row['wins']}/{row['closed_trades']} ({row['win_rate_pct']}%) "
            f"-> LIVE_PEQUENO (holdout {row['holdout_trades']} trades, "
            f"${row['holdout_pnl_usd']})"
        )

    def test_edge_that_fails_holdout_is_only_sombra(self):
        # 70 ganadores primero y 30 perdedores al final: el holdout no confirma.
        row = evaluate_strategy("S99", make_trades(100, 70), 16)
        self.assertEqual(row["state"], "SOMBRA")
        self.assertTrue(row["significant"])
        self.assertFalse(row["holdout_passed"])
        print(
            f"[TEST Estado] 70/100 con holdout negativo -> SOMBRA "
            f"(holdout ${row['holdout_pnl_usd']})"
        )

    def test_no_edge_is_descartada(self):
        row = evaluate_strategy("S99", make_trades(100, 20), 16)
        self.assertEqual(row["state"], "DESCARTADA")
        print(
            f"[TEST Estado] 20/100 vs breakeven 50% -> DESCARTADA "
            f"(wilson_upper {row['wilson_upper_pct']}%)"
        )

    def test_paused_strategy_is_reported_as_pausada(self):
        row = evaluate_strategy("S99", make_mixed_trades(120, 4), 16, paused=True)
        self.assertEqual(row["state"], "PAUSADA")
        print("[TEST Estado] gobernador la pauso -> PAUSADA")

    def test_bonferroni_raises_the_bar(self):
        """Con 16 estrategias el umbral es 16x mas exigente que con una."""
        few = evaluate_strategy("S99", make_mixed_trades(120, 4), 1)
        many = evaluate_strategy("S99", make_mixed_trades(120, 4), 16)
        self.assertGreater(few["bonferroni_alpha"], many["bonferroni_alpha"])
        self.assertEqual(few["bonferroni_alpha"], round(0.05, 5))
        self.assertEqual(many["bonferroni_alpha"], round(0.05 / 16, 5))
        print(
            f"[TEST Bonferroni] alpha con 1 estrategia={few['bonferroni_alpha']} | "
            f"con 16={many['bonferroni_alpha']}"
        )

    def test_marginal_edge_does_not_survive_many_comparisons(self):
        """Un edge marginal que pasa solo no debe pasar cuando se prueban 16."""
        trades = make_trades(60, 38)  # 63.3% vs breakeven 50%
        alone = evaluate_strategy("S99", trades, 1)
        among_many = evaluate_strategy("S99", trades, 16)
        self.assertTrue(alone["significant"])
        self.assertFalse(among_many["significant"])
        print(
            f"[TEST Bonferroni] 38/60: 1 estrategia -> {alone['state']} "
            f"(p={alone['p_value']}) | 16 estrategias -> {among_many['state']} "
            f"(p={among_many['p_value']}, alpha={among_many['bonferroni_alpha']})"
        )


class TestPromotionBoard(unittest.TestCase):
    def test_board_counts_and_promotable(self):
        class FakeTracker:
            trades = {
                f"a{i}": t for i, t in enumerate(make_mixed_trades(120, 4, "S20"))
            }
            trades.update({f"b{i}": t for i, t in enumerate(make_trades(100, 20, "S22"))})
            trades.update({f"c{i}": t for i, t in enumerate(make_trades(80, 80, "ART"))})

        board = build_promotion_board(FakeTracker(), governor=None)
        self.assertEqual(board["strategies_tested"], 3)
        self.assertIn("S20", board["promotable"])
        self.assertNotIn("S22", board["promotable"])
        self.assertNotIn("ART", board["promotable"])
        self.assertEqual(board["counts"]["LIVE_PEQUENO"], 1)
        self.assertEqual(board["counts"]["DESCARTADA"], 1)
        self.assertEqual(board["counts"]["SOSPECHOSA"], 1)
        print(
            f"[TEST Board] {board['counts']} | promovibles: {board['promotable']} | "
            f"alpha corregido {board['bonferroni_alpha']}"
        )

    def test_board_sorts_actionable_first(self):
        class FakeTracker:
            trades = {}

        FakeTracker.trades.update(
            {f"a{i}": t for i, t in enumerate(make_trades(100, 20, "BAD"))}
        )
        FakeTracker.trades.update(
            {f"b{i}": t for i, t in enumerate(make_mixed_trades(120, 4, "GOOD"))}
        )
        FakeTracker.trades.update(
            {f"c{i}": t for i, t in enumerate(make_trades(3, 3, "NEW"))}
        )
        FakeTracker.trades.update(
            {f"d{i}": t for i, t in enumerate(make_trades(80, 80, "ART"))}
        )

        board = build_promotion_board(FakeTracker(), governor=None)
        states = [r["state"] for r in board["rows"]]
        self.assertEqual(
            states, ["SOSPECHOSA", "LIVE_PEQUENO", "CANDIDATA", "DESCARTADA"]
        )
        print(f"[TEST Board] orden por prioridad de accion: {states}")


class TestIntegrityGuard(unittest.TestCase):
    """Un ganador fabricado es más peligroso que un perdedor honesto.

    El modelo de ejecución viejo llenaba al precio soñado y producía series con
    100% de acierto, así que el gate debe rechazar la métrica antes de
    interpretarla estadísticamente.
    """

    @staticmethod
    def _artifact(n: int, pnl_each: float, code: str = "ART") -> list:
        """Estrategia con n operaciones todas ganadoras pero PnL no positivo."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            {
                "strategy_code": code,
                "status": "WON",
                "entry_price": ENTRY,
                "side": "BUY",
                "shares": 100,
                "realized_pnl_usd": pnl_each,
                "closed_at": (base + timedelta(hours=i)).isoformat(),
            }
            for i in range(n)
        ]

    def test_perfect_win_rate_is_flagged(self):
        row = evaluate_strategy("ART", self._artifact(80, 0.5), 16)
        self.assertEqual(row["state"], "SOSPECHOSA")
        self.assertFalse(row["integrity_ok"])
        self.assertEqual(row["integrity_problems"][0][:4], "80/8")
        print(f"[TEST Integridad] 80/80 ganadoras -> SOSPECHOSA ({row['reason'][:78]})")

    def test_real_mm_artifact_is_caught(self):
        """Caso real: MM con 1754 trades, 100% wins y PnL negativo."""
        row = evaluate_strategy("MM", self._artifact(1754, -0.02), 16)
        self.assertEqual(row["state"], "SOSPECHOSA")
        self.assertEqual(len(row["integrity_problems"]), 2)
        print(
            f"[TEST Integridad] MM 1754 trades 100% wins PnL ${row['pnl_usd']} -> "
            f"SOSPECHOSA con {len(row['integrity_problems'])} problemas detectados"
        )

    def test_realistic_win_rate_is_not_flagged(self):
        row = evaluate_strategy("S99", make_mixed_trades(120, 4), 16)
        self.assertTrue(row["integrity_ok"])
        self.assertNotEqual(row["state"], "SOSPECHOSA")
        print(
            f"[TEST Integridad] {row['win_rate_pct']}% de acierto -> integridad OK "
            f"(estado {row['state']})"
        )

    def test_small_perfect_sample_is_not_flagged(self):
        """Con muestra chica no se puede afirmar que los datos sean imposibles."""
        row = evaluate_strategy("S99", make_trades(5, 5), 16)
        self.assertTrue(row["integrity_ok"])
        self.assertEqual(row["state"], "CANDIDATA")
        print("[TEST Integridad] 5/5 ganadoras -> sin veredicto (muestra chica)")

    def test_integrity_beats_pause_and_promotion(self):
        """El aviso de datos no fiables manda sobre cualquier otro estado."""
        artifact = self._artifact(80, 0.5)
        self.assertEqual(
            evaluate_strategy("ART", artifact, 16, paused=True)["state"], "SOSPECHOSA"
        )
        print("[TEST Integridad] SOSPECHOSA prevalece sobre PAUSADA")


if __name__ == "__main__":
    unittest.main()
