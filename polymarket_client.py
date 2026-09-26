"""Cliente async para Kalshi DEMO/PROD.

Mantiene una interfaz compatible con el antiguo `PolymarketClient` para que el
resto del dashboard funcione mientras se migra gradualmente todo a Kalshi.
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import httpx
import websockets

from config import (
    HEARTBEAT_INTERVAL,
    KALSHI_ENV,
    KALSHI_READ_TOKENS_PER_SECOND,
    KALSHI_REQUEST_TOKEN_COST,
    KALSHI_RATE_HEADROOM,
    KALSHI_WEBSOCKET_ENABLED,
    MAX_EVENTS_TRACKED,
    MAX_MARKETS_SCANNED,
    MAX_MARKETS_TRACKED,
    MARKET_MIN_OPEN_INTEREST,
    MARKET_MIN_VOLUME_24H,
    MAX_MARKETS_WITH_EVENT_CONTEXT,
    MAX_MARKETS_WS,
)
from kalshi_auth import KalshiAuth
from kalshi_env import kalshi_env, mask_key_id
from market_registry import registry


class TokenBucket:
    """Limitador de tasa estilo token bucket para respetar los limites de Kalshi.

    Kalshi factura por tokens (10 por request por defecto) y el tier basic
    recarga 200 tokens/s en lectura. Se reserva un margen de seguridad para no
    provocar 429 en una ejecucion 24/7.
    """

    def __init__(self, tokens_per_second: float, cost_per_request: float, headroom: float = 0.6):
        self.rate = max(1.0, float(tokens_per_second) * max(0.1, min(1.0, headroom)))
        self.cost = max(1.0, float(cost_per_request))
        self.capacity = self.rate * 3.0
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= self.cost:
                    self._tokens -= self.cost
                    return
                deficit = self.cost - self._tokens
                await asyncio.sleep(deficit / self.rate)


class KalshiClient:
    """Cliente unificado para Kalshi Trade API v2."""

    def __init__(self):
        self.http = httpx.AsyncClient(timeout=15.0, limits=httpx.Limits(max_connections=50))
        self.auth = KalshiAuth()
        # Detecta automáticamente demo/producción cuando KALSHI_ENV=auto.
        kalshi_env.resolve(self.auth)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.running = False
        self.subscribed_markets: List[str] = []
        self._reconnect_delay = 1
        self._last_status: Optional[int] = None
        self._market_cache: Dict[str, Dict[str, Any]] = {}
        self._event_cache: Dict[str, Dict[str, Any]] = {}
        self._bucket = TokenBucket(KALSHI_READ_TOKENS_PER_SECOND, KALSHI_REQUEST_TOKEN_COST, KALSHI_RATE_HEADROOM)
        self._exchange_status: Dict[str, Any] = {}

    @staticmethod
    def _to_decimal(value: Any, default: str = "0") -> Decimal:
        if value in (None, ""):
            return Decimal(default)
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Optional[httpx.Response]:
        """GET respetando el presupuesto de tokens de Kalshi."""
        await self._bucket.acquire()
        return await self.http.get(f"{kalshi_env.rest_base}{path}", params=params, timeout=timeout)

    async def fetch_exchange_status(self) -> Dict[str, Any]:
        """Estado del exchange: detecta pausas de trading y mantenimiento."""
        try:
            resp = await self._get("/exchange/status")
            if resp is not None and resp.status_code == 200:
                self._exchange_status = resp.json()
        except Exception as exc:
            print(f"[Kalshi Exchange] Error: {exc}")
        return self._exchange_status

    @property
    def trading_active(self) -> bool:
        """False durante pausa de trading / mantenimiento programado."""
        status = self._exchange_status or {}
        if not status:
            return True
        return bool(status.get("trading_active", status.get("exchange_active", True)))

    async def close(self):
        self.running = False
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.ws_task:
            self.ws_task.cancel()
        if self.ws:
            await self.ws.close()
        await self.http.aclose()

    async def restart_websocket(self) -> bool:
        """Reinicia el WebSocket conservando las suscripciones.

        Necesario tras un cambio de entorno: el WS firma el handshake con la
        credencial del entorno activo, así que hay que reconectar contra el host
        nuevo con la clave nueva (cada entorno tiene la suya).
        """
        tickers = list(self.subscribed_markets)
        self.running = False
        for task in (self.ws_task, self.heartbeat_task):
            if task:
                task.cancel()
        self.ws_task = None
        self.heartbeat_task = None
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        self.ws = None
        registry.system_stats["ws_connected"] = False
        self._reconnect_delay = 1
        self.subscribed_markets = tickers
        if not tickers:
            return False
        await self.start_websocket(tickers)
        return True

    async def apply_environment(self, env: str) -> Dict[str, Any]:
        """Cambia el entorno activo: reconstruye credencial y reconecta el WS.

        Las rutas REST/WS se resuelven en cada llamada desde `kalshi_env`, así que
        cambian solas; lo que hay que rehacer es la credencial (demo y producción
        no comparten API keys) y la suscripción del WebSocket.
        """
        previous = kalshi_env.env
        state = kalshi_env.switch(env)
        self.auth = kalshi_env.auth_for(kalshi_env.env)
        if kalshi_env.env != previous or self.subscribed_markets:
            await self.restart_websocket()
        return state

    def environment_status(self) -> Dict[str, Any]:
        """Estado de entorno + credenciales, sin exponer secretos."""
        info = kalshi_env.status()
        info["credentials"] = kalshi_env.credentials_report()
        info["ws_connected"] = registry.system_stats.get("ws_connected", False)
        info["ws_status"] = registry.system_stats.get("ws_status", "")
        info["auth_key_id"] = mask_key_id(getattr(self.auth, "key_id", ""))
        return info

    def public_status(self) -> Dict[str, Any]:
        return {
            "exchange": "kalshi",
            "environment": kalshi_env.env,
            "requested_environment": KALSHI_ENV,
            "rest_base": kalshi_env.rest_base,
            "ws_url": kalshi_env.ws_base,
            "environment_detection": kalshi_env.detection_note,
            "auth_configured": self.auth.configured,
            "auth_error": self.auth.load_error,
            "ws_connected": registry.system_stats.get("ws_connected", False),
        }

    # ========== Market Data REST ==========

    async def fetch_markets(self, limit: int = 100, offset: int = 0, cursor: str = "") -> List[Dict[str, Any]]:
        """Obtener mercados abiertos desde Kalshi.

        El parámetro `offset` se conserva por compatibilidad; Kalshi usa cursor.
        """
        params: Dict[str, Any] = {
            "limit": min(max(int(limit), 1), 1000),
            "status": "open",
            "mve_filter": "exclude",
        }
        if cursor:
            params["cursor"] = cursor
        start = time.time()
        try:
            resp = await self._get("/markets", params=params)
            latency = int((time.time() - start) * 1000)
            registry.system_stats["api_latency_ms"] = latency
            self._last_status = resp.status_code
            if resp.status_code == 200:
                data = resp.json()
                markets = data.get("markets", []) if isinstance(data, dict) else []
                for market in markets:
                    if market.get("ticker"):
                        self._market_cache[str(market["ticker"])] = market
                return markets
            print(f"[Kalshi Markets] Error {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            self._last_status = None
            print(f"[Kalshi Markets] Exception: {exc}")
        return []

    @staticmethod
    def _liquidity_score(market: Dict[str, Any]) -> float:
        """Puntaje de liquidez usado para priorizar el universo escaneado."""
        def _num(value: Any) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        volume = _num(market.get("volume_24h_fp"))
        total = _num(market.get("volume_fp"))
        interest = _num(market.get("open_interest_fp"))
        liquidity = _num(market.get("liquidity_dollars"))
        # El volumen reciente pesa mas; el interes abierto mide riesgo vivo.
        return volume * 4.0 + total * 0.5 + interest * 2.0 + liquidity

    async def fetch_all_active_markets(
        self, priority_tickers: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """Escanear el universo abierto y quedarse con los mercados mas operables.

        Solo ~14% de los mercados abiertos de Kalshi registran volumen, asi que se
        pagina un universo amplio y luego se rankea por liquidez en lugar de tomar
        las primeras paginas. Los tickers con posiciones abiertas se conservan para
        poder seguir valorandolos.
        """
        priority = {str(t) for t in (priority_tickers or set()) if t}
        all_markets: List[Dict[str, Any]] = []
        cursor = ""
        consecutive_errors = 0
        while True:
            params: Dict[str, Any] = {
                "limit": 200,
                "status": "open",
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                start = time.time()
                resp = await self._get("/markets", params=params)
                registry.system_stats["api_latency_ms"] = int((time.time() - start) * 1000)
                self._last_status = resp.status_code
                if resp.status_code != 200:
                    print(f"[Kalshi Markets] Error {resp.status_code}: {resp.text[:200]}")
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        break
                    await asyncio.sleep(2 * consecutive_errors)
                    continue

                data = resp.json()
                markets = data.get("markets", []) if isinstance(data, dict) else []
                for market in markets:
                    ticker = market.get("ticker")
                    if ticker:
                        self._market_cache[str(ticker)] = market
                all_markets.extend(markets)
                cursor = str(data.get("cursor") or "")
                if not cursor or not markets or len(all_markets) >= MAX_MARKETS_SCANNED:
                    break
                await asyncio.sleep(0.15)
            except Exception as exc:
                consecutive_errors += 1
                print(f"[Kalshi Markets] Exception: {exc}")
                if consecutive_errors >= 3:
                    break
                await asyncio.sleep(2 * consecutive_errors)

        filtered: List[Dict[str, Any]] = []
        for market in all_markets:
            ticker = str(market.get("ticker") or "")
            if ticker and ticker in priority:
                filtered.append(market)
                continue
            try:
                volume = float(market.get("volume_24h_fp") or 0)
                interest = float(market.get("open_interest_fp") or 0)
            except (TypeError, ValueError):
                continue
            if volume >= MARKET_MIN_VOLUME_24H or interest >= MARKET_MIN_OPEN_INTEREST:
                filtered.append(market)

        ranked = sorted(filtered, key=self._liquidity_score, reverse=True)
        selected = ranked[:MAX_MARKETS_TRACKED]

        # S05 y S21 razonan sobre el evento completo (canasta / escalera de
        # umbrales). Si solo se sigue el hijo liquido, esas estrategias nunca
        # ven una canasta, asi que se anaden los hermanos de los eventos ya
        # seleccionados, con un tope duro para no desbordar el registro.
        selected_tickers = {str(m.get("ticker") or "") for m in selected}
        selected_events = {
            str(m.get("event_ticker") or "")
            for m in selected
            if m.get("event_ticker")
        }
        extra: List[Dict[str, Any]] = []
        if selected_events:
            for market in ranked:
                if len(selected) + len(extra) >= MAX_MARKETS_WITH_EVENT_CONTEXT:
                    break
                ticker = str(market.get("ticker") or "")
                if not ticker or ticker in selected_tickers:
                    continue
                if str(market.get("event_ticker") or "") in selected_events:
                    extra.append(market)

        selected = selected + extra
        print(f"[Kalshi] Universo: {len(all_markets)} abiertos, {len(filtered)} con actividad, {len(selected)} seleccionados")
        return selected

    async def fetch_all_events(self, status: str = "open") -> List[Dict[str, Any]]:
        """Obtener eventos (metadatos) con paginacion por cursor.

        Provee categoria oficial, `mutually_exclusive` y `settlement_sources`:
        datos que el endpoint de mercados no expone.
        """
        events: List[Dict[str, Any]] = []
        cursor = ""
        consecutive_errors = 0
        while True:
            params: Dict[str, Any] = {
                "limit": 200,
                "status": status,
                "with_nested_markets": "false",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await self._get("/events", params=params)
                if resp is None or resp.status_code != 200:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        break
                    await asyncio.sleep(1.5 * consecutive_errors)
                    continue
                data = resp.json()
                page = data.get("events", []) if isinstance(data, dict) else []
                for event in page:
                    ticker = event.get("event_ticker")
                    if ticker:
                        self._event_cache[str(ticker)] = event
                events.extend(page)
                cursor = str(data.get("cursor") or "")
                if not cursor or not page or len(events) >= MAX_EVENTS_TRACKED:
                    break
                await asyncio.sleep(0.1)
            except Exception as exc:
                consecutive_errors += 1
                print(f"[Kalshi Events] Error: {exc}")
                if consecutive_errors >= 3:
                    break
                await asyncio.sleep(1.5 * consecutive_errors)

        print(f"[Kalshi] Eventos abiertos cargados: {len(events)}")
        return events[:MAX_EVENTS_TRACKED]

    async def fetch_market_detail(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        try:
            resp = await self._get(f"/markets/{market_ticker}")
            if resp.status_code == 200:
                data = resp.json()
                market = data.get("market") if isinstance(data, dict) else data
                if isinstance(market, dict) and market.get("ticker"):
                    self._market_cache[str(market["ticker"])] = market
                return market
            if resp.status_code == 404:
                # Normal en mensajes de ciclo de vida: algunos tickers no son
                # mercados individuales (o ya no existen). No se reporta ruido.
                return None
            print(f"[Kalshi Market Detail] Error {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            print(f"[Kalshi Market Detail] Error: {exc}")
        return None

    async def fetch_event_detail(self, event_ticker: str, with_nested_markets: bool = False) -> Optional[Dict[str, Any]]:
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        try:
            resp = await self._get(f"/events/{event_ticker}", params=params)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            print(f"[Kalshi Event Detail] Error: {exc}")
        return None

    @staticmethod
    def _levels(levels: List[Any]) -> List[Dict[str, str]]:
        normalized = []
        for level in levels or []:
            if isinstance(level, dict):
                price = level.get("price") or level.get("price_dollars") or level.get("0")
                size = level.get("size") or level.get("count") or level.get("count_fp") or level.get("1")
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            else:
                continue
            normalized.append({"price": str(price), "size": str(size)})
        return sorted(normalized, key=lambda x: KalshiClient._to_decimal(x.get("price")), reverse=True)

    @staticmethod
    def _implied_asks(opposite_bids: List[Dict[str, str]]) -> List[Dict[str, str]]:
        asks = []
        for level in opposite_bids:
            price = Decimal("1") - KalshiClient._to_decimal(level.get("price"))
            if Decimal("0") <= price <= Decimal("1"):
                asks.append({"price": f"{price:.4f}", "size": str(level.get("size", "0"))})
        return sorted(asks, key=lambda x: KalshiClient._to_decimal(x.get("price")))

    async def fetch_orderbook(self, market_ticker: str) -> Optional[Dict[str, Any]]:
        """Orderbook compatible: devuelve bids/asks para Yes/No.

        Kalshi solo devuelve bids de YES y NO; las asks se derivan del lado opuesto:
        YES ask = 1 - best NO bid, NO ask = 1 - best YES bid.
        """
        try:
            resp = await self._get(f"/markets/{market_ticker}/orderbook")
            if resp.status_code != 200:
                print(f"[Kalshi Book] Error {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            fp = data.get("orderbook_fp") or data.get("orderbook") or {}
            yes_bids = self._levels(fp.get("yes_dollars") or fp.get("yes") or [])
            no_bids = self._levels(fp.get("no_dollars") or fp.get("no") or [])
            yes_asks = self._implied_asks(no_bids)
            no_asks = self._implied_asks(yes_bids)
            return {
                "ticker": market_ticker,
                "raw": data,
                "by_outcome": {
                    "Yes": {"bids": yes_bids, "asks": yes_asks},
                    "No": {"bids": no_bids, "asks": no_asks},
                },
                # Compatibilidad antigua: por defecto expone YES.
                "bids": yes_bids,
                "asks": yes_asks,
            }
        except Exception as exc:
            print(f"[Kalshi Book] Error: {exc}")
            return None

    @classmethod
    def midpoint_from_book(cls, book: Optional[Dict[str, Any]], outcome: str = "Yes") -> Optional[Decimal]:
        """Calcular midpoint reutilizando un orderbook ya descargado (evita doble REST)."""
        if not book:
            return None
        side = book.get("by_outcome", {}).get(outcome, {})
        bids = side.get("bids") or []
        asks = side.get("asks") or []
        if bids and asks:
            return (cls._to_decimal(bids[0].get("price")) + cls._to_decimal(asks[0].get("price"))) / 2
        if bids:
            return cls._to_decimal(bids[0].get("price"))
        if asks:
            return cls._to_decimal(asks[0].get("price"))
        return None

    async def fetch_midpoint(self, market_ticker: str, outcome: str = "Yes") -> Optional[Decimal]:
        book = await self.fetch_orderbook(market_ticker)
        if not book:
            market = self._market_cache.get(market_ticker) or {}
            if outcome.lower().startswith("n"):
                bid = self._to_decimal(market.get("no_bid_dollars"))
                ask = self._to_decimal(market.get("no_ask_dollars"))
            else:
                bid = self._to_decimal(market.get("yes_bid_dollars"))
                ask = self._to_decimal(market.get("yes_ask_dollars"))
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            last = self._to_decimal(market.get("last_price_dollars"))
            return last if last > 0 else None
        side = book.get("by_outcome", {}).get(outcome, {})
        bids = side.get("bids") or []
        asks = side.get("asks") or []
        if bids and asks:
            return (self._to_decimal(bids[0].get("price")) + self._to_decimal(asks[0].get("price"))) / 2
        if bids:
            return self._to_decimal(bids[0].get("price"))
        return None

    async def fetch_midpoints_batch(self, market_tickers: List[str]) -> Dict[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for ticker in market_tickers[:50]:
            mid = await self.fetch_midpoint(ticker)
            if mid is not None:
                result[ticker] = mid
        return result

    async def fetch_price(self, market_ticker: str, side: str = "BUY") -> Optional[Decimal]:
        book = await self.fetch_orderbook(market_ticker)
        if not book:
            return None
        yes = book.get("by_outcome", {}).get("Yes", {})
        if side.upper() == "BUY":
            asks = yes.get("asks") or []
            return self._to_decimal(asks[0].get("price")) if asks else None
        bids = yes.get("bids") or []
        return self._to_decimal(bids[0].get("price")) if bids else None

    async def fetch_recent_trades(self, limit: int = 250, ticker: str = "") -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": min(max(int(limit), 1), 1000)}
        if ticker:
            params["ticker"] = ticker
        try:
            resp = await self._get("/markets/trades", params=params)
            if resp.status_code == 200:
                data = resp.json()
                trades = data.get("trades", []) if isinstance(data, dict) else []
                return trades
            print(f"[Kalshi Trades] Error {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            print(f"[Kalshi Trades] Error: {exc}")
        return []

    # Kalshi no expone posiciones públicas por wallet como la Data API de Polymarket.
    async def fetch_user_positions(self, wallet: str, limit: int = 100) -> List[Dict[str, Any]]:
        return []

    async def fetch_market_holders(self, condition_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return []

    # ========== Authenticated helpers ==========

    async def fetch_balance(self) -> Optional[Dict[str, Any]]:
        path = "/trade-api/v2/portfolio/balance"
        headers = self.auth.headers("GET", path)
        if not headers:
            print(f"[Kalshi Auth] Balance no disponible: {self.auth.load_error or 'credenciales no configuradas'}")
            return None
        try:
            resp = await self._get("/portfolio/balance")
            if resp.status_code == 200:
                return resp.json()
            print(f"[Kalshi Balance] Error {resp.status_code}: {resp.text[:300]}")
        except Exception as exc:
            print(f"[Kalshi Balance] Error: {exc}")
        return None

    # ========== WebSocket ==========

    async def start_websocket(self, market_tickers: List[str]):
        self.subscribed_markets = [m for m in market_tickers[:MAX_MARKETS_WS] if m]
        if not KALSHI_WEBSOCKET_ENABLED:
            print("[Kalshi WS] Desactivado por configuración")
            return
        if not self.auth.configured:
            registry.system_stats["ws_connected"] = False
            registry.system_stats["ws_status"] = "auth_required"
            print("[Kalshi WS] Requiere KALSHI_KEY_ID y private key. Usando REST polling.")
            return
        if not self.subscribed_markets:
            print("[Kalshi WS] Sin mercados para suscribir; en espera")
            return
        if self.running:
            return
        self.running = True
        self.ws_task = asyncio.create_task(self._ws_loop())
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        print(f"[Kalshi WS] Iniciando conexión para {len(self.subscribed_markets)} mercados")

    async def _ws_loop(self):
        while self.running:
            if not self.subscribed_markets:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                continue
            try:
                headers = self.auth.headers("GET", "/trade-api/ws/v2")
                if not headers:
                    registry.system_stats["ws_status"] = "auth_error"
                    print(f"[Kalshi WS] No se pudo firmar conexión: {self.auth.load_error}")
                    await asyncio.sleep(30)
                    continue
                print(f"[Kalshi WS] Conectando a {kalshi_env.ws_base}...")
                async with websockets.connect(kalshi_env.ws_base, additional_headers=headers, ping_interval=None) as ws:
                    self.ws = ws
                    self._reconnect_delay = 1
                    registry.system_stats["ws_connected"] = True
                    registry.system_stats["ws_status"] = "connected"
                    print("[Kalshi WS] Conectado")

                    await self._subscribe_current()
                    async for message in ws:
                        if not self.running:
                            break
                        await self._handle_ws_message(message)
            except websockets.exceptions.ConnectionClosed:
                print("[Kalshi WS] Conexión cerrada, reconectando...")
            except Exception as exc:
                print(f"[Kalshi WS] Error: {exc}")

            registry.system_stats["ws_connected"] = False
            self.ws = None
            if self.running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _subscribe_current(self):
        if not self.ws or not self.subscribed_markets:
            return
        # `orderbook_delta` requiere market_tickers. `ticker` y `trade` son datos públicos sobre sesión autenticada.
        for channel in ("ticker", "trade", "orderbook_delta", "market_lifecycle_v2"):
            params: Dict[str, Any] = {"channels": [channel]}
            if channel == "orderbook_delta":
                params["market_tickers"] = self.subscribed_markets[:MAX_MARKETS_WS]
                params["use_yes_price"] = True
            msg = {"id": int(time.time() * 1000) % 1_000_000, "cmd": "subscribe", "params": params}
            await self.ws.send(json.dumps(msg))
        print(f"[Kalshi WS] Suscrito a {len(self.subscribed_markets)} mercados")

    async def _heartbeat_loop(self):
        while self.running:
            try:
                if self.ws and not getattr(self.ws, "closed", False):
                    await self.ws.ping()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except Exception:
                await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _handle_ws_message(self, message: str):
        try:
            data = json.loads(message)
            registry.system_stats["last_ws_message"] = time.time()
            if isinstance(data, dict):
                await self._handle_ws_payload(data)
        except json.JSONDecodeError:
            pass
        except Exception as exc:
            print(f"[Kalshi WS Handler] Error: {exc}")

    async def _handle_ws_payload(self, data: Dict[str, Any]):
        msg_type = data.get("type")
        msg = data.get("msg") if isinstance(data.get("msg"), dict) else data
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if not ticker:
            return
        ticker = str(ticker)

        if msg_type in {"ticker", "trade"}:
            yes_price = msg.get("yes_price_dollars") or msg.get("price_dollars") or msg.get("last_price_dollars")
            if yes_price:
                await registry.update_from_ws(ticker, "kalshi_price", {"outcome": "Yes", "price": yes_price})
                no_price = Decimal("1") - self._to_decimal(yes_price)
                await registry.update_from_ws(ticker, "kalshi_price", {"outcome": "No", "price": f"{no_price:.4f}"})
            return

        if msg_type in {"orderbook_snapshot", "orderbook_delta"}:
            # Mantener simple: refrescar por REST para normalizar snapshot/deltas.
            book = await self.fetch_orderbook(ticker)
            if book:
                for outcome, side in book.get("by_outcome", {}).items():
                    await registry.update_from_ws(ticker, "book", {"outcome": outcome, **side})
            return

        if msg_type in {"market_lifecycle_v2", "settled", "determined"}:
            detail = await self.fetch_market_detail(ticker)
            if detail:
                await registry.update_market(ticker, kalshi_market_to_registry(detail))

    async def update_subscriptions(self, market_tickers: List[str]):
        self.subscribed_markets = [m for m in market_tickers[:MAX_MARKETS_WS] if m]
        if not self.subscribed_markets:
            return
        if not self.running:
            await self.start_websocket(self.subscribed_markets)
            return
        if self.ws and not getattr(self.ws, "closed", False):
            await self._subscribe_current()


def _price_ranges_tick_size(market: Dict[str, Any]) -> str:
    ranges = market.get("price_ranges") or []
    if isinstance(ranges, list) and ranges:
        # Usar el menor step publicado por el mercado.
        steps = [KalshiClient._to_decimal(r.get("step"), "0.001") for r in ranges if isinstance(r, dict)]
        if steps:
            return str(min(steps))
    return "0.001"


def kalshi_market_to_registry(m: Dict[str, Any], event_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normaliza un market de Kalshi al esquema interno del dashboard.

    Kalshi ya publica `yes_bid_dollars`/`yes_ask_dollars`/`no_bid_dollars`/
    `no_ask_dollars` en el propio market object (verificado contra el orderbook),
    lo que permite tener precios sin gastar requests adicionales.

    `event_meta` es el evento padre (GET /events) y aporta categoria oficial,
    `mutually_exclusive` y `settlement_sources`.
    """
    ticker = str(m.get("ticker") or "")
    status = str(m.get("status") or "").lower()
    event_ticker = str(m.get("event_ticker") or "")
    event_meta = event_meta or {}

    yes_bid = KalshiClient._to_decimal(m.get("yes_bid_dollars"))
    yes_ask = KalshiClient._to_decimal(m.get("yes_ask_dollars"))
    no_bid = KalshiClient._to_decimal(m.get("no_bid_dollars"))
    no_ask = KalshiClient._to_decimal(m.get("no_ask_dollars"))
    last = KalshiClient._to_decimal(m.get("last_price_dollars"))
    previous = KalshiClient._to_decimal(m.get("previous_price_dollars"))

    def _mid(bid: Decimal, ask: Decimal, fallback: Decimal) -> Decimal:
        if bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        return fallback

    yes_mid = _mid(yes_bid, yes_ask, last)
    no_mid = _mid(no_bid, no_ask, (Decimal("1") - yes_mid) if yes_mid > 0 else Decimal("0"))

    # Rango valido de precios: Kalshi lo publica por bandas en `price_ranges`.
    price_ranges = m.get("price_ranges") or []

    primary_rules = str(m.get("rules_primary") or "")
    secondary_rules = str(m.get("rules_secondary") or "")
    settlement_sources = event_meta.get("settlement_sources") or []
    source_text = primary_rules
    if secondary_rules:
        source_text = (source_text + " | " + secondary_rules).strip()
    if settlement_sources:
        names = []
        for src in settlement_sources:
            if isinstance(src, dict):
                names.append(str(src.get("name") or src.get("url") or ""))
            else:
                names.append(str(src))
        joined = ", ".join([n for n in names if n])
        if joined:
            source_text = (source_text + " | Settlement sources: " + joined).strip()

    event_category = str(event_meta.get("category") or "")
    mutually_exclusive = bool(event_meta.get("mutually_exclusive", False))

    # Variacion de precio vs. sesion previa publicada por Kalshi, usada por
    # Mean Reversion como respaldo cuando aun no hay historial local.
    day_change = None
    if last > 0 and previous > 0:
        day_change = str(last - previous)

    return {
        "condition_id": event_ticker or ticker,
        "event_ticker": event_ticker,
        "slug": ticker.lower(),
        "question": m.get("title") or m.get("subtitle") or ticker,
        "subtitle": m.get("yes_sub_title") or m.get("subtitle") or "",
        "category": event_category,
        "event_category": event_category,
        "mutually_exclusive": mutually_exclusive,
        "exchange_index": m.get("exchange_index"),
        "strike_type": m.get("strike_type") or "",
        "tags": [m.get("series_ticker"), event_ticker, m.get("market_type")],
        "outcomes": ["Yes", "No"],
        "token_ids": {"Yes": ticker, "No": ticker},
        "initial_prices": {"Yes": yes_mid, "No": no_mid},
        "quotes": {
            "Yes": {"bid": str(yes_bid), "ask": str(yes_ask), "mid": str(yes_mid)},
            "No": {"bid": str(no_bid), "ask": str(no_ask), "mid": str(no_mid)},
        },
        "volume_24h": m.get("volume_24h_fp") or "0",
        "volume_7d": m.get("volume_fp") or "0",
        "liquidity": m.get("liquidity_dollars") or "0",
        "open_interest": m.get("open_interest_fp") or 0,
        "gamma_last_trade_price": str(last) if last > 0 else None,
        "gamma_one_day_price_change": day_change,
        "gamma_one_week_price_change": None,
        "gamma_one_month_price_change": None,
        "maker_fee_bps": 0,
        "taker_fee_bps": 0,
        "min_order_size": "1",
        "tick_size": _price_ranges_tick_size(m),
        "price_ranges": price_ranges,
        "neg_risk": bool(m.get("mve_collection_ticker") or m.get("mve_selected_legs")),
        "end_date": m.get("expected_expiration_time") or m.get("close_time") or m.get("latest_expiration_time"),
        "close_time": m.get("close_time"),
        "expected_expiration_time": m.get("expected_expiration_time"),
        "start_date": m.get("open_time") or m.get("created_time"),
        "resolution_source": source_text,
        "settlement_sources": settlement_sources,
        "active": status == "active",
        "closed": status in {"closed", "determined", "disputed", "amended", "finalized"},
        "resolved": status in {"finalized", "settled"} or bool(m.get("result")),
        "kalshi_status": status,
        "kalshi_result": m.get("result") or "",
        "settlement_timer_seconds": m.get("settlement_timer_seconds"),
    }


# Compatibilidad con imports existentes.
PolymarketClient = KalshiClient
pm_client = KalshiClient()
