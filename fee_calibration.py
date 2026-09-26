"""fee_calibration.py — Comisión de Kalshi calibrada con mediciones, no con supuestos.

El simulador cobra `ceil(rate · C · P · (1-P))`, que es la fórmula publicada por
Kalshi. La fórmula no es el problema: el problema son los dos parámetros que se
dieron por buenos sin medirlos.

1. **Precisión del redondeo.** Si la cuenta compensa contra el exchange como
   miembro directo, Kalshi redondea a `$0.0001`; si opera a través de un FCM, a
   `$0.01`. Sobre 1 contrato a 0.03 la comisión cruda es `$0.0021`: redondear a un
   centavo la multiplica por 5, redondear a `$0.0001` la deja casi como está. Ese
   factor decide el breakeven de cualquier estrategia de contratos baratos.

2. **Comisión del maker.** En `.env` está `PAPER_FEE_MAKER_RATE=0.0` "porque los
   maker no pagan". El fee schedule de Kalshi dice que el acumulador de comisiones
   se aplica *"regardless of whether the fills are taker or maker"*, así que un 0
   sin medir es exactamente el tipo de supuesto que hace que una estrategia gane
   en simulación y pierda en real. El histórico del bot (171/171 trades de market
   making con fee 0) no es evidencia: es el modelo cobrando lo que le dijeron.

Este módulo no adivina. Lee `fee_calibration.json`, que escribe
`demo_order_probe.py --calibrate` colocando órdenes reales de 1 contrato contra el
exchange DEMO con dinero ficticio. Reglas de resolución:

* Si hay medición -> manda la medición, aunque contradiga la documentación.
* Si no hay medición -> manda el valor conservador (el que cobra MÁS). Un
  simulador optimista es peor que no simular, porque produce decisiones.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    FEE_CALIBRATION_FILE,
    FEE_ROUNDING,
    FEE_ROUNDING_STEPS,
    PAPER_FEE_MAKER_RATE,
    PAPER_FEE_TAKER_RATE,
)

# Version del formato del fichero. Un fichero de otra version se ignora en vez de
# interpretarse a medias: una calibracion mal leida es peor que ninguna.
CALIBRATION_SCHEMA = 1
VALID_ROUNDINGS = tuple(FEE_ROUNDING_STEPS)


def _warn(message: str) -> None:
    print(f"[FeeCalibration] {message}")


def calibration_path(path: Optional[Any] = None) -> Path:
    return Path(path) if path else Path(FEE_CALIBRATION_FILE)


def load_calibration(path: Optional[Any] = None) -> Dict[str, Any]:
    """Lee `fee_calibration.json`. Devuelve `{}` si no existe o no es utilizable.

    Nunca lanza: la ausencia de medicion es el caso normal (recien instalado) y
    debe degradar a la spec documentada, no romper el arranque del bot.
    """
    target = calibration_path(path)
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        _warn(f"no se pudo leer {target.name} ({exc}); se usa la spec documentada.")
        return {}
    if not isinstance(raw, dict):
        _warn(f"{target.name} no contiene un objeto JSON; se usa la spec documentada.")
        return {}
    try:
        schema = int(raw.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema != CALIBRATION_SCHEMA:
        _warn(
            f"{target.name} tiene schema_version={schema} y se espera "
            f"{CALIBRATION_SCHEMA}; se ignora y se usa la spec documentada."
        )
        return {}
    return raw


def _measured_rate(block: Any) -> Optional[float]:
    """Tasa medida dentro de un bloque `{"rate": ...}`. `None` si no hay dato."""
    if not isinstance(block, dict):
        return None
    value = block.get("rate")
    if value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if rate >= 0 else None


def _measured_rounding(calibration: Dict[str, Any]) -> Optional[str]:
    block = calibration.get("rounding")
    if not isinstance(block, dict):
        return None
    candidate = str(block.get("mode") or "").strip().lower()
    return candidate if candidate in VALID_ROUNDINGS else None


def effective_fee_config(path: Optional[Any] = None) -> Dict[str, Any]:
    """Resuelve `(taker, maker, redondeo)` combinando medicion y documentacion.

    Devuelve ademas `source` (`measured` / `documented` / `mixed`) y `notes` con el
    razonamiento de cada campo: cuando dentro de un mes alguien pregunte de donde
    sale el 0.07, la respuesta tiene que estar en el propio estado del modelo.
    """
    calibration = load_calibration(path)
    notes: List[str] = []

    measured_taker = _measured_rate(calibration.get("taker"))
    measured_maker = _measured_rate(calibration.get("maker"))
    measured_rounding = _measured_rounding(calibration)

    if measured_taker is not None:
        taker_rate = measured_taker
        notes.append(f"taker medido en demo: {taker_rate}")
    else:
        taker_rate = PAPER_FEE_TAKER_RATE
        notes.append(
            f"taker NO medido: se usa PAPER_FEE_TAKER_RATE={taker_rate} (publicado)"
        )

    if measured_maker is not None:
        maker_rate = measured_maker
        notes.append(f"maker medido en demo: {maker_rate}")
    else:
        maker_rate = PAPER_FEE_MAKER_RATE
        notes.append(
            f"maker NO medido: se usa PAPER_FEE_MAKER_RATE={maker_rate} "
            "(el fee schedule aplica el acumulador a maker y taker por igual, "
            "asi que un 0 sin medir es un supuesto, no un dato)"
        )

    if measured_rounding is not None:
        rounding = measured_rounding
        notes.append(f"precision de redondeo medida en demo: {rounding}")
    else:
        rounding = FEE_ROUNDING
        notes.append(
            f"precision de redondeo NO medida: se usa FEE_ROUNDING={rounding} "
            "(el defecto conservador es 'cent': si nos equivocamos, cobramos de mas)"
        )

    measured_fields = [
        measured_taker is not None,
        measured_maker is not None,
        measured_rounding is not None,
    ]
    if all(measured_fields):
        source = "measured"
    elif any(measured_fields):
        source = "mixed"
    else:
        source = "documented"

    return {
        "source": source,
        "taker_rate": float(taker_rate),
        "maker_rate": float(maker_rate),
        "rounding": rounding,
        "rounding_step": FEE_ROUNDING_STEPS[rounding],
        "calibration": calibration,
        "calibration_file": str(calibration_path(path)),
        "notes": notes,
    }


def summarize(config: Optional[Dict[str, Any]] = None) -> str:
    """Resumen legible de la calibracion, para logs y para el diagnostico."""
    resolved = effective_fee_config() if config is None else config
    calibration = resolved.get("calibration") or {}
    generated = calibration.get("generated_at") or "nunca"
    return (
        f"comisiones: fuente={resolved['source']} taker={resolved['taker_rate']} "
        f"maker={resolved['maker_rate']} redondeo={resolved['rounding']} "
        f"(${resolved['rounding_step']}) medido_en={generated}"
    )


# ========== Inferencia a partir de mediciones reales ==========


def fee_basis(contracts: float, price: float) -> float:
    """Base sobre la que se calcula la comisión: `contratos * P * (1-P)`."""
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    return float(contracts) * float(price) * (1.0 - float(price))


def predict_fee(basis: float, rate: float, step: float) -> float:
    """Comisión que la fórmula documentada predice para esa base y precisión."""
    if basis <= 0 or rate <= 0:
        return 0.0
    return round(ceil_to_step_public(basis * rate, step), 10)


def ceil_to_step_public(value: float, step: float) -> float:
    """Redondeo hacia arriba al múltiplo de `step` (mismo criterio que el modelo).

    Se duplica aquí en vez de importarse de `execution_model` a propósito: si el
    modelo y la calibración compartieran la misma implementación, un error en ella
    haría que la medición "confirmara" el error y la calibración perdería todo su
    valor probatorio.
    """
    if value <= 0:
        return 0.0
    if step <= 0:
        return float(value)
    import math

    return math.ceil(value / step - 1e-9) * step


def make_sample(
    kind: str,
    ticker: str,
    contracts: float,
    price: float,
    fee_usd: float,
    order_id: Optional[str] = None,
    filled: Optional[float] = None,
) -> Dict[str, Any]:
    """Normaliza una medición real de comisión en un registro comparable."""
    return {
        "kind": kind,
        "ticker": ticker,
        "order_id": order_id,
        "contracts": float(contracts),
        "fill_count": float(filled if filled is not None else contracts),
        "price": float(price),
        "fee_usd": float(fee_usd),
        "basis": round(fee_basis(contracts, price), 10),
    }


def infer_rate(
    samples: List[Dict[str, Any]], step: float, tolerance: float = 0.02
) -> Dict[str, Any]:
    """Estima la tasa de comisión a partir de fills reales.

    Solo se puede estimar cuando el redondeo es pequeño frente a la comisión
    cobrada: con 1 contrato, el `ceil` a centavo domina el resultado y cualquier
    tasa que se deduzca es ruido. Por eso la función informa `measurable=False` en
    vez de devolver un número inventado, y el llamador conserva la tasa
    documentada.
    """
    usable: List[Dict[str, Any]] = []
    unusable: List[Dict[str, Any]] = []
    zero_fee: List[Dict[str, Any]] = []
    for sample in samples or []:
        basis = float(sample.get("basis") or 0.0)
        fee = float(sample.get("fee_usd") or 0.0)
        if basis <= 0:
            continue
        if fee <= 0:
            # Con `ceil`, cualquier tasa positiva cobra al menos un escalon: una
            # comision de 0 sobre una base positiva es una MEDICION de tasa 0, no
            # un dato ausente.
            zero_fee.append(
                {
                    "ticker": sample.get("ticker"),
                    "price": sample.get("price"),
                    "contracts": sample.get("contracts"),
                    "fee_usd": fee,
                }
            )
            continue
        # El `ceil` cobra de más, así que lo estimado es una cota superior: cuanto
        # mayor la comisión frente al escalón de redondeo, más fina la cota.
        relative = step / fee
        implied = fee / basis
        record = {
            "ticker": sample.get("ticker"),
            "price": sample.get("price"),
            "contracts": sample.get("contracts"),
            "fee_usd": fee,
            "implied_rate": round(implied, 6),
            "rounding_error_share": round(relative, 6),
        }
        if relative <= tolerance and 0.0 < implied < 0.5:
            usable.append(record)
        else:
            unusable.append(record)

    if not usable:
        if zero_fee and not unusable:
            return {
                "measurable": True,
                "rate": 0.0,
                "usable": [],
                "zero_fee": zero_fee,
                "rejected": [],
                "why": (
                    f"{len(zero_fee)} fill(s) con comision 0 sobre base positiva: "
                    "con ceil, cualquier tasa positiva cobraria al menos un escalon"
                ),
            }
        return {
            "measurable": False,
            "rate": None,
            "usable": [],
            "zero_fee": zero_fee,
            "rejected": unusable,
            "why": (
                "el redondeo pesa mas del "
                f"{tolerance * 100:.0f}% de la comision en todas las muestras; "
                "aumenta --contracts para que el redondeo deje de dominar el resultado"
            ),
        }
    # El `ceil` siempre cobra de más, así que la menor tasa implicada es la cota
    # superior más ajustada.
    rate = min(record["implied_rate"] for record in usable)
    return {
        "measurable": True,
        "rate": round(rate, 6),
        "usable": usable,
        "zero_fee": zero_fee,
        "rejected": unusable,
        "why": f"{len(usable)} muestra(s) con redondeo <= {tolerance * 100:.0f}% de la comision",
    }


def infer_rounding(
    samples: List[Dict[str, Any]], rate: Optional[float] = None
) -> Dict[str, Any]:
    """Deduce la precisión del redondeo comparando lo cobrado con lo predicho.

    Solo cuentan las muestras que DISCRIMINAN, es decir, aquellas en las que las
    dos precisiones predicen comisiones distintas (a 0.05 y 1 contrato: $0.01 si
    se redondea a centavo, $0.0034 si se redondea a $0.0001). Contar muestras
    ambiguas daría por buena la precisión equivocada en cuanto lo cobrado fuese
    múltiplo del centavo, que es justo el caso a distinguir.
    """
    prediction_rate = PAPER_FEE_TAKER_RATE if rate is None else float(rate)
    evidence: List[Dict[str, Any]] = []
    matches: Dict[str, int] = {mode: 0 for mode in VALID_ROUNDINGS}
    discriminating = 0

    for sample in samples or []:
        basis = float(sample.get("basis") or 0.0)
        fee = float(sample.get("fee_usd") or 0.0)
        if basis <= 0 or fee <= 0:
            continue
        predictions = {
            mode: predict_fee(basis, prediction_rate, FEE_ROUNDING_STEPS[mode])
            for mode in VALID_ROUNDINGS
        }
        span = max(predictions.values()) - min(predictions.values())
        is_discriminating = span > 1e-9
        if is_discriminating:
            discriminating += 1
        row = {
            "ticker": sample.get("ticker"),
            "price": sample.get("price"),
            "contracts": sample.get("contracts"),
            "basis": round(basis, 10),
            "fee_usd": fee,
            "predictions": predictions,
            "discriminating": is_discriminating,
            "matches": [],
        }
        for mode in VALID_ROUNDINGS:
            tolerance = max(1e-9, FEE_ROUNDING_STEPS[mode] * 1e-3)
            if abs(predictions[mode] - fee) <= tolerance:
                row["matches"].append(mode)
                if is_discriminating:
                    matches[mode] += 1
        evidence.append(row)

    if discriminating == 0:
        mode, decisive = None, False
    elif matches["cent"] > matches["micro"]:
        mode, decisive = "cent", True
    elif matches["micro"] > matches["cent"]:
        mode, decisive = "micro", True
    elif matches["cent"] > 0:
        # Ambas precisiones explican lo cobrado: se conserva la que cobra más.
        mode, decisive = "cent", False
    else:
        mode, decisive = None, False

    return {
        "mode": mode,
        "decisive": decisive,
        "matches": matches,
        "discriminating_samples": discriminating,
        "rate_used_for_prediction": prediction_rate,
        "evidence": evidence,
    }


def calibrate_from_samples(
    taker_samples: List[Dict[str, Any]],
    maker_samples: Optional[List[Dict[str, Any]]] = None,
    environment: str = "demo",
    contracts_per_order: float = 1.0,
    notes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construye el documento de calibración a partir de mediciones reales.

    Un campo a `None` significa "no medido" y `fee_calibration` lo ignora al
    resolver, cayendo en el valor documentado. Es deliberado: escribir una tasa
    deducida de una sola muestra con redondeo dominante envenenaría el simulador
    con un número que parece medido y no lo está.
    """
    taker_samples = list(taker_samples or [])
    maker_samples = list(maker_samples or [])
    rounding = infer_rounding(taker_samples)
    step = FEE_ROUNDING_STEPS[rounding["mode"] or FEE_ROUNDING]
    taker_rate = infer_rate(taker_samples, step)
    maker_rate = infer_rate(maker_samples, step)

    document: Dict[str, Any] = {
        "schema_version": CALIBRATION_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "contracts_per_order": contracts_per_order,
        "formula": "ceil(rate * contratos * P * (1-P)), acumulado por orden",
        "rounding": {
            "mode": rounding["mode"],
            "decisive": rounding["decisive"],
            "discriminating_samples": rounding["discriminating_samples"],
            "matches": rounding["matches"],
            "evidence": rounding["evidence"],
        },
        "taker": {
            "rate": taker_rate["rate"],
            "measurable": taker_rate["measurable"],
            "why": taker_rate["why"],
            "samples": taker_samples,
        },
        "maker": {
            "rate": maker_rate["rate"],
            "measurable": maker_rate["measurable"],
            "why": maker_rate["why"],
            "samples": maker_samples,
        },
        "notes": list(notes or []),
    }
    return document


def write_calibration(document: Dict[str, Any], path: Optional[Any] = None) -> Path:
    """Escribe el documento y devuelve la ruta final."""
    target = calibration_path(path)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target
