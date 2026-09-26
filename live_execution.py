"""Módulo de ejecución para Kalshi.

Por seguridad arranca siempre en PAPER. Para demo/live real se requieren:
KALSHI_KEY_ID y KALSHI_PRIVATE_KEY_PATH en .env. Este módulo prepara la firma y
validación; la colocación real de órdenes se mantiene bloqueada salvo que el modo
LIVE esté explícitamente activado y las credenciales existan.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from config import (
    ALLOW_REAL_MONEY,
    KALSHI_ENV,
    KALSHI_KEY_ID,
    KALSHI_PRIVATE_KEY_PATH,
    LIVE_ABS_MAX_ORDER_USD,
    LIVE_ORDER_MAX_AGE_SEC,
    LIVE_REQUIRE_PROMOTION,
    MAX_OPEN_LIVE_TRADES,
    kalshi_credentials,
)
from kalshi_auth import KalshiAuth
from kalshi_env import kalshi_env, mask_key_id

CREDENTIALS_FILE = Path(__file__).resolve().parent / "kalshi_credentials.json"

# Registro de las órdenes REALES enviadas. Es la única memoria que el bot tiene de lo
# que dejó vivo en el exchange: sin él, un reinicio olvida las órdenes resting y nadie
# las cancela, y no hay forma de comparar lo que el bot cree con lo que pasó de verdad.
LIVE_ORDERS_FILE = Path(__file__).resolve().parent / "live_orders.json"

# Rutas V2 para leer órdenes vivas. El alta y la cancelación usan el endpoint de
# "events orders"; la lectura se intenta en ese mismo prefijo y, si la API no lo
# expone, en el antiguo `/portfolio/orders`. Si ninguna responde, la reconciliación
# lo dice en vez de fingir que todo cuadra.
OPEN_ORDER_PATHS: tuple = ("/portfolio/events/orders", "/portfolio/orders")


class LiveExecutionManager:
    """Gestor de órdenes reales en Kalshi con salvaguardas de riesgo."""

    def __init__(self):
        self.mode: str = "PAPER"
        self.max_live_trade_usd: float = 25.0
        self.max_open_live_trades: int = MAX_OPEN_LIVE_TRADES
        self.key_id: str = KALSHI_KEY_ID
        self.private_key_path: str = KALSHI_PRIVATE_KEY_PATH
        self.kill_switch_active: bool = False
        # Registro de órdenes reales: `live_orders.json` en disco (None = solo memoria,
        # que es lo que usan los tests para no escribir en el repositorio).
        self.orders_file: Optional[Path] = LIVE_ORDERS_FILE
        self.live_orders: Dict[str, Dict[str, Any]] = {}
        self.last_reconcile: Dict[str, Any] = {}
        self._promotion_cache: Dict[str, Any] = {"at": 0.0, "result": None}
        self.auth = KalshiAuth(self.key_id, self.private_key_path)
        self.load_credentials()
        self.load_live_orders()
        # Con KALSHI_ENV=auto se detecta el entorno valido para estas credenciales.
        kalshi_env.resolve(self.auth)

    @property
    def is_live(self) -> bool:
        return self.mode == "LIVE"

    def real_money_guard(self) -> Optional[str]:
        """Motivo por el que NO se puede operar con dinero real (None = autorizado).

        `KALSHI_ENV=production` es el entorno de DATOS del bot, así que estar en
        producción no implica poder enviar órdenes: aquí se exige un acto
        explícito y separado. Las condiciones son deliberadamente redundantes
        porque cada una tapa un accidente distinto:

          1. Entorno distinto de producción (demo) -> dinero ficticio, permitido.
          2. `KALSHI_ENV != "production"` -> el operador no fijó producción; es el
             caso de `auto`, donde el entorno se detecta solo y podría aterrizar
             en producción sin que nadie lo eligiera.
          3. `ALLOW_REAL_MONEY != true` -> no hay autorización para arriesgar
             capital real.
          4. Kill-switch activo.

        Devuelve el texto del bloqueo para que quede en la respuesta y en los
        logs: un rechazo silencioso es indistinguible de un bug de cableado.
        """
        if kalshi_env.env != "production":
            return None
        if KALSHI_ENV != "production":
            return (
                "Orden real bloqueada: el entorno se resolvio a produccion sin que "
                f"KALSHI_ENV lo fijara (KALSHI_ENV={KALSHI_ENV}). Pon "
                "KALSHI_ENV=production de forma explicita para operar con dinero real."
            )
        if not ALLOW_REAL_MONEY:
            return (
                "Orden real bloqueada por el interlock de dinero real: "
                "KALSHI_ENV=production y ALLOW_REAL_MONEY no esta activado. "
                "Anade ALLOW_REAL_MONEY=true al .env solo cuando el capital real este "
                "fondeado y las validaciones en demo esten cerradas."
            )
        if self.kill_switch_active:
            return "Kill-Switch activo."
        return None

    @property
    def real_money_allowed(self) -> bool:
        return self.real_money_guard() is None

    def load_credentials(self):
        if CREDENTIALS_FILE.exists():
            try:
                data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
                self.mode = data.get("mode", "PAPER")
                self.max_live_trade_usd = float(data.get("max_live_trade_usd", self.max_live_trade_usd))
                self.key_id = data.get("key_id") or self.key_id
                self.private_key_path = data.get("private_key_path") or self.private_key_path
                self.auth = KalshiAuth(self.key_id, self.private_key_path)
                kalshi_env.resolve(self.auth)
            except Exception as exc:
                print(f"[LiveExecution] Error leyendo credenciales Kalshi: {exc}")

    def save_credentials(self, data: Dict[str, Any]):
        try:
            self.mode = data.get("mode", self.mode)
            self.max_live_trade_usd = float(data.get("max_live_trade_usd", self.max_live_trade_usd))
            if data.get("key_id") and data.get("key_id") != "***":
                self.key_id = str(data["key_id"])
            if data.get("private_key_path") and data.get("private_key_path") != "***":
                self.private_key_path = str(data["private_key_path"])
            # Compatibilidad con nombres antiguos del dashboard.
            if data.get("api_key") and data.get("api_key") != "***":
                self.key_id = str(data["api_key"])
            self.auth = KalshiAuth(self.key_id, self.private_key_path)
            to_save = {
                "mode": self.mode,
                "max_live_trade_usd": self.max_live_trade_usd,
                "key_id": self.key_id,
                "private_key_path": self.private_key_path,
                "exchange": "kalshi",
                "environment": kalshi_env.env,
                "updated_at": time.time(),
            }
            CREDENTIALS_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
            print("[LiveExecution] Configuración Kalshi actualizada.")
        except Exception as exc:
            print(f"[LiveExecution] Error guardando credenciales Kalshi: {exc}")

    # ========== Registro local de órdenes reales ==========
    #
    # El camino de dinero real necesita memoria propia: qué se envió, con qué
    # client_order_id y qué quedó vivo. Sin esto, un reinicio (o un crash) deja
    # órdenes resting en el exchange que nadie cancela y que el bot ya no recuerda.
    def load_live_orders(self) -> Dict[str, Dict[str, Any]]:
        orders: Dict[str, Dict[str, Any]] = {}
        if self.orders_file and self.orders_file.exists():
            try:
                raw = json.loads(self.orders_file.read_text(encoding="utf-8"))
                orders = {
                    str(key): value
                    for key, value in (raw.get("orders") or {}).items()
                    if isinstance(value, dict)
                }
            except Exception as exc:
                print(f"[LiveExecution] Error leyendo {self.orders_file.name}: {exc}")
        self.live_orders = orders
        return orders

    def _save_live_orders(self):
        """Escritura atómica (tmp + replace).

        Un `live_orders.json` truncado por un corte de luz no se puede distinguir de
        "no había órdenes", y esa diferencia decide si se cancelan posiciones vivas.
        """
        if not self.orders_file:
            return
        try:
            payload = {
                "updated_at": time.time(),
                "environment": kalshi_env.env,
                "orders": self.live_orders,
            }
            tmp_path = self.orders_file.with_suffix(self.orders_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self.orders_file)
        except Exception as exc:
            print(f"[LiveExecution] No se pudo guardar live_orders.json: {exc}")

    def _orders(self) -> Dict[str, Dict[str, Any]]:
        """Estado local, tolerante a managers construidos sin `__init__` (tests)."""
        orders = getattr(self, "live_orders", None)
        if orders is None:
            orders = self.load_live_orders()
        return orders

    @staticmethod
    def _is_open(order: Dict[str, Any]) -> bool:
        """Una orden sigue viva si no consta cerrada y le queda remanente."""
        closed_status = {
            "canceled",
            "cancelled",
            "filled",
            "rejected",
            # Cerrada por la reconciliación: el exchange ya no la tenía viva.
            "closed_remotely",
        }
        if str(order.get("status") or "") in closed_status:
            return False
        remaining = order.get("remaining_count")
        if remaining is None:
            return True
        try:
            return float(remaining) > 0
        except (TypeError, ValueError):
            return True

    def open_live_orders(self) -> List[Dict[str, Any]]:
        return [order for order in self._orders().values() if self._is_open(order)]

    def open_live_order_count(self) -> int:
        return len(self.open_live_orders())

    def recorded_client_order_ids(self) -> set:
        return {
            str(order.get("client_order_id"))
            for order in self._orders().values()
            if order.get("client_order_id")
        }

    def record_live_order(self, order: Dict[str, Any]):
        """Guarda la orden enviada antes de que el proceso pueda olvidarla."""
        key = str(
            order.get("client_order_id")
            or order.get("order_id")
            or f"order-{int(time.time() * 1000)}"
        )
        self._orders()[key] = {**order, "recorded_at": order.get("recorded_at") or time.time()}
        self._save_live_orders()
        return key

    def live_orders_status(self) -> Dict[str, Any]:
        orders = sorted(
            self._orders().values(),
            key=lambda o: float(o.get("recorded_at") or 0.0),
            reverse=True,
        )
        open_orders = [o for o in orders if self._is_open(o)]
        return {
            "environment": kalshi_env.env,
            "mode": self.mode,
            "orders_file": self.orders_file.name if self.orders_file else None,
            "stored": len(orders),
            "open": len(open_orders),
            "max_open_live_trades": getattr(self, "max_open_live_trades", MAX_OPEN_LIVE_TRADES),
            "max_order_age_sec": LIVE_ORDER_MAX_AGE_SEC,
            "open_orders": open_orders,
            "last_reconcile": getattr(self, "last_reconcile", {}),
            "orders": orders[:50],
        }

    # ========== Puerta de promoción ==========
    def promotion_gate(self, code: str, max_age: float = 60.0) -> Dict[str, Any]:
        """¿Autoriza el tablero de promoción a esta estrategia a arriesgar capital?

        El tablero existía pero nadie lo consultaba al enviar: era un informe, no una
        puerta. Un fallo evaluándolo NO autoriza (fail-closed): si no se puede leer el
        estado, la estrategia se queda en paper.
        """
        code = str(code or "").strip()
        if not code:
            return {
                "applies": False,
                "allowed": True,
                "code": "",
                "state": "",
                "reason": "Sin strategy_code: no hay estrategia que promocionar.",
            }
        now = time.time()
        cache = getattr(self, "_promotion_cache", None)
        if not isinstance(cache, dict):
            cache = {"at": 0.0, "result": None}
            self._promotion_cache = cache
        cached = cache.get("result")
        if (
            isinstance(cached, dict)
            and cached.get("code") == code
            and now - float(cache.get("at") or 0.0) < max_age
        ):
            return cached

        result: Dict[str, Any]
        try:
            from paper_tracker import paper_tracker
            from strategy_governor import governor
            from strategy_promotion import build_promotion_board

            board = build_promotion_board(paper_tracker, governor)
            row = next(
                (r for r in (board.get("rows") or []) if str(r.get("code")) == code),
                None,
            )
        except Exception as exc:
            result = {
                "applies": True,
                "allowed": False,
                "code": code,
                "state": "ERROR",
                "reason": f"No se pudo evaluar el tablero ({type(exc).__name__}: {exc}).",
            }
            cache.update({"at": now, "result": result})
            return result

        if row is None:
            result = {
                "applies": True,
                "allowed": False,
                "code": code,
                "state": "SIN_DATOS",
                "closed_trades": 0,
                "reason": f"'{code}' no tiene operaciones cerradas en paper; sin muestra no hay autorización.",
            }
        else:
            state = str(row.get("state") or "")
            result = {
                "applies": True,
                "allowed": state in {"LIVE_PEQUENO", "LIVE"},
                "code": code,
                "state": state,
                "closed_trades": row.get("closed_trades"),
                "reason": row.get("reason") or "",
            }
        cache.update({"at": now, "result": result})
        return result

    def get_public_status(self) -> Dict[str, Any]:
        masked_key = mask_key_id(self.key_id) or "No configurada"
        return {
            "exchange": "kalshi",
            "environment": kalshi_env.env,
            "requested_environment": KALSHI_ENV,
            "environment_detection": kalshi_env.detection_note,
            "auth_verified": kalshi_env.auth_verified,
            "environment_probe": kalshi_env.probe_results,
            "mode": self.mode,
            "is_live": self.mode == "LIVE",
            "has_credentials": bool(self.auth.configured),
            "auth_error": self.auth.load_error,
            "key_id": masked_key,
            "private_key_path": self.private_key_path or "No configurada",
            "max_live_trade_usd": self.max_live_trade_usd,
            "max_open_live_trades": self.max_open_live_trades,
            "kill_switch_active": self.kill_switch_active,
            # Interlock de dinero real: se publica el estado para que el dashboard
            # pueda avisar antes de que alguien pulse "LIVE" en produccion.
            "allow_real_money": ALLOW_REAL_MONEY,
            "real_money_allowed": self.real_money_allowed,
            "real_money_block_reason": self.real_money_guard(),
            "abs_max_order_usd": LIVE_ABS_MAX_ORDER_USD,
            # Estado del registro de órdenes reales: sin esto el dashboard no puede
            # distinguir "no he enviado nada" de "tengo 3 órdenes vivas sin recordar".
            "open_live_orders": self.open_live_order_count(),
            "require_promotion": LIVE_REQUIRE_PROMOTION,
            "max_open_live_trades": getattr(self, "max_open_live_trades", MAX_OPEN_LIVE_TRADES),
            "last_reconcile": getattr(self, "last_reconcile", {}),
        }

    def apply_environment(self, env: str) -> Dict[str, Any]:
        """Cambia el entorno y reconstruye el auth con la credencial del destino.

        Demo y producción no comparten API keys, así que cambiar de host sin
        cambiar de credencial deja todas las llamadas privadas en 401.
        """
        previous = kalshi_env.env
        state = kalshi_env.switch(env)
        creds = kalshi_env.active_credentials
        self.key_id = creds.get("key_id", "")
        self.private_key_path = creds.get("private_key_path", "")
        self.auth = KalshiAuth(
            self.key_id, self.private_key_path, creds.get("private_key_pem", "")
        )
        state = dict(state)
        state["previous_environment"] = previous
        return state

    def set_mode(self, new_mode: str) -> Dict[str, Any]:
        valid_mode = "LIVE" if str(new_mode).upper() == "LIVE" else "PAPER"
        if valid_mode == "LIVE" and not self.auth.configured:
            return {
                "success": False,
                "message": "No se puede activar LIVE sin KALSHI_KEY_ID y private key configurados. Recomiendo probar primero en DEMO.",
                "status": self.get_public_status(),
            }
        if valid_mode == "LIVE" and self.kill_switch_active:
            return {
                "success": False,
                "message": "Kill-Switch activo. Desactívalo antes de LIVE.",
                "status": self.get_public_status(),
            }
        # Entrar en LIVE sin autorizacion de dinero real porque despues el
        # interlock bloquearia cada orden: el bot quedaria "LIVE" sin operar y
        # parecería que funciona. Es mejor no dejar entrar en ese estado.
        if valid_mode == "LIVE":
            guard = self.real_money_guard()
            if guard:
                return {
                    "success": False,
                    "message": guard,
                    "status": self.get_public_status(),
                }
        self.mode = valid_mode
        self.save_credentials({"mode": self.mode})
        return {
            "success": True,
            "message": f"Modo cambiado a {'🟢 KALSHI LIVE/DEMO REAL' if self.mode == 'LIVE' else '🧪 PAPER Trading'}",
            "status": self.get_public_status(),
        }

    def activate_kill_switch(self) -> Dict[str, Any]:
        self.kill_switch_active = True
        self.mode = "PAPER"
        self.save_credentials({"mode": "PAPER"})
        return {"success": True, "message": "🛑 Kill-Switch activado. Modo PAPER.", "status": self.get_public_status()}

    def deactivate_kill_switch(self) -> Dict[str, Any]:
        self.kill_switch_active = False
        return {"success": True, "message": "Kill-Switch desactivado. El modo sigue en PAPER hasta que lo cambies manualmente.", "status": self.get_public_status()}

    async def get_balance(self) -> Dict[str, Any]:
        """Consulta el saldo reintentando ante 5xx.

        En demo el endpoint de balance devuelve HTTP 500 de forma intermitente
        (~30% de los intentos medidos), así que un único intento da falsos
        negativos al diagnosticar las credenciales.
        """
        path = "/trade-api/v2/portfolio/balance"
        headers = self.auth.headers("GET", path)
        if not headers:
            return {"success": False, "error": self.auth.load_error or "Credenciales no configuradas"}
        last_error = ""
        async with httpx.AsyncClient(timeout=10.0) as client:
            for attempt in range(3):
                try:
                    resp = await client.get(f"{kalshi_env.rest_base}/portfolio/balance", headers=headers)
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                if resp.status_code == 200:
                    return {
                        "success": True,
                        "balance": resp.json(),
                        "environment": kalshi_env.env,
                    }
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code < 500:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
        return {"success": False, "error": last_error, "environment": kalshi_env.env}

    @staticmethod
    def build_client_order_id(signal: Dict[str, Any]) -> str:
        """Idempotencia: el mismo intento lógico reutiliza el mismo client_order_id.

        Derivarlo del `dedupe_key`/`signal_id` (en vez de un uuid4 nuevo por intento)
        es lo que convierte un reintento tras un timeout en un rechazo por duplicado
        en el exchange, y no en una segunda posición real.
        """
        anchor = signal.get("dedupe_key") or signal.get("signal_id")
        if anchor:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"quantkalshi:{anchor}"))
        return str(uuid.uuid4())

    @staticmethod
    def build_order_payload(
        signal: Dict[str, Any], market: Any, count: float
    ) -> Dict[str, Any]:
        """Construye el payload V2 de Kalshi.

        Kalshi retiró el endpoint v1 (devuelve HTTP 410
        `deprecated_v1_order_endpoint`). En V2 todo se cotiza desde la pata YES:

            side="bid" -> comprar YES a `price`
            side="ask" -> vender YES a `price` (equivalente a comprar NO a 1-price)

        Por eso comprar NO exige `side="ask"` con el precio complementario: no
        existe un campo `outcome_side`/`no_price_dollars` y enviarlo hace que la
        orden sea rechazada.
        """
        ticker = (
            signal.get("token_id")
            or getattr(market, "market_id", None)
            or signal.get("market_id")
        )
        price = float(signal.get("entry_price") or signal.get("market_price") or 0)
        price = max(0.01, min(0.99, price))

        token_text = str(signal.get("token") or "Yes").lower()
        side_text = str(signal.get("side") or "BUY").upper()
        is_no_leg = "no" in token_text and "no" not in str(signal.get("side", "")).lower()

        # Comprar NO a Q equivale a vender YES a (1 - Q): en V2 solo hay pata YES.
        kalshi_price = round(1.0 - price, 4) if is_no_leg else price
        kalshi_price = max(0.01, min(0.99, kalshi_price))

        # bid = comprar YES; ask = vender YES. Operar la pata NO invierte el lado.
        sell_yes = (side_text == "SELL") != is_no_leg
        kalshi_side = "ask" if sell_yes else "bid"

        order_type = str(signal.get("recommended_order_type") or "").lower()
        post_only = bool(signal.get("post_only")) or ("maker" in order_type)
        return {
            "ticker": ticker,
            "client_order_id": LiveExecutionManager.build_client_order_id(signal),
            "side": kalshi_side,
            "count": f"{max(1.0, float(count)):.2f}",
            "price": f"{kalshi_price:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "cancel_order_on_pause": True,
            "reduce_only": False,
            "subaccount": 0,
        }

    async def execute_order(self, signal: Dict[str, Any], market: Any, size_usd: Optional[float]) -> Dict[str, Any]:
        """Preparar y (solo en modo LIVE) enviar una orden Kalshi V2.

        Firma obligatoria: `execute_order(signal, market, size_usd)`. El tamaño en
        dólares debe venir ya calculado por el gestor de riesgo: cuando esta función se
        llamaba con menos argumentos, el `TypeError` se perdía en un `except` genérico y
        el camino de dinero real quedaba muerto sin que nadie se enterara.

        Nota: se devuelve `dry_run` salvo que mode=LIVE. La activación real queda
        deliberadamente protegida para evitar órdenes accidentales.
        """
        if not isinstance(signal, dict):
            return {
                "executed": False,
                "blocked_by": "invalid_signal",
                "error": "execute_order(signal, market, size_usd): `signal` debe ser un dict.",
            }
        try:
            size_value = float(size_usd) if size_usd is not None else 0.0
        except (TypeError, ValueError):
            size_value = 0.0
        if size_value <= 0:
            # Un tamaño ausente no puede acabar en una orden: `count` salía como
            # 0/precio y `build_order_payload` lo subía a 1 contrato por su max(1.0), de
            # modo que el fallo se convertía en una orden pequeña y silenciosa.
            return {
                "executed": False,
                "mode": self.mode,
                "blocked_by": "invalid_size",
                "error": (
                    f"size_usd inválido o ausente ({size_usd!r}): la orden necesita el tamaño "
                    "en dólares ya calculado por el gestor de riesgo."
                ),
            }
        price = float(signal.get("entry_price") or signal.get("market_price") or 0)
        # Doble tope de tamaño: el ajuste de UI (`max_live_trade_usd`) y el freno
        # duro de config. Se aplica el menor, porque un bug de sizing no debería
        # poder saltarse el límite subiendo el ajuste desde el dashboard.
        effective_cap = min(self.max_live_trade_usd, LIVE_ABS_MAX_ORDER_USD)
        count = min(size_value, effective_cap) / max(price, 0.01)
        order_payload = self.build_order_payload(signal, market, count)

        if self.mode != "LIVE":
            return {
                "executed": False,
                "mode": "PAPER",
                "dry_run_order": order_payload,
                "message": "Simulación; no se envió a Kalshi.",
            }

        # Interlock de dinero real ANTES de cualquier otra comprobación. Es el
        # candado que impide que arreglar el cableado de `execute_order` haga
        # aterrizar la primera orden del bot en producción con capital real.
        guard = self.real_money_guard()
        if guard:
            return {
                "executed": False,
                "mode": "LIVE",
                "environment": kalshi_env.env,
                "dry_run_order": order_payload,
                "blocked_by": "real_money_interlock",
                "error": guard,
            }
        if self.kill_switch_active:
            return {"executed": False, "error": "Kill-Switch activo."}
        if not self.auth.configured:
            return {"executed": False, "error": "Credenciales Kalshi no configuradas."}

        # Protección extra: requiere confirmación explícita en payload para no disparar órdenes por accidente.
        if not signal.get("confirm_live_order"):
            return {
                "executed": False,
                "mode": "LIVE",
                "dry_run_order": order_payload,
                "error": "Falta confirm_live_order=true; orden no enviada por seguridad.",
            }

        # Puerta de promoción. Se evalúa por estrategia y solo si la señal declara una:
        # sin `strategy_code` no hay tablero que consultar. Fail-closed: si el tablero no
        # se puede leer, la orden no sale.
        if LIVE_REQUIRE_PROMOTION:
            gate = self.promotion_gate(str(signal.get("strategy_code") or ""))
            if gate.get("applies") and not gate.get("allowed"):
                return {
                    "executed": False,
                    "mode": "LIVE",
                    "environment": kalshi_env.env,
                    "dry_run_order": order_payload,
                    "blocked_by": "promotion_gate",
                    "promotion_state": gate.get("state"),
                    "error": (
                        f"Estrategia '{gate.get('code')}' no autorizada para dinero real "
                        f"(estado {gate.get('state')}): {gate.get('reason')}"
                    ),
                }

        # Idempotencia: si el client_order_id ya se envió, esto es un reintento (timeout,
        # reinicio, doble señal) y no una orden nueva.
        client_order_id = str(order_payload.get("client_order_id") or "")
        if client_order_id and client_order_id in self.recorded_client_order_ids():
            return {
                "executed": False,
                "mode": "LIVE",
                "environment": kalshi_env.env,
                "blocked_by": "duplicate_client_order_id",
                "client_order_id": client_order_id,
                "error": "Esta orden ya se envió (mismo client_order_id); no se duplica.",
            }

        # Tope de órdenes vivas: `max_open_live_trades` se publicaba en el dashboard pero
        # no se comprobaba al enviar.
        max_open = int(getattr(self, "max_open_live_trades", MAX_OPEN_LIVE_TRADES) or 0)
        open_count = self.open_live_order_count()
        if max_open and open_count >= max_open:
            return {
                "executed": False,
                "mode": "LIVE",
                "environment": kalshi_env.env,
                "blocked_by": "max_open_live_trades",
                "open_live_orders": open_count,
                "max_open_live_trades": max_open,
                "error": (
                    f"Tope de órdenes reales simultáneas alcanzado ({open_count}/{max_open}): "
                    "espera a que se llenen o se cancelen (o llama a /api/live/reconcile)."
                ),
            }

        headers = self.auth.headers("POST", "/trade-api/v2/portfolio/events/orders")
        headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{kalshi_env.rest_base}/portfolio/events/orders",
                json=order_payload,
                headers=headers,
            )
            if resp.status_code in (200, 201):
                data = resp.json() or {}
                order = data.get("order") or data
                # V2 devuelve fill_count/remaining_count y, si hubo fill inmediato,
                # average_fill_price y average_fee_paid: la comisión REAL cobrada.
                remaining = order.get("remaining_count")
                try:
                    remaining_value = float(remaining)
                except (TypeError, ValueError):
                    remaining_value = None
                # Se registra ANTES de devolver: si el proceso muere justo después de
                # enviar, el arranque siguiente tiene que saber que esta orden existe
                # para poder cancelarla o contarla contra el tope.
                self.record_live_order({
                    "order_id": order.get("order_id"),
                    "client_order_id": order_payload.get("client_order_id"),
                    "ticker": order_payload.get("ticker"),
                    "side": order_payload.get("side"),
                    "price": order_payload.get("price"),
                    "count": order_payload.get("count"),
                    "fill_count": order.get("fill_count"),
                    "remaining_count": remaining,
                    "average_fill_price": order.get("average_fill_price"),
                    "average_fee_paid": order.get("average_fee_paid"),
                    "strategy_code": signal.get("strategy_code"),
                    "signal_id": signal.get("signal_id"),
                    "size_usd": size_value,
                    "status": (
                        "unknown"
                        if remaining_value is None
                        else ("filled" if remaining_value <= 0 else "resting")
                    ),
                    "environment": kalshi_env.env,
                })
                return {
                    "executed": True,
                    "mode": "LIVE",
                    "order_id": order.get("order_id"),
                    "client_order_id": order_payload.get("client_order_id"),
                    "fill_count": order.get("fill_count"),
                    "remaining_count": order.get("remaining_count"),
                    "average_fill_price": order.get("average_fill_price"),
                    "average_fee_paid": order.get("average_fee_paid"),
                    "kalshi_response": data,
                    "size_usd": size_value,
                    "environment": kalshi_env.env,
                }
            return {
                "executed": False,
                "mode": "LIVE",
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "payload": order_payload,
                "environment": kalshi_env.env,
            }

    # ========== Reconciliación con el exchange ==========
    async def fetch_resting_order_ids(self) -> Dict[str, Any]:
        """IDs de órdenes vivas según Kalshi. Fail-soft y sin inventar la respuesta.

        Si el listado no existe en ninguna ruta conocida (404/405), se devuelve
        `ok=False` con el motivo: eso es información que el operador necesita, mucho
        mejor que una reconciliación que dice "todo bien" sin haber mirado.
        """
        if not self.auth.configured:
            return {"ok": False, "error": self.auth.load_error or "Credenciales no configuradas"}
        last_error = ""
        for path in OPEN_ORDER_PATHS:
            headers = self.auth.headers("GET", f"/trade-api/v2{path}")
            if not headers:
                return {"ok": False, "error": self.auth.load_error or "Credenciales no configuradas"}
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{kalshi_env.rest_base}{path}",
                        headers=headers,
                        params={"status": "resting", "limit": 200},
                    )
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if resp.status_code == 200:
                data = resp.json() or {}
                orders = data.get("orders") or []
                return {
                    "ok": True,
                    "path": path,
                    "orders": orders,
                    "ids": {str(o.get("order_id")) for o in orders if o.get("order_id")},
                }
            last_error = f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code not in (404, 405):
                break
        return {"ok": False, "error": last_error or "sin respuesta del listado de órdenes"}

    async def reconcile_orders(self, max_age_sec: Optional[float] = None) -> Dict[str, Any]:
        """Cuadra el registro local con el exchange y cancela lo que quedó vivo.

        Cubre dos agujeros del camino live:

          * Órdenes que el exchange ya no tiene vivas (se llenaron, o las canceló algo
            fuera del bot): se marcan para que no sigan contando contra el tope.
          * Órdenes resting más viejas que `LIVE_ORDER_MAX_AGE_SEC`: se cancelan.
            `good_till_canceled` no caduca sola, y una orden viva es una posición que
            nadie decidió tomar.
        """
        max_age = LIVE_ORDER_MAX_AGE_SEC if max_age_sec is None else float(max_age_sec)
        summary: Dict[str, Any] = {
            "at": time.time(),
            "environment": kalshi_env.env,
            "checked": 0,
            "canceled": [],
            "closed_remotely": [],
            "errors": [],
            "exchange_ok": None,
            "open_after": 0,
        }
        local_open = self.open_live_orders()
        summary["checked"] = len(local_open)
        if not local_open:
            summary["open_after"] = 0
            self.last_reconcile = summary
            return summary

        exchange = await self.fetch_resting_order_ids()
        summary["exchange_ok"] = bool(exchange.get("ok"))
        if exchange.get("ok"):
            summary["exchange_path"] = exchange.get("path")
        else:
            summary["errors"].append(str(exchange.get("error")))
        exchange_ids = exchange.get("ids") if exchange.get("ok") else None

        now = time.time()
        for order in local_open:
            order_id = str(order.get("order_id") or "")
            if exchange_ids is not None and order_id and order_id not in exchange_ids:
                # El exchange ya no la tiene: se llenó o la canceló algo fuera del bot.
                order["status"] = "closed_remotely"
                order["reconciled_at"] = now
                summary["closed_remotely"].append(order_id)
                continue
            age = now - float(order.get("recorded_at") or now)
            if not order_id or age < max_age:
                continue
            result = await self.cancel_order(order_id)
            if result.get("success"):
                order["status"] = "canceled"
                order["canceled_at"] = now
                order["cancel_reason"] = f"reconciliacion: viva {age:.0f}s (max {max_age:.0f}s)"
                summary["canceled"].append(order_id)
            else:
                summary["errors"].append(f"{order_id}: {result.get('error')}")

        summary["open_after"] = self.open_live_order_count()
        self._save_live_orders()
        self.last_reconcile = summary
        if summary["errors"]:
            print(f"[LiveExecution] Reconciliacion con incidencias: {summary['errors']}")
        return summary

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancela una orden con el endpoint V2 (el v1 devuelve HTTP 410)."""
        if not order_id:
            return {"success": False, "error": "order_id vacío"}
        path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
        headers = self.auth.headers("DELETE", path)
        if not headers:
            return {"success": False, "error": self.auth.load_error or "Credenciales no configuradas"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(
                    f"{kalshi_env.rest_base}/portfolio/events/orders/{order_id}",
                    headers=headers,
                )
            if resp.status_code in (200, 201, 204):
                return {"success": True, "environment": kalshi_env.env}
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


live_manager = LiveExecutionManager()
