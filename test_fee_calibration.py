"""Tests de la comisión calibrada (fee_calibration.py + execution_model.py).

El fallo que estos tests persiguen no es un crash: es un simulador que cobra de
menos y por eso aprueba estrategias que en real pierden. Cada test defiende una de
las reglas que impiden que la comisión se vuelva optimista por accidente:

  * Sin medición manda el valor conservador (el que cobra MÁS), nunca una
    deducción sacada de muestras donde el redondeo domina el resultado.
  * Con medición manda la medición, incluso si contradice el `0.0` del `.env`.
  * El `ceil` se aplica una vez por orden, no una vez por fill.
  * Un fichero de calibración ilegible o de otra versión se IGNORA entero: leerlo
    a medias es leer un número que parece medido y no lo es.

Ningún test escribe el `fee_calibration.json` real: se usa un directorio temporal.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

import execution_model
from config import (
    FEE_ROUNDING,
    FEE_ROUNDING_STEPS,
    PAPER_FEE_MAKER_RATE,
    PAPER_FEE_TAKER_RATE,
)
from execution_model import kalshi_order_fee, kalshi_trading_fee, reload_fee_calibration
from fee_calibration import (
    CALIBRATION_SCHEMA,
    calibrate_from_samples,
    effective_fee_config,
    infer_rate,
    infer_rounding,
    make_sample,
    write_calibration,
)

# Globals de `execution_model` que cambian al recargar la calibración. Se guardan y
# se restauran: el modelo de comisión es estado del proceso, y dejarlo calibrado
# con un fichero temporal haría que otro test midiera otra cosa.
FEE_GLOBALS = (
    "FEE_TAKER_RATE",
    "FEE_MAKER_RATE",
    "FEE_ROUNDING_MODE",
    "FEE_ROUNDING_STEP",
    "FEE_SOURCE",
    "FEE_NOTES",
    "FEE_CALIBRATION",
)


def _document(
    taker_rate: Optional[float] = None,
    maker_rate: Optional[float] = None,
    rounding: Optional[str] = None,
    schema: Optional[int] = None,
) -> Dict[str, Any]:
    """Documento mínimo con la forma que escribe `demo_order_probe.py --calibrate`."""
    return {
        "schema_version": CALIBRATION_SCHEMA if schema is None else schema,
        "generated_at": "2026-09-25T00:00:00+00:00",
        "environment": "demo",
        "contracts_per_order": 1,
        "rounding": {"mode": rounding, "decisive": rounding is not None},
        "taker": {"rate": taker_rate, "measurable": taker_rate is not None},
        "maker": {"rate": maker_rate, "measurable": maker_rate is not None},
        "notes": [],
    }


class TestEffectiveFeeConfig(unittest.TestCase):
    def test_without_file_the_documented_spec_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = effective_fee_config(Path(tmp) / "no_existe.json")
        self.assertEqual(config["source"], "documented")
        self.assertAlmostEqual(config["taker_rate"], PAPER_FEE_TAKER_RATE)
        self.assertAlmostEqual(config["maker_rate"], PAPER_FEE_MAKER_RATE)
        self.assertEqual(config["rounding"], FEE_ROUNDING)
        self.assertAlmostEqual(config["rounding_step"], FEE_ROUNDING_STEPS[FEE_ROUNDING])
        self.assertTrue(any("NO medido" in note for note in config["notes"]))
        print(
            f"[TEST FeeConfig] sin medicion -> source={config['source']} "
            f"taker={config['taker_rate']} redondeo={config['rounding']} (conservador)"
        )

    def test_measurement_overrides_documentation(self):
        """El dato medido manda sobre el .env, aunque diga lo contrario."""
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(
                _document(taker_rate=0.05, maker_rate=0.02, rounding="micro"),
                Path(tmp) / "fee_calibration.json",
            )
            config = effective_fee_config(target)
        self.assertEqual(config["source"], "measured")
        self.assertAlmostEqual(config["taker_rate"], 0.05)
        self.assertAlmostEqual(config["maker_rate"], 0.02)
        self.assertEqual(config["rounding"], "micro")
        self.assertAlmostEqual(config["rounding_step"], 0.0001)
        print("[TEST FeeConfig] medicion completa -> taker=0.05 maker=0.02 micro")

    def test_partial_measurement_is_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(
                _document(taker_rate=0.05), Path(tmp) / "fee_calibration.json"
            )
            config = effective_fee_config(target)
        self.assertEqual(config["source"], "mixed")
        self.assertAlmostEqual(config["taker_rate"], 0.05)
        self.assertAlmostEqual(config["maker_rate"], PAPER_FEE_MAKER_RATE)
        print("[TEST FeeConfig] solo taker medido -> mixed, el maker cae al documentado")

    def test_measured_zero_taker_rate_is_a_measurement(self):
        """Un 0 medido es un dato: con `ceil`, cualquier tasa positiva cobraría algo."""
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(
                _document(taker_rate=0.0), Path(tmp) / "fee_calibration.json"
            )
            config = effective_fee_config(target)
        self.assertAlmostEqual(config["taker_rate"], 0.0)
        self.assertNotAlmostEqual(config["taker_rate"], PAPER_FEE_TAKER_RATE)
        self.assertEqual(config["source"], "mixed")
        print("[TEST FeeConfig] 0 medido -> se respeta (no vuelve al 0.07 publicado)")

    def test_other_schema_version_is_ignored(self):
        """Una calibración de otra versión no se interpreta a medias."""
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(
                _document(taker_rate=0.01, rounding="micro", schema=CALIBRATION_SCHEMA + 1),
                Path(tmp) / "fee_calibration.json",
            )
            config = effective_fee_config(target)
        self.assertEqual(config["source"], "documented")
        self.assertAlmostEqual(config["taker_rate"], PAPER_FEE_TAKER_RATE)
        print(
            f"[TEST FeeConfig] schema {CALIBRATION_SCHEMA + 1} -> ignorado entero, "
            "cae al documentado"
        )

    def test_corrupt_file_degrades_to_documented(self):
        # Nombre distinto al real a propósito: el aviso de `load_calibration` imprime
        # el nombre del fichero y un log que dijera "fee_calibration.json corrupto"
        # haría pensar que el fichero bueno del operador está roto.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "calibracion_rota.json"
            target.write_text("{esto no es json", encoding="utf-8")
            config = effective_fee_config(target)
        self.assertEqual(config["source"], "documented")
        print("[TEST FeeConfig] fichero corrupto -> no lanza, cae al documentado")

    def test_unknown_rounding_mode_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(
                _document(taker_rate=0.05, rounding="half"),
                Path(tmp) / "fee_calibration.json",
            )
            config = effective_fee_config(target)
        self.assertEqual(config["rounding"], FEE_ROUNDING)
        self.assertEqual(config["source"], "mixed")
        print("[TEST FeeConfig] redondeo desconocido -> se conserva el documentado (cent)")


class TestRateInference(unittest.TestCase):
    def test_zero_fees_on_a_positive_basis_measure_a_zero_rate(self):
        """Con `ceil`, tasa positiva => comisión > 0: un 0 es una medición, no un hueco."""
        samples = [make_sample("taker", "T", 1, 0.5, 0.0)]
        result = infer_rate(samples, FEE_ROUNDING_STEPS["cent"])
        self.assertTrue(result["measurable"])
        self.assertAlmostEqual(result["rate"], 0.0)
        self.assertEqual(len(result["zero_fee"]), 1)
        self.assertIn("ceil", result["why"])
        print("[TEST Tasa] fee 0 sobre base positiva -> tasa 0 MEDIDA (no 'sin dato')")

    def test_rounding_dominated_samples_are_not_measurable(self):
        """1 contrato a 0.05: el centavo del `ceil` es el 100% de la comisión.

        Cualquier tasa deducida de ahí sería ruido con formato de dato, así que la
        función se niega a inventarla y el llamador conserva la documentada.
        """
        samples = [make_sample("taker", "T", 1, 0.05, 0.01)]
        result = infer_rate(samples, FEE_ROUNDING_STEPS["cent"])
        self.assertFalse(result["measurable"])
        self.assertIsNone(result["rate"])
        self.assertIn("--contracts", result["why"])
        print(f"[TEST Tasa] muestra dominada por redondeo -> sin tasa ({result['why'][:58]}...)")

    def test_large_order_measures_the_published_rate(self):
        """1000 contratos a 0.50: base $250 y comisión $17.50 -> 0.07."""
        samples = [make_sample("taker", "T", 1000, 0.5, 17.5)]
        result = infer_rate(samples, FEE_ROUNDING_STEPS["cent"])
        self.assertTrue(result["measurable"])
        self.assertAlmostEqual(result["rate"], 0.07)
        self.assertLess(result["usable"][0]["rounding_error_share"], 0.01)
        print(f"[TEST Tasa] orden grande -> tasa medida {result['rate']} (error de redondeo 0.06%)")

    def test_the_usable_sample_wins_over_a_zero_fee_one(self):
        """Dos mediciones que se contradicen: se toma la que cobra más."""
        samples = [
            make_sample("taker", "GRANDE", 1000, 0.5, 17.5),
            make_sample("taker", "RARA", 1, 0.5, 0.0),
        ]
        result = infer_rate(samples, FEE_ROUNDING_STEPS["cent"])
        self.assertTrue(result["measurable"])
        self.assertAlmostEqual(result["rate"], 0.07)
        self.assertEqual(len(result["zero_fee"]), 1)
        self.assertEqual(result["zero_fee"][0]["ticker"], "RARA")
        print("[TEST Tasa] muestras en conflicto -> manda la utilizable (la conservadora)")


class TestRoundingInference(unittest.TestCase):
    def test_cent_rounding_is_detected_from_a_cheap_contract(self):
        """1 contrato a 0.05 -> $0.01 si redondea a centavo, $0.0034 si a $0.0001."""
        samples = [make_sample("taker", "T", 1, 0.05, 0.01)]
        result = infer_rounding(samples)
        self.assertEqual(result["mode"], "cent")
        self.assertTrue(result["decisive"])
        self.assertEqual(result["matches"]["cent"], 1)
        self.assertEqual(result["matches"]["micro"], 0)
        print("[TEST Redondeo] cobrado $0.01 sobre base $0.0475 -> cent (discriminante)")

    def test_micro_rounding_is_detected_from_the_same_order(self):
        samples = [make_sample("taker", "T", 1, 0.05, 0.0034)]
        result = infer_rounding(samples)
        self.assertEqual(result["mode"], "micro")
        self.assertTrue(result["decisive"])
        print("[TEST Redondeo] cobrado $0.0034 sobre base $0.0475 -> micro (discriminante)")

    def test_ambiguous_samples_do_not_decide_the_rounding(self):
        """4 contratos a 0.50 dan base exacta $1.00: ambas precisiones dan $0.07.

        Contar esta muestra como evidencia daría por buena la precisión equivocada
        en cuanto lo cobrado fuese múltiplo del centavo, que es el caso a distinguir.
        """
        samples = [make_sample("taker", "T", 4, 0.5, 0.07)]
        result = infer_rounding(samples)
        self.assertIsNone(result["mode"])
        self.assertFalse(result["decisive"])
        self.assertEqual(result["discriminating_samples"], 0)
        self.assertIn("cent", result["evidence"][0]["matches"])
        self.assertIn("micro", result["evidence"][0]["matches"])
        print("[TEST Redondeo] muestra no discriminante -> sin conclusion (mode=None)")

    def test_only_discriminating_samples_count(self):
        samples = [
            make_sample("taker", "AMBIGUA", 4, 0.5, 0.07),
            make_sample("taker", "CLARA", 1, 0.05, 0.0034),
        ]
        result = infer_rounding(samples)
        self.assertEqual(result["discriminating_samples"], 1)
        self.assertEqual(result["mode"], "micro")
        self.assertTrue(result["decisive"])
        self.assertEqual(result["matches"]["cent"], 0)
        print("[TEST Redondeo] 1 ambigua + 1 clara -> micro (la ambigua no vota)")


class TestPerOrderAccumulator(unittest.TestCase):
    def test_one_ceil_per_order_not_one_per_fill(self):
        """Kalshi cobra el pedido, no cada fill: el `ceil` va al final.

        Redondear por fill cobra de más en cada fill adicional, y con precisión de
        centavo ese centavo extra son varios puntos porcentuales del nocional.
        """
        whole = kalshi_order_fee([(10, 0.5), (10, 0.5)])
        per_fill = kalshi_trading_fee(10, 0.5) + kalshi_trading_fee(10, 0.5)
        self.assertAlmostEqual(whole, 0.35)
        self.assertAlmostEqual(per_fill, 0.36)
        self.assertLess(whole, per_fill)
        print(f"[TEST Acumulador] 2 fills de 10 -> orden ${whole:.2f} vs por-fill ${per_fill:.2f}")

    def test_single_fill_delegates_to_the_order_accumulator(self):
        for price in (0.03, 0.35, 0.5, 0.9):
            with self.subTest(price=price):
                self.assertEqual(
                    kalshi_trading_fee(1, price), kalshi_order_fee([(1, price)])
                )
        print("[TEST Acumulador] kalshi_trading_fee coincide con la orden de 1 fill")

    def test_micro_rounding_keeps_the_cent_off_a_cheap_contract(self):
        """La precisión decide el breakeven de los contratos baratos: 3x de diferencia."""
        cent = kalshi_order_fee([(1, 0.05)], rounding="cent")
        micro = kalshi_order_fee([(1, 0.05)], rounding="micro")
        self.assertAlmostEqual(cent, 0.01)
        self.assertAlmostEqual(micro, 0.0034)
        print(f"[TEST Acumulador] 1 contrato a 0.05 -> cent ${cent:.4f} vs micro ${micro:.4f}")

    def test_empty_and_malformed_fills_are_free(self):
        self.assertEqual(kalshi_order_fee([]), 0.0)
        self.assertEqual(kalshi_order_fee(None), 0.0)
        self.assertEqual(kalshi_order_fee([("x", "y")]), 0.0)
        self.assertAlmostEqual(kalshi_order_fee([("x", "y"), (10, 0.5)]), 0.18)
        print("[TEST Acumulador] fills vacios o malformados -> 0 (no revienta)")


class TestCalibrationRoundTrip(unittest.TestCase):
    """De la medición al modelo: el fichero se escribe, se relee y cambia lo cobrado."""

    def setUp(self) -> None:
        self._saved = {name: getattr(execution_model, name) for name in FEE_GLOBALS}

    def tearDown(self) -> None:
        # El modelo de comisión es estado del proceso: se deja como estaba.
        for name, value in self._saved.items():
            setattr(execution_model, name, value)

    def test_measured_rate_moves_the_live_model(self):
        document = calibrate_from_samples([make_sample("taker", "T", 100, 0.5, 1.50)])
        self.assertTrue(document["taker"]["measurable"])
        self.assertAlmostEqual(document["taker"]["rate"], 0.06)
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(document, Path(tmp) / "fee_calibration.json")
            status = reload_fee_calibration(target)
            measured = kalshi_order_fee([(100, 0.5)])
            documented = kalshi_order_fee([(100, 0.5)], rate=PAPER_FEE_TAKER_RATE)
        self.assertAlmostEqual(status["taker_rate"], 0.06)
        # La muestra no discrimina la precisión, así que el redondeo sigue siendo el
        # conservador y la fuente es "mixed": medido donde hay dato, spec donde no.
        self.assertEqual(status["source"], "mixed")
        self.assertAlmostEqual(measured, 1.50)
        self.assertAlmostEqual(documented, 1.75)
        self.assertLess(measured, documented)
        print(f"[TEST Calibracion] tasa medida 0.06 -> cobra ${measured:.2f} donde la spec cobraba ${documented:.2f}")

    def test_rounding_dominated_probe_does_not_invent_a_rate(self):
        """Una sonda de 1 contrato mide la precisión, no la tasa: no debe escribir una."""
        document = calibrate_from_samples([make_sample("taker", "T", 1, 0.05, 0.01)])
        self.assertIsNone(document["taker"]["rate"])
        self.assertFalse(document["taker"]["measurable"])
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(document, Path(tmp) / "fee_calibration.json")
            persisted = json.loads(target.read_text(encoding="utf-8"))
            status = reload_fee_calibration(target)
        self.assertIsNone(persisted["taker"]["rate"])
        self.assertAlmostEqual(status["taker_rate"], PAPER_FEE_TAKER_RATE)
        self.assertEqual(status["source"], "mixed")
        print("[TEST Calibracion] sonda dominada por redondeo -> taker sigue en la spec (0.07)")

    def test_missing_file_leaves_the_model_on_the_documented_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = reload_fee_calibration(Path(tmp) / "no_existe.json")
            charged = kalshi_trading_fee(10, 0.5)
        self.assertEqual(status["source"], "documented")
        self.assertAlmostEqual(status["taker_rate"], PAPER_FEE_TAKER_RATE)
        self.assertAlmostEqual(charged, 0.18)
        print(f"[TEST Calibracion] sin fichero -> source=documented, 10@0.50 cobra ${charged:.2f}")

    def test_measured_zero_maker_rate_is_kept(self):
        """Un maker de 0 medido se conserva como dato; lo que no vale es suponerlo."""
        document = calibrate_from_samples(
            [make_sample("taker", "T", 1000, 0.5, 17.5)],
            [make_sample("maker", "T", 1000, 0.5, 0.0)],
        )
        self.assertAlmostEqual(document["maker"]["rate"], 0.0)
        self.assertTrue(document["maker"]["measurable"])
        with tempfile.TemporaryDirectory() as tmp:
            target = write_calibration(document, Path(tmp) / "fee_calibration.json")
            status = reload_fee_calibration(target)
        self.assertAlmostEqual(status["maker_rate"], 0.0)
        self.assertTrue(any("maker medido" in note for note in status["notes"]))
        print("[TEST Calibracion] maker 0 medido -> se conserva con la nota de origen")


if __name__ == "__main__":
    unittest.main()



