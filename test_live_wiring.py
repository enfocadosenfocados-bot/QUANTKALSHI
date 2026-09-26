"""Tests del cableado del camino de dinero real.

El accidente que estos tests impiden ya ocurrió una vez: `lead_lag_engine` llamaba a
`live_manager.execute_order(signal)` con 1 de sus 3 argumentos, el `TypeError` caía en
un `except Exception` que solo escribía una línea de log, y el resultado era
indistinguible de "no había nada que enviar". Ningún test fallaba porque ningún test
miraba el cableado. Aquí se mira el cableado (AST), los candados que faltaban en el
envío (tamaño explícito, promoción, idempotencia, tope de órdenes vivas) y la
reconciliación con el exchange.

La red se sustituye por un doble que registra llamadas: un test que sale a Kalshi
comprueba la conexión, no la seguridad.
"""
from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from unittest import mock

import kalshi_env as kalshi_env_module
import live_execution
from config import LIVE_ABS_MAX_ORDER_USD
from live_execution import LiveExecutionManager

REPO = Path(__file__).resolve().parent


class _FakeResponse:
    def __init__(self, payload: Optional[Dict[str, Any]] = None, status: int = 200):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, network: "_RecordingNetwork"):
        self.network = network

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResponse:
        self.network.calls.append("POST")
        self.network.payloads.append(json)
        return self.network.post_response

    async def get(self, url: str, headers: Any = None, params: Any = None) -> _FakeResponse:
        self.network.calls.append("GET")
        return self.network.get_response

    async def delete(self, url: str, headers: Any = None) -> _FakeResponse:
        self.network.calls.append("DELETE")
        self.network.deleted.append(url)
        return self.network.delete_response


class _RecordingNetwork:
    """Doble de `httpx`: registra métodos y devuelve respuestas preparadas."""

    def __init__(
        self,
        post_payload: Optional[Dict[str, Any]] = None,
        get_payload: Optional[Dict[str, Any]] = None,
        get_status: int = 200,
    ):
        self.calls: List[str] = []
        self.payloads: List[Any] = []
        self.deleted: List[str] = []
        self.post_response = _FakeResponse(
            post_payload
            or {"order": {"order_id": "live-1", "fill_count": 0, "remaining_count": 10}}
        )
        self.get_response = _FakeResponse(
            get_payload if get_payload is not None else {"orders": []}, status=get_status
        )
        self.delete_response = _FakeResponse({})

    def AsyncClient(self, *args: Any, **kwargs: Any) -> _FakeClient:
        return _FakeClient(self)


def _bare_manager(
    mode: str = "LIVE",
    live_orders: Optional[Dict[str, Dict[str, Any]]] = None,
    orders_file: Optional[Path] = None,
    max_open: int = 5,
    authenticated: bool = True,
) -> LiveExecutionManager:
    """Manager sin `__init__` (no sondea red ni lee credenciales) pero con el estado real."""
    manager = LiveExecutionManager.__new__(LiveExecutionManager)
    manager.mode = mode
    manager.max_live_trade_usd = 25.0
    manager.max_open_live_trades = max_open
    manager.key_id = "test-key"
    manager.private_key_path = "test.pem"
    manager.kill_switch_active = False
    manager.orders_file = orders_file
    manager.live_orders = dict(live_orders or {})
    manager.last_reconcile = {}
    manager._promotion_cache = {"at": 0.0, "result": None}
    manager.auth = types.SimpleNamespace(
        configured=authenticated,
        load_error=None if authenticated else "sin credenciales",
        headers=lambda *args, **kwargs: {"KALSHI-ACCESS-KEY": "test-key"},
    )
    return manager


def _open_order(order_id: str = "live-1", age_sec: float = 0.0) -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": f"cid-{order_id}",
        "status": "resting",
        "remaining_count": 10,
        "recorded_at": time.time() - age_sec,
    }


class _LiveWiringCase(unittest.TestCase):
    """Entorno demo, red interceptada y flags restaurados en cada test."""

    def setUp(self) -> None:
        self._saved = (
            live_execution.KALSHI_ENV,
            live_execution.ALLOW_REAL_MONEY,
            live_execution.LIVE_ABS_MAX_ORDER_USD,
            live_execution.LIVE_REQUIRE_PROMOTION,
            kalshi_env_module.kalshi_env.env,
            live_execution.httpx,
        )
        live_execution.KALSHI_ENV = "demo"
        kalshi_env_module.kalshi_env.env = "demo"
        live_execution.ALLOW_REAL_MONEY = False
        live_execution.LIVE_REQUIRE_PROMOTION = True
        self.network = _RecordingNetwork()
        live_execution.httpx = self.network

    def tearDown(self) -> None:
        (
            live_execution.KALSHI_ENV,
            live_execution.ALLOW_REAL_MONEY,
            live_execution.LIVE_ABS_MAX_ORDER_USD,
            live_execution.LIVE_REQUIRE_PROMOTION,
            kalshi_env_module.kalshi_env.env,
            live_execution.httpx,
        ) = self._saved

    @staticmethod
    def _signal(confirm: bool = True, **extra: Any) -> Dict[str, Any]:
        signal: Dict[str, Any] = {
            "token_id": "TEST-MARKET",
            "token": "Yes",
            "side": "BUY",
            "entry_price": "0.50",
            "recommended_order_type": "LIMIT (Maker)",
        }
        if confirm:
            signal["confirm_live_order"] = True
        signal.update(extra)
        return signal


class TestCallSitesAreWired(_LiveWiringCase):
    """El bug original no era de lógica sino de cableado: 1 argumento de 3.

    Un test de comportamiento no lo ve porque el `except Exception` del auto-sniper se
    lo comía; uno de AST sí, y además cubre call-sites futuros.
    """

    def _execute_order_calls(self) -> List[tuple]:
        offenders: List[tuple] = []
        for path in sorted(REPO.glob("*.py")):
            # `read_text(encoding="utf-8")` falla con los ficheros que llevan BOM
            # (config.py lo tiene), y un test de cableado que no puede leer el fichero
            # no comprueba el cableado.
            source = path.read_bytes()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_execute = (
                    isinstance(func, ast.Attribute) and func.attr == "execute_order"
                ) or (isinstance(func, ast.Name) and func.id == "execute_order")
                if not is_execute:
                    continue
                has_star = any(isinstance(arg, ast.Starred) for arg in node.args)
                if len(node.args) < 3 and not has_star:
                    offenders.append((path.name, node.lineno, len(node.args)))
        return offenders

    def test_every_execute_order_call_passes_signal_market_and_size(self):
        offenders = self._execute_order_calls()
        self.assertEqual(
            offenders,
            [],
            f"execute_order(signal, market, size_usd) con menos de 3 argumentos: {offenders}",
        )
        print("[TEST Cableado] todas las llamadas a execute_order pasan (signal, market, size_usd)")

    def test_auto_sniper_delegates_to_the_logging_sender(self):
        """El envío debe pasar por `_send_live_order`, que escribe el desenlace."""
        source = (REPO / "lead_lag_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("execute_order(signal)", source)
        self.assertIn("_send_live_order", source)
        print("[TEST Cableado] el auto-sniper delega en _send_live_order (log del resultado)")


async def _run_auto_snipe(engine, opp) -> None:
    """Ejecuta el auto-snipe (síncrono) y espera la tarea que agenda con create_task."""
    before = set(asyncio.all_tasks())
    engine._auto_execute_snipe(opp)
    await asyncio.sleep(0)
    pending = [
        task
        for task in asyncio.all_tasks()
        if task not in before and task is not asyncio.current_task()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestAutoSniperWiring(_LiveWiringCase):
    """El auto-snipe es el único camino que envía órdenes sin que nadie pulse nada."""

    @staticmethod
    def _opportunity(opp_id: str, symbol: str = "BTC"):
        import lead_lag_engine as lead_lag_module

        return lead_lag_module.LeadLagOpportunity(
            id=opp_id,
            symbol=symbol,
            target_market_id=f"KX{symbol}-1",
            market_question=f"{symbol} por encima del objetivo",
            condition_id=f"cond-{opp_id}",
            outcome="Yes",
            clob_price=0.5,
            binance_spot_price=100.0,
            binance_velocity_10s=0.15,
            implied_fair_price=0.6,
            edge_pct=3.0,
            latency_advantage_ms=2200,
            detected_at=time.time(),
            status="ACTIVE",
            expiration_seconds=15,
        )

    def test_paper_rejection_stops_the_live_order(self):
        import lead_lag_engine as lead_lag_module
        import paper_tracker as paper_module

        engine = lead_lag_module.LeadLagEngine()
        sent: List[tuple] = []

        class _FakeLive:
            is_live = True
            kill_switch_active = False

            async def execute_order(self, *args: Any):
                sent.append(args)
                return {"executed": True}

        class _RejectingTracker:
            @staticmethod
            def record_signal(signal: Any, market: Any):
                return None

        opp = self._opportunity("opp-reject")
        with mock.patch.object(paper_module, "paper_tracker", _RejectingTracker()), mock.patch.object(
            live_execution, "live_manager", _FakeLive()
        ):
            asyncio.run(_run_auto_snipe(engine, opp))
        self.assertEqual(sent, [])
        self.assertEqual(opp.status, "ACTIVE")
        print("[TEST Cableado] paper rechaza la senal -> 0 envios a live")

    def test_live_order_carries_signal_market_and_paper_size(self):
        import lead_lag_engine as lead_lag_module
        import paper_tracker as paper_module

        engine = lead_lag_module.LeadLagEngine()
        sent: List[tuple] = []

        class _FakeLive:
            is_live = True
            kill_switch_active = False

            async def execute_order(self, *args: Any):
                sent.append(args)
                return {"executed": True, "order_id": "live-9"}

        class _AcceptingTracker:
            @staticmethod
            def record_signal(signal: Any, market: Any):
                signal["position_size_usd"] = 42.5
                return {"position_size_usd": 42.5}

        opp = self._opportunity("opp-accept", symbol="ETH")
        with mock.patch.object(paper_module, "paper_tracker", _AcceptingTracker()), mock.patch.object(
            live_execution, "live_manager", _FakeLive()
        ):
            asyncio.run(_run_auto_snipe(engine, opp))
        self.assertEqual(len(sent), 1, "debe enviarse exactamente una orden real")
        self.assertEqual(len(sent[0]), 3, "execute_order recibe (signal, market, size_usd)")
        self.assertEqual(sent[0][2], 42.5, "el tamano es el calculado por el paper tracker")
        self.assertEqual(sent[0][0]["strategy_code"], "LL_SNIPER")
        self.assertEqual(sent[0][1]["market_id"], "KXETH-1")
        self.assertEqual(opp.status, "EXECUTED_AUTO")
        print("[TEST Cableado] auto-sniper -> execute_order(signal, market, 42.5)")


class TestLiveOrderGates(_LiveWiringCase):
    """Candados que faltaban en el envío: tamaño, promoción, idempotencia y tope."""

    def test_missing_size_is_blocked_and_never_sent(self):
        manager = _bare_manager()
        for size in (None, 0, 0.0, -5, "no-numero"):
            result = asyncio.run(manager.execute_order(self._signal(), None, size))
            self.assertFalse(result["executed"], f"size={size!r} no debe enviar")
            self.assertEqual(result["blocked_by"], "invalid_size")
        self.assertEqual(self.network.calls, [])
        print("[TEST Candados] size_usd ausente/0/no numerico -> bloqueado, 0 llamadas")

    def test_open_order_cap_is_enforced(self):
        five = {f"o{index}": _open_order(f"o{index}") for index in range(5)}
        manager = _bare_manager(live_orders=five)
        blocked = asyncio.run(manager.execute_order(self._signal(dedupe_key="NEW"), None, 25.0))
        self.assertEqual(blocked["blocked_by"], "max_open_live_trades")
        self.assertEqual(self.network.calls, [])
        manager.live_orders.pop("o0")
        sent = asyncio.run(manager.execute_order(self._signal(dedupe_key="NEW"), None, 25.0))
        self.assertTrue(sent["executed"])
        self.assertEqual(self.network.calls, ["POST"])
        expected_count = min(25.0, LIVE_ABS_MAX_ORDER_USD) / 0.5
        self.assertEqual(self.network.payloads[0]["count"], f"{expected_count:.2f}")
        print("[TEST Candados] 5 ordenes vivas -> la 6a no sale; con hueco, 1 POST")

    def test_same_signal_is_not_sent_twice(self):
        manager = _bare_manager()
        signal = self._signal(dedupe_key="LL:M1:Yes:1")
        first = asyncio.run(manager.execute_order(signal, None, 25.0))
        self.assertTrue(first["executed"])
        second = asyncio.run(manager.execute_order(signal, None, 25.0))
        self.assertEqual(second["blocked_by"], "duplicate_client_order_id")
        self.assertEqual(self.network.calls.count("POST"), 1)
        print("[TEST Candados] reintento de la misma senal -> 1 solo POST (idempotencia)")

    def test_client_order_id_is_stable_for_the_same_signal(self):
        first = LiveExecutionManager.build_order_payload(
            self._signal(dedupe_key="LL:M1:Yes:1"), None, 10
        )
        second = LiveExecutionManager.build_order_payload(
            self._signal(dedupe_key="LL:M1:Yes:1"), None, 10
        )
        other = LiveExecutionManager.build_order_payload(
            self._signal(dedupe_key="LL:M1:Yes:2"), None, 10
        )
        self.assertEqual(first["client_order_id"], second["client_order_id"])
        self.assertNotEqual(first["client_order_id"], other["client_order_id"])
        print("[TEST Candados] client_order_id estable por senal (reintento != orden nueva)")

    @staticmethod
    def _patch_board(rows: List[Dict[str, Any]]):
        return mock.patch(
            "strategy_promotion.build_promotion_board",
            return_value={"rows": rows, "promotable": []},
        )

    def test_promotion_gate_blocks_a_strategy_without_evidence(self):
        manager = _bare_manager()
        rows = [{"code": "LL_SNIPER", "state": "CANDIDATA", "reason": "muestra insuficiente"}]
        with self._patch_board(rows):
            result = asyncio.run(
                manager.execute_order(self._signal(strategy_code="LL_SNIPER"), None, 25.0)
            )
        self.assertEqual(result["blocked_by"], "promotion_gate")
        self.assertEqual(result["promotion_state"], "CANDIDATA")
        self.assertEqual(self.network.calls, [])
        print("[TEST Promocion] CANDIDATA -> sin orden real")

    def test_promotion_gate_lets_a_promoted_strategy_trade(self):
        manager = _bare_manager()
        rows = [{"code": "LL_SNIPER", "state": "LIVE_PEQUENO", "reason": "significativa"}]
        with self._patch_board(rows):
            result = asyncio.run(
                manager.execute_order(self._signal(strategy_code="LL_SNIPER"), None, 25.0)
            )
        self.assertTrue(result["executed"])
        self.assertEqual(self.network.calls.count("POST"), 1)
        print("[TEST Promocion] LIVE_PEQUENO -> la orden sale")

    def test_promotion_gate_is_fail_closed(self):
        manager = _bare_manager()
        with mock.patch(
            "strategy_promotion.build_promotion_board",
            side_effect=RuntimeError("tablero roto"),
        ):
            result = asyncio.run(
                manager.execute_order(self._signal(strategy_code="LL_SNIPER"), None, 25.0)
            )
        self.assertEqual(result["blocked_by"], "promotion_gate")
        self.assertEqual(result["promotion_state"], "ERROR")
        self.assertEqual(self.network.calls, [])
        print("[TEST Promocion] tablero ilegible -> fail-closed (0 POST)")

    def test_gate_disabled_only_when_declared(self):
        live_execution.LIVE_REQUIRE_PROMOTION = False
        manager = _bare_manager()
        with self._patch_board([{"code": "LL_SNIPER", "state": "DESCARTADA"}]):
            result = asyncio.run(
                manager.execute_order(self._signal(strategy_code="LL_SNIPER"), None, 25.0)
            )
        self.assertTrue(result["executed"])
        print("[TEST Promocion] LIVE_REQUIRE_PROMOTION=false -> candado apagado a proposito")


class TestReconcile(_LiveWiringCase):
    """El exchange es la fuente de verdad; el registro local solo es su copia."""

    def test_stale_resting_order_is_canceled(self):
        manager = _bare_manager(live_orders={"live-1": _open_order(age_sec=3600)})
        self.network.get_response = _FakeResponse({"orders": [{"order_id": "live-1"}]})
        summary = asyncio.run(manager.reconcile_orders())
        self.assertEqual(summary["canceled"], ["live-1"])
        self.assertTrue(summary["exchange_ok"])
        self.assertEqual(summary["open_after"], 0)
        self.assertEqual(self.network.calls.count("DELETE"), 1)
        print("[TEST Reconciliacion] orden resting de 1h -> DELETE y fuera del recuento")

    def test_order_the_exchange_no_longer_has_is_marked_closed(self):
        manager = _bare_manager(live_orders={"live-1": _open_order(age_sec=30)})
        self.network.get_response = _FakeResponse({"orders": []})
        summary = asyncio.run(manager.reconcile_orders())
        self.assertEqual(summary["closed_remotely"], ["live-1"])
        self.assertEqual(self.network.calls.count("DELETE"), 0)
        self.assertEqual(manager.open_live_order_count(), 0)
        print("[TEST Reconciliacion] orden ya no viva en Kalshi -> marcada; no cuenta contra el tope")

    def test_fresh_order_is_left_alone(self):
        manager = _bare_manager(live_orders={"live-1": _open_order(age_sec=5)})
        self.network.get_response = _FakeResponse({"orders": [{"order_id": "live-1"}]})
        summary = asyncio.run(manager.reconcile_orders())
        self.assertEqual(summary["canceled"], [])
        self.assertEqual(self.network.calls.count("DELETE"), 0)
        self.assertEqual(summary["open_after"], 1)
        print("[TEST Reconciliacion] orden recien enviada -> no se toca")

    def test_listing_unavailable_is_reported_not_hidden(self):
        manager = _bare_manager(live_orders={"live-1": _open_order(age_sec=5)})
        self.network.get_response = _FakeResponse({}, status=404)
        summary = asyncio.run(manager.reconcile_orders())
        self.assertFalse(summary["exchange_ok"])
        self.assertTrue(summary["errors"], "el fallo del listado debe declararse")
        print(f"[TEST Reconciliacion] listado no disponible -> se declara ({summary['errors'][0][:48]}...)")

    def test_reconcile_makes_no_calls_without_local_orders(self):
        manager = _bare_manager()
        summary = asyncio.run(manager.reconcile_orders())
        self.assertEqual(summary["checked"], 0)
        self.assertEqual(self.network.calls, [])
        print("[TEST Reconciliacion] sin ordenes locales -> 0 llamadas al exchange")


class TestLiveOrderDurability(_LiveWiringCase):
    def test_submitted_order_survives_a_restart_and_is_not_resent(self):
        tmp_dir = Path(tempfile.mkdtemp())
        orders_file = tmp_dir / "live_orders.json"
        manager = _bare_manager(orders_file=orders_file)
        first = asyncio.run(manager.execute_order(self._signal(dedupe_key="K"), None, 25.0))
        self.assertTrue(first["executed"])
        self.assertTrue(orders_file.exists(), "la orden enviada queda en disco")
        raw = json.loads(orders_file.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["orders"]), 1)
        self.assertFalse(list(tmp_dir.glob("*.tmp")), "sin temporales sin renombrar")

        restarted = _bare_manager(orders_file=orders_file)
        restarted.load_live_orders()
        self.assertEqual(restarted.open_live_order_count(), 1)
        again = asyncio.run(restarted.execute_order(self._signal(dedupe_key="K"), None, 25.0))
        self.assertEqual(again["blocked_by"], "duplicate_client_order_id")
        print("[TEST Duracion] orden real persistida: sobrevive al reinicio y no se duplica")


class TestStatusPublishesTheGates(_LiveWiringCase):
    def test_public_status_exposes_live_state(self):
        manager = _bare_manager(live_orders={"live-1": _open_order()})
        status = manager.get_public_status()
        self.assertEqual(status["open_live_orders"], 1)
        self.assertTrue(status["require_promotion"])
        self.assertEqual(status["max_open_live_trades"], 5)
        print("[TEST Estado] el dashboard puede ver ordenes vivas, tope y puerta de promocion")


if __name__ == "__main__":
    unittest.main()




