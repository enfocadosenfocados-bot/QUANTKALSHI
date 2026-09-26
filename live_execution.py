"""Módulo de ejecución para Kalshi.

Por seguridad arranca siempre en PAPER. Para demo/live real se requieren:
KALSHI_KEY_ID y KALSHI_PRIVATE_KEY_PATH en .env. Este módulo prepara la firma y
validación; la colocación real de órdenes se mantiene bloqueada salvo que el modo
LIVE esté explícitamente activado y las credenciales existan.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from config import (
    ALLOW_REAL_MONEY,
    KALSHI_ENV,
    KALSHI_KEY_ID,
    KALSHI_PRIVATE_KEY_PATH,
    LIVE_ABS_MAX_ORDER_USD,
    kalshi_credentials,
)
from kalshi_auth import KalshiAuth
from kalshi_env import kalshi_env, mask_key_id

CREDENTIALS_FILE = Path(__file__).resolve().parent / "kalshi_credentials.json"


class LiveExecutionManager:
    """Gestor de órdenes reales en Kalshi con salvaguardas de riesgo."""

    def __init__(self):
        self.mode: str = "PAPER"
        self.max_live_trade_usd: float = 25.0
        self.max_open_live_trades: int = 5
        self.key_id: str = KALSHI_KEY_ID
        self.private_key_path: str = KALSHI_PRIVATE_KEY_PATH
        self.kill_switch_active: bool = False
        self.auth = KalshiAuth(self.key_id, self.private_key_path)
        self.load_credentials()
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
            "client_order_id": str(uuid.uuid4()),
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

    async def execute_order(self, signal: Dict[str, Any], market: Any, size_usd: float) -> Dict[str, Any]:
        """Preparar y (solo en modo LIVE) enviar una orden Kalshi V2.

        Nota: se devuelve `dry_run` salvo que mode=LIVE. La activación real queda
        deliberadamente protegida para evitar órdenes accidentales.
        """
        price = float(signal.get("entry_price") or signal.get("market_price") or 0)
        # Doble tope de tamaño: el ajuste de UI (`max_live_trade_usd`) y el freno
        # duro de config. Se aplica el menor, porque un bug de sizing no debería
        # poder saltarse el límite subiendo el ajuste desde el dashboard.
        effective_cap = min(self.max_live_trade_usd, LIVE_ABS_MAX_ORDER_USD)
        count = min(float(size_usd), effective_cap) / max(price, 0.01)
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
                return {
                    "executed": True,
                    "mode": "LIVE",
                    "order_id": order.get("order_id"),
                    "fill_count": order.get("fill_count"),
                    "remaining_count": order.get("remaining_count"),
                    "average_fill_price": order.get("average_fill_price"),
                    "average_fee_paid": order.get("average_fee_paid"),
                    "kalshi_response": data,
                    "size_usd": size_usd,
                    "environment": kalshi_env.env,
                }
            return {
                "executed": False,
                "mode": "LIVE",
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "payload": order_payload,
                "environment": kalshi_env.env,
            }

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
