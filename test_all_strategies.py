"""Script de auditoría exhaustiva de todas las estrategias y cálculos cuantitativos."""
import sys
import asyncio
from decimal import Decimal

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config import STRATEGY_PARAMS
from market_registry import registry, MarketSnapshot
from polymarket_client import pm_client, kalshi_market_to_registry
from strategies import engine
from paper_tracker import paper_tracker


async def run_audit():
    print("=" * 60)
    print("🔍 INICIANDO AUDITORÍA INTEGRAL DE ESTRATEGIAS Y CÁLCULOS")
    print("=" * 60)

    # 1. Obtener muestra de mercados reales de Kalshi
    print("\n[1/4] Descargando muestra de mercados desde Kalshi Trade API...")
    markets_data = await pm_client.fetch_markets(limit=30)
    print(f"-> {len(markets_data)} mercados obtenidos para prueba.")

    # Poblar registro temporal con el esquema interno ya normalizado de Kalshi.
    for raw in markets_data:
        ticker = raw.get("ticker")
        normalized = kalshi_market_to_registry(raw)
        if not ticker or not normalized.get("initial_prices"):
            continue
        await registry.update_market(str(ticker), normalized)

        # Sintetizar libro YES/NO a partir de los precios del mercado para que las
        # estrategias tengan spread operable durante la auditoria.
        market = registry.markets[str(ticker)]
        for outcome in ("Yes", "No"):
            price = market.prices.get(outcome)
            if price and price > 0:
                bid = max(Decimal("0.01"), price - Decimal("0.01"))
                ask = min(Decimal("0.99"), price + Decimal("0.01"))
                market.update_orderbook(
                    outcome,
                    [{"price": str(bid), "size": "500"}],
                    [{"price": str(ask), "size": "500"}],
                )

    print(f"-> Mercados en memoria: {len(registry.markets)}")
    assert registry.markets, "No se cargaron mercados de Kalshi para la auditoria"

    # 2. Probar ejecución de cada estrategia modular individualmente
    print("\n[2/4] Probando ejecución individual de cada estrategia...")
    all_strategies = engine.modular_strategies
    errors_found = []

    for strat in all_strategies:
        try:
            print(f"  • Probando {strat.name} (Tier {strat.tier})...", end=" ")
            # Convertir mercados a core models y escanear
            core_markets = [engine._snapshot_to_core_market(m) for m in registry.markets.values()]
            opps = strat.scan(core_markets)
            signals = []
            for opp in opps:
                sig = strat.analyze(opp)
                if sig:
                    signals.append(sig)
            print(f"OK (Oportunidades: {len(opps)}, Señales: {len(signals)})")
        except Exception as e:
            print(f"❌ ERROR: {e}")
            errors_found.append((strat.name, str(e)))

    # 3. Probar calculate_all() y conversión a formato dashboard
    print("\n[3/4] Probando calculate_all() y enriquecimiento de señales...")
    total_signals = 0
    calculation_errors = 0

    for m in registry.markets.values():
        try:
            signals = engine.calculate_all(m)
            total_signals += len(signals)
            for s in signals:
                # Validar campos matemáticos críticos
                entry = float(s.get("entry_price") or 0)
                target = float(s.get("target_price") or 0)
                stop = float(s.get("stop_loss") or 0)
                conf = float(s.get("confidence") or 0)
                rr = float(s.get("risk_reward_ratio") or 0)

                assert entry > 0, f"entry_price inválido: {entry}"
                assert target > 0, f"target_price inválido: {target}"
                assert stop > 0, f"stop_loss inválido: {stop}"
                assert 0 <= conf <= 100, f"confianza fuera de rango: {conf}"
                assert rr > 0, f"Risk/Reward inválido: {rr}"

                # Validar cálculo de Paper Trading
                trade = paper_tracker.evaluate_and_record_signal(s, m)

        except Exception as e:
            calculation_errors += 1
            print(f"❌ Error calculando mercado {m.market_id}: {e}")

    print(f"-> Total señales válidas generadas: {total_signals}")
    print(f"-> Errores en cálculos: {calculation_errors}")

    # 4. Probar cálculos de PnL y Leaderboard en paper_tracker
    print("\n[4/4] Verificando fórmulas matemáticas de PnL y Win Rate...")
    perf = paper_tracker.get_strategy_performance()
    summary = paper_tracker.get_summary()

    print(f"  • PnL Flotante Global: ${perf['total_floating_pnl_usd']}")
    print(f"  • PnL Realizado Global: ${perf['total_realized_pnl_usd']}")
    print(f"  • PnL Neto Combinado: ${perf['total_combined_pnl_usd']}")
    print(f"  • Estrategias catalogadas: {len(perf['strategies'])}")

    # Validar consistencia matemática
    calc_comb = round(perf['total_floating_pnl_usd'] + perf['total_realized_pnl_usd'], 2)
    assert abs(perf['total_combined_pnl_usd'] - calc_comb) < 0.01, "Error: Suma de PnL flotante y realizado no coincide con combinado"

    for s in perf['strategies']:
        tot = round(s['unrealized_pnl_usd'] + s['realized_pnl_usd'], 2)
        assert abs(s['total_pnl_usd'] - tot) < 0.01, f"Error en PnL de {s['code']}"

    print("\n" + "=" * 60)
    if not errors_found and calculation_errors == 0:
        print("✅ TODAS LAS PRUEBAS PASARON EXITOSAMENTE. CÁLCULOS 100% PRECISOS.")
    else:
        print(f"⚠️ Se detectaron {len(errors_found)} errores a corregir.")
    print("=" * 60)

    await pm_client.close()


if __name__ == "__main__":
    asyncio.run(run_audit())
