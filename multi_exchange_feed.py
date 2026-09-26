"""
multi_exchange_feed.py
======================
Feed Cuantitativo Multi-Exchange en Paralelo (Binance + Coinbase + Bybit).

Mecánica:
1. Conexión WebSocket asíncrona simultánea con los 3 exchanges globales líderes:
   - Binance: wss://stream.binance.com:9443
   - Coinbase: wss://ws-feed.exchange.coinbase.com
   - Bybit: wss://stream.bybit.com/v5/public/spot
2. Normalización de precios de BTC, ETH y SOL en tiempo real con marcas de tiempo en milisegundos.
3. Detección de "First-Mover" (Líder de Ruptura):
   Identifica qué exchange rompe primero un nivel de precio o acelera con mayor momentum,
   otorgando entre 200ms y 500ms de ventaja temporal adicional frente a los creadores de mercado de Polymarket.
4. Resiliencia y redundancia automática: Si un exchange tiene mantenimiento o micro-cortes,
   los otros dos mantienen la alimentación de precios sin interrupción.
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

import httpx

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger("multi_exchange_feed")
logger.setLevel(logging.INFO)


@dataclass
class ExchangePrice:
    exchange: str
    symbol: str
    price: float
    timestamp: float
    velocity_5s: float = 0.0


@dataclass
class FirstMoverSignal:
    symbol: str
    leader_exchange: str
    leader_price: float
    consensus_median_price: float
    max_price_spread_pct: float
    lead_velocity_5s: float
    timestamp: float
    status: str  # "STABLE", "LEAD_BREAKOUT_UP", "LEAD_BREAKOUT_DOWN"


class MultiExchangeFeed:
    def __init__(self):
        self.running = False
        self.exchanges = ["BINANCE", "COINBASE", "BYBIT"]
        self.symbols = ["BTC", "ETH", "SOL"]

        # Matriz de precios actuales: symbol -> exchange -> ExchangePrice
        self.prices: Dict[str, Dict[str, ExchangePrice]] = {
            sym: {
                ex: ExchangePrice(exchange=ex, symbol=sym, price=0.0, timestamp=0.0)
                for ex in self.exchanges
            }
            for sym in self.symbols
        }

        # Historial reciente de ticks para calcular velocidad por exchange
        self.tick_history: Dict[str, Dict[str, List[tuple]]] = {
            sym: {ex: [] for ex in self.exchanges} for sym in self.symbols
        }

        # Estado de conexión de cada exchange
        self.connection_status: Dict[str, str] = {
            "BINANCE": "DISCONNECTED",
            "COINBASE": "DISCONNECTED",
            "BYBIT": "DISCONNECTED",
        }

        # Señales de First Mover activas
        self.first_mover_signals: Dict[str, FirstMoverSignal] = {}
        self.first_mover_history: List[FirstMoverSignal] = []

        self._tasks: List[asyncio.Task] = []

    def start(self):
        """Inicia los WebSockets paralelos."""
        if self.running:
            return
        self.running = True
        self._tasks = [
            asyncio.create_task(self._run_binance_ws()),
            asyncio.create_task(self._run_coinbase_ws()),
            asyncio.create_task(self._run_bybit_ws()),
            asyncio.create_task(self._run_first_mover_evaluator()),
        ]
        logger.info("⚡ [MULTI-EXCHANGE] Feed triple iniciado (Binance, Coinbase, Bybit).")

    def stop(self):
        """Detiene todas las tareas."""
        self.running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for ex in self.exchanges:
            self.connection_status[ex] = "STOPPED"
        logger.info("Multi-Exchange Feed detenido.")

    def record_price(self, exchange: str, symbol: str, price: float, now: Optional[float] = None):
        """Registra un tick de precio normalizado y actualiza la velocidad de 5s."""
        if price <= 0:
            return
        t = now or time.time()
        self.prices[symbol][exchange] = ExchangePrice(
            exchange=exchange,
            symbol=symbol,
            price=price,
            timestamp=t,
        )

        history = self.tick_history[symbol][exchange]
        history.append((t, price))
        cutoff = t - 15.0
        self.tick_history[symbol][exchange] = [pt for pt in history if pt[0] >= cutoff]

        # Velocidad en 5s
        v5 = self._compute_velocity(symbol, exchange, window_sec=5.0, current_time=t)
        self.prices[symbol][exchange].velocity_5s = v5

    def _compute_velocity(self, symbol: str, exchange: str, window_sec: float, current_time: float) -> float:
        history = self.tick_history[symbol][exchange]
        if len(history) < 2:
            return 0.0
        current_price = history[-1][1]
        target_t = current_time - window_sec
        past_price = history[0][1]
        for pt in reversed(history):
            if pt[0] <= target_t:
                past_price = pt[1]
                break
        if past_price <= 0:
            return 0.0
        return ((current_price - past_price) / past_price) * 100.0

    async def _run_binance_ws(self):
        """WebSocket de Binance Spot."""
        url = "wss://stream.binance.com:9443/ws/btcusdt@ticker/ethusdt@ticker/solusdt@ticker"
        symbol_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
        while self.running:
            try:
                if websockets is None:
                    await self._fallback_http_binance()
                    continue
                self.connection_status["BINANCE"] = "CONNECTING"
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self.connection_status["BINANCE"] = "CONNECTED"
                    logger.info("✅ [BINANCE WS] Conectado en tiempo real.")
                    async for message in ws:
                        if not self.running:
                            break
                        data = json.loads(message)
                        s_raw = data.get("s", "")
                        sym = symbol_map.get(s_raw)
                        p_raw = data.get("c")
                        if sym and p_raw:
                            self.record_price("BINANCE", sym, float(p_raw))
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connection_status["BINANCE"] = "RECONNECTING"
                logger.debug(f"Binance WS error: {e}, reintentando...")
                await asyncio.sleep(2.0)

    async def _run_coinbase_ws(self):
        """WebSocket de Coinbase Advanced / Pro."""
        url = "wss://ws-feed.exchange.coinbase.com"
        product_map = {"BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL"}
        sub_msg = {
            "type": "subscribe",
            "product_ids": list(product_map.keys()),
            "channels": ["ticker"],
        }
        while self.running:
            try:
                if websockets is None:
                    await self._fallback_http_coinbase()
                    continue
                self.connection_status["COINBASE"] = "CONNECTING"
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps(sub_msg))
                    self.connection_status["COINBASE"] = "CONNECTED"
                    logger.info("✅ [COINBASE WS] Conectado en tiempo real.")
                    async for message in ws:
                        if not self.running:
                            break
                        data = json.loads(message)
                        if data.get("type") == "ticker":
                            pid = data.get("product_id", "")
                            sym = product_map.get(pid)
                            price_s = data.get("price")
                            if sym and price_s:
                                self.record_price("COINBASE", sym, float(price_s))
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connection_status["COINBASE"] = "RECONNECTING"
                logger.debug(f"Coinbase WS error: {e}, reintentando...")
                await asyncio.sleep(2.0)

    async def _run_bybit_ws(self):
        """WebSocket de Bybit Spot."""
        url = "wss://stream.bybit.com/v5/public/spot"
        topic_map = {"tickers.BTCUSDT": "BTC", "tickers.ETHUSDT": "ETH", "tickers.SOLUSDT": "SOL"}
        sub_msg = {
            "op": "subscribe",
            "args": list(topic_map.keys()),
        }
        while self.running:
            try:
                if websockets is None:
                    await self._fallback_http_bybit()
                    continue
                self.connection_status["BYBIT"] = "CONNECTING"
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps(sub_msg))
                    self.connection_status["BYBIT"] = "CONNECTED"
                    logger.info("✅ [BYBIT WS] Conectado en tiempo real.")
                    async for message in ws:
                        if not self.running:
                            break
                        data = json.loads(message)
                        topic = data.get("topic", "")
                        sym = topic_map.get(topic)
                        tdata = data.get("data", {})
                        price_s = tdata.get("lastPrice")
                        if sym and price_s:
                            self.record_price("BYBIT", sym, float(price_s))
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connection_status["BYBIT"] = "RECONNECTING"
                logger.debug(f"Bybit WS error: {e}, reintentando...")
                await asyncio.sleep(2.0)

    async def _fallback_http_binance(self):
        """Fallback HTTP para Binance."""
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://api.binance.com/api/v3/ticker/price")
            if resp.status_code == 200:
                for item in resp.json():
                    s = item.get("symbol")
                    if s == "BTCUSDT":
                        self.record_price("BINANCE", "BTC", float(item["price"]))
                    elif s == "ETHUSDT":
                        self.record_price("BINANCE", "ETH", float(item["price"]))
                    elif s == "SOLUSDT":
                        self.record_price("BINANCE", "SOL", float(item["price"]))
                self.connection_status["BINANCE"] = "CONNECTED_HTTP"
        await asyncio.sleep(1.0)

    async def _fallback_http_coinbase(self):
        """Fallback HTTP para Coinbase."""
        async with httpx.AsyncClient(timeout=3.0) as client:
            for sym in ["BTC", "ETH", "SOL"]:
                r = await client.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot")
                if r.status_code == 200:
                    p = float(r.json()["data"]["amount"])
                    self.record_price("COINBASE", sym, p)
            self.connection_status["COINBASE"] = "CONNECTED_HTTP"
        await asyncio.sleep(1.5)

    async def _fallback_http_bybit(self):
        """Fallback HTTP para Bybit."""
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("https://api.bybit.com/v5/market/tickers?category=spot")
            if r.status_code == 200:
                for item in r.json().get("result", {}).get("list", []):
                    s = item.get("symbol")
                    if s == "BTCUSDT":
                        self.record_price("BYBIT", "BTC", float(item["lastPrice"]))
                    elif s == "ETHUSDT":
                        self.record_price("BYBIT", "ETH", float(item["lastPrice"]))
                    elif s == "SOLUSDT":
                        self.record_price("BYBIT", "SOL", float(item["lastPrice"]))
                self.connection_status["BYBIT"] = "CONNECTED_HTTP"
        await asyncio.sleep(1.5)

    async def _run_first_mover_evaluator(self):
        """
        Evalúa continuamente cuál exchange tiene el momentum de liderazgo.
        Detecta divergencias de precio entre exchanges (First-Mover Breakouts).
        """
        while self.running:
            try:
                now = time.time()
                for sym in self.symbols:
                    valid_exchanges = [
                        ex for ex in self.exchanges
                        if self.prices[sym][ex].price > 0 and (now - self.prices[sym][ex].timestamp) < 10.0
                    ]
                    if len(valid_exchanges) < 2:
                        continue

                    prices = [self.prices[sym][ex].price for ex in valid_exchanges]
                    velocities = {ex: self.prices[sym][ex].velocity_5s for ex in valid_exchanges}

                    # Mediana de consenso
                    sorted_p = sorted(prices)
                    median_p = sorted_p[len(sorted_p) // 2]
                    spread_pct = ((max(prices) - min(prices)) / median_p) * 100.0

                    # Líder por velocidad absoluta de 5s
                    leader_ex = max(velocities, key=lambda k: abs(velocities[k]))
                    lead_vel = velocities[leader_ex]

                    status = "STABLE"
                    if lead_vel >= 0.15:
                        status = "LEAD_BREAKOUT_UP"
                    elif lead_vel <= -0.15:
                        status = "LEAD_BREAKOUT_DOWN"

                    sig = FirstMoverSignal(
                        symbol=sym,
                        leader_exchange=leader_ex,
                        leader_price=self.prices[sym][leader_ex].price,
                        consensus_median_price=round(median_p, 2),
                        max_price_spread_pct=round(spread_pct, 3),
                        lead_velocity_5s=round(lead_vel, 2),
                        timestamp=now,
                        status=status,
                    )
                    self.first_mover_signals[sym] = sig

                    if status != "STABLE":
                        if not any(s.symbol == sym and s.leader_exchange == leader_ex and abs(s.lead_velocity_5s - lead_vel) < 0.05 for s in self.first_mover_history[:3]):
                            self.first_mover_history.insert(0, sig)
                            if len(self.first_mover_history) > 30:
                                self.first_mover_history.pop()

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Error evaluando First Mover: {e}")
                await asyncio.sleep(1.0)

    def get_status(self) -> Dict[str, Any]:
        """Estado analítico multi-exchange para el Dashboard."""
        formatted_prices = {}
        for sym in self.symbols:
            formatted_prices[sym] = {
                ex: {
                    "price": self.prices[sym][ex].price,
                    "velocity_5s": self.prices[sym][ex].velocity_5s,
                    "age_ms": int((time.time() - self.prices[sym][ex].timestamp) * 1000) if self.prices[sym][ex].timestamp > 0 else 999999,
                }
                for ex in self.exchanges
            }

        first_movers = {sym: asdict(s) for sym, s in self.first_mover_signals.items()}
        history = [asdict(s) for s in self.first_mover_history[:10]]

        return {
            "connection_status": self.connection_status,
            "prices_by_exchange": formatted_prices,
            "first_mover_signals": first_movers,
            "recent_breakout_events": history,
        }


# Instancia singleton del feed multi-exchange
multi_exchange_feed = MultiExchangeFeed()
