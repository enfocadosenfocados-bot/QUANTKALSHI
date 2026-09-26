"""Tests del payload de órdenes de Kalshi V2.

Kalshi retiró el endpoint v1: `POST /portfolio/orders` devuelve HTTP 410
`deprecated_v1_order_endpoint`, y el payload antiguo (action/outcome_side/
yes_price_dollars/no_price_dollars) ya no existe. Como el modo PAPER nunca envía
órdenes, este fallo es invisible hasta el primer día de live. Estos tests fijan
el contrato V2 para que no se rompa otra vez.
"""
import unittest

from live_execution import LiveExecutionManager

# Campos del contrato V2 (documentado en docs.kalshi.com/api-reference/orders)
V2_REQUIRED = {
    "ticker",
    "side",
    "count",
    "price",
    "time_in_force",
    "self_trade_prevention_type",
}
# Campos del contrato v1 retirado: si reaparecen, algo se revirtió por error.
V1_FORBIDDEN = {
    "action",
    "outcome_side",
    "type",
    "yes_price_dollars",
    "no_price_dollars",
}


class TestOrderPayloadV2(unittest.TestCase):
    def build(self, token: str, side: str, price: float, order_type: str = "LIMIT (Maker)"):
        signal = {
            "token_id": "KXTEST-24JAN01-T60",
            "token": token,
            "side": side,
            "entry_price": f"{price:.4f}",
            "recommended_order_type": order_type,
        }
        return LiveExecutionManager.build_order_payload(signal, None, count=10)

    def test_required_v2_fields_present(self):
        payload = self.build("Yes", "BUY", 0.56)
        self.assertTrue(V2_REQUIRED.issubset(payload.keys()))
        print(f"[TEST V2] campos requeridos presentes: {sorted(V2_REQUIRED)}")

    def test_v1_fields_are_gone(self):
        payload = self.build("Yes", "BUY", 0.56)
        overlap = V1_FORBIDDEN.intersection(payload.keys())
        self.assertEqual(overlap, set())
        print("[TEST V2] sin campos v1 (action/outcome_side/yes_price_dollars...)")

    def test_buy_yes_is_bid(self):
        payload = self.build("Yes", "BUY", 0.56)
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["price"], "0.5600")
        print("[TEST V2] BUY YES 0.56 -> side=bid price=0.5600")

    def test_sell_yes_is_ask(self):
        payload = self.build("Yes", "SELL", 0.56)
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["price"], "0.5600")
        print("[TEST V2] SELL YES 0.56 -> side=ask price=0.5600")

    def test_buy_no_is_ask_at_complement(self):
        """Comprar NO a 0.44 equivale a vender YES a 0.56."""
        payload = self.build("No", "BUY", 0.44)
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["price"], "0.5600")
        print("[TEST V2] BUY NO 0.44 -> side=ask price=0.5600 (complemento)")

    def test_sell_no_is_bid_at_complement(self):
        payload = self.build("No", "SELL", 0.44)
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["price"], "0.5600")
        print("[TEST V2] SELL NO 0.44 -> side=bid price=0.5600 (complemento)")

    def test_count_and_price_are_fixed_point_strings(self):
        payload = self.build("Yes", "BUY", 0.56)
        self.assertIsInstance(payload["count"], str)
        self.assertIsInstance(payload["price"], str)
        self.assertEqual(payload["count"], "10.00")
        print("[TEST V2] count y price son strings en punto fijo ('10.00', '0.5600')")

    def test_minimum_count_is_one_contract(self):
        signal = {
            "token_id": "KXTEST",
            "token": "Yes",
            "side": "BUY",
            "entry_price": "0.9700",
            "recommended_order_type": "LIMIT (Maker)",
        }
        payload = LiveExecutionManager.build_order_payload(signal, None, count=0.15)
        self.assertEqual(payload["count"], "1.00")
        print("[TEST V2] count 0.15 -> '1.00' (respeta min_order_size)")

    def test_maker_signals_are_post_only(self):
        payload = self.build("Yes", "BUY", 0.56, order_type="LIMIT (Maker)")
        self.assertTrue(payload["post_only"])
        print("[TEST V2] senal maker -> post_only=True (no paga comision de taker)")

    def test_taker_signals_are_not_post_only(self):
        payload = self.build("Yes", "BUY", 0.56, order_type="MARKET (Taker)")
        self.assertFalse(payload["post_only"])
        print("[TEST V2] senal taker -> post_only=False")

    def test_price_is_clamped_to_operable_range(self):
        low = self.build("Yes", "BUY", 0.001)
        high = self.build("Yes", "BUY", 1.5)
        self.assertEqual(low["price"], "0.0100")
        self.assertEqual(high["price"], "0.9900")
        print("[TEST V2] precios fuera de rango acotados a [0.0100, 0.9900]")

    def test_client_order_id_is_unique(self):
        first = self.build("Yes", "BUY", 0.56)
        second = self.build("Yes", "BUY", 0.56)
        self.assertNotEqual(first["client_order_id"], second["client_order_id"])
        print("[TEST V2] client_order_id unico por orden (idempotencia)")


if __name__ == "__main__":
    unittest.main()
