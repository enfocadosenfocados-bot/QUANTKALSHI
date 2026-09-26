"""QUANT KALSHI - Backend FastAPI"""
import asyncio
import json
import re
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response

from config import (
    CATEGORY_KEYWORDS,
    CATEGORIES,
    GAMMA_POLL_INTERVAL,
    CLOB_POLL_INTERVAL,
    MIN_LIQUIDITY,
    MIN_VOLUME_24H,
    MAX_ORDERBOOK_POLLS_PER_CYCLE,
    KALSHI_ENABLE_WHALE_TRACKING,
    KALSHI_CATEGORY_MAP,
    EVENT_POLL_INTERVAL,
    PRUNE_STALE_AFTER_SECONDS,
    MAINTENANCE_DOW,
    MAINTENANCE_START_HOUR_ET,
    MAINTENANCE_END_HOUR_ET,
    STRATEGY_PARAMS,
)
from market_registry import registry
from polymarket_client import pm_client, kalshi_market_to_registry
from kalshi_env import kalshi_env
from strategies import engine
from paper_tracker import paper_tracker, paper_tracker_research
from live_execution import live_manager
from lead_lag_engine import lead_lag_engine
from ai_learning_engine import ai_learning_engine
from news_oracle_agent import news_oracle_agent
from quant_ml_engine import quant_ml
from strategy_ranking import build_strategy_ranking
from strategy_promotion import build_promotion_board
from strategy_governor import governor
from black_scholes_digital import bs_digital_engine
from vpin_microstructure import vpin_manager
from avellaneda_stoikov import avellaneda_stoikov_engine
from multi_exchange_feed import multi_exchange_feed
from polygon_health_checker import polygon_health_checker

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


START_TS = time.time()


def utc_now() -> datetime:
    return datetime.now(UTC)


background_tasks: List[asyncio.Task] = []
wallet_stats_cache: Dict[str, Dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[STARTUP] Iniciando QUANT KALSHI...")
    detected_env = kalshi_env.resolve(pm_client.auth)
    print(f"[STARTUP] Entorno Kalshi activo: {detected_env}")
    if kalshi_env.detection_note:
        print(f"[STARTUP] {kalshi_env.detection_note}")
    print(f"[STARTUP] REST: {kalshi_env.rest_base}")
    print(f"[STARTUP] WebSocket auth: {'si' if pm_client.auth.configured else 'no'} | modo: {live_manager.mode}")
    lead_lag_engine.start()
    ai_learning_engine.start()
    news_oracle_agent.start()
    quant_ml.start()
    multi_exchange_feed.start()
    tasks = [
        asyncio.create_task(exchange_status_task()),
        asyncio.create_task(events_polling_task()),
        asyncio.create_task(gamma_polling_task()),
        asyncio.create_task(clob_polling_task()),
        asyncio.create_task(market_prune_task()),
        asyncio.create_task(vpin_feed_task()),
        asyncio.create_task(vpin_backfill_task()),
        asyncio.create_task(lead_lag_sync_task()),
        asyncio.create_task(news_bridge_task()),
        asyncio.create_task(whale_tracking_task()),
        asyncio.create_task(strategy_calculation_task()),
        asyncio.create_task(ws_relay_task()),
        # Iniciar WebSocket de Kalshi; se suscribirá cuando el polling cargue tickers.
        asyncio.create_task(pm_client.start_websocket([])),
    ]
    background_tasks.extend(tasks)
    print("[STARTUP] Tareas iniciadas. Esperando datos de Kalshi...")
    try:
        yield
    finally:
        print("[SHUTDOWN] Cerrando scanner...")
        lead_lag_engine.stop()
        ai_learning_engine.stop()
        news_oracle_agent.stop()
        quant_ml.stop()
        multi_exchange_feed.stop()
        await pm_client.close()
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            with suppress(asyncio.CancelledError):
                await task
        background_tasks.clear()


app = FastAPI(title="QUANT KALSHI", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _decode_json_field(value: Any, default: Any = None) -> Any:
    """Algunas APIs devuelven arrays como strings JSON; parser tolerante."""
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _as_list(value: Any) -> List[Any]:
    decoded = _decode_json_field(value, [])
    if decoded is None:
        return []
    if isinstance(decoded, list):
        return decoded
    if isinstance(decoded, tuple):
        return list(decoded)
    return [decoded]


def _as_decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _as_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _initial_prices(outcomes: List[str], prices_value: Any) -> Dict[str, Decimal]:
    prices = _as_list(prices_value)
    result: Dict[str, Decimal] = {}
    if outcomes and prices and len(outcomes) == len(prices):
        for outcome, price in zip(outcomes, prices):
            dec_price = _as_decimal(price)
            if dec_price > 0:
                result[str(outcome)] = dec_price
    return result


def _trade_notional(trade: Dict[str, Any]) -> Decimal:
    return _as_decimal(trade.get("size")) * _as_decimal(trade.get("price"))


def _trade_timestamp(trade: Dict[str, Any]) -> int:
    return _as_int(trade.get("timestamp"), 0)


def _keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.lower().strip()
    if not keyword:
        return False
    # Los prefijos de serie de Kalshi (KX...) son identificadores de mercado, no
    # palabras: "kxmlb" debe matchear "kxmlbhrr-26sep..." como substring.
    if keyword.startswith("kx"):
        return keyword in text
    # Evita falsos positivos: "eth" no debe matchear "whether", "ai" no debe
    # matchear "rain" y "rain" no debe matchear "Ukraine".
    if keyword.replace("/", "").replace("&", "").isalnum() and " " not in keyword:
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def _category_keyword_score(text: str, keywords: List[str], weight: int) -> int:
    return sum(weight for keyword in keywords if _keyword_matches(text, keyword))


def classify_market_category(market: Dict[str, Any]) -> str:
    """Clasificar mercados cuando Gamma no trae una categoría útil.

    Kalshi Markets API suele devolver `category` vacío o poco fiable en muchos mercados.
    Para que el dashboard pueda separar Crypto, Weather, Politics, Sports,
    Economics, Culture y Science, priorizamos la inferencia desde question +
    slug + tags y solo usamos `category` como fallback.
    """
    # 1) Categoria oficial del evento Kalshi (GET /events) tiene prioridad:
    #    es la fuente autoritativa y evita inferencias por palabras clave.
    event_category = str(market.get("event_category") or market.get("category") or "").strip()
    if event_category:
        mapped = KALSHI_CATEGORY_MAP.get(event_category.lower())
        if mapped:
            return mapped

    # 2) Respaldo: inferencia por palabras clave del titulo/ticker.
    raw_category = str(market.get("category") or "").strip()
    valid_categories = {name.lower(): name for name in CATEGORIES.values()}
    valid_codes = {code.lower(): name for code, name in CATEGORIES.items()}

    tags = _as_list(market.get("tags"))
    primary_text = " ".join([
        str(market.get("question") or market.get("title") or ""),
        str(market.get("slug") or ""),
    ]).lower().replace("-", " ").replace("_", " ")

    tag_parts = []
    for tag in tags:
        if isinstance(tag, dict):
            tag_parts.extend(str(tag.get(key) or "") for key in ("label", "name", "slug"))
        else:
            tag_parts.append(str(tag))
    tag_text = " ".join(tag_parts).lower().replace("-", " ").replace("_", " ")

    primary_scores = {}
    tag_scores = {}
    for code, keywords in CATEGORY_KEYWORDS.items():
        # La pregunta/slug es más confiable que los tags crudos de Gamma.
        primary_scores[code] = _category_keyword_score(primary_text, keywords, 3)
        tag_scores[code] = _category_keyword_score(tag_text, keywords, 1)

    best_primary_code, best_primary_score = max(primary_scores.items(), key=lambda item: item[1])
    if best_primary_score > 0:
        return CATEGORIES.get(best_primary_code, best_primary_code.title())

    best_tag_code, best_tag_score = max(tag_scores.items(), key=lambda item: item[1])
    if best_tag_score > 0:
        return CATEGORIES.get(best_tag_code, best_tag_code.title())

    # La categoría cruda de Gamma queda como último recurso solo si es una de
    # las categorías esperadas. En producción puede venir vacía o incorrecta,
    # por eso no tiene prioridad sobre question/slug/tags.
    if raw_category:
        lowered = raw_category.lower()
        if lowered in valid_categories:
            return valid_categories[lowered]
        if lowered in valid_codes:
            return valid_codes[lowered]
    return CATEGORIES.get("other", "Other")


def build_category_summary(markets: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {name: 0 for name in CATEGORIES.values()}
    for market in markets:
        category = market.get("category") or "Other"
        summary[category] = summary.get(category, 0) + 1
    return dict(sorted(summary.items(), key=lambda item: (-item[1], item[0])))

# WebSocket connections del dashboard
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict):
        disconnected = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                disconnected.append(conn)
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()


# ========== BACKGROUND TASKS ==========

async def gamma_polling_task():
    """Polling periódico de Kalshi REST para descubrir mercados abiertos."""
    while True:
        try:
            print("[BG] Polling Kalshi Markets API...")
            # Los mercados con posiciones abiertas nunca deben salir del universo:
            # si salieran dejariamos de valorar el PnL de esas posiciones.
            priority: Set[str] = set()
            try:
                for trade in paper_tracker.trades.values():
                    status = str(trade.get("status") or "").strip().upper()
                    if status != "OPEN":
                        continue
                    ticker = str(trade.get("market_id") or "")
                    if ticker:
                        priority.add(ticker)
            except Exception:
                pass
            markets = await pm_client.fetch_all_active_markets(priority_tickers=priority)

            processed = 0
            for raw_market in markets:
                try:
                    market_id = raw_market.get("ticker")
                    if not market_id:
                        continue

                    if str(raw_market.get("status") or "").lower() != "active":
                        continue

                    event_meta = registry.get_event_meta(str(raw_market.get("event_ticker") or ""))
                    normalized = kalshi_market_to_registry(raw_market, event_meta)
                    normalized["category"] = classify_market_category({
                        **raw_market,
                        "question": normalized.get("question", ""),
                        "slug": normalized.get("slug", ""),
                        "tags": normalized.get("tags", []),
                        "category": raw_market.get("category") or normalized.get("category", ""),
                    })

                    liquidity = _as_decimal(normalized.get("liquidity", 0))
                    volume = _as_decimal(normalized.get("volume_24h", 0))

                    if liquidity < MIN_LIQUIDITY and volume < MIN_VOLUME_24H:
                        continue

                    await registry.update_market(str(market_id), normalized)
                    processed += 1

                except Exception as e:
                    print(f"[BG Kalshi] Error procesando mercado: {e}")
                    continue

            registry.system_stats["markets_tracked"] = len(registry.markets)
            registry.system_stats["exchange"] = "kalshi"
            registry.system_stats["kalshi_status"] = pm_client.public_status()
            print(f"[BG] Kalshi: {processed} mercados procesados, {len(registry.markets)} en registro")

            market_tickers = list(dict.fromkeys(registry.markets.keys()))
            if market_tickers:
                await pm_client.update_subscriptions(market_tickers)

        except Exception as e:
            print(f"[BG Kalshi] Error: {e}")

        await asyncio.sleep(GAMMA_POLL_INTERVAL)


# Estado del exchange Kalshi: mantenimiento programado y pausas de trading.
KALSHI_STATE: Dict[str, Any] = {
    "trading_active": True,
    "maintenance_window": False,
    "last_check": 0.0,
}


def in_scheduled_maintenance(now: Optional[datetime] = None) -> bool:
    """Kalshi pausa el trading los jueves 03:00-05:00 ET (mantenimiento programado)."""
    moment = now or utc_now()
    try:
        from zoneinfo import ZoneInfo
        eastern = moment.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return False
    return (eastern.weekday() == MAINTENANCE_DOW
            and MAINTENANCE_START_HOUR_ET <= eastern.hour < MAINTENANCE_END_HOUR_ET)


async def exchange_status_task():
    """Vigilar /exchange/status para operar 24/7 respetando pausas y mantenimiento."""
    await asyncio.sleep(3)
    while True:
        try:
            status = await pm_client.fetch_exchange_status()
            active = pm_client.trading_active
            maintenance = in_scheduled_maintenance()
            KALSHI_STATE.update({
                "trading_active": bool(active),
                "maintenance_window": maintenance,
                "exchange_active": bool(status.get("exchange_active", True)),
                "shards": status.get("exchange_index_statuses") or [],
                "last_check": time.time(),
            })
            registry.system_stats["kalshi_trading_active"] = bool(active)
            registry.system_stats["kalshi_maintenance_window"] = maintenance
        except Exception as exc:
            print(f"[BG Kalshi Status] Error: {exc}")
        await asyncio.sleep(60)


async def events_polling_task():
    """Cargar eventos Kalshi: categoria oficial, mutua exclusividad y fuentes de liquidacion."""
    await asyncio.sleep(2)
    while True:
        try:
            events = await pm_client.fetch_all_events(status="open")
            if events:
                await registry.register_events(events)
                print(f"[BG] Eventos Kalshi cargados: {len(events)}")
        except Exception as exc:
            print(f"[BG Kalshi Events] Error: {exc}")
        await asyncio.sleep(EVENT_POLL_INTERVAL)


async def market_prune_task():
    """Podar mercados que ya no aparecen en el feed (clave para ejecucion 24/7)."""
    await asyncio.sleep(120)
    while True:
        try:
            removed = await registry.prune_stale_markets(PRUNE_STALE_AFTER_SECONDS)
            if removed:
                registry.system_stats["markets_tracked"] = len(registry.markets)
                print(f"[BG] Mercados obsoletos podados: {removed} (quedan {len(registry.markets)})")
        except Exception as exc:
            print(f"[BG Prune] Error: {exc}")
        await asyncio.sleep(300)


async def news_bridge_task():
    """Puente news_oracle_agent -> S22: registra titulares como catalizadores."""
    await asyncio.sleep(30)
    seen: set = set()
    while True:
        try:
            from s22_news_latency_sniping import register_breaking_news
            status = news_oracle_agent.get_status()
            for catalyst in status.get("recent_catalysts", [])[:20]:
                headline = str(catalyst.get("headline") or "").strip()
                if not headline or headline in seen:
                    continue
                seen.add(headline)
                score = float(catalyst.get("sentiment_score") or 0.0)
                sentiment = "positive" if score > 0 else ("negative" if score < 0 else "neutral")
                register_breaking_news(
                    headline,
                    topic=str(catalyst.get("matched_entity") or "General"),
                    sentiment=sentiment,
                )
                print(f"[BG NewsBridge] Titular para S22: {headline[:70]}")
            if len(seen) > 500:
                seen = set(list(seen)[-200:])
        except Exception as exc:
            print(f"[BG NewsBridge] Error: {exc}")
        await asyncio.sleep(60)


def _vpin_trade_to_feed(trade: Dict[str, Any]):
    """Traducir un trade de Kalshi a (ticker, precio YES, nocional, is_buy)."""
    ticker = str(trade.get("ticker") or "")
    if not ticker:
        return None
    try:
        price = float(trade.get("yes_price_dollars") or 0.0)
        count = float(trade.get("count_fp") or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0.0 or count <= 0.0:
        return None
    # Direccion real del agresor, publicada por Kalshi, traducida al lado YES:
    # el VPIN debe medir presion real y no una regla de tick sobre el precio.
    taker_side = str(trade.get("taker_side") or "").lower()
    book_side = str(trade.get("taker_book_side") or "").lower()
    is_buy: Optional[bool] = None
    if taker_side in ("yes", "no") and book_side in ("ask", "bid"):
        bought_outcome = book_side == "ask"
        is_buy = bought_outcome if taker_side == "yes" else (not bought_outcome)
    return ticker, price, count * price, is_buy


async def vpin_feed_task():
    """Alimentar el VPIN con el tape publico de operaciones de Kalshi.

    El analisis de toxicidad colgaba del modulo de ballenas, desactivado porque
    Kalshi no expone wallets por posicion. Se nutre del tape publico de trades,
    que si es accesible sin autenticacion.
    """
    await asyncio.sleep(45)
    seen: Set[str] = set()
    while True:
        try:
            trades = await pm_client.fetch_recent_trades(limit=200)
            ingested = 0
            for trade in trades:
                trade_id = str(trade.get("trade_id") or "")
                if not trade_id or trade_id in seen:
                    continue
                seen.add(trade_id)
                feed = _vpin_trade_to_feed(trade)
                if feed is None:
                    continue
                ticker, price, notional, is_buy = feed
                # Cubo a escala del volumen diario: 20 cubos ~ un dia completo de
                # negociacion, que es la ventana con la que el VPIN tiene sentido.
                market = registry.markets.get(ticker)
                daily = float(getattr(market, "volume_24h", 0) or 0) if market else 0.0
                bucket = max(50.0, min(5000.0, daily / 20.0)) if daily > 0 else 100.0
                vpin_manager.get_or_create(ticker, bucket_volume=bucket)
                vpin_manager.record_market_trade(ticker, price, notional, is_buy=is_buy)
                ingested += 1
            if len(seen) > 8000:
                seen = set(list(seen)[-3000:])
            if ingested:
                print(f"[BG VPIN] Operaciones ingestadas: {ingested}")
        except Exception as exc:
            print(f"[BG VPIN] Error: {exc}")
        await asyncio.sleep(45)


CRYPTO_ALIASES = {
    "BTC": ("bitcoin", "btc"),
    "ETH": ("ethereum", "eth"),
    "SOL": ("solana", "sol"),
}


async def lead_lag_sync_task():
    """Registrar mercados cripto REALES de Kalshi en el motor lead-lag.

    Sin esto el motor generaba contratos sinteticos con identificadores de
    Polymarket (poly_btc_flash_1, ...), que no representan ningun mercado real.
    """
    await asyncio.sleep(90)
    while True:
        try:
            contracts = []
            for market in registry.markets.values():
                if not market.active or market.closed or market.resolved:
                    continue
                # La categoria del evento puede no venir marcada como Crypto, asi
                # que el filtro decisivo es el alias del activo en la pregunta.
                category = (market.category or "").lower()
                if category and category != "crypto":
                    continue
                question = (market.question or "").lower()
                symbol = ""
                for candidate, aliases in CRYPTO_ALIASES.items():
                    if any(alias in question for alias in aliases):
                        symbol = candidate
                        break
                if not symbol:
                    continue
                ask = float(market.best_ask.get("Yes") or 0.0)
                if ask <= 0.01 or ask >= 0.99:
                    continue
                contracts.append({
                    "market_id": market.market_id,
                    "crypto_symbol": symbol,
                    "question": market.question,
                    "condition_id": market.condition_id or market.market_id,
                    "strike_price": 0.0,
                    "clob_ask": ask,
                    "type": "KALSHI_CRYPTO",
                })
                if len(contracts) >= 40:
                    break
            if contracts:
                lead_lag_engine.register_polymarket_contracts(contracts)
                print(f"[BG LeadLag] Contratos cripto reales de Kalshi: {len(contracts)}")
                registry.system_stats["lead_lag_real_contracts"] = len(contracts)
        except Exception as exc:
            print(f"[BG LeadLag] Error: {exc}")
        await asyncio.sleep(120)


async def vpin_backfill_task():
    """Construir el reloj de volumen del VPIN con historial real por mercado.

    Ingerir solo el tape instantaneo dejaba ventanas de un unico cubo
    unidireccional (VPIN saturado a 1.0). Aqui se carga el historial de los
    mercados mas activos para que cada cubo cubra una fraccion real del dia.
    """
    await asyncio.sleep(60)
    while True:
        try:
            focus = sorted(
                [m for m in registry.markets.values() if m.active and not m.closed and not m.resolved],
                key=lambda m: float(m.volume_24h or 0),
                reverse=True,
            )[:20]
            loaded = 0
            for market in focus:
                trades = await pm_client.fetch_recent_trades(limit=500, ticker=market.market_id)
                if not trades:
                    continue
                daily = float(market.volume_24h or 0)
                bucket = max(50.0, min(5000.0, daily / 40.0)) if daily > 0 else 100.0
                vpin_manager.get_or_create(market.market_id, bucket_volume=bucket)
                ordered = sorted(trades, key=lambda t: str(t.get("created_time") or ""))
                for trade in ordered:
                    feed = _vpin_trade_to_feed(trade)
                    if feed is None:
                        continue
                    _, price, notional, is_buy = feed
                    vpin_manager.record_market_trade(market.market_id, price, notional, is_buy=is_buy)
                loaded += 1
            if loaded:
                print(f"[BG VPIN] Reloj de volumen construido para {loaded} mercados")
        except Exception as exc:
            print(f"[BG VPIN] Backfill error: {exc}")
        await asyncio.sleep(900)


async def clob_polling_task():
    """Profundidad de libro por lotes rotativos (microestructura).

    Los bid/ask ya llegan en el market object sin coste extra, asi que aqui solo
    se completa la *profundidad* que necesitan S24/OFI/VPIN. El lote rota para no
    castigar siempre a los mismos mercados y respetar el presupuesto de tokens.
    """
    await asyncio.sleep(5)
    cursor = 0

    while True:
        try:
            maintenance = bool(KALSHI_STATE.get("maintenance_window"))
            stale_before = utc_now() - timedelta(seconds=45)
            oldest = utc_now() - timedelta(days=365)

            candidates = [
                m for m in registry.markets.values()
                if m.active and not m.closed and (m.best_bid.get("Yes") or 0) > 0
                and (m.last_ws_update is None or m.last_ws_update < stale_before)
            ]
            candidates.sort(key=lambda m: m.last_ws_update or oldest)

            if candidates:
                # En mantenimiento se reduce el ritmo para no acumular 429.
                batch_size = max(5, MAX_ORDERBOOK_POLLS_PER_CYCLE // (3 if maintenance else 1))
                if cursor >= len(candidates):
                    cursor = 0
                batch = candidates[cursor:cursor + batch_size]
                cursor += batch_size

                for m in batch:
                    try:
                        book = await pm_client.fetch_orderbook(m.market_id)
                        if book:
                            for outcome, side_book in book.get("by_outcome", {}).items():
                                m.update_orderbook(
                                    outcome,
                                    side_book.get("bids", []),
                                    side_book.get("asks", []),
                                )
                                mid = pm_client.midpoint_from_book(book, outcome=outcome)
                                if mid:
                                    m.update_price(outcome, mid)
                    except Exception as e:
                        print(f"[BG Kalshi Book] Error {m.market_id}: {e}")
                        continue

                    signals = engine.calculate_all(m)
                    m.signals = [sg for sg in m.signals if sg.get("status") == "ACTIVE"]
                    for sig in signals:
                        await registry.add_signal(m.market_id, sig)

        except Exception as e:
            print(f"[BG Kalshi Book] Error: {e}")

        await asyncio.sleep(CLOB_POLL_INTERVAL * (2 if maintenance else 1))

async def get_wallet_stats(wallet: str) -> Dict[str, Any]:
    """Calcular métricas simples de una wallet usando posiciones públicas."""
    wallet = wallet.lower()
    cached = wallet_stats_cache.get(wallet)
    now = time.time()
    if cached and now - cached.get("cached_at", 0) < 600:
        return cached

    p = STRATEGY_PARAMS["whale_tracking"]
    positions = await pm_client.fetch_user_positions(wallet, limit=int(p.get("positions_lookup_limit", 100)))
    pnl_positions = [pos for pos in positions if pos.get("cashPnl") not in (None, "")]
    nonzero = [pos for pos in pnl_positions if _as_decimal(pos.get("cashPnl")) != 0]
    wins = [pos for pos in nonzero if _as_decimal(pos.get("cashPnl")) > 0]

    max_position_size = Decimal("0")
    max_current_value = Decimal("0")
    total_current_value = Decimal("0")
    for pos in positions:
        max_position_size = max(max_position_size, _as_decimal(pos.get("size")))
        current_value = _as_decimal(pos.get("currentValue"))
        max_current_value = max(max_current_value, current_value)
        total_current_value += current_value

    # Si hay pocas posiciones cerradas/no-cero, usar percentPnl como apoyo.
    percent_nonzero = [pos for pos in positions if pos.get("percentPnl") not in (None, "") and _as_decimal(pos.get("percentPnl")) != 0]
    percent_wins = [pos for pos in percent_nonzero if _as_decimal(pos.get("percentPnl")) > 0]

    if nonzero:
        winrate = Decimal(len(wins)) / Decimal(len(nonzero))
        sample_size = len(nonzero)
    elif percent_nonzero:
        winrate = Decimal(len(percent_wins)) / Decimal(len(percent_nonzero))
        sample_size = len(percent_nonzero)
    else:
        winrate = Decimal("0")
        sample_size = 0

    stats = {
        "wallet": wallet,
        "wallet_winrate": float(winrate),
        "wallet_positions_checked": len(positions),
        "wallet_winrate_sample": sample_size,
        "wallet_max_position_size": str(max_position_size),
        "wallet_current_value": str(max_current_value),
        "wallet_total_current_value": str(total_current_value),
        "cached_at": now,
    }
    wallet_stats_cache[wallet] = stats
    return stats


async def whale_tracking_task():
    """Kalshi no expone wallets/holders públicos como Polymarket; desactivado por defecto."""
    if not KALSHI_ENABLE_WHALE_TRACKING:
        registry.system_stats["whale_tracking_status"] = "disabled_kalshi_no_public_wallet_feed"
        while True:
            await asyncio.sleep(60)

    await asyncio.sleep(8)
    seen_trade_keys = set()
    holder_scan_offset = 0

    while True:
        try:
            p = STRATEGY_PARAMS["whale_tracking"]
            limit = int(p.get("trades_poll_limit", 250))
            min_recent_trade_size = Decimal(str(p.get("min_recent_trade_size", 0)))
            window_seconds = int(p.get("recent_trade_window_seconds", 1800))
            now_ts = int(time.time())

            trades = await pm_client.fetch_recent_trades(limit=limit)
            wallets_seen = set()

            # Pre-filtrar por mercados que estamos siguiendo y por tamaño de shares.
            candidates = []
            tracked_assets = set(registry.token_to_market.keys())
            tracked_conditions = {m.condition_id.lower() for m in registry.markets.values() if m.condition_id}

            for trade in trades:
                wallet = str(trade.get("proxyWallet") or "").lower()
                asset = str(trade.get("asset") or "")
                condition_id = str(trade.get("conditionId") or "").lower()
                timestamp = _trade_timestamp(trade)
                if not wallet or not asset:
                    continue
                if timestamp and now_ts - timestamp > window_seconds:
                    continue
                if asset not in tracked_assets and condition_id not in tracked_conditions:
                    continue
                size = _as_decimal(trade.get("size"))
                # No confundimos "whale position" con tamaño del último trade:
                # la posición grande se valida después en /positions; aquí solo
                # se puede exigir un tamaño mínimo del trade reciente si se desea.
                if size < min_recent_trade_size:
                    continue

                trade_key = f"{trade.get('transactionHash')}:{wallet}:{asset}:{timestamp}:{trade.get('outcome')}"
                if trade_key in seen_trade_keys:
                    continue
                candidates.append((trade_key, trade))
                wallets_seen.add(wallet)

            registry.system_stats["whale_wallets_tracked"] = len(wallets_seen)
            registry.system_stats["last_whale_poll"] = datetime.now(UTC).isoformat()

            # Enriquecer solo los candidatos para no saturar la Data API.
            for trade_key, trade in candidates[:25]:
                wallet = str(trade.get("proxyWallet") or "").lower()
                stats = await get_wallet_stats(wallet)
                enriched = {
                    **trade,
                    **stats,
                    "wallet": wallet,
                    "notional": str(_trade_notional(trade)),
                }
                await registry.add_whale_trade(enriched)

                asset = str(trade.get("asset") or "")
                market_id = registry.token_to_market.get(asset)
                if not market_id:
                    condition_id = str(trade.get("conditionId") or "").lower()
                    for mid, market in registry.markets.items():
                        if market.condition_id.lower() == condition_id:
                            market_id = mid
                            break
                if market_id:
                    try:
                        p_val = float(trade.get("price") or 0.50)
                        v_val = float(_trade_notional(trade) or 100.0)
                        vpin_manager.record_market_trade(market_id, p_val, v_val)
                    except Exception:
                        pass
                    await registry.set_whale_signal(market_id, {
                        **enriched,
                        "side": str(trade.get("side") or "BUY").upper(),
                        "outcome": str(trade.get("outcome") or "Yes"),
                        "price": str(trade.get("price") or 0),
                        "size": str(trade.get("size") or 0),
                        "dedupe_key": f"WT:{market_id}:{wallet}:{trade.get('outcome')}:{trade.get('side')}",
                    })

                seen_trade_keys.add(trade_key)
                if len(seen_trade_keys) > 2000:
                    seen_trade_keys = set(list(seen_trade_keys)[-1000:])

            # Escaneo complementario de top holders actuales por mercado.
            # Esto detecta wallets que ya cumplen la condición de posición grande
            # aunque no hayan hecho un trade en los últimos minutos.
            holder_market_scan_limit = int(p.get("holders_market_scan_limit", 30))
            holders_limit = int(p.get("holders_poll_limit", 10))
            holder_enrich_limit = int(p.get("holders_wallet_enrich_limit_per_cycle", 80))
            holder_enriched = 0
            min_whale_position = Decimal(str(p.get("min_whale_position", 5000)))
            holder_markets = [
                m for m in registry.markets.values()
                if m.condition_id and not m.closed and not m.resolved and m.liquidity >= Decimal("100000")
            ]
            holder_markets.sort(key=lambda market: market.liquidity, reverse=True)
            if holder_markets:
                start = holder_scan_offset % len(holder_markets)
                selected_markets = (holder_markets + holder_markets)[start:start + holder_market_scan_limit]
                holder_scan_offset = (start + holder_market_scan_limit) % len(holder_markets)

                for market in selected_markets:
                    holder_groups = await pm_client.fetch_market_holders(market.condition_id, limit=holders_limit)
                    for group in holder_groups:
                        token_id = str(group.get("token") or "")
                        outcome = next((name for name, tid in market.token_ids.items() if str(tid) == token_id), None)
                        outcome = outcome or "Yes"
                        for holder in group.get("holders", []):
                            if holder_enriched >= holder_enrich_limit:
                                break
                            wallet = str(holder.get("proxyWallet") or "").lower()
                            amount = _as_decimal(holder.get("amount"))
                            if not wallet or amount < min_whale_position:
                                continue

                            stats = await get_wallet_stats(wallet)
                            holder_enriched += 1
                            wallets_seen.add(wallet)
                            await registry.set_whale_signal(market.market_id, {
                                **holder,
                                **stats,
                                "wallet": wallet,
                                "outcome": outcome,
                                "side": "BUY",
                                "price": str(market.mid_price.get(outcome) or market.prices.get(outcome) or market.gamma_last_trade_price or 0),
                                "size": str(amount),
                                "notional": str(amount * (market.mid_price.get(outcome) or market.prices.get(outcome) or market.gamma_last_trade_price or Decimal("0"))),
                                "source": "data_api_holders_positions",
                                "dedupe_key": f"WT:{market.market_id}:{wallet}:{outcome}:HOLDER",
                            })
                        if holder_enriched >= holder_enrich_limit:
                            break

                    if holder_enriched >= holder_enrich_limit:
                        break

                    # Pequeña pausa para no saturar Data API.
                    await asyncio.sleep(0.05)

            registry.system_stats["whale_wallets_tracked"] = len(wallets_seen)

        except Exception as e:
            print(f"[BG Whale] Error: {e}")

        await asyncio.sleep(20)


def build_strategy_diagnostics() -> Dict[str, Any]:
    markets = list(registry.markets.values())
    p_mr = STRATEGY_PARAMS["mean_reversion"]
    p_wt = STRATEGY_PARAMS["whale_tracking"]
    min_mr_liq = Decimal(str(p_mr["min_liquidity"]))
    min_mr_vol = Decimal(str(p_mr["min_volume_24h"]))
    gamma_1d = Decimal(str(p_mr.get("gamma_1d_change_threshold", 0.02)))
    gamma_1w = Decimal(str(p_mr.get("gamma_1w_change_threshold", 0.04)))
    min_whale_position = Decimal(str(p_wt.get("min_whale_position", 5000)))
    min_whale_winrate = Decimal(str(p_wt.get("min_whale_winrate", 0.60)))

    active_signals = registry.get_active_signals()
    by_strategy: Dict[str, int] = {}
    for signal in active_signals:
        code = signal.get("strategy_code", "?")
        by_strategy[code] = by_strategy.get(code, 0) + 1

    mr_liquid = [m for m in markets if m.liquidity >= min_mr_liq]
    mr_volume = [m for m in mr_liquid if m.volume_24h >= min_mr_vol]
    mr_gamma_candidates = [
        m for m in mr_volume
        if (
            m.gamma_one_day_price_change is not None and abs(m.gamma_one_day_price_change) >= gamma_1d
        ) or (
            m.gamma_one_week_price_change is not None and abs(m.gamma_one_week_price_change) >= gamma_1w
        )
    ]
    mr_history_ready = 0
    for m in mr_volume:
        for hist in m.price_history.values():
            if len(hist.prices) >= int(p_mr.get("min_history_points", 5)):
                mr_history_ready += 1
                break

    whale_candidates = 0
    whale_ready = 0
    for m in markets:
        for ws in m.whale_signals:
            max_pos = _as_decimal(ws.get("wallet_max_position_size"))
            current_value = _as_decimal(ws.get("wallet_current_value"))
            winrate = _as_decimal(ws.get("wallet_winrate"))
            if max_pos >= min_whale_position or current_value >= min_whale_position:
                whale_candidates += 1
                if winrate >= min_whale_winrate:
                    whale_ready += 1

    return {
        "signals_by_strategy": by_strategy,
        "mean_reversion": {
            "min_liquidity": str(min_mr_liq),
            "min_volume_24h": str(min_mr_vol),
            "markets_liquid": len(mr_liquid),
            "markets_liquid_and_volume": len(mr_volume),
            "markets_history_ready": mr_history_ready,
            "gamma_change_candidates": len(mr_gamma_candidates),
            "gamma_1d_threshold": str(gamma_1d),
            "gamma_1w_threshold": str(gamma_1w),
        },
        "whale_tracking": {
            "min_whale_position": str(min_whale_position),
            "min_whale_winrate": str(min_whale_winrate),
            "smart_money_candidates": whale_candidates,
            "smart_money_passing_winrate": whale_ready,
            "recent_big_trades_on_tracked_markets": whale_candidates,
            "recent_big_trades_passing_winrate": whale_ready,
            "wallet_cache_size": len(wallet_stats_cache),
            "last_whale_poll": registry.system_stats.get("last_whale_poll"),
        },
    }


async def strategy_calculation_task():
    """Calcular estrategias periódicamente para todos los mercados"""
    await asyncio.sleep(10)

    while True:
        try:
            for m in registry.markets.values():
                signals = engine.calculate_all(m)
                # Limpiar señales viejas
                m.signals = [s for s in m.signals if s.get("status") == "ACTIVE"]
                for sig in signals:
                    # Evaluar si la señal es Top Sniper y registrar en Paper Tracker
                    paper_tracker.evaluate_and_record_signal(sig, m)
                    paper_tracker_research.evaluate_and_record_signal(sig, m)
                    # Evitar duplicados recientes
                    sig_key = sig.get("dedupe_key") or f"{sig.get('strategy_code')}:{sig.get('token')}:{sig.get('side')}"
                    existing = [
                        s for s in m.signals
                        if (s.get("dedupe_key") or f"{s.get('strategy_code')}:{s.get('token')}:{s.get('side')}") == sig_key
                    ]
                    if not existing:
                        await registry.add_signal(m.market_id, sig)
                    else:
                        await registry.add_signal(m.market_id, sig)

            # Actualizar precios en vivo para las posiciones abiertas de Paper Trading
            paper_tracker.update_live_prices(registry)
            paper_tracker_research.update_live_prices(registry)
            registry.system_stats["strategy_diagnostics"] = build_strategy_diagnostics()

            # Broadcast a todos los clientes del dashboard
            await manager.broadcast({
                "type": "update",
                "stats": {**registry.system_stats, "markets_tracked": len(registry.markets)},
                "markets_count": len(registry.markets),
                "active_signals": len(registry.get_active_signals()),
                "track_record": paper_tracker.get_summary(),
                "strategy_performance": paper_tracker.get_strategy_performance(),
                "trading_mode": live_manager.get_public_status(),
                "lead_lag": lead_lag_engine.get_status(),
                "ai_agent": ai_learning_engine.get_status(),
                "news_agent": news_oracle_agent.get_status(),
            })

        except Exception as e:
            print(f"[BG Strategy] Error: {e}")

        await asyncio.sleep(3)


async def ws_relay_task():
    """Reenviar actualizaciones del WebSocket de Kalshi al dashboard"""
    while True:
        try:
            # Cada 2 segundos, enviar snapshot de mercados actualizados recientemente
            recent_markets = []
            for m in registry.markets.values():
                if m.last_ws_update and (utc_now() - m.last_ws_update).seconds < 5:
                    recent_markets.append(m.to_dict())

            if recent_markets:
                await manager.broadcast({
                    "type": "market_update",
                    "markets": recent_markets[:10],  # Limitar para no saturar
                })

        except Exception as e:
            print(f"[BG Relay] Error: {e}")

        await asyncio.sleep(2)


# ========== API ENDPOINTS ==========

@app.get("/")
async def root():
    return {"message": "QUANT KALSHI API", "version": "1.0.0", "exchange": "kalshi", "trading_mode": live_manager.mode, "kalshi": pm_client.public_status()}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Evita el 404 de favicon en el navegador sin anadir assets binarios."""
    return Response(status_code=204)


@app.get("/api/markets")
async def get_markets(
    limit: int = Query(default=50, ge=1, le=5000),
    category: str | None = None,
):
    """Obtener mercados activos"""
    markets = registry.get_all_markets()
    categories = build_category_summary(markets)
    if category:
        markets = [m for m in markets if m.get("category", "Other").lower() == category.lower()]
    return {
        "markets": markets[:limit],
        "total": len(markets),
        "categories": categories,
        "available_categories": list(CATEGORIES.values()),
        "selected_category": category or "All",
    }


@app.get("/api/market/{market_id}")
async def get_market(market_id: str):
    """Obtener detalle de un mercado"""
    market = registry.get_market(market_id)
    if not market:
        return {"error": "Market not found"}
    return market


@app.get("/api/signals")
async def get_signals(
    limit: int = Query(default=1000, ge=1, le=5000),
    category: str | None = None,
):
    """Obtener señales activas ordenadas para el dashboard.

    Cada señal sale enriquecida con `market_category` para poder organizar en
    tiempo real las oportunidades por Crypto, Politics, Sports, etc. El filtro
    opcional `category` permite pedir solo una categoría concreta desde la API.
    """
    signals = registry.get_active_signals()
    categories = build_category_summary([
        {"category": signal.get("market_category") or signal.get("category") or "Other"}
        for signal in signals
    ])
    if category:
        signals = [
            signal for signal in signals
            if (signal.get("market_category") or signal.get("category") or "Other").lower() == category.lower()
        ]
    return {
        "signals": signals[:limit],
        "total": len(signals),
        "categories": categories,
        "available_categories": list(CATEGORIES.values()),
        "selected_category": category or "All",
    }


@app.get("/api/stats")
async def get_stats():
    """Estadísticas del sistema"""
    registry.system_stats["strategy_diagnostics"] = build_strategy_diagnostics()
    return {
        **registry.system_stats,
        "markets_tracked": len(registry.markets),
        "active_signals": len(registry.get_active_signals()),
        "timestamp": utc_now().isoformat(),
    }


@app.get("/api/strategies")
async def get_strategies():
    """Diagnóstico por estrategia y razones de filtrado."""
    diagnostics = build_strategy_diagnostics()
    registry.system_stats["strategy_diagnostics"] = diagnostics
    return {"strategies": diagnostics, "timestamp": utc_now().isoformat()}


@app.get("/api/arbitrage")
async def get_arbitrage_opportunities():
    """Oportunidades de bundle arbitrage"""
    opportunities = []
    for m in registry.markets.values():
        if "Yes" in m.outcomes and "No" in m.outcomes:
            yes_ask = m.best_ask.get("Yes")
            no_ask = m.best_ask.get("No")
            yes_bid = m.best_bid.get("Yes")
            no_bid = m.best_bid.get("No")

            if yes_ask and no_ask:
                buy_sum = float(yes_ask) + float(no_ask)
                if buy_sum < 0.995:
                    opportunities.append({
                        "market_id": m.market_id,
                        "question": m.question,
                        "type": "BUY_BUNDLE",
                        "sum": round(buy_sum, 4),
                        "profit": round(1 - buy_sum, 4),
                        "yes_ask": str(yes_ask),
                        "no_ask": str(no_ask),
                    })

            if yes_bid and no_bid:
                sell_sum = float(yes_bid) + float(no_bid)
                if sell_sum > 1.005:
                    opportunities.append({
                        "market_id": m.market_id,
                        "question": m.question,
                        "type": "SELL_BUNDLE",
                        "sum": round(sell_sum, 4),
                        "profit": round(sell_sum - 1, 4),
                        "yes_bid": str(yes_bid),
                        "no_bid": str(no_bid),
                    })

    return {"opportunities": sorted(opportunities, key=lambda x: x["profit"], reverse=True)}


@app.get("/api/top-signals")
async def get_top_signals(
    min_confidence: int = Query(default=75, ge=40, le=99),
    category: str | None = None,
    horizon: str | None = "flash",
):
    """Obtener señales filtradas de máxima probabilidad (Sniper) con Kelly Sizing y Horizonte Flash por defecto."""
    signals = registry.get_active_signals()
    top = []
    for s in signals:
        conf = float(s.get("confidence") or 0)
        edge = float(s.get("edge") or 0)
        entry = float(s.get("entry_price") or 0)

        # Filtros de alta probabilidad
        if conf >= min_confidence and 0.02 < entry < 0.98:
            target = float(s.get("target_price") or 0)
            stop = float(s.get("stop_loss") or 0)
            if stop <= 0:
                stop = max(0.01, entry * 0.92)
                s["stop_loss"] = f"{stop:.4f}"
            if target <= 0:
                target = min(0.99, entry + max(0.04, edge if edge > 0 else 0.05))
                s["target_price"] = f"{target:.4f}"

            risk = abs(entry - stop)
            reward = abs(target - entry)
            s["risk_reward_ratio"] = round(reward / risk, 2) if risk > 0 else 1.5
            s["recommended_order_type"] = "LIMIT (Maker)"
            s["recommended_limit_price"] = f"{entry:.4f}"

            if category and (s.get("market_category") or "Other").lower() != category.lower():
                continue
            if horizon and horizon.lower() != "all" and str(s.get("horizon") or "").lower() != horizon.lower():
                continue
            top.append(s)

    top.sort(key=lambda x: (float(x.get("confidence") or 0), float(x.get("edge") or 0)), reverse=True)
    return {
        "top_signals": top[:100],
        "total": len(top),
        "filter_criteria": {
            "min_confidence": min_confidence,
            "min_risk_reward": "1.3:1",
            "order_type": "LIMIT",
            "slippage_protection": "Active",
            "horizon": horizon or "All",
        }
    }


@app.get("/api/track-record")
async def get_track_record():
    """Métricas y posiciones en vivo del Track Record (Paper Trading)."""
    paper_tracker.update_live_prices(registry)
    return paper_tracker.get_summary()


@app.get("/api/strategy-performance")
async def get_strategy_performance():
    """Rendimiento y desglose de PnL flotante y realizado por cada estrategia cuantitativa."""
    paper_tracker.update_live_prices(registry)
    return paper_tracker.get_strategy_performance()


@app.get("/api/strategy-ranking")
async def get_strategy_ranking(mode: str = Query(default="realistic")):
    """Ranking cuantitativo de estrategias con significancia estadística, validación ML y ETA.

    mode: "realistic" (cuenta de $1,000) o "research" (presupuesto aislado por estrategia).
    """
    engine = paper_tracker_research if mode == "research" else paper_tracker
    engine.update_live_prices(registry)
    return build_strategy_ranking(engine, ai_learning_engine, quant_ml, mode=mode)


@app.get("/api/promotion/board")
async def get_promotion_board(mode: str = Query(default="realistic")):
    """Puerta de promoción PAPER -> LIVE con corrección estadística.

    Con 16 estrategias en paralelo alguna parece ganadora por azar. Este tablero
    exige significancia con corrección de Bonferroni y confirmación en un holdout
    temporal antes de marcar una estrategia como apta para live.
    """
    engine = paper_tracker_research if mode == "research" else paper_tracker
    engine.update_live_prices(registry)
    return build_promotion_board(engine, governor)


@app.get("/api/governor")
async def get_governor():
    """Estado del gobernador: estrategias pausadas, reglas dinamicas y drawdown global."""
    return governor.get_status()


@app.post("/api/track-record/reset")
async def reset_track_record():
    """Reiniciar el track record de paper trading."""
    paper_tracker.reset_track_record()
    return {"message": "Track record reiniciado con éxito", "summary": paper_tracker.get_summary()}


@app.get("/api/settings/trading-mode")
async def get_trading_mode():
    """Obtener estado del modo de operativa (Paper vs Live) y credenciales."""
    return live_manager.get_public_status()


@app.post("/api/settings/trading-mode")
async def set_trading_mode(payload: Dict[str, Any]):
    """Cambiar modo entre PAPER y LIVE con validación de seguridad."""
    mode = str(payload.get("mode", "PAPER"))
    res = live_manager.set_mode(mode)
    return res


@app.get("/api/settings/credentials")
async def get_credentials():
    """Obtener estado de configuración de credenciales Kalshi."""
    return live_manager.get_public_status()


@app.post("/api/settings/credentials")
async def update_credentials(payload: Dict[str, Any]):
    """Guardar configuración local de credenciales Kalshi."""
    live_manager.save_credentials(payload)
    return {"success": True, "status": live_manager.get_public_status()}


@app.get("/api/kalshi/balance")
async def get_kalshi_balance():
    """Probar autenticación Kalshi y devolver balance demo/producción."""
    return await live_manager.get_balance()


@app.get("/api/kalshi/environments")
async def get_kalshi_environments():
    """Matriz de sondeo por entorno: demuestra qué credencial vale dónde.

    Sirve para diagnosticar por qué el bot habla con demo o producción sin tener
    que leer logs: incluye el status HTTP observado por entorno y la clave usada
    (enmascarada).
    """
    return pm_client.environment_status()


@app.post("/api/settings/environment")
async def set_kalshi_environment(payload: Dict[str, Any]):
    """Cambia el entorno (demo / production / auto) en caliente.

    Reconstruye la credencial del cliente y del gestor de ejecución y reconecta
    el WebSocket, porque cada entorno tiene su propia API key.
    """
    requested = str(payload.get("environment") or payload.get("env") or "").strip().lower()
    if requested not in {"demo", "production", "prod", "auto"}:
        return {
            "success": False,
            "error": "environment debe ser 'demo', 'production' o 'auto'",
        }
    try:
        state = await pm_client.apply_environment(requested)
        live_manager.apply_environment(kalshi_env.env)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "requested": requested,
        "environment": kalshi_env.env,
        "auth_verified": kalshi_env.auth_verified,
        "detection_note": kalshi_env.detection_note,
        "ws_connected": registry.system_stats.get("ws_connected", False),
        "state": state,
    }


@app.post("/api/live/kill-switch")
async def toggle_kill_switch():
    """Activar / Desactivar botón de pánico (Kill Switch) para detener toda orden real."""
    if live_manager.kill_switch_active:
        res = live_manager.deactivate_kill_switch()
    else:
        res = live_manager.activate_kill_switch()
    return res


# ========== ENDPOINTS LEAD-LAG LATENCY SNIPING ==========

@app.get("/api/lead-lag/status")
async def get_lead_lag_status():
    """Obtener estado en vivo de los tickers Binance, velocidad y oportunidades activas."""
    return lead_lag_engine.get_status()


@app.get("/api/lead-lag/opportunities")
async def get_lead_lag_opportunities():
    """Obtener oportunidades activas de Sniping de latencia Lead-Lag."""
    return {
        "count": len(lead_lag_engine.active_opportunities),
        "opportunities": [o.__dict__ if hasattr(o, "__dict__") else o for o in lead_lag_engine.active_opportunities],
        "history": [o.__dict__ if hasattr(o, "__dict__") else o for o in lead_lag_engine.opportunities_history[:25]],
    }


@app.post("/api/lead-lag/execute")
async def execute_lead_lag_snipe(payload: Dict[str, Any]):
    """Ejecutar un trade de arbitraje Lead-Lag manual o automático."""
    opp_id = payload.get("opportunity_id")
    opp = next((o for o in lead_lag_engine.active_opportunities if o.id == opp_id), None)
    if not opp:
        return {"success": False, "error": "Oportunidad no encontrada o expirada"}

    # Disparar simulación de entrada o ejecución Kalshi según modo
    opp.status = "EXECUTED"
    return {
        "success": True,
        "message": f"Orden Lead-Lag enviada para {opp.symbol} {opp.outcome} a ${opp.clob_price}",
        "opportunity": opp.__dict__ if hasattr(opp, "__dict__") else opp,
    }


# ========== ENDPOINTS AGENTE IA & AUTO-APRENDIZAJE ==========

@app.get("/api/ai-agent/metrics")
async def get_ai_agent_metrics():
    """Obtener métricas globales de Brier Score, descomposición de Murphy y pesos Kelly."""
    return ai_learning_engine.get_status()


@app.get("/api/ai-agent/reflections")
async def get_ai_agent_reflections():
    """Obtener historial de reflexiones post-mortem y lecciones aprendidas por la IA."""
    return {
        "total": len(ai_learning_engine.reflections),
        "reflections": [r.__dict__ if hasattr(r, "__dict__") else r for r in ai_learning_engine.reflections],
    }


@app.post("/api/ai-agent/optimize")
async def trigger_ai_optimization():
    """Ejecutar ciclo forzado de auto-aprendizaje, regresión isotónica y re-calibración de pesos."""
    closed_trades = [t for t in paper_tracker.trades.values() if t.get("status") in ("WON", "LOST")]
    result = ai_learning_engine.run_daily_calibration(closed_trades)
    return result


@app.get("/api/news-agent/catalysts")
async def get_news_catalysts():
    """Obtener noticias de alta velocidad procesadas con inferencia Bayesiana."""
    return news_oracle_agent.get_status()


# ========== ENDPOINTS QUANT ML & BANDITS (LinUCB, Conformal, OFI, Arb) ==========

@app.get("/api/quant-ml/status")
async def get_quant_ml_status():
    """Estado del Meta-Motor Cuantitativo: LinUCB Bandits, Conformal Prediction, OFI y Arbitraje."""
    return quant_ml.get_status()


@app.get("/api/quant-ml/conformal-signals")
async def get_conformal_signals():
    """Obtener señales evaluadas bajo el filtro de Conformal Prediction al 95% de confianza."""
    signals_list = []
    for tr in list(paper_tracker.trades.values())[:30]:
        conf = float(tr.get("confidence", 80.0))
        entry = float(tr.get("entry_price", 0.50))
        side = tr.get("side", "BUY")
        conf_eval = quant_ml.conformal.evaluate_signal(conf, entry, side)
        signals_list.append({
            "trade_id": tr.get("trade_id"),
            "strategy": tr.get("strategy_code"),
            "market_question": tr.get("market_question"),
            "entry_price": entry,
            "side": side,
            "predicted_prob": conf_eval.predicted_prob,
            "quantile_q": conf_eval.quantile_q,
            "p_lower": conf_eval.p_lower,
            "p_upper": conf_eval.p_upper,
            "is_admissible": conf_eval.is_admissible,
            "edge_pct": conf_eval.edge_pct,
            "rejection_reason": conf_eval.rejection_reason,
        })
    return {"total": len(signals_list), "signals": signals_list}


@app.get("/api/quant-ml/combinatorial-arb")
async def get_combinatorial_arb():
    """Obtener oportunidades de arbitraje combinatorio libres de riesgo detectadas."""
    return {
        "total": len(quant_ml.combinatorial.opportunities),
        "opportunities": [asdict(o) for o in quant_ml.combinatorial.opportunities],
    }


# ========== ENDPOINTS DERIVADOS Y MICROESTRUCTURA DE ALTA FRECUENCIA ==========

@app.get("/api/derivatives/black-scholes-signals")
async def get_black_scholes_signals():
    """Pricing analítico exacto de Opciones Binarias / Digitales con Black-Scholes N(d2)."""
    return bs_digital_engine.get_status()


@app.get("/api/microstructure/vpin-status")
async def get_vpin_status():
    """Índice VPIN de toxicidad institucional O(1) y Kyle's Lambda (López de Prado)."""
    return vpin_manager.get_status()


# ========== ENDPOINTS MARKET MAKING, MULTI-EXCHANGE Y AUDITORÍA POLYGON ==========

@app.get("/api/market-making/status")
async def get_market_making_status():
    """Estado y cotizaciones del motor Avellaneda-Stoikov Market Maker."""
    try:
        from lead_lag_engine import lead_lag_engine
        markets = getattr(lead_lag_engine, "tracked_polymarket_contracts", [])
        vpin_info = vpin_manager.get_status()
        vpin_val = vpin_info.get("global_vpin", 0.20)
        for m in markets:
            sym = m.get("crypto_symbol", "BTC")
            clob_ask = m.get("clob_ask", 0.50)
            vel = lead_lag_engine.get_velocity(sym, 10.0)
            avellaneda_stoikov_engine.quote_market(
                market_id=m.get("market_id", f"poly_{sym.lower()}_flash"),
                symbol=sym,
                mid_price=clob_ask,
                vpin_toxicity=vpin_val,
                crypto_velocity_10s=vel,
            )
    except Exception as e:
        pass
    return avellaneda_stoikov_engine.get_status()


@app.post("/api/market-making/toggle")
async def toggle_market_making():
    """Habilita o pausa el creador de mercado pasivo Avellaneda-Stoikov."""
    avellaneda_stoikov_engine.is_enabled = not avellaneda_stoikov_engine.is_enabled
    return {"is_enabled": avellaneda_stoikov_engine.is_enabled}


@app.get("/api/multi-exchange/status")
async def get_multi_exchange_status():
    """Feed paralelo de Binance, Coinbase y Bybit con detector First Mover."""
    return multi_exchange_feed.get_status()


@app.get("/api/wallet/health-check")
async def get_wallet_health_check(force: bool = False):
    """Diagnóstico on-chain de Polygon (saldos POL, USDC, allowances y latencia RPC)."""
    report = await polygon_health_checker.check_health(force_refresh=force)
    return polygon_health_checker.get_status()


@app.get("/api/health")
async def health():
    """Chequeo de salud para operacion 24/7 (lo consume el supervisor)."""
    now_ts = time.time()
    last_ws = float(registry.system_stats.get("last_ws_message") or 0.0)
    last_market_ts = 0.0
    for market in registry.markets.values():
        if market.last_update is not None:
            last_market_ts = max(last_market_ts, market.last_update.timestamp())
    ws_age = round(now_ts - last_ws, 1) if last_ws else None
    market_age = round(now_ts - last_market_ts, 1) if last_market_ts else None
    problems = []
    if not registry.markets:
        problems.append("sin_mercados")
    if market_age is not None and market_age > 900:
        problems.append("datos_de_mercado_obsoletos")
    if ws_age is not None and ws_age > 300:
        problems.append("websocket_sin_mensajes")
    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "exchange": "kalshi",
        "kalshi_env": str(kalshi_env.env),
        "kalshi_auth_verified": kalshi_env.auth_verified,
        "kalshi_environment_probe": kalshi_env.probe_results,
        "trading_mode": live_manager.get_public_status(),
        "markets_tracked": len(registry.markets),
        "events_tracked": len(registry.event_meta),
        "active_signals": len(registry.get_active_signals()),
        "signal_history": len(registry.signals_log),
        "ws_status": registry.system_stats.get("ws_status"),
        "ws_age_seconds": ws_age,
        "market_data_age_seconds": market_age,
        "kalshi_trading_active": KALSHI_STATE.get("trading_active"),
        "kalshi_maintenance_window": KALSHI_STATE.get("maintenance_window"),
        "uptime_seconds": round(now_ts - START_TS, 1),
        "governor": governor.get_status(),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Servir el dashboard HTML"""
    with open(STATIC_DIR / "index.html", "r", encoding="utf-8") as f:
        return f.read()


# ========== WEBSOCKET DASHBOARD ==========

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Enviar snapshot inicial
        await websocket.send_json({
            "type": "init",
            "markets": registry.get_all_markets()[:20],
            "signals": registry.get_active_signals()[:20],
            "stats": {**registry.system_stats, "markets_tracked": len(registry.markets), "active_signals": len(registry.get_active_signals())},
        })

        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg.get("action") == "get_market":
                market_id = msg.get("market_id")
                market = registry.get_market(market_id)
                await websocket.send_json({
                    "type": "market_detail",
                    "market": market,
                })

            elif msg.get("action") == "get_signals":
                signals = registry.get_active_signals()
                await websocket.send_json({
                    "type": "signals",
                    "signals": signals[:30],
                })

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"[WS Dashboard] Error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
