"""demo_order_probe.py — Valida el cableado de órdenes reales en Kalshi DEMO.

Este script existe porque hay una clase de sorpresa que ningún simulador puede
cubrir: "mi estrategia era buena pero mi código de órdenes falló el primer día".
Aquí se ejercita el camino real (firma, payload, colocación, cancelación, fill)
contra el exchange DEMO, que usa dinero ficticio.

Mide además la comisión REAL de un fill y la compara con la que modela
`execution_model`, para calibrar el simulador con datos medidos y no estimados.

`--calibrate` lleva la medición hasta el final: cruza a mercado en hasta 3
mercados (precios objetivo 0.05 / 0.50 / 0.90, que es donde las dos precisiones de
redondeo predicen comisiones distintas), intenta además un fill pasivo, y escribe
`fee_calibration.json`. La precisión del redondeo ($0.01 frente a $0.0001) y la
tasa del maker son exactamente los dos parámetros que `execution_model` daba por
buenos sin medir: ese fichero es lo que los sustituye.

Uso:
    python demo_order_probe.py                            # solo lectura
    python demo_order_probe.py --place-order              # coloca y cancela 1 contrato
    python demo_order_probe.py --place-order --calibrate  # + mide y calibra

Seguridad: solo opera contra el host DEMO y con `--contracts` (1 por defecto).
Nunca toca producción aunque las credenciales de producción estén configuradas.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import FEE_CALIBRATION_FILE, kalshi_credentials
from execution_model import kalshi_trading_fee
from fee_calibration import (
    FEE_ROUNDING_STEPS,
    calibrate_from_samples,
    effective_fee_config,
    make_sample,
    summarize,
    write_calibration,
)
from kalshi_auth import KalshiAuth
from kalshi_env import KALSHI_REST_BASES, mask_key_id
from live_execution import LiveExecutionManager

DEMO_ENV = "demo"

# Precios donde las dos precisiones de redondeo predicen comisiones distintas, que
# es la unica condicion para poder decidir cual usa la cuenta. Un muestreo solo a
# 0.50 no distingue nada si la comision sale multiplo exacto del centavo.
CALIBRATION_TARGETS: Tuple[float, ...] = (0.05, 0.50, 0.90)
TARGET_TOLERANCE = 0.06

# Campos de la respuesta de orden donde Kalshi publica la comision cobrada. Se leen
# en dolares; los campos homonimos en centavos de la API V1 se ignoran a proposito,
# porque confundir centavos con dolares desviaria la calibracion por 100x sin que
# nada chillara.
FEE_FIELDS: Tuple[str, ...] = ("average_fee_paid", "average_fee_paid_dollars")



def build_demo_auth() -> KalshiAuth:
    creds = kalshi_credentials(DEMO_ENV)
    return KalshiAuth(
        creds["key_id"], creds["private_key_path"], creds["private_key_pem"]
    )


def api(base: str, auth: KalshiAuth, method: str, path: str, **kwargs) -> httpx.Response:
    full = "/trade-api/v2" + path
    headers = auth.headers(method, full)
    headers["Content-Type"] = "application/json"
    return httpx.request(method, base + path, headers=headers, timeout=20.0, **kwargs)


def get_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def fetch_open_markets(base: str, pages: int = 10) -> List[Dict[str, Any]]:
    """Universo de mercados abiertos, paginando de verdad.

    Se devuelve la lista completa (y no el ganador) porque la calibración necesita
    mercados en precios concretos: quedarse con el de más volumen de la primera
    página garantiza no encontrar nunca los de 0.05 ni 0.90.
    """
    markets: List[Dict[str, Any]] = []
    seen = set()
    cursor = None
    for _ in range(pages):
        params: Dict[str, Any] = {"limit": 200, "status": "open", "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(base + "/markets", params=params, timeout=40.0)
        if resp.status_code != 200:
            break
        data = resp.json()
        for market in data.get("markets", []):
            ticker = market.get("ticker")
            if ticker in seen:
                continue
            seen.add(ticker)
            markets.append(market)
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets


def quotes(market: Dict[str, Any]) -> Tuple[float, float]:
    """`(bid, ask)` utilizables del mercado, o `(0, 0)` si no lo son."""
    try:
        bid = float(market.get("yes_bid_dollars") or 0)
        ask = float(market.get("yes_ask_dollars") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if bid <= 0 or ask <= 0 or bid >= ask:
        return 0.0, 0.0
    return bid, ask


def volume_24h(market: Dict[str, Any]) -> float:
    try:
        return float(market.get("volume_24h_fp") or 0)
    except (TypeError, ValueError):
        return 0.0


def read_fee(order: Dict[str, Any]) -> Optional[float]:
    """Comisión cobrada de una orden, en dólares, según la respuesta del exchange.

    Devuelve `None` si el campo no viene: preferimos declarar "no medido" antes que
    deducir la comisión de un campo que quizá esté en otra unidad.
    """
    for key in FEE_FIELDS:
        if order.get(key) is not None:
            try:
                return float(order[key])
            except (TypeError, ValueError):
                return None
    return None


def pick_liquid_market(base: str, auth: KalshiAuth) -> Optional[Dict[str, Any]]:
    """Mercado demo con profundidad real, para poder medir un fill.

    Va relajando el criterio: primero exige volumen 24h y un precio central, y si
    el entorno demo no lo ofrece acepta cualquier mercado con bid y ask
    utilizables. Devuelve también el criterio aplicado, para poder reportarlo.
    """
    candidates = [m for m in fetch_open_markets(base) if quotes(m)[1] > 0]
    with_volume = [
        m for m in candidates if volume_24h(m) > 0 and 0.10 < quotes(m)[0] < 0.90
    ]
    pool = with_volume or candidates
    if not pool:
        return None
    best = max(pool, key=volume_24h)
    best["_probe_criterion"] = "volumen 24h" if with_volume else "sin volumen (solo quotes)"
    return best


def pick_calibration_markets(
    base: str, targets: Tuple[float, ...] = CALIBRATION_TARGETS
) -> List[Tuple[float, Optional[Dict[str, Any]]]]:
    """Un mercado distinto por precio objetivo; `None` donde no haya ninguno.

    Se exige un ask a menos de `TARGET_TOLERANCE` del objetivo: la muestra sirve
    para decidir qué precisión de redondeo usa la cuenta, y solo discrimina si el
    precio cae donde las dos precisiones predicen comisiones distintas.
    """
    markets = fetch_open_markets(base)
    picked: List[Tuple[float, Optional[Dict[str, Any]]]] = []
    used = set()
    for target in targets:
        pool = []
        for market in markets:
            if market.get("ticker") in used:
                continue
            _, ask = quotes(market)
            if ask <= 0 or abs(ask - target) > TARGET_TOLERANCE:
                continue
            pool.append(market)
        if not pool:
            picked.append((target, None))
            continue
        with_volume = [m for m in pool if volume_24h(m) > 0]
        best = dict(max(with_volume or pool, key=volume_24h))
        best["_probe_target"] = target
        best["_probe_criterion"] = (
            "volumen 24h" if with_volume else "sin volumen (solo quotes)"
        )
        used.add(best.get("ticker"))
        picked.append((target, best))
    return picked


def cross_and_measure(
    base: str, auth: KalshiAuth, market: Dict[str, Any], contracts: int
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Cruza a mercado y devuelve la muestra de comisión, o `(None, motivo)`."""
    ticker = market.get("ticker")
    _, ask = quotes(market)
    if ask <= 0:
        return None, "sin ask utilizable"
    signal = {
        "token_id": ticker,
        "token": "Yes",
        "side": "BUY",
        "entry_price": f"{ask:.4f}",
        "recommended_order_type": "MARKET (Taker)",
    }
    payload = LiveExecutionManager.build_order_payload(signal, None, count=contracts)
    resp = api(base, auth, "POST", "/portfolio/events/orders", json=payload)
    if resp.status_code not in (200, 201):
        return None, f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
    body = get_json(resp) or {}
    order = body.get("order") or body
    fill_count = float(order.get("fill_count") or 0)
    avg_price = float(order.get("average_fill_price") or 0) or ask
    fee = read_fee(order)
    print(
        f"      fill={fill_count:g} precio={avg_price:.4f} comision="
        f"{fee if fee is not None else 'ausente'}"
    )
    if fill_count <= 0:
        return None, "sin fill (puede no haber contrapartida en demo)"
    if fee is None:
        present = [k for k in order if "fee" in k.lower()]
        return None, f"la respuesta no publica la comision (campos: {present or 'ninguno'})"
    sample = make_sample(
        "taker",
        ticker,
        fill_count,
        avg_price,
        fee,
        order_id=order.get("order_id"),
        filled=fill_count,
    )
    return sample, "ok"


def poll_maker_fill(
    base: str, auth: KalshiAuth, order_id: Optional[str], seconds: float
) -> Optional[Dict[str, Any]]:
    """Espera a que la orden pasiva se cruce sola y devuelve su estado final.

    Un maker solo mide si alguien lo cruza. En demo puede no pasar nunca, y el
    resultado honesto es "sin fill, sigue sin medir", no una tasa inventada.
    """
    if not order_id or seconds <= 0:
        return None
    deadline = time.time() + seconds
    order: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        for path in (
            f"/portfolio/events/orders/{order_id}",
            f"/portfolio/orders/{order_id}",
        ):
            resp = api(base, auth, "GET", path)
            if resp.status_code != 200:
                continue
            body = get_json(resp) or {}
            order = body.get("order") or body
            if float(order.get("fill_count") or 0) > 0:
                return order
            break
        time.sleep(2.0)
    return order


def _num(value: Any, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def print_calibration_report(document: Dict[str, Any]) -> None:
    """Lo medido frente a lo que predice cada precisión, y el veredicto."""
    rounding = document.get("rounding") or {}
    print()
    print(f"  {'mercado':<30}{'contratos':>10}{'precio':>9}{'base':>9}"
          f"{'cobrado':>9}{'cent':>9}{'micro':>9}")
    for row in rounding.get("evidence") or []:
        predictions = row.get("predictions") or {}
        note = "" if row.get("discriminating") else "   (no discrimina)"
        print(
            f"  {str(row.get('ticker'))[:29]:<30}{_num(row.get('contracts'), 1):>10}"
            f"{_num(row.get('price')):>9}{_num(row.get('basis')):>9}"
            f"{_num(row.get('fee_usd')):>9}{_num(predictions.get('cent')):>9}"
            f"{_num(predictions.get('micro')):>9}{note}"
        )

    mode = rounding.get("mode")
    if mode:
        print(
            f"  => redondeo medido: {mode} (${FEE_ROUNDING_STEPS[mode]}) "
            f"discriminan={rounding.get('discriminating_samples')} "
            f"decisivo={'si' if rounding.get('decisive') else 'no'}"
        )
    else:
        print("  => redondeo NO concluyente: se conserva el defecto conservador (cent)")

    for leg in ("taker", "maker"):
        block = document.get(leg) or {}
        rate = block.get("rate")
        verdict = "NO medida" if rate is None else f"medida={rate}"
        print(f"  => tasa {leg}: {verdict} ({block.get('why')})")


def finish_calibration(document: Dict[str, Any], out_path: Optional[str]) -> int:
    """Escribe el fichero y confirma con qué valores arrancará el simulador."""
    written = write_calibration(document, out_path)
    print()
    print(f"  fichero de calibracion: {written}")
    print(f"  {summarize(effective_fee_config(out_path))}")
    print("  execution_model lo lee al importarse: el proximo arranque del bot ya")
    print("  simula con estos valores medidos en lugar de con la spec documentada.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sonda de órdenes reales en Kalshi demo")
    parser.add_argument(
        "--place-order",
        action="store_true",
        help="Coloca y cancela contratos reales en demo (dinero ficticio)",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Mide la comision real y escribe fee_calibration.json (implica --place-order)",
    )
    parser.add_argument(
        "--contracts",
        type=int,
        default=1,
        help="Contratos por orden (defecto 1; mas contratos hacen la tasa medible)",
    )
    parser.add_argument(
        "--maker-wait",
        type=float,
        default=12.0,
        help="Segundos de espera a que la orden pasiva se cruce (defecto 12)",
    )
    parser.add_argument(
        "--calibration-out",
        default=None,
        help=f"Destino de la calibracion (defecto {FEE_CALIBRATION_FILE})",
    )
    args = parser.parse_args()

    # Calibrar sin operar es imposible: la calibracion ES lo que cobra el exchange
    # por nuestras ordenes. Y rellenar el hueco con una estimacion seria volver al
    # supuesto que este paso existe para eliminar.
    if args.calibrate and not args.place_order:
        print("[AVISO] --calibrate implica colocar ordenes; activando --place-order.")
        args.place_order = True
    if args.contracts < 1:
        print("--contracts debe ser >= 1")
        return 5

    base = KALSHI_REST_BASES[DEMO_ENV]
    auth = build_demo_auth()
    print("=" * 72)
    print("SONDA DE ORDENES - KALSHI DEMO (dinero ficticio)")
    print("=" * 72)
    print(f"host    : {base}")
    print(f"key_id  : {mask_key_id(auth.key_id)}")
    print(f"firmado : {auth.configured} | error: {auth.load_error}")
    print(f"modo    : contratos={args.contracts} calibrar={args.calibrate}")
    print()

    print("[1/7] Autenticacion")
    for path in ("/portfolio/positions", "/portfolio/orders"):
        resp = api(base, auth, "GET", path)
        print(f"      {resp.status_code}  {path}")
        if resp.status_code != 200:
            print("      ERROR: sin autenticacion valida no se puede continuar.")
            return 1

    limits = get_json(api(base, auth, "GET", "/account/limits")) or {}
    read_lim = limits.get("read") or {}
    print(
        f"      tier={limits.get('usage_tier')} "
        f"read_bucket={read_lim.get('bucket_capacity')} refill={read_lim.get('refill_rate')}/s"
    )

    print()
    print("[2/7] Saldo")
    balance_raw = get_json(api(base, auth, "GET", "/portfolio/balance")) or {}
    balance_cents = float(balance_raw.get("balance") or 0)
    print(f"      saldo demo: ${balance_cents / 100:.2f}")
    if balance_cents <= 0:
        print("      AVISO: la cuenta demo esta sin fondos.")

    print()
    print("[3/7] Seleccion de mercado con profundidad")
    market = pick_liquid_market(base, auth)
    if not market:
        print("      No se encontro mercado demo operable.")
        return 1
    ticker = market.get("ticker")
    bid = float(market.get("yes_bid_dollars") or 0)
    ask = float(market.get("yes_ask_dollars") or 0)
    print(f"      {ticker}")
    print(f"      bid={bid:.4f} ask={ask:.4f} vol24h={float(market.get('volume_24h_fp') or 0):.0f}")
    print(f"      criterio: {market.get('_probe_criterion')}")

    print()
    print("[4/7] Comision que modela el simulador")
    modeled = kalshi_trading_fee(args.contracts, ask)
    print(
        f"      {args.contracts} contrato(s) a {ask:.4f} -> "
        f"fee modelada ${modeled:.4f}"
    )

    if not args.place_order:
        print()
        print("[5/7] OMITIDO: sin --place-order no se envia nada")
        print("[6/7] OMITIDO")
        print("[7/7] OMITIDO: sin ordenes no hay comision que medir")
        print()
        print("Para validar el camino real de ordenes, repite con --place-order")
        print("Para medir ademas la comision real, anade --calibrate")
        return 0

    if balance_cents <= 0:
        print()
        print("[5/7] IMPOSIBLE: la cuenta demo no tiene fondos para colocar la orden.")
        print("      Fondea la cuenta en la web de demo (demo.kalshi.co) y repite.")
        print("      Todo el resto del cableado (firma, autenticacion, lectura de")
        print("      cuenta y limites) SI ha quedado validado sin ordenes.")
        return 2

    print()
    print("[5/7] Orden limite pasiva + cancelacion (valida payload V2 y cancelacion)")
    passive_price = max(0.01, round(bid - 0.02, 2))
    signal = {
        "token_id": ticker,
        "token": "Yes",
        "side": "BUY",
        "entry_price": f"{passive_price:.4f}",
        "recommended_order_type": "LIMIT (Maker)",
    }
    # Se reutiliza el MISMO constructor de payload que usa el bot en live, para
    # que la sonda valide el codigo real y no una copia paralela.
    order_payload = LiveExecutionManager.build_order_payload(
        signal, None, count=args.contracts
    )
    resp = api(base, auth, "POST", "/portfolio/events/orders", json=order_payload)
    print(f"      POST /portfolio/events/orders -> {resp.status_code}")
    print(f"      payload: {order_payload}")
    if resp.status_code not in (200, 201):
        print(f"      respuesta: {(resp.text or '')[:300]}")
        print("      El camino de ordenes NO quedo validado.")
        return 3

    created = get_json(resp) or {}
    order = created.get("order") or created
    order_id = order.get("order_id")
    print(f"      order_id={order_id} fill_count={order.get('fill_count')} "
          f"remaining={order.get('remaining_count')}")

    # El maker solo se mide si alguien cruza la orden. Se le da su tiempo antes de
    # cancelarla, porque un maker que nunca se cruza deja la tasa sin medir y ese
    # es justo el parametro que hoy vale 0 en el simulador sin ninguna prueba.
    maker_sample: Optional[Dict[str, Any]] = None
    if args.calibrate:
        print(f"      esperando hasta {args.maker_wait:.0f}s a que la cruce alguien...")
        filled = poll_maker_fill(base, auth, order_id, args.maker_wait)
        if not filled:
            print("      sin estado de la orden: no se puede medir el maker")
        else:
            maker_fill = float(filled.get("fill_count") or 0)
            maker_fee = read_fee(filled)
            maker_price = float(filled.get("average_fill_price") or 0) or passive_price
            print(f"      estado pasivo: fill={maker_fill:g} "
                  f"precio={maker_price:.4f} comision={maker_fee}")
            if maker_fill > 0 and maker_fee is not None:
                maker_sample = make_sample(
                    "maker",
                    ticker,
                    maker_fill,
                    maker_price,
                    maker_fee,
                    order_id=order_id,
                    filled=maker_fill,
                )
            elif maker_fill <= 0:
                print("      sin fill pasivo: el maker seguira SIN medir (no se")
                print("      inventa una tasa; el modelo conserva la documentada)")

    cancel = api(base, auth, "DELETE", f"/portfolio/events/orders/{order_id}")
    print(f"      DELETE /portfolio/events/orders/{order_id} -> {cancel.status_code}")

    print()
    print(f"[6/7] Fill real de {args.contracts} contrato(s) para medir la comision")
    taker_sample, why = cross_and_measure(base, auth, market, args.contracts)
    if taker_sample is None:
        print(f"      sin medicion: {why}")
        print("      El payload y el endpoint SI quedaron validados (orden aceptada).")
    else:
        print(f"      {ticker}: fill={taker_sample['fill_count']:g} "
              f"precio={taker_sample['price']:.4f} "
              f"comision=${taker_sample['fee_usd']:.4f}")
        modeled_at_fill = kalshi_trading_fee(
            taker_sample["fill_count"], taker_sample["price"]
        )
        delta = taker_sample["fee_usd"] - modeled_at_fill
        verdict = "COINCIDE" if abs(delta) <= 0.011 else "DIFIERE"
        print(f"      fee_modelada=${modeled_at_fill:.4f} -> {verdict} "
              f"(diferencia ${delta:+.4f})")
        if verdict == "DIFIERE":
            print("      Esa diferencia ES la divergencia papel/real. --calibrate la")
            print("      convierte en un fichero de calibracion en vez de un aviso.")

    if not args.calibrate:
        return 0

    print()
    print("[7/7] Calibracion: medicion real -> fee_calibration.json")
    samples: List[Dict[str, Any]] = [taker_sample] if taker_sample else []
    for target, candidate in pick_calibration_markets(base):
        if candidate is None:
            print(f"      objetivo {target:.2f}: ningun mercado con ask a menos de "
                  f"{TARGET_TOLERANCE:.2f}; se omite esta muestra")
            continue
        if candidate.get("ticker") == ticker:
            print(f"      objetivo {target:.2f}: ya cubierto por {ticker}")
            continue
        print(f"      objetivo {target:.2f}: {candidate.get('ticker')} "
              f"ask={quotes(candidate)[1]:.4f} ({candidate.get('_probe_criterion')})")
        sample, why = cross_and_measure(base, auth, candidate, args.contracts)
        if sample is None:
            print(f"      sin medicion: {why}")
            continue
        samples.append(sample)

    document = calibrate_from_samples(
        taker_samples=samples,
        maker_samples=[maker_sample] if maker_sample else [],
        environment=DEMO_ENV,
        contracts_per_order=args.contracts,
        notes=[
            "medido por demo_order_probe.py --calibrate contra el exchange demo",
            f"host de la medicion: {base}",
            f"contratos por orden: {args.contracts}",
            "un valor ausente significa NO MEDIDO: el modelo conserva el documentado",
        ],
    )
    print_calibration_report(document)
    if not samples:
        print()
        print("  Sin ninguna muestra taker no se escribe fichero: cambiar la spec")
        print("  documentada por una calibracion vacia seria peor que no calibrar.")
        return 0
    return finish_calibration(document, args.calibration_out)


if __name__ == "__main__":
    sys.exit(main())

