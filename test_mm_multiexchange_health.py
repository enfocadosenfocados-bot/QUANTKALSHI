"""
test_mm_multiexchange_health.py
===============================
Suite de pruebas unitarias para:
1. Avellaneda-Stoikov Market Maker (Precios de Reserva, Half-Spread, Toxic Retreat).
2. Feed Multi-Exchange Paralelo (Binance, Coinbase, Bybit, First-Mover Velocity).
3. Polygon Health Checker (Encoding ABI ERC-20, RPC Benchmark).
"""

import unittest
import time
import math
from avellaneda_stoikov import AvellanedaStoikovEngine
from multi_exchange_feed import MultiExchangeFeed
from polygon_health_checker import PolygonHealthChecker


class TestMMMultiExchangeHealth(unittest.TestCase):
    def setUp(self):
        self.mm = AvellanedaStoikovEngine(
            risk_aversion_gamma=0.25,
            liquidity_kappa=1.80,
            volatility_sigma=0.45,
            max_inventory=50,
            portfolio_capital=1000.0,
        )
        self.feed = MultiExchangeFeed()
        self.checker = PolygonHealthChecker()

    def test_avellaneda_inventory_asymmetry(self):
        mid = 0.50
        # Con inventario q=0, r = mid
        r_neutral = self.mm.compute_reservation_price(mid, inventory_q=0)
        self.assertAlmostEqual(r_neutral, mid, places=3)

        # Con inventario largo q=+20, el precio de reserva debe bajar (r < mid)
        r_long = self.mm.compute_reservation_price(mid, inventory_q=20)
        self.assertLess(r_long, mid)
        print(f"[TEST Avellaneda] q=0 -> r=${r_neutral:.3f} | q=+20 -> r=${r_long:.3f} (Desincentivo de compras)")

        # Con inventario corto q=-20, el precio de reserva debe subir (r > mid)
        r_short = self.mm.compute_reservation_price(mid, inventory_q=-20)
        self.assertGreater(r_short, mid)

    def test_avellaneda_anti_toxic_retreat(self):
        # 1. Cotización normal (VPIN bajo, velocidad baja)
        q_normal = self.mm.quote_market(
            market_id="m1",
            symbol="BTC",
            mid_price=0.50,
            vpin_toxicity=0.15,
            crypto_velocity_10s=0.02,
        )
        self.assertTrue(q_normal.is_active)
        self.assertEqual(q_normal.status, "QUOTING")

        # 2. Cotización bajo flujo tóxico (VPIN = 0.70)
        q_toxic = self.mm.quote_market(
            market_id="m1",
            symbol="BTC",
            mid_price=0.50,
            vpin_toxicity=0.72,
            crypto_velocity_10s=0.02,
        )
        self.assertFalse(q_toxic.is_active)
        self.assertEqual(q_toxic.status, "RETREAT_TOXIC")
        self.assertGreater(q_toxic.half_spread, q_normal.half_spread)
        print(f"[TEST Toxic Retreat] Normal Spread: {q_normal.half_spread:.3f} -> Retreat Spread: {q_toxic.half_spread:.3f} | Activo: {q_toxic.is_active}")

    def test_avellaneda_simulate_fill_and_pnl(self):
        # Cotizar mercado
        self.mm.quote_market("poly_btc", "BTC", mid_price=0.50, vpin_toxicity=0.10)
        # Simular ejecución de compra en el Bid
        fill = self.mm.simulate_fill("poly_btc", side="BUY_BID", contracts=15)
        self.assertIsNotNone(fill)
        self.assertEqual(self.mm.inventory["poly_btc"], 15)
        self.assertGreater(self.mm.total_spread_pnl, 0.0)
        print(f"[TEST MM Fill] Comprados {fill.contracts} contratos a ${fill.price:.3f} | Spread Capturado: ${fill.spread_captured:.3f}")

    def test_multi_exchange_feed_and_first_mover(self):
        t0 = time.time()
        # Registrar precios en Binance, Coinbase y Bybit
        self.feed.record_price("BINANCE", "BTC", 65000.0, now=t0 - 5.0)
        self.feed.record_price("COINBASE", "BTC", 65000.0, now=t0 - 5.0)
        self.feed.record_price("BYBIT", "BTC", 65000.0, now=t0 - 5.0)

        # Coinbase acelera fuertemente (+0.30%) antes que Binance (+0.05%)
        self.feed.record_price("BINANCE", "BTC", 65032.5, now=t0)
        self.feed.record_price("COINBASE", "BTC", 65195.0, now=t0)
        self.feed.record_price("BYBIT", "BTC", 65020.0, now=t0)

        coinbase_vel = self.feed.prices["BTC"]["COINBASE"].velocity_5s
        binance_vel = self.feed.prices["BTC"]["BINANCE"].velocity_5s
        self.assertGreater(coinbase_vel, binance_vel)
        self.assertGreater(coinbase_vel, 0.25)
        print(f"[TEST Multi-Exchange] Vel Coinbase: +{coinbase_vel:.2f}% vs Binance: +{binance_vel:.2f}% (First Mover detectado)")

    def test_polygon_health_checker_abi_encoding(self):
        dummy_wallet = "0x71C83d31F1F5685718a3C5F60B0C46FEfB9D6F0A"
        # 1. balanceOf encoding
        bal_data = self.checker._encode_balance_of(dummy_wallet)
        self.assertTrue(bal_data.startswith("0x70a08231"))
        self.assertEqual(len(bal_data), 10 + 64)  # 0x + 8 chars selector + 64 chars padded address

        # 2. allowance encoding
        spender = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
        allow_data = self.checker._encode_allowance(dummy_wallet, spender)
        self.assertTrue(allow_data.startswith("0xdd62ed3e"))
        self.assertEqual(len(allow_data), 10 + 128)  # 0x + 8 chars selector + 128 chars
        print(f"[TEST Polygon Health] ABI Encoding validado: balanceOf ({len(bal_data)} chars), allowance ({len(allow_data)} chars)")


if __name__ == "__main__":
    unittest.main()
