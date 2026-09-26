"""Tests de integridad del track record de paper trading.

El historial de trades es la base del gate de promocion: si se corrompe o se
pierde, la puerta PAPER -> LIVE deja de tener fundamento. Estos tests cubren el
guardado atomico y el arranque con un archivo dañado.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from paper_tracker import PaperTradingEngine


class TestPaperTradeDurability(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "paper_trades.json"

    def tearDown(self):
        self._dir.cleanup()

    def _tracker(self) -> PaperTradingEngine:
        return PaperTradingEngine(storage_path=self.path)

    @staticmethod
    def _trade(code: str = "S20", status: str = "WON") -> dict:
        # confidence >= 75 para que el filtro de carga lo conserve.
        return {"strategy_code": code, "status": status, "confidence": 90.0}

    def test_save_then_load_roundtrip(self):
        tracker = self._tracker()
        tracker.trades = {"S20:M1:Yes:BUY": self._trade()}
        tracker.save_to_disk()

        self.assertTrue(self.path.exists())
        self.assertEqual(self._tracker().trades, tracker.trades)
        print("[TEST Durabilidad] guardar y recargar conserva los trades")

    def test_save_leaves_no_temp_file_behind(self):
        tracker = self._tracker()
        tracker.trades = {"A": self._trade()}
        tracker.save_to_disk()

        self.assertEqual(sorted(p.name for p in Path(self._dir.name).glob("*")),
                         ["paper_trades.json"])
        print("[TEST Durabilidad] el temporal no queda huerfano tras guardar")

    def test_file_is_never_left_truncated_across_overwrites(self):
        """Antes se truncaba el archivo antes de escribir: leer ahi lo rompia."""
        tracker = self._tracker()
        for i in range(25):
            tracker.trades = {f"k{j}": self._trade() for j in range(i + 1)}
            tracker.save_to_disk()
            # Cualquier lectura concurrente debe encontrar JSON completo.
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(len(parsed["trades"]), i + 1)
        print("[TEST Durabilidad] 25 sobrescrituras seguidas, JSON siempre completo")

    def test_corrupt_file_warns_and_starts_clean(self):
        self.path.write_text('{"trades": {"a": ', encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tracker = self._tracker()

        self.assertEqual(tracker.trades, {})
        self.assertIn("historial ilegible", buf.getvalue())
        print("[TEST Durabilidad] archivo corrupto no rompe el arranque y avisa")

    def test_missing_file_is_not_an_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tracker = self._tracker()
        self.assertEqual(tracker.trades, {})
        self.assertEqual(buf.getvalue(), "")
        print("[TEST Durabilidad] arranque sin archivo previo es limpio y silencioso")

    def test_low_confidence_trades_are_filtered_on_load(self):
        tracker = self._tracker()
        tracker.trades = {
            "alta": {"confidence": 90.0, "status": "WON"},
            "baja": {"confidence": 60.0, "status": "WON"},
        }
        tracker.save_to_disk()
        self.assertEqual(list(self._tracker().trades), ["alta"])
        print("[TEST Durabilidad] el filtro de confianza >= 75 sigue aplicandose")


if __name__ == "__main__":
    unittest.main()
