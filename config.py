"""Configuración global de QUANT KALSHI.

La app arranca en Kalshi DEMO por defecto. No pongas secretos aquí: usa variables de
entorno o un archivo .env local no versionado.
"""
import os
from pathlib import Path
from typing import Dict

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path = BASE_DIR / ".env") -> None:
    """Cargar un .env simple sin depender de python-dotenv."""
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:
        print(f"[Config] No se pudo cargar .env: {exc}")


_load_env_file()

# ========== Kalshi environments ==========
# KALSHI_ENV admite: "demo", "production" o "auto".
# Con "auto" se detecta en arranque en qué entorno son validas las credenciales
# (Kalshi no comparte API keys entre demo y produccion).
KALSHI_ENV = os.getenv("KALSHI_ENV", "auto").strip().lower()
if KALSHI_ENV == "prod":
    KALSHI_ENV = "production"
if KALSHI_ENV not in {"demo", "production", "auto"}:
    KALSHI_ENV = "auto"
KALSHI_IS_DEMO = KALSHI_ENV == "demo"

# ========== Interlock de dinero real ==========
# Que el entorno activo sea produccion NO autoriza a enviar ordenes con dinero
# real. `KALSHI_ENV=production` es el plano de DATOS: alli viven los ~2.826
# mercados con volumen que usan las estrategias, y por eso es el entorno normal
# del bot. Colocar ordenes reales es un acto distinto y explicito, y este
# interlock lo separa:
#
#   * KALSHI_ENV=demo          -> dinero ficticio, siempre permitido.
#   * KALSHI_ENV=auto          -> el entorno ni siquiera esta fijado; no se envia.
#   * KALSHI_ENV=production    -> BLOQUEADO salvo ALLOW_REAL_MONEY=true.
#
# El escenario que esto evita es concreto: arreglar el cableado de
# `execute_order` (que hoy se llama con 1 de 3 argumentos) sin este candado hacia
# que la primera orden del bot aterrizara en produccion con dinero real.
ALLOW_REAL_MONEY = os.getenv("ALLOW_REAL_MONEY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Tope duro por orden real, en dolares. Independiente de `max_live_trade_usd`
# (que es un ajuste de UI y se puede subir desde el dashboard): esto es el freno
# de ultimo recurso contra un bug de sizing en el camino de dinero real.
LIVE_ABS_MAX_ORDER_USD = float(os.getenv("LIVE_ABS_MAX_ORDER_USD", "50"))

KALSHI_REST_BASES: Dict[str, str] = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}
KALSHI_WS_BASES: Dict[str, str] = {
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    "production": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
}

_INITIAL_ENV = "demo" if KALSHI_ENV == "auto" else KALSHI_ENV
KALSHI_API = os.getenv("KALSHI_API", KALSHI_REST_BASES[_INITIAL_ENV]).rstrip("/")
KALSHI_WS = os.getenv("KALSHI_WS", KALSHI_WS_BASES[_INITIAL_ENV])

# ========== Credenciales por entorno ==========
# Kalshi NO comparte API keys entre demo y produccion: una key creada en la web de
# demo solo autentica contra demo y viceversa. Por eso cada entorno puede tener su
# propio par (key_id + clave privada) y la deteccion elige el que corresponde.
#
# Kalshi acepta claves RSA (PKCS#1 "BEGIN RSA PRIVATE KEY") y Ed25519 (PKCS#8
# "BEGIN PRIVATE KEY"); `kalshi_auth` firma con el algoritmo que corresponda.
#
# Nombres legacy (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH / KALSHI_PRIVATE_KEY_PEM)
# se conservan como respaldo del entorno solicitado para no romper .env existentes.
def _env_specific_credentials(env: str) -> Dict[str, str]:
    prefix = "KALSHI_DEMO" if env == "demo" else "KALSHI_PROD"
    return {
        "key_id": os.getenv(f"{prefix}_KEY_ID", "").strip(),
        "private_key_path": os.getenv(f"{prefix}_PRIVATE_KEY_PATH", "").strip(),
        "private_key_pem": os.getenv(f"{prefix}_PRIVATE_KEY_PEM", "").strip(),
    }


# Credenciales legacy: no declaran a que entorno pertenecen, asi que se ofrecen
# como respaldo a cualquier entorno candidato y es la deteccion la que decide cual
# las acepta (esa es justamente la funcion de KALSHI_ENV=auto).
_LEGACY_CREDENTIALS: Dict[str, str] = {
    "key_id": os.getenv("KALSHI_KEY_ID", "").strip(),
    "private_key_path": os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip(),
    "private_key_pem": os.getenv("KALSHI_PRIVATE_KEY_PEM", "").strip(),
}


def kalshi_credentials(env: str) -> Dict[str, str]:
    """Credenciales para un entorno: las específicas mandan, si no las legacy.

    Devuelve un dict con `key_id`, `private_key_path` y `private_key_pem` (puede
    venir vacío si no hay nada configurado para ese entorno).
    """
    specific = _env_specific_credentials(env)
    if specific["key_id"] or specific["private_key_path"] or specific["private_key_pem"]:
        return specific
    return dict(_LEGACY_CREDENTIALS)


def kalshi_credentials_source(env: str) -> str:
    """Indica de dónde salen las credenciales de un entorno (para diagnóstico)."""
    specific = _env_specific_credentials(env)
    if specific["key_id"] or specific["private_key_path"] or specific["private_key_pem"]:
        return "specific"
    if _LEGACY_CREDENTIALS["key_id"] or _LEGACY_CREDENTIALS["private_key_path"]:
        return "legacy"
    return "none"


KALSHI_CREDENTIALS_BY_ENV: Dict[str, Dict[str, str]] = {
    "demo": kalshi_credentials("demo"),
    "production": kalshi_credentials("production"),
}


# Perfil activo al arranque. Un cambio de entorno en runtime reconstruye el auth
# con las credenciales del entorno destino (ver kalshi_env).
_ACTIVE_CREDS = kalshi_credentials(_INITIAL_ENV)
KALSHI_KEY_ID = _ACTIVE_CREDS["key_id"]
KALSHI_PRIVATE_KEY_PATH = _ACTIVE_CREDS["private_key_path"]
KALSHI_PRIVATE_KEY_PEM = _ACTIVE_CREDS["private_key_pem"]
KALSHI_WEBSOCKET_ENABLED = os.getenv("KALSHI_WEBSOCKET_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

# Endpoint usado para detectar en que entorno valen las credenciales.
# Se usa /portfolio/positions y no /portfolio/balance porque en demo el endpoint de
# balance devuelve HTTP 500 de forma intermitente (~30% de los intentos medidos),
# lo que hacia que `auto` descartara demo y aterrizara en produccion.
KALSHI_ENV_PROBE_PATH = os.getenv("KALSHI_ENV_PROBE_PATH", "/portfolio/positions")
KALSHI_ENV_PROBE_ATTEMPTS = int(os.getenv("KALSHI_ENV_PROBE_ATTEMPTS", "3"))

# Nombres legacy para mantener compatibilidad con módulos existentes.
GAMMA_API = KALSHI_API
CLOB_API = KALSHI_API
DATA_API = KALSHI_API
WS_MARKET = KALSHI_WS

# Polling intervals (segundos)
GAMMA_POLL_INTERVAL = int(os.getenv("MARKET_POLL_INTERVAL", "30"))
CLOB_POLL_INTERVAL = int(os.getenv("ORDERBOOK_POLL_INTERVAL", "5"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))

# Filtros de mercado. En demo hay muchos mercados con poco book; defaults más bajos.
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "0"))
MIN_VOLUME_24H = int(os.getenv("MIN_VOLUME_24H", "0"))
MAX_MARKETS_WS = int(os.getenv("MAX_MARKETS_WS", "100"))
MAX_MARKETS_TRACKED = int(os.getenv("MAX_MARKETS_TRACKED", "500"))
MAX_ORDERBOOK_POLLS_PER_CYCLE = int(os.getenv("MAX_ORDERBOOK_POLLS_PER_CYCLE", "50"))
MAX_EVENTS_TRACKED = int(os.getenv("MAX_EVENTS_TRACKED", "4000"))
# Universo escaneado por ciclo antes de rankear por liquidez.
# Solo ~14% de los mercados abiertos de Kalshi registran volumen, asi que
# tomar las primeras paginas dejaria fuera a los mercados operables.
MAX_MARKETS_SCANNED = int(os.getenv("MAX_MARKETS_SCANNED", "1600"))
MARKET_MIN_VOLUME_24H = float(os.getenv("MARKET_MIN_VOLUME_24H", "0"))
MARKET_MIN_OPEN_INTEREST = float(os.getenv("MARKET_MIN_OPEN_INTEREST", "1"))
MARKET_MIN_VOLUME_24H = float(os.getenv("MARKET_MIN_VOLUME_24H", "1"))
# Tope duro de mercados tras anadir los hermanos de evento (S05/S21 necesitan
# ver todos los hijos de un evento mutuamente excluyente, no solo el liquido).
MAX_MARKETS_WITH_EVENT_CONTEXT = int(os.getenv("MAX_MARKETS_WITH_EVENT_CONTEXT", "900"))
EVENT_POLL_INTERVAL = int(os.getenv("EVENT_POLL_INTERVAL", "300"))
PRUNE_STALE_AFTER_SECONDS = int(os.getenv("PRUNE_STALE_AFTER_SECONDS", "1800"))

# ========== Control de rate limits (Kalshi usa tokens) ==========
# Tier basic: read 200 tokens/s (bucket 600). Coste por defecto 10 tokens/request.
KALSHI_READ_TOKENS_PER_SECOND = int(os.getenv("KALSHI_READ_TOKENS_PER_SECOND", "200"))
KALSHI_REQUEST_TOKEN_COST = int(os.getenv("KALSHI_REQUEST_TOKEN_COST", "10"))
# Kalshi factura por API key (no por proceso), asi que se reserva margen para
# el supervisor, los tests y el polling de orderbooks simultaneo.
KALSHI_RATE_HEADROOM = float(os.getenv("KALSHI_RATE_HEADROOM", "0.35"))

# Kalshi pausa el trading los jueves 03:00-05:00 ET (mantenimiento programado).
MAINTENANCE_DOW = int(os.getenv("MAINTENANCE_DOW", "3"))   # 0=lunes ... 3=jueves
MAINTENANCE_START_HOUR_ET = int(os.getenv("MAINTENANCE_START_HOUR_ET", "3"))
MAINTENANCE_END_HOUR_ET = int(os.getenv("MAINTENANCE_END_HOUR_ET", "5"))

# ========== Paper Trading: realismo de ejecución y límite de capital ==========
# La equity simulada debe coincidir con el bankroll real que se piensa fondear:
# si no, cada decisión de sizing y Kelly cambia al pasar a live.
PAPER_INITIAL_BALANCE = float(os.getenv("PAPER_INITIAL_BALANCE", "1000"))
PAPER_ENFORCE_CAPITAL = True
PAPER_MAX_EXPOSURE_USD = float(os.getenv("PAPER_MAX_EXPOSURE_USD", str(PAPER_INITIAL_BALANCE)))
PAPER_MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "12"))
PAPER_RESEARCH_BUDGET_PER_STRATEGY = 2000.0
PAPER_RESEARCH_MAX_OPEN_PER_STRATEGY = 25

# ========== Modelo de comisiones de Kalshi ==========
# Kalshi cobra al taker: fee = ceil_a_centavo(rate * contratos * P * (1-P)).
# El redondeo a centavo es demoledor en contratos baratos, porque la comision no
# puede bajar de 1 centavo: a 0.03 supone el 33% de la apuesta y sube el breakeven
# de 3.0% a 4.0%. Los maker no pagan comision (pero asumen riesgo de cola).
PAPER_FEE_TAKER_RATE = float(os.getenv("PAPER_FEE_TAKER_RATE", "0.07"))
PAPER_FEE_MAKER_RATE = float(os.getenv("PAPER_FEE_MAKER_RATE", "0.0"))

# Compatibilidad: PAPER_FEE_RATE se usaba como fraccion fija del nocional. Se
# mantiene el nombre pero ya no se aplica de forma plana (ver paper_tracker).
PAPER_FEE_RATE = PAPER_FEE_TAKER_RATE

# Precisión con la que Kalshi redondea la comisión.
# Kalshi redondea **hacia arriba** y lo hace sobre la comisión ACUMULADA del
# pedido, no sobre cada fill parcial: dos fills de la misma orden comparten un
# único redondeo. Eso importa en contratos baratos, donde el `ceil` es casi todo
# la comisión.
#
# La precisión depende del tipo de cuenta (miembro directo que compensa contra el
# exchange vs FCM/retail), así que es configurable y la mide
# `demo_order_probe.py --calibrate`:
#   "cent"  -> $0.01   (conservador; valor por defecto hasta tener medición)
#   "micro" -> $0.0001 (miembro directo, según el fee schedule de Kalshi)
#
# El default es "cent" a propósito: si nos equivocamos, el simulador cobra de
# MÁS y deja fuera estrategias que en real habrían sobrevivido. Al revés sería un
# simulador optimista, que es justo el error que este proyecto quiere erradicar.
FEE_ROUNDING = (os.getenv("FEE_ROUNDING", "").strip().lower() or "cent")
if FEE_ROUNDING not in {"cent", "micro"}:
    FEE_ROUNDING = "cent"
FEE_ROUNDING_STEPS: Dict[str, float] = {"cent": 0.01, "micro": 0.0001}
FEE_ROUNDING_STEP = FEE_ROUNDING_STEPS[FEE_ROUNDING]

# Comisiones MEDIDAS en demo. Si este fichero existe, manda sobre la spec
# documentada: el simulador se calibra con datos medidos, no con estimaciones.
FEE_CALIBRATION_FILE = Path(
    os.getenv("FEE_CALIBRATION_FILE", str(BASE_DIR / "fee_calibration.json"))
)

# ========== Modelo de ejecucion (fills) ==========
# "tick"   -> fill realista: precio ajustado al tick legal, tope por profundidad,
#             comision por tramo (maker/taker), latencia y min_order_size.
# "legacy" -> comportamiento anterior (slippage en bps, comision plana). Solo para
#             comparar; no usar para decidir que estrategia pasa a live.
PAPER_FILL_MODEL = os.getenv("PAPER_FILL_MODEL", "tick").strip().lower()
# Latencia total asumida entre el snapshot de la senal y la orden en el libro.
# Medida en produccion: ~572 ms de latencia de API; se suma margen de decision.
PAPER_LATENCY_MS = int(os.getenv("PAPER_LATENCY_MS", "900"))
# Un maker solo llena si el libro cruza su nivel. Probabilidad de fill pasivo por
# ciclo de evaluacion (el resto queda pendiente o se cancela).
PAPER_MAKER_FILL_PROBABILITY = float(os.getenv("PAPER_MAKER_FILL_PROBABILITY", "0.35"))
PAPER_MAKER_MAX_PENDING_CYCLES = int(os.getenv("PAPER_MAKER_MAX_PENDING_CYCLES", "6"))
# Slippage de entrada en bps: se conserva solo para el modelo legacy.
PAPER_SLIPPAGE_BPS = int(os.getenv("PAPER_SLIPPAGE_BPS", "5"))
# Multiplicador de seguridad sobre la profundidad visible del libro: no se asume
# que todo el tamano mostrado sea ejecutable en nuestra direccion.
PAPER_DEPTH_SAFETY = float(os.getenv("PAPER_DEPTH_SAFETY", "0.5"))

# Muestra minima de trades cerrados antes de mezclar el win rate empirico con la
# confianza de la senal. Con 5 trades el win rate es ruido, y mezclarlo inflaba
# el sizing justo cuando no habia evidencia.
PAPER_MIN_TRADES_FOR_EMPIRICAL_WR = int(os.getenv("PAPER_MIN_TRADES_FOR_EMPIRICAL_WR", "20"))

# Filtro de plausibilidad de senales. Evita floods de contratos sub-centavo
# (p.ej. brackets de temperatura a 0.5c) donde un modelo simple reporta edges
# irreales, y limita el R:R mostrado a rangos operables.
MIN_SIGNAL_ENTRY_PRICE = float(os.getenv("MIN_SIGNAL_ENTRY_PRICE", "0.02"))

# ========== Admision de operaciones en Paper Trading ==========
# Kalshi publica liquidity_dollars en 0.00 en la mayoria de mercados, por lo que
# la liquidez se evalua con el mejor indicador disponible (volumen u open interest)
# en lugar de depender de un unico campo pensado para Polymarket.
PAPER_MIN_LIQUIDITY_USD = float(os.getenv("PAPER_MIN_LIQUIDITY_USD", "500"))
PAPER_MIN_VOLUME_24H_USD = float(os.getenv("PAPER_MIN_VOLUME_24H_USD", "250"))
PAPER_MIN_OPEN_INTEREST = float(os.getenv("PAPER_MIN_OPEN_INTEREST", "100"))
PAPER_MIN_CONFIDENCE = float(os.getenv("PAPER_MIN_CONFIDENCE", "75"))
# En Kalshi la mayoria de mercados con volumen real resuelven a mas de 48h;
# exigir horizonte flash dejaba al bot practicamente inactivo.
PAPER_ALLOW_NON_FLASH = os.getenv("PAPER_ALLOW_NON_FLASH", "1") not in ("0", "false", "False")

# ========== Gobernador de riesgo ==========
# Una estrategia que pierde se pausa; 24h era demasiado tiempo y dejaba al bot# sin parte de sus estrategias durante un dia completo.
GOVERNOR_PAUSE_HOURS = float(os.getenv("GOVERNOR_PAUSE_HOURS", "3"))
MAX_SIGNAL_RISK_REWARD = float(os.getenv("MAX_SIGNAL_RISK_REWARD", "12.0"))

# Estrategias que dependen de datos específicos de Polymarket quedan desactivadas por defecto.
KALSHI_ENABLE_WHALE_TRACKING = os.getenv("KALSHI_ENABLE_WHALE_TRACKING", "false").strip().lower() in {"1", "true", "yes", "on"}
KALSHI_ENABLE_POLYGON_HEALTH = os.getenv("KALSHI_ENABLE_POLYGON_HEALTH", "false").strip().lower() in {"1", "true", "yes", "on"}

# Parámetros de estrategias
STRATEGY_PARAMS = {
    "market_making": {
        "min_spread_bps": 50,
        "min_depth_usd": 1,
        "min_volume_24h": 0,
    },
    "bundle_arbitrage": {
        "min_inefficiency": 0.005,
    },
    "mean_reversion": {
        "min_liquidity": 0,
        "min_volume_24h": 0,
        "z_score_threshold": 2.0,
        "price_velocity_threshold": 0.05,
        "gamma_1d_change_threshold": 0.02,
        "gamma_1w_change_threshold": 0.04,
        "reversion_fraction": 0.50,
        "min_history_points": 5,
    },
    "favorite_longshot": {
        "favorite_threshold": 0.85,
        "longshot_threshold": 0.15,
        "min_volume_24h": 0,
    },
    "external_data": {
        "max_lag_seconds": 120,
    },
    "whale_tracking": {
        "enabled": KALSHI_ENABLE_WHALE_TRACKING,
        "min_whale_position": 5000,
        "min_whale_winrate": 0.60,
        "min_recent_trade_size": 0,
        "recent_trade_window_seconds": 1800,
        "trades_poll_limit": 250,
        "positions_lookup_limit": 500,
        "holders_poll_limit": 10,
        "holders_market_scan_limit": 30,
        "holders_wallet_enrich_limit_per_cycle": 80,
    }
}

# Categorías
CATEGORIES = {
    "politics": "Politics",
    "sports": "Sports",
    "weather": "Weather",
    "economics": "Economics",
    "crypto": "Crypto",
    "culture": "Culture",
    "science": "Science",
    "other": "Other",
}

# Categorias oficiales de Kalshi (campo `category` de GET /events) mapeadas a
# las categorias del dashboard. Es la fuente primaria; CATEGORY_KEYWORDS queda
# como respaldo cuando el evento no trae categoria.
KALSHI_CATEGORY_MAP = {
    "elections": "Politics",
    "politics": "Politics",
    "world": "Politics",
    "social": "Culture",
    "sports": "Sports",
    "economics": "Economics",
    "financials": "Economics",
    "business": "Economics",
    "companies": "Economics",
    "commodities": "Economics",
    "crypto": "Crypto",
    "entertainment": "Culture",
    "culture": "Culture",
    "science and technology": "Science",
    "science & technology": "Science",
    "ai": "Science",
    "health": "Science",
    "climate and weather": "Weather",
    "weather": "Weather",
    "climate": "Weather",
}

CATEGORY_KEYWORDS = {
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "doge",
        "dogecoin", "litecoin", "crypto", "cryptocurrency", "blockchain", "token",
        "stablecoin", "usdc", "usdt", "binance", "coinbase", "satoshi", "defi",
        "spot etf", "etf crypto",
        # Series/tickers crypto de Kalshi
        "kxbtc", "kxeth", "kxsol", "kxxrp", "kxdoge", "kxada", "kxcrypto",
        "kxbTCD", "kxbnb", "kxltc", "kxlink", "kxavax",
    ],
    "weather": [
        "weather", "temperature", "rain", "snow", "hurricane", "storm", "tornado",
        "flood", "heat", "cold", "wind", "noaa", "climate", "temperatura", "lluvia",
        "huracán", "tormenta", "clima", "snowfall", "blizzard",
        # Series/tickers de clima de Kalshi (p.ej. KXHIGHNY, KXLOWTCHI)
        "kxhigh", "kxlow", "kxtemp", "kxrain", "kxsnow", "kxhur", "kxstorm", "kxweather",
    ],
    "politics": [
        "trump", "biden", "president", "election", "senate", "congress", "governor",
        "mayor", "democrat", "democrats", "republican", "republicans", "nomination", "primary", "cabinet", "minister",
        "parliament", "vote", "poll", "politics", "putin", "zelensky", "israel", "gaza",
        "ukraine", "russia", "china", "tariff", "government", "supreme court",
        "nato", "donbas", "snowden", "presidential", "democratic", "republican",
        "governor race", "presidential election", "election", "pope", "white house",
        # Series/tickers politicos de Kalshi
        "kxpres", "kxtrump", "kxbiden", "kxsena", "kxhouse", "kxgovparty", "kxelect",
        "kxscotus", "kxnominee", "kxpardons", "kxapproval", "kxgeopolitics", "kxukraine",
        "kxisrael", "kxiran", "kxchina", "kxputin",
    ],
    "sports": [
        "nba", "nfl", "mlb", "nhl", "ufc", "fifa", "soccer", "football", "tennis",
        "golf", "formula 1", "f1", "champions league", "world cup", "super bowl",
        "baseball", "basketball", "hockey", "olympics", "ballon d'or", "premier league",
        "laliga", "liga", "serie a", "wimbledon", "us open", "ncaa", "game", "touchdown",
        # Series/tickers de deportes de Kalshi (KX = Kalshi Exchange)
        "kxmlb", "kxnfl", "kxnba", "kxnhl", "kxncaa", "kxufc", "kxsoccer", "kxtennis",
        "kxgolf", "kxf1", "kxnascar", "kxepl", "kxucl", "kxwnba", "kxmls", "kxsport",
        "kxtte", "kxcs2", "kxlol", "kxdota", "kxvalorant", "kxboxing", "kxcricket",
        "kxatp", "kxwta", "kxligamx", "kxnky", "kxnpb", "kxkbo", "kxmls", "kxsrl",
        "kxbundes", "kxefl", "kxslb", "kxacb", "kxeuro", "kxconcacaf", "kxcopa",
        # Estadisticas tipicas de props deportivos
        "rbi", "home run", "homer", "strikeout", "strike out", "earned run", "hit",
        "passing yards", "rushing yards", "receiving yards", "touchdown", "field goal",
        "rebound", "assist", "points", "three pointer", "double double", "saves",
        "goals", "goal scorer", "shots on goal", "ace", "birdie", "round", "innings",
        "match", "series", "playoff", "vs", "winner of", "beat", "defeat",
    ],
    "economics": [
        "fed", "federal reserve", "rate cut", "interest rate", "inflation", "cpi", "ppi",
        "jobs report", "unemployment", "gdp", "recession", "s&p", "sp500", "nasdaq",
        "dow", "oil", "gold", "silver", "treasury", "bond", "yield", "stock market",
        "economy", "tariff", "fomc", "macro",
        # Series/tickers economicos de Kalshi
        "kxfed", "kxrate", "kxinflation", "kxcpi", "kxppi", "kxgdp", "kxpayroll",
        "kxunrate", "kxjobs", "kxrecssnber", "kxinx", "kxnasdaq100", "kxecon",
        "kxmortgage", "kxgas", "kxoil", "kxgold", "kxsilver",
    ],
    "culture": [
        "oscar", "oscars", "grammy", "emmy", "movie", "film", "box office", "album",
        "song", "music", "celebrity", "taylor swift", "netflix", "disney", "youtube",
        "tiktok", "x/twitter", "twitter", "met gala", "eurovision", "festival",
        "george r. r. martin", "winds of winter", "game of thrones", "book",
        # Series/tickers de cultura y entretenimiento de Kalshi
        "kxoscar", "kxgrammy", "kxemmy", "kxmovie", "kxboxoffice", "kxalbum",
        "kxsong", "kxmusic", "kxrotten", "kxcelebrity", "kxtimepoty",
    ],
    "science": [
        "spacex", "nasa", "starship", "rocket", "launch", "moon", "mars", "space",
        "ai", "artificial intelligence", "openai", "anthropic", "google deepmind",
        "covid", "vaccine", "fda", "science", "research", "clinical trial",
        # Series/tickers de ciencia y tecnologia de Kalshi
        "kxspacex", "kxnasa", "kxmoon", "kxmars", "kxartemis", "kxopenai", "kxai",
        "kxllm", "kxagi", "kxtech",
    ],
}
