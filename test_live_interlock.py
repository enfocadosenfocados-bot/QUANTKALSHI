"""Tests del interlock de dinero real (live_execution.py).

El accidente que estos tests impiden es concreto y caro: arreglar por fin el
cableado de `execute_order`, dejar `KALSHI_ENV=production` en el `.env` y descubrir
que la primera orden que cruzo el mercado lo hizo en produccion con capital real.
La regla que se comprueba aqui es que operar con dinero real exige DOS actos
separados -- fijar el entorno Y autorizar el dinero -- y que ninguno de los dos
por si solo basta.

Un test que sale a la red no comprueba el interlock, comprueba la conexion: por eso
`httpx` se sustituye por un doble que registra los POST en vez de enviarlos, y las
aserciones sobre "no se envio nada" son sobre esa lista de llamadas.
"""
from __future__ import annotations

import asyncio
import types
import unittest
from typing import Any, Dict, List

import kalshi_env as kalshi_env_module
import live_execution
from live_execution import LiveExecutionManager


class _FakeResponse:
    """Respuesta V2 minima: lo justo para que el camino de exito no reviente."""

    status_code = 200
    text = '{"order": {"order_id": "fake-order"}}'

    def json(self) -> Dict[str, Any]:
        return {"order": {"order_id": "fake-order"}}


class _FakeClient:
    def __init__(self, network: "_RecordingNetwork"):
        self.network = network

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResponse:
        self.network.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse()


class _RecordingNetwork:
    """Doble de `httpx` que registra los POST en lugar de abrirlos."""

    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []

    def AsyncClient(self, *args: Any, **kwargs: Any) -> _FakeClient:
        return _FakeClient(self)


def _bare_manager(
    mode: str = "PAPER", authenticated: bool = True, kill_switch: bool = False
) -> LiveExecutionManager:
    """Manager sin pasar por `__init__`.

    `__init__` resuelve el entorno de verdad (sondeo a Kalshi) y lee el fichero de
    credenciales; un test que hace eso comprueba la red y el disco, no el interlock.
    """
    manager = LiveExecutionManager.__new__(LiveExecutionManager)
    manager.mode = mode
    manager.max_live_trade_usd = 25.0
    manager.max_open_live_trades = 5
    manager.key_id = "test-key"
    manager.private_key_path = "test.pem"
    manager.kill_switch_active = kill_switch
    # Estado del camino de dinero real en memoria: `orders_file=None` evita que un test
    # escriba `live_orders.json` en el repositorio, que es la misma razón por la que
    # `save_credentials` ya estaba anulado aquí.
    manager.orders_file = None
    manager.live_orders = {}
    manager.last_reconcile = {}
    manager._promotion_cache = {"at": 0.0, "result": None}
    manager.auth = types.SimpleNamespace(
        configured=authenticated,
        load_error=None if authenticated else "sin credenciales",
        headers=lambda *args, **kwargs: {"KALSHI-ACCESS-KEY": "test-key"},
    )
    # `set_mode` persiste el modo en disco; en un test de interlock eso no aporta y
    # deja el .env del operador a medias entre dos estados.
    manager.save_credentials = lambda payload=None, **kwargs: None
    return manager


class _InterlockTestCase(unittest.TestCase):
    """Base con el entorno parcheado y restaurado, y la red interceptada."""

    def setUp(self) -> None:
        self._saved = (
            live_execution.KALSHI_ENV,
            live_execution.ALLOW_REAL_MONEY,
            live_execution.LIVE_ABS_MAX_ORDER_USD,
            kalshi_env_module.kalshi_env.env,
            live_execution.httpx,
        )

    def tearDown(self) -> None:
        (
            live_execution.KALSHI_ENV,
            live_execution.ALLOW_REAL_MONEY,
            live_execution.LIVE_ABS_MAX_ORDER_USD,
            kalshi_env_module.kalshi_env.env,
            live_execution.httpx,
        ) = self._saved

    def _configure(
        self,
        requested_env: str = "production",
        resolved_env: str = "production",
        allow_real_money: bool = False,
        cap: Any = None,
    ) -> _RecordingNetwork:
        """Fija entorno solicitado, entorno resuelto y autorizacion de dinero."""
        live_execution.KALSHI_ENV = requested_env
        live_execution.ALLOW_REAL_MONEY = allow_real_money
        kalshi_env_module.kalshi_env.env = resolved_env
        if cap is not None:
            live_execution.LIVE_ABS_MAX_ORDER_USD = cap
        network = _RecordingNetwork()
        live_execution.httpx = network
        return network


class TestRealMoneyGuard(_InterlockTestCase):
    def test_demo_is_allowed_without_any_flag(self):
        """En demo el dinero es ficticio: exigir el flag ahi seria ruido."""
        self._configure("demo", "demo", allow_real_money=False)
        manager = _bare_manager()
        self.assertIsNone(manager.real_money_guard())
        self.assertTrue(manager.real_money_allowed)
        print("[TEST Interlock] entorno demo -> permitido aunque ALLOW_REAL_MONEY=false")

    def test_production_without_flag_is_blocked(self):
        self._configure("production", "production", allow_real_money=False)
        reason = _bare_manager().real_money_guard()
        self.assertIsNotNone(reason)
        self.assertIn("ALLOW_REAL_MONEY", reason)
        print(f"[TEST Interlock] produccion sin flag -> BLOQUEADO: {reason[:70]}...")

    def test_production_with_flag_is_allowed(self):
        self._configure("production", "production", allow_real_money=True)
        self.assertIsNone(_bare_manager().real_money_guard())
        print("[TEST Interlock] produccion + ALLOW_REAL_MONEY=true -> permitido")

    def test_auto_landing_in_production_is_blocked(self):
        """`KALSHI_ENV=auto` no es una decision del operador.

        Si la deteccion automatica aterriza en produccion con el flag puesto, el
        bloqueo sigue: autorizar el dinero no es lo mismo que elegir produccion.
        """
        self._configure("auto", "production", allow_real_money=True)
        reason = _bare_manager().real_money_guard()
        self.assertIsNotNone(reason)
        self.assertIn("KALSHI_ENV=production", reason)
        print("[TEST Interlock] auto -> produccion -> BLOQUEADO pese al flag")

    def test_kill_switch_wins_over_authorization(self):
        self._configure("production", "production", allow_real_money=True)
        reason = _bare_manager(kill_switch=True).real_money_guard()
        self.assertEqual(reason, "Kill-Switch activo.")
        print("[TEST Interlock] kill-switch manda incluso con autorizacion explicita")


class TestSetModeLive(_InterlockTestCase):
    def test_live_refused_in_production_without_flag(self):
        """No se entra en LIVE si despues cada orden va a ser rechazada.

        Un bot "LIVE" que no opera se parece demasiado a un bot que funciona: el
        panel dice LIVE, no hay errores visibles y no se ejecuta nada.
        """
        self._configure("production", "production", allow_real_money=False)
        manager = _bare_manager()
        result = manager.set_mode("LIVE")
        self.assertFalse(result["success"])
        self.assertEqual(manager.mode, "PAPER")
        self.assertIn("ALLOW_REAL_MONEY", result["message"])
        self.assertFalse(result["status"]["real_money_allowed"])
        print("[TEST Modo] LIVE en produccion sin flag -> rechazado, sigue en PAPER")

    def test_live_allowed_in_demo(self):
        self._configure("demo", "demo", allow_real_money=False)
        manager = _bare_manager()
        result = manager.set_mode("LIVE")
        self.assertTrue(result["success"])
        self.assertEqual(manager.mode, "LIVE")
        print("[TEST Modo] set_mode('LIVE') en demo -> aceptado (dinero ficticio)")

    def test_live_refused_without_credentials(self):
        self._configure("demo", "demo")
        manager = _bare_manager(authenticated=False)
        result = manager.set_mode("LIVE")
        self.assertFalse(result["success"])
        self.assertIn("KALSHI_KEY_ID", result["message"])
        self.assertEqual(manager.mode, "PAPER")
        print("[TEST Modo] set_mode('LIVE') sin credenciales -> rechazado")


class TestExecuteOrderInterlock(_InterlockTestCase):
    @staticmethod
    def _signal(confirm: bool = False) -> Dict[str, Any]:
        signal = {
            "token_id": "TEST-MARKET",
            "token": "Yes",
            "side": "BUY",
            "entry_price": "0.50",
            "recommended_order_type": "LIMIT (Maker)",
        }
        if confirm:
            signal["confirm_live_order"] = True
        return signal

    def test_paper_builds_dry_run_and_never_sends(self):
        network = self._configure("production", "production", allow_real_money=True)
        manager = _bare_manager(mode="PAPER")
        result = asyncio.run(manager.execute_order(self._signal(), None, 25.0))
        self.assertFalse(result["executed"])
        self.assertEqual(result["mode"], "PAPER")
        self.assertIn("dry_run_order", result)
        self.assertEqual(network.posts, [])
        print("[TEST Orden] modo PAPER -> dry run construido y 0 POST enviados")

    def test_live_in_production_without_flag_is_blocked_before_network(self):
        network = self._configure("production", "production", allow_real_money=False)
        manager = _bare_manager(mode="LIVE")
        result = asyncio.run(
            manager.execute_order(self._signal(confirm=True), None, 25.0)
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["blocked_by"], "real_money_interlock")
        self.assertIn("ALLOW_REAL_MONEY", result["error"])
        self.assertEqual(network.posts, [])
        print("[TEST Orden] LIVE en produccion sin flag -> bloqueada con 0 POST")
        print(f"            motivo: {result['error'][:78]}...")

    def test_live_in_demo_reaches_the_send(self):
        """En demo el interlock NO bloquea: el freno es que el dinero es ficticio.

        Se comprueba el POST al endpoint V2 para separar "no se bloqueo" de "no se
        intento": un interlock que bloquea tambien en demo esconde ordenes.
        """
        network = self._configure("demo", "demo", allow_real_money=False)
        manager = _bare_manager(mode="LIVE")
        result = asyncio.run(
            manager.execute_order(self._signal(confirm=True), None, 25.0)
        )
        self.assertEqual(len(network.posts), 1)
        self.assertIn("/portfolio/events/orders", network.posts[0]["url"])
        self.assertTrue(result["executed"])
        print("[TEST Orden] LIVE en demo -> 1 POST a /portfolio/events/orders")
        print(f"            payload: {network.posts[0]['json']}")

    def test_live_without_confirmation_flag_does_not_send(self):
        """`confirm_live_order` es el tercer candado y se comprueba por separado."""
        network = self._configure("demo", "demo", allow_real_money=False)
        manager = _bare_manager(mode="LIVE")
        result = asyncio.run(manager.execute_order(self._signal(), None, 25.0))
        self.assertFalse(result["executed"])
        self.assertIn("confirm_live_order", result["error"])
        self.assertEqual(network.posts, [])
        print("[TEST Orden] LIVE sin confirm_live_order -> no se envia nada")

    def test_hard_cap_beats_a_huge_ui_setting(self):
        """El tope de config manda sobre el ajuste de UI: un bug de sizing no lo salta."""
        self._configure("production", "production", allow_real_money=True)
        cap = live_execution.LIVE_ABS_MAX_ORDER_USD
        manager = _bare_manager(mode="PAPER")
        manager.max_live_trade_usd = 1_000_000.0
        result = asyncio.run(manager.execute_order(self._signal(), None, 1_000_000.0))
        sent = float(result["dry_run_order"]["count"])
        self.assertAlmostEqual(sent, cap / 0.50, places=2)
        self.assertLess(sent, 1_000_000.0 / 0.50)
        print(f"[TEST Sizing] UI $1.000.000 -> tope duro ${cap:.0f} -> {sent:.0f} contratos")


if __name__ == "__main__":
    unittest.main()

