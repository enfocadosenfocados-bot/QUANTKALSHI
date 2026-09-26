"""
ai_learning_engine.py
=====================
Motor de Machine Learning, Calibración de Brier Score y Auto-Aprendizaje Cuantitativo.

Mecánica Cuantitativa:
1. Puntuación de Brier (Brier Score):
   BS = (1/N) * sum((f_i - o_i)^2)
   Donde f_i es la probabilidad prevista y o_i es el resultado real (1 = Ganada, 0 = Perdida).
2. Descomposición de Murphy:
   BS = Confiabilidad (Reliability) - Resolución (Resolution) + Incertidumbre (Uncertainty)
   - Confiabilidad: Mide la calibración matemática (si decimos 85% de acierto, ¿ganamos el 85%?).
   - Resolución: Mide la capacidad de separar eventos seguros de improbables.
   - Incertidumbre: Varianza inherente del mercado base.
3. Regresión Isotónica (PAVA - Pool Adjacent Violators Algorithm):
   Mapeo no paramétrico que re-calibra probabilidades brutas sobreestimadas a su valor justo empírico.
4. Memoria Post-Mortem y Reflexión de IA (trade_memory.json):
   Analiza cada operación perdedora o subóptima para extraer reglas de exclusión y mejora operativa.
5. Re-ponderación Dinámica de Kelly:
   Ajusta diariamente el multiplicador de asignación de capital (de 0.35x a 1.65x) para cada una
   de las 15 estrategias según su Brier Score móvil.
"""

import os
import json
import time
import math
import asyncio
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger("ai_learning_engine")
logger.setLevel(logging.INFO)

TRADE_MEMORY_PATH = "trade_memory.json"
CALIBRATION_STATE_PATH = "calibration_state.json"


@dataclass
class TradeReflection:
    id: str
    trade_id: str
    strategy_id: str
    market_title: str
    predicted_prob: float
    outcome: int  # 1 for win, 0 for loss
    pnl: float
    brier_error: float
    root_cause: str
    actionable_rule: str
    created_at: float


@dataclass
class MurphyDecomposition:
    brier_score: float
    reliability: float
    resolution: float
    uncertainty: float
    sample_size: int
    brier_skill_score: float  # BSS = 1 - (BS / Uncertainty)


class PAVAIsotonicCalibrator:
    """
    Implementación matemática pura del algoritmo PAVA (Pool Adjacent Violators Algorithm)
    para regresión isotónica de probabilidades sin requerir scikit-learn o numpy.
    """
    def __init__(self):
        self.fitted_pairs: List[Tuple[float, float]] = []  # [(x_threshold, calibrated_prob)]

    def fit(self, predictions: List[float], outcomes: List[int]):
        if len(predictions) < 3 or len(predictions) != len(outcomes):
            # Fallback identidad si hay pocos datos
            self.fitted_pairs = [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
            return

        # 1. Ordenar pares por predicción
        combined = sorted(zip(predictions, outcomes), key=lambda x: x[0])
        
        # Bloques iniciales: cada punto es un bloque (peso=1, suma_y=y, media_x=x)
        blocks = []
        for x, y in combined:
            blocks.append({
                "weight": 1.0,
                "sum_y": float(y),
                "val": float(y),
                "x_min": x,
                "x_max": x,
            })

        # 2. PAVA loop: fusionar bloques adyacentes que violen monotonicidad
        i = 0
        while i < len(blocks) - 1:
            if blocks[i]["val"] > blocks[i + 1]["val"]:
                # Violación: fusionar bloques i e i+1
                w_new = blocks[i]["weight"] + blocks[i + 1]["weight"]
                sum_new = blocks[i]["sum_y"] + blocks[i + 1]["sum_y"]
                val_new = sum_new / w_new
                blocks[i] = {
                    "weight": w_new,
                    "sum_y": sum_new,
                    "val": val_new,
                    "x_min": blocks[i]["x_min"],
                    "x_max": blocks[i + 1]["x_max"],
                }
                del blocks[i + 1]
                # Retroceder para re-comprobar monotonía anterior
                if i > 0:
                    i -= 1
            else:
                i += 1

        self.fitted_pairs = [(b["x_max"], min(max(b["val"], 0.01), 0.99)) for b in blocks]

    def predict_proba(self, raw_prob: float) -> float:
        """Mapea una probabilidad bruta a su valor calibrado."""
        if not self.fitted_pairs:
            return raw_prob
        # Interpolación por escalones
        for threshold, cal_val in self.fitted_pairs:
            if raw_prob <= threshold:
                return cal_val
        return self.fitted_pairs[-1][1]


class AILearningEngine:
    def __init__(self):
        self.calibrator = PAVAIsotonicCalibrator()
        self.reflections: List[TradeReflection] = []
        self.strategy_weights: Dict[str, float] = {}  # strategy_id -> multiplier (0.35x - 1.65x)
        self.strategy_brier_scores: Dict[str, Dict[str, Any]] = {}
        self.global_murphy: Optional[MurphyDecomposition] = None
        self.last_calibration_time = 0.0
        self.daily_loop_running = False
        self._task: Optional[asyncio.Task] = None
        self._load_memory()

    def start(self):
        """Inicia el bucle de auto-aprendizaje continuo 100% autónomo."""
        if self.daily_loop_running:
            return
        self.daily_loop_running = True
        self._task = asyncio.create_task(self._run_auto_learning_loop())
        logger.info("Motor de Auto-Aprendizaje IA continuo iniciado (100% autónomo).")

    def stop(self):
        """Detiene el bucle de auto-aprendizaje."""
        self.daily_loop_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Motor de Auto-Aprendizaje IA detenido.")

    async def _run_auto_learning_loop(self):
        """Bucle en segundo plano que ejecuta la calibración y reajuste de Kelly de forma automática cada 60s."""
        while self.daily_loop_running:
            try:
                from paper_tracker import paper_tracker, paper_tracker_research
                closed_trades = [t for t in paper_tracker.trades.values() if t.get("status") in ("WON", "LOST")]
                closed_trades += [t for t in paper_tracker_research.trades.values() if t.get("status") in ("WON", "LOST")]
                self.run_daily_calibration(closed_trades)
            except Exception as e:
                logger.error(f"Error en bucle auto_learning: {e}")
            await asyncio.sleep(60.0)

    def _load_memory(self):
        """Carga el registro histórico de reflexiones y calibraciones previas."""
        if os.path.exists(TRADE_MEMORY_PATH):
            try:
                with open(TRADE_MEMORY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data.get("reflections", []):
                        if item.get("predicted_prob", 0.0) > 1.0:
                            item["predicted_prob"] = round(item["predicted_prob"] / 100.0, 3)
                        item["predicted_prob"] = max(0.01, min(0.99, item.get("predicted_prob", 0.50)))
                        item["brier_error"] = round((item["predicted_prob"] - item.get("outcome", 0)) ** 2, 4)
                        self.reflections.append(TradeReflection(**item))
                    self.strategy_weights = data.get("strategy_weights", {})
                    logger.info(f"Cargadas {len(self.reflections)} reflexiones de IA desde {TRADE_MEMORY_PATH}")
            except Exception as e:
                logger.error(f"Error cargando {TRADE_MEMORY_PATH}: {e}")

    def _save_memory(self):
        """Persiste las reflexiones y pesos en disco."""
        try:
            data = {
                "updated_at": time.time(),
                "reflections": [asdict(r) for r in self.reflections],
                "strategy_weights": self.strategy_weights,
                "strategy_brier_scores": self.strategy_brier_scores,
            }
            with open(TRADE_MEMORY_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando {TRADE_MEMORY_PATH}: {e}")

    def calculate_brier_score(self, forecasts: List[float], outcomes: List[int]) -> float:
        """Calcula el Brier Score básico."""
        if not forecasts or len(forecasts) != len(outcomes):
            return 0.25
        n = len(forecasts)
        return sum((f - o) ** 2 for f, o in zip(forecasts, outcomes)) / n

    def calculate_murphy_decomposition(self, forecasts: List[float], outcomes: List[int], num_bins: int = 5) -> MurphyDecomposition:
        """
        Calcula la descomposición matemática de Murphy del Brier Score:
        BS = Confiabilidad - Resolución + Incertidumbre
        """
        n = len(forecasts)
        if n == 0:
            return MurphyDecomposition(0.25, 0.0, 0.0, 0.25, 0, 0.0)

        # Base rate
        base_rate = sum(outcomes) / n
        uncertainty = base_rate * (1.0 - base_rate)
        if uncertainty <= 0:
            uncertainty = 0.001

        # Agrupar en bins
        bins: Dict[int, Dict[str, Any]] = {
            i: {"forecasts": [], "outcomes": []} for i in range(num_bins)
        }

        for f, o in zip(forecasts, outcomes):
            # Bin index de 0 a num_bins - 1
            idx = min(int(f * num_bins), num_bins - 1)
            bins[idx]["forecasts"].append(f)
            bins[idx]["outcomes"].append(o)

        reliability = 0.0
        resolution = 0.0

        for b in bins.values():
            n_k = len(b["forecasts"])
            if n_k == 0:
                continue
            mean_f_k = sum(b["forecasts"]) / n_k
            mean_o_k = sum(b["outcomes"]) / n_k

            reliability += (n_k / n) * ((mean_f_k - mean_o_k) ** 2)
            resolution += (n_k / n) * ((mean_o_k - base_rate) ** 2)

        # Brier Score recalculado por identidad
        bs = reliability - resolution + uncertainty
        # BSS (Brier Skill Score frente al azar/climatología)
        bss = 1.0 - (bs / uncertainty) if uncertainty > 0 else 0.0

        return MurphyDecomposition(
            brier_score=round(bs, 4),
            reliability=round(reliability, 4),
            resolution=round(resolution, 4),
            uncertainty=round(uncertainty, 4),
            sample_size=n,
            brier_skill_score=round(bss, 4),
        )

    def analyze_trade_post_mortem(self, trade: Dict[str, Any]) -> Optional[TradeReflection]:
        """
        Analiza una operación cerrada y, si fue perdedora o con slippage alto,
        genera una reflexión estructurada para auto-aprendizaje.
        """
        status = trade.get("status", "")
        pnl = float(trade.get("realized_pnl_usd", trade.get("pnl", 0.0)))
        entry_price = float(trade.get("entry_price", 0.50))
        predicted_prob = float(trade.get("confidence", 0.80))
        if predicted_prob > 1.0:
            predicted_prob /= 100.0
        predicted_prob = max(0.01, min(0.99, predicted_prob))
        strategy_id = trade.get("strategy_code") or trade.get("strategy_id", "UNKNOWN")
        market_title = trade.get("market_question", trade.get("market_title", "Mercado Desconocido"))
        trade_id = trade.get("trade_id") or trade.get("id", str(time.time()))

        outcome = 1 if pnl > 0 else 0
        brier_err = (predicted_prob - outcome) ** 2

        # Solo reflexionamos en pérdidas o trades con error Brier significativo (> 0.25)
        if outcome == 1 and brier_err < 0.25:
            return None

        # Diagnóstico de Causa Raíz
        root_cause = "Divergencia de mercado estándar"
        actionable_rule = "Mantener monitorización habitual."

        if "STOP_LOSS" in status or pnl < -15.0:
            if "S20" in strategy_id:
                root_cause = "Resolución con retardo del oráculo o disputa UMA no anticipada."
                actionable_rule = "Aumentar margen de seguridad a 98% y verificar oráculo UMA antes de entrar."
            elif "S10" in strategy_id:
                root_cause = "Sesgo de YES sobre-castigado por noticias imprevistas."
                actionable_rule = "Exigir spread de CLOB < 0.03 y volumen 24h > $25k en S10."
            elif "S24" in strategy_id:
                root_cause = "Falso desbalance de libro de órdenes absorbido por ballena institucional."
                actionable_rule = "Reducir ventana de reversión de 3m a 90s y aplicar stop más ceñido (-1.5%)."
            elif "TIME_STOP" in status:
                root_cause = "Contrato ilíquido que no convergió al precio objetivo antes del límite de 48h."
                actionable_rule = "Filtrar únicamente contratos con vencimiento < 24h o volumen diario > $50,000."
            else:
                root_cause = "Volatilidad adversa en el desenlace del evento."
                actionable_rule = "Reducir tamaño Kelly fraccional en este cluster de mercado."

        reflection = TradeReflection(
            id=f"REFL-{int(time.time() * 1000)}",
            trade_id=trade_id,
            strategy_id=strategy_id,
            market_title=market_title[:80],
            predicted_prob=round(predicted_prob, 3),
            outcome=outcome,
            pnl=round(pnl, 2),
            brier_error=round(brier_err, 4),
            root_cause=root_cause,
            actionable_rule=actionable_rule,
            created_at=time.time(),
        )

        self.reflections.insert(0, reflection)
        if len(self.reflections) > 200:
            self.reflections.pop()

        self._save_memory()
        logger.info(f"🧠 [IA REFLEXIÓN] Trade {trade_id} ({strategy_id}): {root_cause} | Regla: {actionable_rule}")
        # Disparo inmediato de auto-recalibración 100% automático al registrar una nueva reflexión
        try:
            from paper_tracker import paper_tracker, paper_tracker_research
            all_closed = [t for t in paper_tracker.trades.values() if t.get("status") in ("WON", "LOST")]
            all_closed += [t for t in paper_tracker_research.trades.values() if t.get("status") in ("WON", "LOST")]
            self.run_daily_calibration(all_closed)
        except Exception:
            pass
        return reflection

    def run_daily_calibration(self, closed_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Ejecuta el ciclo diario de auto-aprendizaje:
        1. Recalibra probabilidades con PAVA Isotonic.
        2. Descompone Brier Score global con Murphy.
        3. Calcula métricas por estrategia y ajusta ponderaciones de Kelly.
        """
        now = time.time()
        # Calibrar SOLO con operaciones reales cerradas (sin baseline sintético).
        effective_trades = list(closed_trades)
        if not effective_trades:
            self.last_calibration_time = now
            return {
                "status": "NO_DATA",
                "calibrated_at": now,
                "message": "Sin trades reales cerrados todavía; no se generan métricas sintéticas.",
            }

        all_forecasts = []
        all_outcomes = []
        strat_data: Dict[str, Dict[str, List]] = {}

        for tr in effective_trades:
            prob = float(tr.get("confidence", 0.75))
            if prob > 1.0:
                prob = prob / 100.0
            prob = max(0.01, min(0.99, prob))
            pnl = float(tr.get("realized_pnl_usd", tr.get("pnl", 0.0)))
            strat = tr.get("strategy_code") or tr.get("strategy_id", "GENERIC")
            outcome = 1 if pnl > 0 else 0

            all_forecasts.append(prob)
            all_outcomes.append(outcome)

            if strat not in strat_data:
                strat_data[strat] = {"forecasts": [], "outcomes": [], "pnls": []}
            strat_data[strat]["forecasts"].append(prob)
            strat_data[strat]["outcomes"].append(outcome)
            strat_data[strat]["pnls"].append(pnl)

        # 1. Ajustar calibrador PAVA
        self.calibrator.fit(all_forecasts, all_outcomes)

        # 2. Descomposición de Murphy global
        self.global_murphy = self.calculate_murphy_decomposition(all_forecasts, all_outcomes)

        # 3. Optimización de Pesos Kelly por Estrategia
        new_weights = {}
        brier_breakdown = {}

        for strat, data in strat_data.items():
            s_fc = data["forecasts"]
            s_oc = data["outcomes"]
            s_bs = self.calculate_brier_score(s_fc, s_oc)
            s_winrate = sum(s_oc) / len(s_oc) if s_oc else 0.5
            s_murphy = self.calculate_murphy_decomposition(s_fc, s_oc, num_bins=3)

            # Fórmula de Kelly Adaptativa basada en Brier Score:
            # Si BS < 0.10 y WinRate > 75%, el multiplicador sube hasta 1.45x
            # Si BS > 0.20 o WinRate < 60%, el multiplicador baja hasta 0.40x
            base_multiplier = 1.0
            brier_advantage = (0.18 - s_bs) * 2.0  # Positivo si BS < 0.18
            winrate_advantage = (s_winrate - 0.65) * 1.5

            raw_mult = base_multiplier + brier_advantage + winrate_advantage
            clamped_mult = round(min(max(raw_mult, 0.40), 1.60), 2)
            new_weights[strat] = clamped_mult

            brier_breakdown[strat] = {
                "brier_score": round(s_bs, 4),
                "win_rate": round(s_winrate * 100.0, 1),
                "trades_count": len(s_oc),
                "kelly_multiplier": clamped_mult,
                "reliability": s_murphy.reliability,
                "resolution": s_murphy.resolution,
                "status": "BOOSTED" if clamped_mult > 1.10 else ("REDUCED" if clamped_mult < 0.85 else "OPTIMAL"),
            }

        self.strategy_weights = new_weights
        self.strategy_brier_scores = brier_breakdown
        self.last_calibration_time = now
        self._save_memory()

        logger.info(f"✅ Ciclo de Auto-Aprendizaje completado. Brier Global: {self.global_murphy.brier_score} | Pesos calibrados para {len(new_weights)} estrategias.")

        return {
            "status": "SUCCESS",
            "calibrated_at": now,
            "global_murphy": asdict(self.global_murphy) if self.global_murphy else None,
            "strategy_weights": self.strategy_weights,
            "strategy_metrics": self.strategy_brier_scores,
            "reflections_count": len(self.reflections),
        }

    def _generate_baseline_history(self) -> List[Dict[str, Any]]:
        """Genera historial base coherente para inicializar el modelo en frío."""
        baselines = []
        specs = [
            ("S20", 0.96, 0.94, 25),
            ("S21", 0.92, 0.90, 20),
            ("S12", 0.88, 0.87, 18),
            ("S22", 0.84, 0.82, 15),
            ("S23", 0.82, 0.80, 14),
            ("S05", 0.85, 0.83, 16),
            ("S24", 0.76, 0.74, 22),
            ("S10", 0.78, 0.75, 18),
            ("S02", 0.80, 0.79, 12),
            ("S03", 0.79, 0.77, 12),
            ("BA", 0.75, 0.73, 10),
            ("MR", 0.74, 0.72, 10),
            ("MM", 0.72, 0.70, 10),
            ("FLB", 0.76, 0.74, 10),
            ("WT", 0.77, 0.75, 10),
        ]
        t_now = time.time()
        for strat, prob, actual_rate, count in specs:
            wins = int(count * actual_rate)
            losses = count - wins
            for _ in range(wins):
                baselines.append({
                    "id": f"BASE-{strat}-W",
                    "strategy_id": strat,
                    "confidence": prob,
                    "pnl": 24.50,
                    "market_title": f"Market calibrated for {strat}",
                    "closed_at": t_now,
                })
            for _ in range(losses):
                baselines.append({
                    "id": f"BASE-{strat}-L",
                    "strategy_id": strat,
                    "confidence": prob,
                    "pnl": -18.20,
                    "market_title": f"Market calibrated for {strat}",
                    "closed_at": t_now,
                })
        return baselines

    def get_strategy_multiplier(self, strategy_id: str) -> float:
        """Devuelve el multiplicador de Kelly asignado a una estrategia."""
        return self.strategy_weights.get(strategy_id, 1.0)

    def calibrate_probability(self, raw_prob: float) -> float:
        """Calibra una probabilidad cruda usando la regresión isotónica ajustada."""
        return round(self.calibrator.predict_proba(raw_prob), 4)

    def get_status(self) -> Dict[str, Any]:
        """Devuelve el resumen de métricas para la UI y API."""
        return {
            "last_calibration_time": self.last_calibration_time,
            "global_murphy": asdict(self.global_murphy) if self.global_murphy else None,
            "strategy_metrics": self.strategy_brier_scores,
            "reflections": [asdict(r) for r in self.reflections[:30]],
            "reflections_total": len(self.reflections),
        }


# Instancia singleton del motor de auto-aprendizaje
ai_learning_engine = AILearningEngine()
