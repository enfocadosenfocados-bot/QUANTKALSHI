# QUANT KALSHI

Version adaptada de QUANT POLYMARKET que consume datos de **Kalshi** (Trade API v2).
El bot arranca en modo **PAPER**: no envia ordenes reales salvo activacion explicita.

## Diferencias clave frente a Polymarket

| Tema | Polymarket | Kalshi |
|------|-----------|--------|
| Resolucion | UMA Optimistic Oracle (propuesta + disputa + voto UMA) | Kalshi determina y liquida con reglas del contrato y fuentes oficiales |
| Identificador | `conditionId` + `clobTokenIds` | `ticker` y `event_ticker` |
| Orderbook | bids y asks explicitos | solo bids de YES y NO; las asks se derivan del lado opuesto |
| Precio YES ask | directo | `1 - best NO bid` |
| Precio NO ask | directo | `1 - best YES bid` |
| Datos publicos | REST sin auth | REST sin auth |
| WebSocket | sin auth para mercado | **requiere auth** aunque el canal sea publico |
| Wallets / holders | Data API publica | no existe feed publico equivalente |
| Estados | active/closed/resolved | initialized, active, inactive, closed, determined, disputed, amended, finalized |

### Sobre la resolucion (no hay UMA)

En Kalshi cada mercado define sus reglas y sus `settlement_sources`. Kalshi cierra,
determina (`result` = yes/no/scalar) y liquida automaticamente. En la API se ve el
ciclo `closed -> determined -> finalized` y `settlement_timer_seconds` marca la
ventana en la que un resultado aun puede disputarse. La estrategia S20
(antes "descuento UMA") ahora opera como **descuento post-cierre / retraso de
fuente oficial**, no como arbitraje del oraculo UMA.

## Instalacion

```bat
git clone https://github.com/enfocadosenfocados-bot/QUANTKALSHI.git
cd QUANTKALSHI
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

En Windows tambien puedes usar `start.bat` (supervisor 24/7). En **Linux/VPS**
el despliegue completo es un solo comando: `bash deploy/install_vps.sh`
(ver la seccion siguiente).

- Dashboard: http://localhost:8000/dashboard
- API REST: http://localhost:8000

## Despliegue 24/7 en VPS (Linux)

Repo: <https://github.com/enfocadosenfocados-bot/QUANTKALSHI>

El repo trae todo lo necesario para dejarlo corriendo y olvidarse: servicio
**systemd** con `Restart=always`, healthcheck contra `/api/health`, instalador
idempotente, script de actualizacion y `.env` de ejemplo para Linux.

### Instalacion en 4 comandos

```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/enfocadosenfocados-bot/QUANTKALSHI.git ~/quantkalshi
cd ~/quantkalshi
bash deploy/install_vps.sh
```

El repositorio es **publico**, asi que ese `git clone` no pide credenciales. Si en algun
momento vuelve a ser privado, autentica antes (`gh auth login`) o clona con un token:
`git clone https://<TOKEN>@github.com/enfocadosenfocados-bot/QUANTKALSHI.git`.

`deploy/install_vps.sh` es idempotente (repetirlo repara/reinstala) y hace:

1. Exige **Python 3.11+** — el codigo usa `datetime.UTC`. Ubuntu 24.04 y Debian 12
   ya lo traen; en 22.04 el script te dice como instalar `python3.12` (deadsnakes).
2. Crea `venv/` e instala `requirements.txt`.
3. Ejecuta la suite de tests: si algo esta en rojo **no** arranca el servicio
   (`SKIP_TESTS=1` para saltartelo conscientemente).
4. Crea `.env` desde `deploy/env.vps.example` si falta, con permisos `600`.
5. Instala `/etc/systemd/system/quantkalshi.service`, lo habilita en el arranque y
   lo levanta.
6. Espera a que responda `/api/health` y resume estado + comandos utiles.

### Credenciales en la VPS

```bash
mkdir -p ~/.kalshi && chmod 700 ~/.kalshi
nano ~/.kalshi/prod_key.pem        # pega la clave privada (RSA PKCS#1 o Ed25519 PKCS#8)
chmod 600 ~/.kalshi/prod_key.pem
nano ~/quantkalshi/.env            # KALSHI_PROD_KEY_ID=... y KALSHI_PROD_PRIVATE_KEY_PATH=/home/TU_USUARIO/.kalshi/prod_key.pem
sudo systemctl restart quantkalshi
```

Kalshi **no comparte API keys entre entornos**: una key de demo solo autentica en
demo y viceversa. Sin credenciales el bot arranca igual, pero solo con datos
publicos (no puede operar ni leer tu portfolio).


### Acceso al dashboard

El puerto 8000 **no** se abre a internet: systemd lo sirve en la VPS y entras por
tunel SSH.

```bash
ssh -N -L 8000:127.0.0.1:8000 usuario@TU_VPS
# y en tu PC:  http://localhost:8000/dashboard
```

### Pasar a dinero real: dos actos explicitos

`KALSHI_ENV=production` es el plano de **datos** (los ~2.826 mercados con volumen
viven ahi, por eso es el entorno normal del bot). Colocar ordenes con dinero real
es un acto **distinto**, y el interlock exige las cuatro cosas a la vez:

1. `KALSHI_ENV=production` en `.env`
2. `ALLOW_REAL_MONEY=true` en `.env` (por defecto `false`)
3. modo `LIVE` en el dashboard, kill-switch desactivado y `confirm_live_order=true`
   en la senal
4. que el tablero de promocion marque la estrategia como `LIVE_PEQUENO` o `LIVE`
   (`GET /api/promotion/board`). Con `LIVE_REQUIRE_PROMOTION=false` se puede saltar,
   y por eso viene **activado** por defecto: sin la muestra que da potencia
   estadistica, una ganadora de paper es indistinguible del azar.

Y estos limites se comprueban **en el envio**, no solo en el informe:

| Variable | Por defecto | Que evita |
| --- | --- | --- |
| `LIVE_ABS_MAX_ORDER_USD` | 50 | Un bug de sizing; no se puede subir desde el dashboard |
| `MAX_OPEN_LIVE_TRADES` | 5 | N senales seguidas dejando N ordenes vivas a la vez |
| `LIVE_ORDER_MAX_AGE_SEC` | 900 | Ordenes `good_till_canceled` que no caducan solas: la reconciliacion las cancela |

Cada orden se envia con un `client_order_id` derivado de la senal, de modo que un
reintento (timeout, reinicio, senal repetida) es un rechazo por duplicado y no una
segunda posicion. Lo enviado queda en `live_orders.json` (`GET /api/live/orders`) y
`POST /api/live/reconcile` cuadra ese registro con el exchange y cancela lo que
quedo vivo; el bot tambien lo hace solo cada 2 minutos.

Ruta recomendada: dejar `ALLOW_REAL_MONEY=false` unos dias, ver el PnL del paper
trading en la VPS, validar el cableado de ordenes contra el exchange **demo**
(`bash deploy/healthcheck.sh`, o `python demo_order_probe.py --place-order` desde
la VPS) y solo despues activar produccion con dinero.

### Operacion diaria

```bash
journalctl -u quantkalshi -f        # logs en vivo (systemd los rota solo)
systemctl status quantkalshi        # estado + ultimas lineas
systemctl restart quantkalshi       # reiniciar
systemctl stop quantkalshi          # parar (kill-switch de emergencia: para el proceso)
bash deploy/healthcheck.sh          # comprobar /api/health
bash deploy/update_vps.sh           # git pull + deps + tests + reinicio
bash deploy/uninstall_vps.sh        # quitar el servicio (no borra el checkout)
```

### Alternativa con Docker

Si prefieres contenedor en vez de systemd:

```bash
cp deploy/env.vps.example .env     # rellena credenciales
docker compose up -d --build
docker compose logs -f
docker compose down
```

`docker-compose.yml` monta el checkout completo en `/app` (el bot guarda su estado
en JSON junto al codigo) y publica el puerto solo en `127.0.0.1`, con
`restart: unless-stopped` y healthcheck propio.

### Migrar el estado de paper trading (opcional)

Los JSON de estado estan en `.gitignore` porque cambian en cada ciclo, asi que una
clonacion nueva arranca con el historial vacio (el bot los crea solo). Para
continuar en la VPS con el historial de tu PC:

```bash
scp paper_trades.json paper_trades_research.json trade_memory.json \
    quant_ml_state.json strategy_governor.json fee_calibration.json \
    usuario@TU_VPS:~/quantkalshi/
sudo systemctl restart quantkalshi
```

### Tests

```bash
python -m unittest discover -p 'test_*.py'   # suite completa
python -m unittest test_live_interlock -v    # interlock de dinero real
python -m unittest test_live_wiring -v       # cableado y candados del envio live
python -m unittest test_fee_calibration -v   # modelo de comisiones
```


## Configuracion de credenciales Kalshi

Kalshi usa **API key asimetrica** (Key ID + private key PEM), no un secret compartido.
Ademas, **las credenciales no se comparten entre demo y produccion**: una key creada
en produccion devuelve `401 authentication_error` contra demo y viceversa.

Crea el archivo `.env` en la raiz del proyecto (esta en `.gitignore`):

```env
# auto | demo | production
KALSHI_ENV=auto
KALSHI_KEY_ID=tu_key_id
KALSHI_PRIVATE_KEY_PATH=C:\Users\TU_USUARIO\.kalshi\demo_private_key.pem
KALSHI_WEBSOCKET_ENABLED=true
MIN_LIQUIDITY=0
MIN_VOLUME_24H=0
MAX_MARKETS_TRACKED=500
MAX_ORDERBOOK_POLLS_PER_CYCLE=50
```

Con `KALSHI_ENV=auto` el arranque prueba el endpoint de balance en demo y en
produccion y usa el entorno donde tus credenciales son validas. Veras algo como:

```text
[STARTUP] Entorno Kalshi activo: production
[STARTUP] credenciales validas en production
[STARTUP] REST: https://external-api.kalshi.com/trade-api/v2
[STARTUP] WebSocket auth: si | modo: PAPER
```

Nunca subas `.env`, `*.pem`, `*.key` ni `kalshi_credentials.json`.
## Operacion 24/7

El bot queda corriendo en segundo plano con reinicio automatico. El guardian es
`run_forever.ps1`, que relanza `python main.py` si el proceso muere o si
`/api/health` deja de responder, con backoff exponencial y rotacion de logs.

```powershell
# Arrancar (o rearrancar) el bot en segundo plano
powershell -ExecutionPolicy Bypass -File .\run_forever.ps1

# Ver el estado de salud
Invoke-RestMethod http://localhost:8000/api/health | ConvertTo-Json

# Ver los logs en vivo
Get-Content .\logs\bot_*.log -Tail 50 -Wait

# Detenerlo
powershell -ExecutionPolicy Bypass -File .\stop_bot.ps1
```

Arranque automatico al iniciar Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

El instalador intenta primero registrar una **tarea programada** (se reinicia
sola si falla; requiere administrador). Si no hay permisos de administrador, cae
automaticamente a un **lanzador oculto en la carpeta de Inicio**, que arranca el
supervisor al iniciar sesion. Para revertirlo:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Uninstall
```

### Dashboard

`http://localhost:8000/dashboard` se actualiza por WebSocket y muestra PnL,
equity, win rate y posiciones abiertas del Paper Trading, las senales activas por
estrategia (entrada, objetivo, stop, Kelly), los paneles por estrategia (A-F,
S02-S24) y el estado del agente IA, el bandido contextual, VPIN y lead-lag.

### Nota sobre demo y produccion

Kalshi **no comparte API keys entre demo y produccion**: las claves de produccion
devuelven 401 contra `demo-api.kalshi.co`. Con `KALSHI_ENV=auto` el bot detecta el
entorno donde tus credenciales son validas y mantiene `mode=PAPER`, es decir, no
envia ordenes reales: todo el trading es simulado sobre datos de mercado reales.
Para operar realmente contra demo necesitas una key creada en la web de demo.

### Seleccion del universo de mercados

Solo ~14% de los mercados abiertos de Kalshi registran volumen, por lo que el bot
escanea `MAX_MARKETS_SCANNED` mercados por ciclo y se queda con los
`MAX_MARKETS_TRACKED` mas operables (volumen 24h, open interest y liquidez). Los
mercados con posiciones abiertas se conservan siempre, y se anaden los hermanos
de evento para que S05 (canasta) y S21 (escalera de umbrales) puedan razonar.



## Realismo del paper trading

El simulador anterior regalaba dinero de tres formas: comision cero, slippage en
puntos basicos y fills instantaneos al precio de la senal. Cualquier ganadora
nacida asi muere al operar en real, por eso `execution_model.py` aplica lo que
Kalshi cobra y lo que el libro permite:

- **Comision**: `ceil_a_centavo(0.07 x contratos x P x (1-P))`, la formula
  publicada por Kalshi. En contratos baratos la comision relativa es enorme (mas
  del 30% del nocional a $0.03), asi que las estrategias de centavos dejan de
  parecer rentables.
- **Tick**: los precios se redondean al tick antes de cruzar.
- **Taker**: el fill consume profundidad real del libro, asi que el precio medio
  suele ser peor que la mejor punta. Se ve en los cierres: "cierre completado a
  0.7900 por profundidad insuficiente" cuando el ask estaba en 0.780.
- **Maker**: el fill es probabilistico (`PAPER_MAKER_FILL_PROBABILITY`), no
  garantizado. Un precio pasivo ya no es una venta segura.
- **RFM inyectable**: los tests fijan la semilla, asi que los resultados son
  reproducibles.

Los dos costes de salida se pueden medir en el propio registro: en los 9 cierres del
baseline nuevo, 8 completaron por debajo del libro visible (`exit_reference_price` vs
`exit_price`, entre 0.04 y 0.97 ticks adversos, media 0.63) y la comision de salida
sumo el 4.5% del nocional cerrado. Lo paga tambien una operacion ganadora, asi que la
vara es la misma para todos; conviene saberlo antes de leer un win rate, porque medio
tick de profundidad mas la comision se comen buena parte del edge declarado.

Las metricas tambien dejaron de mentir: Sharpe devuelve `None` por debajo de 10
trades cerrados, el Kelly con win rate empirico espera a 20 trades y el bandido
contextual premia por PnL normalizado en vez de por acierto.

### Base de medida: entrada, pico y salida

Una operacion no se mide si el simulador se mide a si mismo. El harness fija cuatro
invariantes en `paper_tracker.py` y **descarta** la operacion cuando no se cumplen,
en vez de registrarla con una metrica imposible:

- **El riesgo se ancla al fill, no a la senal.** Si el libro se movio entre la senal
  y la orden, stop y objetivo se reescalan al precio real de ejecucion; si el desvio
  supera `PAPER_ENTRY_DRIFT_PCT` / `PAPER_ENTRY_DRIFT_MIN_ABS`, la operacion se
  descarta porque la tesis con la que se genero ya no describe ese mercado.
- **El stop y el objetivo tienen que ser alcanzables desde el precio al que se SALE**
  (bid para un largo, ask para un corto). Un stop ya cruzado respecto a esa base
  cierra la posicion en el primer ciclo a un precio peor: una perdida que el mercado
  nunca dio y que despues mueve el circuit breaker del gobernador.
- **El spread es una puerta.** El limite es el mayor de `PAPER_MAX_SPREAD_PCT` (5%
  del precio de ejecucion) y `PAPER_MAX_SPREAD_MIN_ABS` (3 centavos, porque un tick
  de Kalshi ya es 0.01). Un libro 0.47/0.85 sobre una entrada de 0.8756 (43% de
  spread) no es operable: el objetivo medido desde el ask es inalcanzable y el stop
  nace por encima del bid.
- **El pico se sigue en la base de salida**, no en el precio medio. Con el mid, un
  libro ancho marcaba un +8.5% inexistente sobre la entrada, activaba el break-even
  y dejaba el trailing stop por encima de la entrada: cierre inmediato etiquetado
  "Stop Loss" con PnL negativo. El PnL flotante tambien se valora ahi.

Medido sobre el registro real del harness corregido: de 28 cierres, 18 duraron menos
de 0.7 s por un stop cruzado al abrir. Eso medía al harness, no a las estrategias.
Con el anclaje a la base de salida ya activo, los 9 cierres del baseline nuevo
duraron entre 13.5 s y 113 s (media 62 s), con 0 por stop cruzado al abrir y 0 con
spread fuera de puerta.

### Puerta de promocion PAPER -> LIVE

`strategy_promotion.py` decide que estrategia merece capital real. Ninguna pasa
solo por tener buen PnL: hace falta significancia con **correccion de Bonferroni**
por comparaciones multiples (con 16 estrategias se exige p < 0.00313) y confirmar
en un **holdout temporal** (el 30% de trades mas reciente, que no se usa para
decidir).

| Estado | Significado |
|--------|-------------|
| `SOSPECHOSA` | Metricas imposibles: el registro no describe una operacion real |
| `LIVE_PEQUENO` | Significativa y con holdout limpio: probar con $1-5 por operacion |
| `LIVE` | Ya operando con capital real |
| `SOMBRA` | Significativa pero el holdout no acompana |
| `CANDIDATA` | Opera pero sin muestra suficiente para opinar |
| `SIN_DATOS` | No ha cerrado ninguna operacion |
| `DESCARTADA` | Significativamente peor que el breakeven implicito |
| `PAUSADA` | El gobernador la paro por drawdown |

`SOSPECHOSA` es el estado mas util en la practica: un 100% de acierto o un PnL
negativo con todas las operaciones ganadas son imposibles en mercados binarios, y
significan que el registro esta mal, no que la estrategia sea buena. Se detectan
antes de interpretarlos estadisticamente.

El historial anterior al modelo realista esta en `archivo_pre_realismo_*/`. No se
borro, pero se aparto: un gate que decide sobre datos fabricados solo produce
decisiones fabricadas. El panel esta en el tab **Ranking de Estrategias** del
dashboard, encima del gobernador.

### Integridad del historial

`paper_trades.json` se guarda de forma **atomica** (temporal + `os.replace`). Antes
se truncaba el archivo antes de escribir, asi que un reinicio del bot en ese
instante dejaba un JSON roto y el track record se perdia entero al arrancar. Un
archivo ilegible ahora avisa explicitamente en el log en vez de desaparecer en
silencio.

### Diagnostico de admision

Un descarte es indistinguible de "no hubo senal" mientras nadie lo cuente. El harness
escribia el motivo en la senal (spread, desvio, geometria, capital, gobernador) y la
descartaba acto seguido, asi que `execution_skipped` y `capital_skipped` no se leian
en ningun punto del bot: ni en los logs ni en el dashboard. En 23 logs reales no habia
una sola linea de descarte, y sin ese dato no se puede decidir si el bot no opera
porque no ve oportunidades o porque las rechaza todas.

`PaperTradingEngine` los cuenta en `admission_stats` y los publica en
`GET /api/track-record` (`admission` y `admission_total`), de mayor a menor:

| Clave | Significado |
|-------|-------------|
| `no_sniper:<causa>` | No pasa el filtro de conviccion: `confianza`, `horizonte`, `liquidez` o `precio` |
| `spread_excesivo` | Libro no operable (mas de `PAPER_MAX_SPREAD_PCT` / `MIN_ABS`) |
| `desvio_entrada` | El fill se alejo mas de `PAPER_ENTRY_DRIFT_PCT` / `MIN_ABS` de la senal |
| `geometria_invalida` | Stop y objetivo no son alcanzables desde la entrada real |
| `objetivo_ya_alcanzado` | El objetivo ya estaba cruzado respecto a la base de salida |
| `stop_degenerado` | El bid (largo) o el ask (corto) no admite un stop con riesgo |
| `fill_rechazado` | El libro no permitio ejecutar: minimo de tamano o sin liquidez en el limite |
| `max_positions` / `no_capital` | Cupo de posiciones o presupuesto agotado |
| `global_paused` / `paused` / `below_min_confidence` / `above_max_entry_price` | Descarte del gobernador |
| `duplicada` | La posicion ya existe: no se abre otra |

Los contadores viven en memoria (un reinicio empieza a contar de cero) y el log solo
imprime la primera aparicion de cada motivo y cada 50 repeticiones: el bucle evalua
miles de senales por ciclo y un print por descarte inundaria el log.

Cada senal se pasa a los dos motores (`paper_tracker` y `paper_tracker_research`), asi
que un mismo motivo aparece dos veces en el log con el mismo contador: son dos
contadores independientes, y el que publica `GET /api/track-record` es el del motor
realista. La primera lectura en vivo, con el freno global armado, fue
`{"global_paused": 5732}`: el 100% de los descartes era el gobernador, no el filtro de
conviccion ni el libro.


| Endpoint | Descripcion |
|----------|-------------|
| `GET /` | Estado del backend, entorno Kalshi y modo de trading |
| `GET /api/markets?limit=1000&category=Sports` | Mercados Kalshi normalizados |
| `GET /api/market/{ticker}` | Detalle interno de un mercado |
| `GET /api/signals?limit=1000` | Senales activas ordenadas |
| `GET /api/top-signals` | Senales de alta confianza con Kelly sizing |
| `GET /api/arbitrage` | Arbitraje YES/NO derivado del book |
| `GET /api/stats` | Estado del sistema |
| `GET /api/strategies` | Diagnostico por estrategia |
| `GET /api/kalshi/balance` | Prueba de autenticacion: balance de la cuenta |
| `GET /api/strategy-ranking?mode=realistic` | Ranking cuantitativo con significancia y ML |
| `GET /api/promotion/board?mode=realistic` | Puerta PAPER -> LIVE: estado, p-valor y holdout por estrategia |
| `GET /api/kalshi/environments` | Entornos Kalshi y donde son validas las credenciales |
| `POST /api/settings/environment` | Cambiar de entorno Kalshi (auto/demo/production) |
| `GET /api/settings/trading-mode` | Estado PAPER/LIVE y credenciales |
| `POST /api/settings/trading-mode` | Cambiar PAPER/LIVE |
| `POST /api/live/kill-switch` | Boton de panico |
| `GET /api/live/orders` | Ordenes reales enviadas, tope de abiertas y ultima reconciliacion |
| `POST /api/live/reconcile` | Cuadrar el registro local con Kalshi y cancelar ordenes vivas |
| `WS /ws` | WebSocket del dashboard |

## Arquitectura

```
Kalshi Trade API v2
    +-- GET /markets          -> descubrimiento (status=open, mve_filter=exclude)
    +-- GET /markets/{t}/orderbook -> bids YES/NO, asks derivadas
    +-- GET /markets/trades   -> trades publicos recientes
    +-- WSS /trade-api/ws/v2  -> ticker, trade, orderbook_delta (auth requerida)
              |
              v
    QUANT KALSHI
    +-- config.py             -> entorno, filtros, parametros
    +-- kalshi_env.py         -> autodeteccion demo/produccion
    +-- kalshi_auth.py        -> firma RSA-PSS / Ed25519
    +-- polymarket_client.py  -> cliente Kalshi (nombre legacy por compatibilidad)
    +-- market_registry.py    -> cache en memoria
    +-- strategies.py         -> motor de estrategias + adaptadores Sxx
    +-- execution_model.py    -> comision, tick, profundidad y fills realistas
    +-- paper_tracker.py      -> track record con guardado atomico
    +-- strategy_promotion.py -> puerta PAPER -> LIVE (Bonferroni + holdout)
    +-- live_execution.py     -> PAPER por defecto, ordenes Kalshi protegidas + reconciliacion
    +-- main.py               -> FastAPI + WebSocket del dashboard
```

## Seguridad

- El backend arranca en **PAPER**; `kalshi_credentials.json` no se crea hasta que
  cambies de modo explicitamente.
- `execute_order(signal, market, size_usd)` exige `mode=LIVE`, kill-switch desactivado,
  credenciales validas, `confirm_live_order=true` en la senal, `strategy_code` promovido
  por el tablero, tamano explicito y hueco bajo `MAX_OPEN_LIVE_TRADES`. Si algo falla
  devuelve `blocked_by` con el motivo (nunca un error generico) y solo un
  `dry_run_order`. La firma se comprueba en `test_live_wiring.py`: un call-site con
  menos de 3 argumentos rompe la suite, porque ese `TypeError` ya dejo el camino de
  dinero real muerto una vez y solo se veia en el log.
- Las ordenes enviadas se registran en `live_orders.json` (escritura atomica) y la
  reconciliacion las cuadra contra el exchange: lo que ya no esta vivo se marca y lo
  que sigue vivo pasado `LIVE_ORDER_MAX_AGE_SEC` se cancela.
- Whale tracking queda **desactivado por defecto** porque Kalshi no expone wallets ni
  holders publicos. Se puede reactivar con `KALSHI_ENABLE_WHALE_TRACKING=true`, pero
  no producira datos utiles mientras no exista esa fuente.
- Rota cualquier API key que se haya compartido en texto plano.

## Fuentes oficiales

- API environments: https://docs.kalshi.com/getting_started/api_environments.md
- API keys: https://docs.kalshi.com/getting_started/api_keys.md
- Market data quick start: https://docs.kalshi.com/getting_started/quick_start_market_data.md
- WebSockets: https://docs.kalshi.com/getting_started/quick_start_websockets.md
- Market lifecycle: https://docs.kalshi.com/getting_started/market_lifecycle.md
- Market settlement: https://docs.kalshi.com/getting_started/market_settlement.md
- Orderbook responses: https://docs.kalshi.com/getting_started/orderbook_responses.md
- Order direction: https://docs.kalshi.com/getting_started/order_direction.md
- Resolucion Polymarket (referencia): https://docs.polymarket.com/developers/resolution/UMA

## Licencia

MIT - Uso bajo tu propio riesgo. El trading conlleva riesgo de perdida.
