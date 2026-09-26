"""
test_ai_and_lead_lag.py
=======================
Pruebas cuantitativas y de verificación para:
- Motor Lead-Lag de Latencia (Binance vs CLOB)
- Agente IA de Auto-Aprendizaje, Brier Score y Descomposición de Murphy
- Inferencia Bayesiana de Noticias
- Integración en Paper Tracker y Kelly Sizing
"""

import time
import math
from lead_lag_engine import lead_lag_engine, LeadLagEngine
from ai_learning_engine import ai_learning_engine, AILearningEngine, PAVAIsotonicCalibrator
from news_oracle_agent import news_oracle_agent, NewsOracleAgent
from paper_tracker import calculate_kelly_size, paper_tracker


def test_lead_lag_engine():
    print("[TEST 1/4] Probando Motor Lead-Lag...")
    engine = LeadLagEngine()
    now = time.time()

    # Simular ticks con aceleración alcista (+0.54% en 10 segundos)
    engine._record_price("BTC", 65000.0, now - 12)
    engine._record_price("BTC", 65000.0, now - 10)
    engine._record_price("BTC", 65150.0, now - 5)
    engine._record_price("BTC", 65350.0, now)

    vel_10s = engine.get_velocity("BTC", 10.0)
    print(f"  -> BTC Precio: 65,350 | Velocidad 10s: {vel_10s:+.3f}%")
    assert vel_10s > 0.40, f"Velocidad esperada > 0.40%, obtenido {vel_10s}"

    # Inyectar contrato flash
    engine.register_polymarket_contracts([{
        "market_id": "test_btc_flash",
        "crypto_symbol": "BTC",
        "question": "Will BTC be above $65,200 in 15m?",
        "condition_id": "0xtest_cond",
        "strike_price": 65200.0,
        "clob_ask": 0.48,
    }])

    engine._evaluate_lead_lag_opportunity("BTC")
    opps = engine.active_opportunities
    print(f"  -> Oportunidades detectadas: {len(opps)}")
    assert len(opps) >= 1, "Debería detectar oportunidad por breakout en Binance antes del CLOB"
    opp = opps[0]
    assert opp.edge_pct > 8.0, "El edge matemático debe superar el 8%"
    assert opp.status == "EXECUTED_AUTO", f"La oportunidad debe ejecutarse de forma 100% automática, obtenido {opp.status}"
    print(f"  -> Disparo 100% Autónomo: Status = {opp.status}")
    print("  [OK] Motor Lead-Lag y Auto-Sniper validados exitosamente.\n")


def test_ai_learning_murphy():
    print("[TEST 2/4] Probando Brier Score y Descomposición de Murphy...")
    engine = AILearningEngine()

    # Pronósticos y desenlaces sintéticos conocidos
    forecasts = [0.90, 0.85, 0.80, 0.70, 0.60, 0.40, 0.20, 0.10]
    outcomes =  [1,    1,    1,    1,    0,    0,    0,    0]

    murphy = engine.calculate_murphy_decomposition(forecasts, outcomes, num_bins=4)
    print(f"  -> Brier Score: {murphy.brier_score}")
    print(f"  -> Reliability (Confiabilidad): {murphy.reliability}")
    print(f"  -> Resolution (Resolución): {murphy.resolution}")
    print(f"  -> Uncertainty (Incertidumbre): {murphy.uncertainty}")

    # Identidad matemática: BS = Rel - Res + Unc
    expected_bs = murphy.reliability - murphy.resolution + murphy.uncertainty
    diff = abs(murphy.brier_score - expected_bs)
    print(f"  -> Verificación de Identidad Murphy (|BS - (Rel-Res+Unc)|): {diff:.6f}")
    assert diff < 0.001, f"Identidad de Murphy violada por {diff}"
    print("  [OK] Descomposición de Murphy validada matemáticamente.\n")


def test_isotonic_regression():
    print("[TEST 3/4] Probando Regresión Isotónica PAVA...")
    calibrator = PAVAIsotonicCalibrator()
    # Datos ruidosos no monótonos
    preds = [0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95]
    outs =  [0,   1,   0,    1,   0,    1,   1]

    calibrator.fit(preds, outs)
    cal_02 = calibrator.predict_proba(0.2)
    cal_08 = calibrator.predict_proba(0.8)
    print(f"  -> Raw 0.20 => Calibrado: {cal_02:.4f}")
    print(f"  -> Raw 0.80 => Calibrado: {cal_08:.4f}")
    assert cal_08 >= cal_02, "La regresión isotónica DEBE ser monótona no decreciente"
    print("  [OK] Regresión Isotónica PAVA validada.\n")


def test_bayesian_news_and_kelly():
    print("[TEST 4/4] Probando Inferencia Bayesiana de Noticias y Kelly Adaptativo...")
    agent = NewsOracleAgent()
    sentiment_bull = agent._calculate_sentiment("Bitcoin surges to new record high as Fed approves rate cut")
    print(f"  -> Sentimiento titular alcista: {sentiment_bull}")
    assert sentiment_bull > 0.3, "Sentimiento debió ser marcadamente alcista"

    prior = 0.50
    post = agent._compute_bayesian_posterior(prior, sentiment_bull)
    edge = (post - prior) * 100.0
    print(f"  -> Prior: {prior} => Posterior: {post} | Bayesian Edge: +{edge:.1f}%")
    assert post > prior, "Posterior Bayesiano debe subir tras noticia alcista"

    # Auto-Aprendizaje: Calibración y Kelly Multipliers
    test_trades = [
        {"strategy_id": "S20", "confidence": 0.96, "pnl": 45.0, "status": "WON"},
        {"strategy_id": "S20", "confidence": 0.95, "pnl": 35.0, "status": "WON"},
        {"strategy_id": "S20", "confidence": 0.97, "pnl": 50.0, "status": "WON"},
        {"strategy_id": "S24", "confidence": 0.74, "pnl": -15.0, "status": "LOST"},
        {"strategy_id": "S24", "confidence": 0.72, "pnl": -18.0, "status": "LOST"},
    ]
    cal_res = ai_learning_engine.run_daily_calibration(test_trades)
    s20_mult = ai_learning_engine.get_strategy_multiplier("S20")
    s24_mult = ai_learning_engine.get_strategy_multiplier("S24")
    print(f"  -> S20 Kelly Multiplier (Alta precisión): {s20_mult}x")
    print(f"  -> S24 Kelly Multiplier (Baja precisión): {s24_mult}x")
    assert s20_mult > s24_mult, "S20 con mayor acierto debe recibir mayor multiplicador Kelly que S24"

    # Reflexión post-mortem
    losing_trade = {
        "id": "T-LOST-1",
        "strategy_id": "S20",
        "market_title": "Will Oracle resolve at 5pm?",
        "confidence": 0.96,
        "entry_price": 0.92,
        "pnl": -92.0,
        "status": "STOP_LOSS",
    }
    refl = ai_learning_engine.analyze_trade_post_mortem(losing_trade)
    assert refl is not None, "Debería generar reflexión post-mortem en pérdida de alta confianza"
    print(f"  -> Reflexión generada: {refl.root_cause} | Regla: {refl.actionable_rule}")
    print("  [OK] Inferencia Bayesiana, Kelly y Memoria Post-Mortem validados con éxito.\n")


if __name__ == "__main__":
    print("=== INICIANDO PRUEBAS CUANTITATIVAS AVANZADAS ===")
    test_lead_lag_engine()
    test_ai_learning_murphy()
    test_isotonic_regression()
    test_bayesian_news_and_kelly()
    print("[EXITO TOTAL] TODAS LAS PRUEBAS CUANTITATIVAS PASARON AL 100%!")
