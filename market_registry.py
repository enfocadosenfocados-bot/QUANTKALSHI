"""Market Registry - Cache en memoria de todos los mercados activos"""
import asyncio
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_decimal(value: Any, default: str = "0") -> Decimal:
    """Conversión segura a Decimal para payloads externos."""
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def order_price(level: Any) -> Decimal:
    if isinstance(level, dict):
        return to_decimal(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return to_decimal(level[0])
    return Decimal("0")


def order_size(level: Any) -> Decimal:
    if isinstance(level, dict):
        return to_decimal(level.get("size"))
    if isinstance(level, (list, tuple)) and len(level) > 1:
        return to_decimal(level[1])
    return Decimal("0")


def sort_bids(bids: List[Any]) -> List[Any]:
    """CLOB devuelve bids de menor a mayor en algunos endpoints; el best bid es el mayor."""
    return sorted(bids or [], key=order_price, reverse=True)


def sort_asks(asks: List[Any]) -> List[Any]:
    """CLOB devuelve asks de mayor a menor en algunos endpoints; el best ask es el menor."""
    return sorted(asks or [], key=order_price)


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parsear fechas ISO/epoch de Gamma de forma tolerante."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            if value > 10_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value, tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


@dataclass
class PriceHistory:
    """Historial de precios para cálculos de estrategia"""
    timestamps: List[datetime] = field(default_factory=list)
    prices: List[Decimal] = field(default_factory=list)
    volumes: List[Decimal] = field(default_factory=list)

    def add(self, price: Decimal, volume: Decimal = Decimal("0")):
        self.timestamps.append(utc_now())
        self.prices.append(price)
        self.volumes.append(volume)
        # Mantener solo últimos 1000 puntos
        if len(self.prices) > 1000:
            self.timestamps = self.timestamps[-1000:]
            self.prices = self.prices[-1000:]
            self.volumes = self.volumes[-1000:]

    def get_window(self, minutes: int) -> List[Decimal]:
        cutoff = utc_now() - timedelta(minutes=minutes)
        return [p for t, p in zip(self.timestamps, self.prices) if t >= cutoff]

    def sma(self, minutes: int) -> Optional[Decimal]:
        window = self.get_window(minutes)
        if not window:
            return None
        return sum(window) / len(window)

    def std(self, minutes: int) -> Optional[Decimal]:
        window = self.get_window(minutes)
        if len(window) < 2:
            return None
        mean = sum(window) / len(window)
        variance = sum((p - mean) ** 2 for p in window) / len(window)
        return variance.sqrt()

    def z_score(self, minutes: int, current_price: Decimal) -> Optional[Decimal]:
        sma = self.sma(minutes)
        std = self.std(minutes)
        if sma is None or std is None or std == 0:
            return None
        return (current_price - sma) / std

    def velocity_1m(self) -> Optional[Decimal]:
        window = self.get_window(1)
        if len(window) < 2:
            return None
        return (window[-1] - window[0]) / window[0] if window[0] != 0 else Decimal("0")


@dataclass
class MarketSnapshot:
    """Snapshot unificado de un mercado"""
    # IDs
    market_id: str = ""
    condition_id: str = ""
    slug: str = ""
    question: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)

    # Outcomes
    outcomes: List[str] = field(default_factory=list)
    token_ids: Dict[str, str] = field(default_factory=dict)

    # Precios actuales
    prices: Dict[str, Decimal] = field(default_factory=dict)
    best_bid: Dict[str, Decimal] = field(default_factory=dict)
    best_ask: Dict[str, Decimal] = field(default_factory=dict)
    spread: Dict[str, Decimal] = field(default_factory=dict)
    mid_price: Dict[str, Decimal] = field(default_factory=dict)
    last_trade: Dict[str, Decimal] = field(default_factory=dict)

    # Order Book (top 10 niveles)
    order_book: Dict[str, Any] = field(default_factory=dict)

    # Métricas de liquidez
    volume_24h: Decimal = Decimal("0")
    volume_7d: Decimal = Decimal("0")
    liquidity: Decimal = Decimal("0")
    open_interest: int = 0

    # Cambios de precio publicados por Gamma (en puntos de probabilidad, ej. 0.03 = 3c)
    gamma_last_trade_price: Optional[Decimal] = None
    gamma_one_day_price_change: Optional[Decimal] = None
    gamma_one_week_price_change: Optional[Decimal] = None
    gamma_one_month_price_change: Optional[Decimal] = None

    # Fees
    maker_fee_bps: int = 0
    taker_fee_bps: int = 0
    min_order_size: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.001")
    neg_risk: bool = False

    # Temporal
    end_date: Optional[datetime] = None
    start_date: Optional[datetime] = None
    resolution_source: str = ""
    active: bool = True
    closed: bool = False
    resolved: bool = False

    # Metadatos de Kalshi
    event_ticker: str = ""
    mutually_exclusive: bool = False
    exchange_index: Optional[int] = None
    kalshi_status: str = ""
    kalshi_result: str = ""
    strike_type: str = ""
    close_time: Optional[datetime] = None
    settlement_sources: List[Any] = field(default_factory=list)
    price_ranges: List[Any] = field(default_factory=list)
    subtitle: str = ""

    # Historial de precios
    price_history: Dict[str, PriceHistory] = field(default_factory=dict)

    # Señales activas
    signals: List[Dict] = field(default_factory=list)

    # Smart Money
    top_holders: Dict[str, List[Dict]] = field(default_factory=dict)
    recent_trades: List[Dict] = field(default_factory=list)
    whale_signals: List[Dict] = field(default_factory=list)

    # Timestamps
    last_update: datetime = field(default_factory=utc_now)
    last_ws_update: Optional[datetime] = None

    def update_price(self, outcome: str, price: Decimal):
        if price <= 0:
            return
        self.prices[outcome] = price
        if outcome not in self.price_history:
            self.price_history[outcome] = PriceHistory()
        self.price_history[outcome].add(price)

    def apply_quotes(self, quotes: Dict[str, Dict[str, Any]]):
        """Aplicar los bid/ask que Kalshi publica en el market object.

        Verificado contra GET /markets/{ticker}/orderbook: coinciden, y evita
        decenas de miles de requests en ejecucion 24/7.
        """
        for outcome, quote in (quotes or {}).items():
            if not isinstance(quote, dict):
                continue
            bid = to_decimal(quote.get("bid"))
            ask = to_decimal(quote.get("ask"))
            mid = to_decimal(quote.get("mid"))
            if bid > 0:
                self.best_bid[outcome] = bid
            if ask > 0:
                self.best_ask[outcome] = ask
            if bid > 0 and ask > 0 and ask >= bid:
                self.spread[outcome] = ask - bid
            if mid > 0:
                self.mid_price[outcome] = mid
                self.update_price(outcome, mid)

    def quote_mid(self) -> Decimal:
        """Mid mas fiable disponible para el lado YES."""
        for source in (self.mid_price, self.prices, self.last_trade):
            value = source.get("Yes")
            if value and value > 0:
                return value
        return Decimal("0")

    def update_orderbook(self, outcome: str, bids: List[Dict], asks: List[Dict]):
        bids = sort_bids(bids)
        asks = sort_asks(asks)
        self.order_book[outcome] = {"bids": bids, "asks": asks}
        if bids:
            self.best_bid[outcome] = order_price(bids[0])
        if asks:
            self.best_ask[outcome] = order_price(asks[0])
        if outcome in self.best_bid and outcome in self.best_ask:
            self.spread[outcome] = self.best_ask[outcome] - self.best_bid[outcome]
            self.mid_price[outcome] = (self.best_bid[outcome] + self.best_ask[outcome]) / 2
            self.update_price(outcome, self.mid_price[outcome])

    def to_dict(self) -> Dict:
        """Serializar para JSON"""
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "slug": self.slug,
            "question": self.question,
            "category": self.category,
            "tags": self.tags,
            "outcomes": self.outcomes,
            "token_ids": self.token_ids,
            "prices": {k: str(v) for k, v in self.prices.items()},
            "best_bid": {k: str(v) for k, v in self.best_bid.items()},
            "best_ask": {k: str(v) for k, v in self.best_ask.items()},
            "spread": {k: str(v) for k, v in self.spread.items()},
            "mid_price": {k: str(v) for k, v in self.mid_price.items()},
            "last_trade": {k: str(v) for k, v in self.last_trade.items()},
            "volume_24h": str(self.volume_24h),
            "volume_7d": str(self.volume_7d),
            "liquidity": str(self.liquidity),
            "open_interest": self.open_interest,
            "gamma_last_trade_price": str(self.gamma_last_trade_price) if self.gamma_last_trade_price is not None else None,
            "gamma_one_day_price_change": str(self.gamma_one_day_price_change) if self.gamma_one_day_price_change is not None else None,
            "gamma_one_week_price_change": str(self.gamma_one_week_price_change) if self.gamma_one_week_price_change is not None else None,
            "gamma_one_month_price_change": str(self.gamma_one_month_price_change) if self.gamma_one_month_price_change is not None else None,
            "maker_fee_bps": self.maker_fee_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "min_order_size": str(self.min_order_size),
            "tick_size": str(self.tick_size),
            "neg_risk": self.neg_risk,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "resolution_source": self.resolution_source,
            "active": self.active,
            "closed": self.closed,
            "resolved": self.resolved,
            "event_ticker": self.event_ticker,
            "mutually_exclusive": self.mutually_exclusive,
            "exchange_index": self.exchange_index,
            "kalshi_status": self.kalshi_status,
            "kalshi_result": self.kalshi_result,
            "strike_type": self.strike_type,
            "subtitle": self.subtitle,
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "settlement_sources": self.settlement_sources,
            "signals": self.signals,
            "recent_trades": self.recent_trades[-20:],
            "whale_signals": self.whale_signals[-20:],
            "last_update": self.last_update.isoformat(),
            "last_ws_update": self.last_ws_update.isoformat() if self.last_ws_update else None,
        }


class MarketRegistry:
    """Registro global de mercados en memoria"""

    def __init__(self):
        self.markets: Dict[str, MarketSnapshot] = {}
        self.token_to_market: Dict[str, str] = {}
        # Evento Kalshi -> mercados hijos y metadatos del evento (categoria
        # oficial, mutua exclusividad, fuentes de liquidacion).
        self.event_index: Dict[str, List[str]] = {}
        self.event_meta: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self.signals_log: List[Dict] = []
        self.system_stats = {
            "markets_tracked": 0,
            "ws_connected": False,
            "last_ws_message": None,
            "api_latency_ms": 0,
            "signals_generated": 0,
            "whale_trades_seen": 0,
            "whale_wallets_tracked": 0,
            "last_whale_poll": None,
            "strategy_diagnostics": {},
        }

    async def get_or_create(self, market_id: str) -> MarketSnapshot:
        async with self._lock:
            if market_id not in self.markets:
                self.markets[market_id] = MarketSnapshot(market_id=market_id)
            return self.markets[market_id]

    async def update_market(self, market_id: str, data: Dict):
        async with self._lock:
            if market_id not in self.markets:
                self.markets[market_id] = MarketSnapshot(market_id=market_id)
            m = self.markets[market_id]

            # Actualizar campos básicos
            for field in ["condition_id", "slug", "question", "category", "tags",
                         "outcomes", "resolution_source", "active", "closed", "resolved",
                         "event_ticker", "mutually_exclusive", "exchange_index", "kalshi_status",
                         "kalshi_result", "strike_type", "subtitle", "settlement_sources",
                         "price_ranges"]:
                if field in data:
                    setattr(m, field, data[field])

            if "token_ids" in data:
                m.token_ids = data["token_ids"]
                for outcome, tid in data["token_ids"].items():
                    self.token_to_market[tid] = market_id

            if "volume_24h" in data:
                m.volume_24h = to_decimal(data["volume_24h"])
            if "volume_7d" in data:
                m.volume_7d = to_decimal(data["volume_7d"])
            if "liquidity" in data:
                m.liquidity = to_decimal(data["liquidity"])
            if "open_interest" in data:
                try:
                    m.open_interest = int(float(data["open_interest"] or 0))
                except (ValueError, TypeError):
                    m.open_interest = 0

            for source_field, attr in [
                ("gamma_last_trade_price", "gamma_last_trade_price"),
                ("gamma_one_day_price_change", "gamma_one_day_price_change"),
                ("gamma_one_week_price_change", "gamma_one_week_price_change"),
                ("gamma_one_month_price_change", "gamma_one_month_price_change"),
            ]:
                if source_field in data:
                    raw = data[source_field]
                    setattr(m, attr, None if raw in (None, "") else to_decimal(raw))
            if "maker_fee_bps" in data:
                try:
                    m.maker_fee_bps = int(float(data["maker_fee_bps"] or 0))
                except (ValueError, TypeError):
                    m.maker_fee_bps = 0
            if "taker_fee_bps" in data:
                try:
                    m.taker_fee_bps = int(float(data["taker_fee_bps"] or 0))
                except (ValueError, TypeError):
                    m.taker_fee_bps = 0
            if "min_order_size" in data:
                m.min_order_size = to_decimal(data["min_order_size"])
            if "tick_size" in data:
                m.tick_size = to_decimal(data["tick_size"], "0.001")
            if "neg_risk" in data:
                m.neg_risk = bool(data["neg_risk"])
            if "end_date" in data:
                m.end_date = parse_datetime(data["end_date"])
            if "start_date" in data:
                m.start_date = parse_datetime(data["start_date"])
            if "close_time" in data:
                m.close_time = parse_datetime(data["close_time"])

            # Precios publicados por Kalshi: evita pedir el orderbook por mercado.
            if "quotes" in data and isinstance(data["quotes"], dict):
                m.apply_quotes(data["quotes"])

            # Indice evento -> mercados hijos, base de las estrategias de canasta.
            if m.event_ticker:
                bucket = self.event_index.setdefault(m.event_ticker, [])
                if market_id not in bucket:
                    bucket.append(market_id)

            if "initial_prices" in data and isinstance(data["initial_prices"], dict):
                for outcome, price in data["initial_prices"].items():
                    dec_price = to_decimal(price)
                    if dec_price > 0 and m.prices.get(outcome) != dec_price:
                        m.update_price(outcome, dec_price)

            m.last_update = utc_now()

    async def update_from_ws(self, token_id: str, event_type: str, payload: Dict):
        """Actualizar desde evento WebSocket"""
        if token_id not in self.token_to_market:
            return

        market_id = self.token_to_market[token_id]
        if market_id not in self.markets:
            return

        m = self.markets[market_id]
        m.last_ws_update = utc_now()

        # Kalshi usa el mismo ticker para YES y NO, por lo que el outcome debe
        # venir explicito en el payload cuando el feed lo conoce.
        explicit_outcome = payload.get("outcome") if isinstance(payload, dict) else None
        if explicit_outcome and str(explicit_outcome) in m.token_ids:
            outcome = str(explicit_outcome)
        else:
            outcome = None
            for o, tid in m.token_ids.items():
                if tid == token_id:
                    outcome = o
                    break

        if not outcome:
            return

        if event_type in ("book", "orderbook_snapshot"):
            bids = payload.get("bids", [])
            asks = payload.get("asks", [])
            m.update_orderbook(outcome, bids, asks)

        elif event_type == "price_change":
            for pc in payload.get("price_changes", []):
                if pc.get("best_bid"):
                    m.best_bid[outcome] = to_decimal(pc["best_bid"])
                if pc.get("best_ask"):
                    m.best_ask[outcome] = to_decimal(pc["best_ask"])
                if outcome in m.best_bid and outcome in m.best_ask:
                    m.spread[outcome] = m.best_ask[outcome] - m.best_bid[outcome]
                    m.mid_price[outcome] = (m.best_bid[outcome] + m.best_ask[outcome]) / 2
                    m.update_price(outcome, m.mid_price[outcome])

        elif event_type in ("last_trade_price", "kalshi_price"):
            price = to_decimal(payload.get("price", 0))
            if price <= 0:
                return
            m.last_trade[outcome] = price
            m.update_price(outcome, price)
            if outcome.lower() == "yes":
                inverse = Decimal("1") - price
                m.mid_price["No"] = inverse
                m.update_price("No", inverse)
            elif outcome.lower() == "no":
                inverse = Decimal("1") - price
                m.mid_price["Yes"] = inverse
                m.update_price("Yes", inverse)

        elif event_type == "best_bid_ask":
            if payload.get("best_bid"):
                m.best_bid[outcome] = to_decimal(payload["best_bid"])
            if payload.get("best_ask"):
                m.best_ask[outcome] = to_decimal(payload["best_ask"])
            if outcome in m.best_bid and outcome in m.best_ask:
                m.spread[outcome] = m.best_ask[outcome] - m.best_bid[outcome]
                m.mid_price[outcome] = (m.best_bid[outcome] + m.best_ask[outcome]) / 2
                m.update_price(outcome, m.mid_price[outcome])

    async def add_signal(self, market_id: str, signal: Dict):
        async with self._lock:
            if market_id in self.markets:
                m = self.markets[market_id]
                signal_key = signal.get("dedupe_key") or f"{signal.get('strategy_code')}:{signal.get('token')}:{signal.get('side')}"
                signal["dedupe_key"] = signal_key
                replaced = False
                for idx, existing in enumerate(m.signals):
                    existing_key = existing.get("dedupe_key") or f"{existing.get('strategy_code')}:{existing.get('token')}:{existing.get('side')}"
                    if existing_key == signal_key:
                        signal.setdefault("first_seen", existing.get("first_seen") or existing.get("timestamp"))
                        m.signals[idx] = signal
                        replaced = True
                        break
                if not replaced:
                    m.signals.append(signal)
                # Mantener solo últimas 20 señales por mercado
                if len(m.signals) > 20:
                    m.signals = m.signals[-20:]

            signal["timestamp"] = utc_now().isoformat()
            log_key = signal.get("dedupe_key")
            # Dedupe sobre TODO el histórico (no solo las últimas 200) para no
            # re-contar repetidamente la misma señal y no inflar el contador.
            if not log_key or not any(s.get("dedupe_key") == log_key for s in self.signals_log):
                self.signals_log.append(signal)
                if len(self.signals_log) > 1000:
                    self.signals_log = self.signals_log[-1000:]
                self.system_stats["signals_generated"] += 1

    async def add_whale_trade(self, trade: Dict):
        """Guardar una operación grande reciente en el mercado correspondiente."""
        condition_id = str(trade.get("conditionId") or trade.get("condition_id") or "").lower()
        asset = str(trade.get("asset") or trade.get("token_id") or "")
        market_id = None

        if asset and asset in self.token_to_market:
            market_id = self.token_to_market[asset]
        elif condition_id:
            for mid, market in self.markets.items():
                if market.condition_id.lower() == condition_id:
                    market_id = mid
                    break

        if not market_id or market_id not in self.markets:
            return

        async with self._lock:
            m = self.markets[market_id]
            tx = trade.get("transactionHash") or trade.get("transaction_hash") or ""
            wallet = (trade.get("proxyWallet") or trade.get("user") or "").lower()
            outcome = str(trade.get("outcome") or "")
            key = f"{tx}:{wallet}:{asset}:{outcome}:{trade.get('timestamp')}"
            if any(t.get("trade_key") == key for t in m.recent_trades[-100:]):
                return
            enriched = dict(trade)
            enriched["trade_key"] = key
            enriched["notional"] = str(to_decimal(trade.get("size")) * to_decimal(trade.get("price")))
            m.recent_trades.append(enriched)
            m.recent_trades = m.recent_trades[-100:]
            self.system_stats["whale_trades_seen"] += 1

    async def set_whale_signal(self, market_id: str, signal: Dict):
        async with self._lock:
            if market_id not in self.markets:
                return
            m = self.markets[market_id]
            key = signal.get("dedupe_key") or f"{signal.get('wallet')}:{signal.get('token')}:{signal.get('side')}"
            signal["dedupe_key"] = key
            m.whale_signals = [s for s in m.whale_signals if s.get("dedupe_key") != key]
            m.whale_signals.append(signal)
            m.whale_signals = m.whale_signals[-20:]

    async def register_events(self, events: List[Dict[str, Any]]) -> None:
        """Guardar metadatos de evento (categoria oficial, mutua exclusividad)."""
        async with self._lock:
            for event in events or []:
                ticker = str(event.get("event_ticker") or "")
                if not ticker:
                    continue
                self.event_meta[ticker] = event
        self.system_stats["events_tracked"] = len(self.event_meta)

    def get_event_meta(self, event_ticker: str) -> Dict[str, Any]:
        return self.event_meta.get(event_ticker or "") or {}

    def event_siblings(self, event_ticker: str, exclude_market_id: str = "") -> List[MarketSnapshot]:
        """Mercados hermanos del mismo evento, excluyendo el propio."""
        result: List[MarketSnapshot] = []
        for market_id in self.event_index.get(event_ticker or "", []):
            if market_id == exclude_market_id:
                continue
            market = self.markets.get(market_id)
            if market is not None:
                result.append(market)
        return result

    def active_market_ids(self) -> List[str]:
        return [mid for mid, m in self.markets.items() if m.active and not m.closed and not m.resolved]

    async def prune_stale_markets(self, max_age_seconds: int) -> int:
        """Eliminar mercados que dejaron de aparecer en el feed (evita crecimiento infinito en 24/7)."""
        cutoff = utc_now() - timedelta(seconds=max(60, int(max_age_seconds)))
        removed = 0
        async with self._lock:
            for market_id in list(self.markets.keys()):
                market = self.markets[market_id]
                if market.last_update >= cutoff:
                    continue
                self.markets.pop(market_id, None)
                for token in list(market.token_ids.values()):
                    if self.token_to_market.get(token) == market_id:
                        self.token_to_market.pop(token, None)
                bucket = self.event_index.get(market.event_ticker)
                if bucket and market_id in bucket:
                    bucket.remove(market_id)
                    if not bucket:
                        self.event_index.pop(market.event_ticker, None)
                removed += 1
        if removed:
            self.system_stats["markets_pruned"] = self.system_stats.get("markets_pruned", 0) + removed
        return removed

    def get_all_markets(self) -> List[Dict]:
        return [m.to_dict() for m in self.markets.values()]

    def get_market(self, market_id: str) -> Optional[Dict]:
        if market_id in self.markets:
            return self.markets[market_id].to_dict()
        return None

    def get_active_signals(self) -> List[Dict]:
        signals = []
        for m in self.markets.values():
            for s in m.signals:
                if s.get("status") == "ACTIVE":
                    category = m.category or "Other"
                    signals.append({
                        **s,
                        "market_question": m.question,
                        "market_id": m.market_id,
                        "market_slug": m.slug,
                        "market_category": category,
                        "category": category,
                        "market_liquidity": str(m.liquidity),
                        "market_volume_24h": str(m.volume_24h),
                    })

        def sort_value(signal: Dict, field: str, default: int = 0) -> int:
            try:
                return int(signal.get(field, default) or default)
            except (TypeError, ValueError):
                return default

        return sorted(
            signals,
            key=lambda x: (
                sort_value(x, "expected_profit_bps") == 800,
                sort_value(x, "expected_profit_bps"),
                sort_value(x, "confidence"),
                x.get("timestamp", ""),
            ),
            reverse=True,
        )


# Instancia global
registry = MarketRegistry()
