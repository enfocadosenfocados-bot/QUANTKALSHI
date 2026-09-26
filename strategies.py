"""Motor de estrategias quant para Polymarket"""
from decimal import Decimal
from typing import Dict, List, Optional, Any
import uuid

from config import STRATEGY_PARAMS, MIN_SIGNAL_ENTRY_PRICE, MAX_SIGNAL_RISK_REWARD
from core_models import Market, Signal
from market_registry import MarketSnapshot, order_price, order_size, registry
from s02_weather_noaa import WeatherNOAA
from s03_nothing_ever_happens import NothingEverHappens
from s05_negrisk_rebalancing import NegRiskRebalancing
from s10_yes_bias import YesBiasExploitation
from s12_high_prob_harvesting import HighProbabilityHarvesting
from s20_oracle_delay_sniping import OracleDelaySniping
from s21_conditional_arbitrage import ConditionalArbitrage
from s22_news_latency_sniping import FastNewsSniping
from s23_resolution_rules_lawyer import ResolutionRulesLawyer
from s24_order_flow_imbalance import OrderFlowImbalance
from position_side import is_long_side


class StrategyEngine:
    """Motor de cálculo de estrategias en tiempo real"""

    MODULAR_STRATEGY_LABELS = {
        "s02_weather_noaa": "S02: Weather NOAA",
        "s03_nothing_ever_happens": "S03: Nothing Ever Happens",
        "s05_negrisk_rebalancing": "S05: NegRisk Rebalancing",
        "s10_yes_bias": "S10: Yes Bias",
        "s12_high_prob_harvesting": "S12: High Prob Harvesting",
        "s20_oracle_delay_sniping": "S20: Oracle Delay Sniping",
        "s21_conditional_arbitrage": "S21: Arbitraje Condicional",
        "s22_news_latency_sniping": "S22: Fast News Sniping",
        "s23_resolution_rules_lawyer": "S23: Rules-Lawyer",
        "s24_order_flow_imbalance": "S24: Order Flow Imbalance",
    }

    def __init__(self):
        self.params = STRATEGY_PARAMS
        self.modular_strategies = [
            WeatherNOAA(),
            NothingEverHappens(),
            NegRiskRebalancing(),
            YesBiasExploitation(),
            HighProbabilityHarvesting(),
            OracleDelaySniping(),
            ConditionalArbitrage(),
            FastNewsSniping(),
            ResolutionRulesLawyer(),
            OrderFlowImbalance(),
        ]

    @staticmethod
    def _clamp_probability(value: Decimal) -> Decimal:
        return max(Decimal("0.001"), min(Decimal("0.999"), value))

    @staticmethod
    def _safe_profit_bps(entry: Decimal, target: Decimal) -> int:
        if entry <= 0:
            return 0
        return int(abs((target - entry) / entry) * 10000)

    def calculate_all(self, market: MarketSnapshot) -> List[Dict]:
        """Calcular todas las estrategias para un mercado"""
        signals = []

        # Solo mercados activos con liquidez. Kalshi devuelve mercados que aun no
        # abren (`initialized`) sin libro real; operar sobre ellos genera ruido.
        if market.closed or market.resolved or not market.active:
            return signals

        # Estrategia A: Market Making
        sig = self._market_making(market)
        if sig:
            signals.append(sig)

        # Estrategia B: Bundle Arbitrage
        sig = self._bundle_arbitrage(market)
        if sig:
            signals.append(sig)

        # Estrategia C: Mean Reversion
        sig = self._mean_reversion(market)
        if sig:
            signals.append(sig)

        # Estrategia D: Favorite-Longshot Bias
        sig = self._favorite_longshot(market)
        if sig:
            signals.append(sig)

        # Estrategia E: External Data (placeholder - requiere fuentes externas)
        sig = self._external_data(market)
        if sig:
            signals.append(sig)

        # Estrategia F: Whale Tracking con Data API pública de trades/positions
        sig = self._whale_tracking(market)
        if sig:
            signals.append(sig)

        # Estrategias modulares S02/S03/S05/S10/S12
        signals.extend(self._calculate_modular_strategies(market))

        # Las estrategias legacy (A/C/D/E/F) no definian target/stop; se normalizan
        # aqui para que el dashboard, el Paper Tracker y el gobernador de riesgo
        # reciban siempre precios de salida validos.
        normalized = (self._ensure_risk_fields(market, sig) for sig in signals)
        return [sig for sig in normalized if sig]

    def _risk_price_for_outcome(self, m: MarketSnapshot, token: str) -> Decimal:
        """Precio de referencia para un token, tolerando etiquetas BOTH/BOTH_BUNDLE."""
        for candidate in (token, "Yes", "No"):
            if not candidate:
                continue
            price = self._decimal_to_float(self._market_price_for_outcome(m, candidate))
            if price > 0:
                return Decimal(str(price))
        return Decimal("0")

    def _ensure_risk_fields(self, m: MarketSnapshot, signal: Dict) -> Optional[Dict]:
        """Garantiza entry_price, target_price, stop_loss y risk_reward_ratio validos."""
        token = str(signal.get("token") or "")
        side = str(signal.get("side") or "BUY").upper()

        entry = self._decimal_to_float(signal.get("entry_price"))
        if entry <= 0 or entry >= 1:
            reference = self._risk_price_for_outcome(m, token)
            entry = float(reference)
        if entry <= 0:
            return None
        entry = max(0.001, min(0.999, entry))
        # Contratos sub-centavo generan edges irreales con modelos simples.
        if entry < MIN_SIGNAL_ENTRY_PRICE:
            return None

        edge = self._decimal_to_float(signal.get("edge"))
        edge = max(0.02, abs(edge)) if edge else 0.02

        target = self._decimal_to_float(signal.get("target_price"))
        stop = self._decimal_to_float(signal.get("stop_loss"))

        if target <= 0 or target >= 1:
            if side in ("SELL", "SELL_BUNDLE"):
                target = max(0.01, entry - max(0.04, edge))
            elif side in ("BOTH", "BUY_BUNDLE"):
                # Market making / bundle: captura de spread, no movimiento direccional.
                target = min(0.999, entry + 0.02)
            else:
                target = min(0.999, entry + max(0.04, edge))

        if stop <= 0 or stop >= 1:
            if side in ("SELL", "SELL_BUNDLE"):
                stop = min(0.999, entry + 0.04)
            elif side in ("BOTH", "BUY_BUNDLE"):
                stop = max(0.001, entry - 0.02)
            else:
                stop = max(0.001, entry - max(0.04, entry * 0.08))

        target = max(0.001, min(0.999, target))
        stop = max(0.001, min(0.999, stop))

        risk = abs(entry - stop)
        reward = abs(target - entry)
        signal["entry_price"] = f"{entry:.4f}"
        signal["target_price"] = f"{target:.4f}"
        signal["stop_loss"] = f"{stop:.4f}"
        raw_rr = (reward / risk) if risk > 0 else 1.5
        signal["risk_reward_ratio"] = round(min(raw_rr, MAX_SIGNAL_RISK_REWARD), 2)
        signal.setdefault("strategy_code", "GEN")
        signal.setdefault("status", "ACTIVE")
        signal.setdefault("token", token or "Yes")
        signal.setdefault("market_price", round(entry, 6))
        signal.setdefault("estimated_prob", round(target, 6))
        signal["edge"] = round(reward, 6)
        return signal

    @staticmethod
    def _decimal_to_float(value: Any, default: float = 0.0) -> float:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _market_price_for_outcome(self, m: MarketSnapshot, outcome: str) -> float:
        """Obtener precio actual robusto para un outcome."""
        for source in (m.mid_price, m.prices, m.last_trade, m.best_ask, m.best_bid):
            price = self._decimal_to_float(source.get(outcome))
            if price > 0:
                return price

        if outcome.lower() in ("yes", "no"):
            inverse = "No" if outcome.lower() == "yes" else "Yes"
            for source in (m.mid_price, m.prices, m.last_trade, m.best_ask, m.best_bid):
                inverse_price = self._decimal_to_float(source.get(inverse))
                if inverse_price > 0:
                    return max(0.0, min(1.0, 1.0 - inverse_price))

        return 0.0

    def _snapshot_to_core_market(self, m: MarketSnapshot) -> Market:
        """Adaptar MarketSnapshot al modelo estable usado por las estrategias."""
        tokens = []
        for outcome in m.outcomes:
            token_id = str(m.token_ids.get(outcome, ""))
            price = self._market_price_for_outcome(m, outcome)
            tokens.append({
                "outcome": outcome,
                "token_id": token_id,
                "tokenId": token_id,
                "price": price,
                "best_bid": self._decimal_to_float(m.best_bid.get(outcome)),
                "best_ask": self._decimal_to_float(m.best_ask.get(outcome)),
                "mid_price": self._decimal_to_float(m.mid_price.get(outcome)),
            })

        end_date_iso = None
        if m.end_date:
            end_date_iso = m.end_date.isoformat() if hasattr(m.end_date, "isoformat") else str(m.end_date)
        close_time_iso = None
        if m.close_time:
            close_time_iso = m.close_time.isoformat() if hasattr(m.close_time, "isoformat") else str(m.close_time)

        # Contexto de evento Kalshi: los mercados binarios tienen 2 outcomes cada uno,
        # asi que las estrategias de canasta/escalera necesitan ver a los hermanos
        # del mismo `event_ticker` (S05 multiresultado, S21 escaleras).
        siblings = []
        if m.event_ticker:
            for sib in registry.event_siblings(m.event_ticker, exclude_market_id=m.market_id):
                if not sib.active or sib.closed or sib.resolved:
                    continue
                sib_yes = self._market_price_for_outcome(sib, "Yes")
                if sib_yes <= 0:
                    continue
                sib_tokens = []
                for outcome in sib.outcomes:
                    tid = str(sib.token_ids.get(outcome, ""))
                    sib_tokens.append({
                        "outcome": outcome,
                        "token_id": tid,
                        "tokenId": tid,
                        "price": self._market_price_for_outcome(sib, outcome),
                        "best_bid": self._decimal_to_float(sib.best_bid.get(outcome)),
                        "best_ask": self._decimal_to_float(sib.best_ask.get(outcome)),
                    })
                siblings.append({
                    "market_id": sib.market_id,
                    "condition_id": sib.condition_id,
                    "question": sib.question,
                    "subtitle": sib.subtitle,
                    "yes_price": sib_yes,
                    "volume": self._decimal_to_float(sib.volume_24h),
                    "open_interest": sib.open_interest,
                    "tokens": sib_tokens,
                })

        return Market(
            condition_id=m.market_id or m.condition_id,
            question=m.question,
            category=m.category or "Other",
            tokens=tokens,
            volume=self._decimal_to_float(m.volume_24h),
            volume_24h=self._decimal_to_float(m.volume_24h),
            liquidity=self._decimal_to_float(m.liquidity),
            active=m.active and not m.closed and not m.resolved,
            end_date_iso=end_date_iso,
            description=m.resolution_source,
            resolution_source=m.resolution_source,
            event_id=m.event_ticker or m.condition_id,
            mutually_exclusive=m.mutually_exclusive,
            strike_type=m.strike_type,
            close_time_iso=close_time_iso,
            settlement_sources=list(m.settlement_sources or []),
            event_siblings=siblings,
        )
    @staticmethod
    def _strategy_code(strategy_name: str) -> str:
        return strategy_name.split("_", 1)[0].upper() if strategy_name else "MOD"

    @staticmethod
    def _confidence_pct(confidence: float) -> int:
        return max(0, min(99, int(round(confidence * 100 if confidence <= 1 else confidence))))

    @staticmethod
    def _clean_metric_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): StrategyEngine._clean_metric_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [StrategyEngine._clean_metric_value(v) for v in value]
        return str(value)

    def _outcome_for_token(self, m: MarketSnapshot, token_id: str) -> str:
        for outcome, tid in m.token_ids.items():
            if str(tid) == str(token_id):
                return outcome
        return token_id or "-"

    def _display_edge(self, signal: Signal) -> float:
        edge = signal.edge
        if edge >= 0:
            return edge
        if signal.strategy_name == "s05_negrisk_rebalancing" and signal.metadata.get("overprice"):
            return abs(edge)
        return 0.0

    def _modular_trigger_reason(self, signal: Signal) -> str:
        edge = self._display_edge(signal)
        metadata = signal.metadata or {}

        if signal.strategy_name == "s05_negrisk_rebalancing":
            total = float(metadata.get("total_basket_price", 0) or 0)
            overprice = float(metadata.get("overprice", 0) or 0)
            return (
                f"Canasto multi-resultado YES suma {total:.4f}; sobreprecio {overprice * 100:.2f}%. "
                "Arbitraje/rebalanceo matemático detectado."
            )
        if signal.strategy_name == "s02_weather_noaa":
            side = metadata.get("side_chosen", "YES")
            target = metadata.get("target_temp", "N/A")
            mu = metadata.get("forecast_mu", "N/A")
            return f"Modelo meteorológico favorece {side}. Umbral: {target}; forecast estimado: {mu}. Edge: {edge * 100:.2f}%."
        if signal.strategy_name == "s03_nothing_ever_happens":
            return f"Evento dramático detectado; sesgo histórico anti-dramatismo favorece BUY NO. Edge: {edge * 100:.2f}%."
        if signal.strategy_name == "s10_yes_bias":
            viral = "viral" if metadata.get("viral_hype") else "alto volumen"
            return f"YES potencialmente sobrecomprado por narrativa {viral}; estrategia favorece BUY NO. Edge: {edge * 100:.2f}%."
        if signal.strategy_name == "s12_high_prob_harvesting":
            return (
                f"Contrato de alta probabilidad cerca de resolución. Quedan {metadata.get('days_left', 'N/A')} días; "
                f"yield anualizado estimado {metadata.get('annualized_yield', 'N/A')}."
            )
        if signal.strategy_name == "s20_oracle_delay_sniping":
            return f"Descuento UMA de liquidación: contrato cotiza a ${signal.market_price:.3f} con liquidación esperada a $1.00 (-{metadata.get('discount_pct', 0)}% off)."
        if signal.strategy_name == "s21_conditional_arbitrage":
            return f"Arbitraje condicional: {metadata.get('trigger_reason', 'Discrepancia en escalera o prerrequisito de eventos')}."
        if signal.strategy_name == "s22_news_latency_sniping":
            return f"Fast News Snipe: {metadata.get('headline', 'Catalizador detectado')}. Órdenes desactualizadas en el book."
        if signal.strategy_name == "s23_resolution_rules_lawyer":
            return f"Reglas Contractuales Estrictas: {metadata.get('trigger_reason', 'Condición legal de resolución favorece BUY NO')}."
        if signal.strategy_name == "s24_order_flow_imbalance":
            return f"Order Flow Imbalance (OFI): Absorción de liquidez agresiva en microestructura. Momentum para scalping."
        return f"Señal modular con edge estimado {edge * 100:.2f}%."

    def _modular_signal_to_dashboard(self, m: MarketSnapshot, signal: Signal) -> Optional[Dict]:
        if not signal.token_id:
            return None

        confidence = self._confidence_pct(signal.confidence)
        edge = self._display_edge(signal)
        expected_profit_bps = int(edge * 10000)
        strategy_code = self._strategy_code(signal.strategy_name)
        strategy_label = self.MODULAR_STRATEGY_LABELS.get(signal.strategy_name, signal.strategy_name)
        token_label = self._outcome_for_token(m, signal.token_id)

        if signal.strategy_name == "s05_negrisk_rebalancing" and signal.metadata.get("recommendation"):
            outcome = signal.metadata.get("outcome") or token_label
            token_label = f"NO/{outcome}"

        side = str(signal.side or "BUY").upper()
        if side not in {"BUY", "SELL", "BOTH", "BUY_BUNDLE", "SELL_BUNDLE"}:
            side = "BUY"

        urgency = "HIGH" if confidence >= 85 or expected_profit_bps >= 500 else "MEDIUM" if confidence >= 70 else "LOW"
        metadata = self._clean_metric_value(signal.metadata)
        dedupe_suffix = token_label.replace(" ", "_")

        entry_f = float(signal.market_price)
        target_f = max(0.001, min(0.999, float(signal.estimated_prob)))
        if is_long_side(side):
            if signal.strategy_name == "s20_oracle_delay_sniping":
                stop_f = max(0.01, entry_f * 0.95)
            elif signal.strategy_name == "s24_order_flow_imbalance":
                stop_f = max(0.01, entry_f * 0.965)
            elif signal.strategy_name == "s12_high_prob_harvesting":
                stop_f = max(0.01, entry_f * 0.94)
            elif signal.strategy_name == "s02_weather_noaa":
                stop_f = max(0.01, entry_f * 0.75)
            elif signal.strategy_name in ("s03_nothing_ever_happens", "s10_yes_bias", "s23_resolution_rules_lawyer"):
                stop_f = max(0.01, entry_f * 0.84)
            else:
                stop_f = max(0.01, entry_f * 0.90)
        else:
            stop_f = min(0.99, entry_f * 1.10)

        risk_val = abs(entry_f - stop_f)
        reward_val = abs(target_f - entry_f)
        rr_ratio = round(reward_val / risk_val, 2) if risk_val > 0 else 1.5

        return {
            "signal_id": str(uuid.uuid4())[:8],
            "strategy": strategy_label,
            "strategy_code": strategy_code,
            "side": side,
            "token": token_label,
            "token_id": signal.token_id,
            "entry_price": f"{entry_f:.4f}",
            "target_price": f"{target_f:.4f}",
            "stop_loss": f"{stop_f:.4f}",
            "recommended_order_type": "LIMIT (Maker)",
            "risk_reward_ratio": rr_ratio,
            "size": "100",
            "confidence": confidence,
            "urgency": urgency,
            "expected_profit_bps": expected_profit_bps,
            "estimated_prob": round(signal.estimated_prob, 6),
            "market_price": round(signal.market_price, 6),
            "edge": round(edge, 6),
            "trigger_reason": self._modular_trigger_reason(signal),
            "status": "ACTIVE",
            "dedupe_key": f"{strategy_code}:{m.market_id}:{dedupe_suffix}:{side}",
            "metrics": {
                "strategy_name": signal.strategy_name,
                "strategy_id": strategy_code,
                "token_id": signal.token_id,
                "estimated_prob": round(signal.estimated_prob, 6),
                "market_price": round(signal.market_price, 6),
                "edge_pct": round(edge * 100, 3),
                "stop_loss": round(stop_f, 4),
                "target_price": round(target_f, 4),
                "risk_reward_ratio": rr_ratio,
                "metadata": metadata,
            },
        }

    def _calculate_modular_strategies(self, m: MarketSnapshot) -> List[Dict]:
        core_market = self._snapshot_to_core_market(m)
        signals: List[Dict] = []

        for strategy in self.modular_strategies:
            try:
                for opportunity in strategy.scan([core_market]):
                    signal = strategy.analyze(opportunity)
                    if signal:
                        dashboard_signal = self._modular_signal_to_dashboard(m, signal)
                        if dashboard_signal:
                            signals.append(dashboard_signal)
            except Exception as e:
                print(f"[Strategy {strategy.name}] Error: {e}")

        return signals

    def _market_making(self, m: MarketSnapshot) -> Optional[Dict]:
        """Estrategia A: Market Making intra-mercado"""
        p = self.params["market_making"]

        for outcome in m.outcomes:
            if outcome not in m.spread or outcome not in m.best_bid:
                continue

            spread = m.spread[outcome]
            spread_bps = int((spread / m.mid_price[outcome]) * 10000) if m.mid_price.get(outcome) else 0

            # Verificar condiciones
            if spread_bps < p["min_spread_bps"]:
                continue
            if m.volume_24h < Decimal(str(p["min_volume_24h"])):
                continue
            if m.neg_risk:
                continue

            # Calcular depth (suma de size en ±10% del mid)
            depth_bid = Decimal("0")
            depth_ask = Decimal("0")
            mid = m.mid_price.get(outcome, Decimal("0"))
            if mid == 0:
                continue

            ob = m.order_book.get(outcome, {})
            for bid in ob.get("bids", []):
                price = order_price(bid)
                size = order_size(bid)
                if price >= mid * Decimal("0.9"):
                    depth_bid += size * price

            for ask in ob.get("asks", []):
                price = order_price(ask)
                size = order_size(ask)
                if price <= mid * Decimal("1.1"):
                    depth_ask += size * price

            if depth_bid < Decimal(str(p["min_depth_usd"])) or depth_ask < Decimal(str(p["min_depth_usd"])):
                continue

            expected_profit = spread_bps - m.taker_fee_bps

            return {
                "signal_id": str(uuid.uuid4())[:8],
                "strategy": "A: Market Making",
                "strategy_code": "MM",
                "side": "BOTH",
                "token": outcome,
                "entry_price": str(m.mid_price[outcome]),
                "bid_price": str(m.best_bid[outcome] + m.tick_size),
                "ask_price": str(m.best_ask[outcome] - m.tick_size),
                "size": str(min(depth_bid, depth_ask) * Decimal("0.1")),
                "confidence": min(85, 50 + spread_bps // 2),
                "urgency": "MEDIUM",
                "expected_profit_bps": expected_profit,
                "trigger_reason": f"Spread de {spread_bps} bps en {outcome}. Depth: ${float(depth_bid):.0f} bid / ${float(depth_ask):.0f} ask",
                "status": "ACTIVE",
                "metrics": {
                    "spread_bps": spread_bps,
                    "depth_bid": str(depth_bid),
                    "depth_ask": str(depth_ask),
                    "volume_24h": str(m.volume_24h),
                }
            }
        return None

    def _bundle_arbitrage(self, m: MarketSnapshot) -> Optional[Dict]:
        """Estrategia B: Bundle Arbitrage (YES + NO != $1)"""
        p = self.params["bundle_arbitrage"]

        if len(m.outcomes) != 2 or "Yes" not in m.outcomes or "No" not in m.outcomes:
            return None

        yes_bid = m.best_bid.get("Yes")
        yes_ask = m.best_ask.get("Yes")
        no_bid = m.best_bid.get("No")
        no_ask = m.best_ask.get("No")

        if not all([yes_bid, yes_ask, no_bid, no_ask]):
            return None

        # Caso 1: Comprar ambos por menos de $1
        buy_bundle = yes_ask + no_ask
        if buy_bundle < Decimal("1.0") - Decimal(str(p["min_inefficiency"])):
            profit = Decimal("1.0") - buy_bundle
            max_size = min(
                order_size((m.order_book.get("Yes", {}).get("asks") or [{}])[0]),
                order_size((m.order_book.get("No", {}).get("asks") or [{}])[0])
            )
            return {
                "signal_id": str(uuid.uuid4())[:8],
                "strategy": "B: Bundle Arbitrage",
                "strategy_code": "BA",
                "side": "BUY_BUNDLE",
                "token": "BOTH",
                "entry_price": str(buy_bundle),
                "target_price": "1.00",
                "size": str(max_size),
                "confidence": 95,
                "urgency": "HIGH",
                "expected_profit_bps": int(profit * 10000),
                "trigger_reason": f"YES ask ({yes_ask}) + NO ask ({no_ask}) = {buy_bundle:.4f}. Profit: ${profit:.4f} por bundle",
                "status": "ACTIVE",
                "metrics": {
                    "yes_ask": str(yes_ask),
                    "no_ask": str(no_ask),
                    "bundle_sum": str(buy_bundle),
                    "profit_per_unit": str(profit),
                }
            }

        # Caso 2: Vender ambos por más de $1
        sell_bundle = yes_bid + no_bid
        if sell_bundle > Decimal("1.0") + Decimal(str(p["min_inefficiency"])):
            profit = sell_bundle - Decimal("1.0")
            max_size = min(
                order_size((m.order_book.get("Yes", {}).get("bids") or [{}])[0]),
                order_size((m.order_book.get("No", {}).get("bids") or [{}])[0])
            )
            return {
                "signal_id": str(uuid.uuid4())[:8],
                "strategy": "B: Bundle Arbitrage",
                "strategy_code": "BA",
                "side": "SELL_BUNDLE",
                "token": "BOTH",
                "entry_price": str(sell_bundle),
                "target_price": "1.00",
                "size": str(max_size),
                "confidence": 95,
                "urgency": "HIGH",
                "expected_profit_bps": int(profit * 10000),
                "trigger_reason": f"YES bid ({yes_bid}) + NO bid ({no_bid}) = {sell_bundle:.4f}. Profit: ${profit:.4f} por bundle",
                "status": "ACTIVE",
                "metrics": {
                    "yes_bid": str(yes_bid),
                    "no_bid": str(no_bid),
                    "bundle_sum": str(sell_bundle),
                    "profit_per_unit": str(profit),
                }
            }

        return None

    def _mean_reversion(self, m: MarketSnapshot) -> Optional[Dict]:
        """Estrategia C: Mean Reversion"""
        p = self.params["mean_reversion"]

        if m.liquidity < Decimal(str(p["min_liquidity"])):
            return None
        if m.volume_24h < Decimal(str(p["min_volume_24h"])):
            return None

        min_history_points = int(p.get("min_history_points", 5))

        # 1) Señal intradía con historial local. Antes exigía siempre 15m completos
        # y velocidad >5%; al arrancar localmente casi nunca había suficientes puntos.
        for outcome in m.outcomes:
            hist = m.price_history.get(outcome)
            if not hist:
                continue

            current = m.mid_price.get(outcome)
            if not current:
                continue

            if len(hist.prices) < min_history_points:
                continue

            z_score = hist.z_score(15, current)
            velocity = hist.velocity_1m()

            if z_score is None or velocity is None:
                continue

            if abs(z_score) < Decimal(str(p["z_score_threshold"])):
                continue
            if abs(velocity) < Decimal(str(p["price_velocity_threshold"])):
                continue

            sma = hist.sma(15)
            side = "BUY" if z_score < 0 else "SELL"
            entry = current
            target = sma if sma else current * (Decimal("1.02") if side == "BUY" else Decimal("0.98"))
            stop = entry * (Decimal("0.97") if side == "BUY" else Decimal("1.03"))

            return {
                "signal_id": str(uuid.uuid4())[:8],
                "strategy": "C: Mean Reversion",
                "strategy_code": "MR",
                "side": side,
                "token": outcome,
                "entry_price": str(entry),
                "target_price": str(target),
                "stop_loss": str(stop),
                "size": "100",
                "confidence": min(75, 50 + int(abs(z_score)) * 10),
                "urgency": "HIGH" if abs(z_score) > 3 else "MEDIUM",
                "expected_profit_bps": int(abs((target - entry) / entry) * 10000),
                "trigger_reason": f"Z-score: {float(z_score):.2f}, Velocidad 1m: {float(velocity)*100:.1f}%. Sobre-reacción detectada en {outcome}.",
                "status": "ACTIVE",
                "dedupe_key": f"MR:local:{m.market_id}:{outcome}:{side}",
                "metrics": {
                    "source": "local_price_history",
                    "z_score": float(z_score),
                    "velocity_1m": float(velocity),
                    "sma_15m": str(sma) if sma else "N/A",
                }
            }

        # 2) Fallback con cambios de Gamma. Útil justo al iniciar el scanner:
        # Gamma ya trae oneDay/oneWeek change aunque nuestro historial local esté vacío.
        fallback_token = "Yes" if "Yes" in m.outcomes else (m.outcomes[0] if m.outcomes else "Yes")
        current = m.gamma_last_trade_price
        if current is None:
            current = m.mid_price.get(fallback_token) or m.prices.get(fallback_token)
        if current is None or current <= Decimal("0.01") or current >= Decimal("0.99"):
            return None

        change_1d = m.gamma_one_day_price_change
        change_1w = m.gamma_one_week_price_change
        selected_change = None
        selected_window = None

        if change_1d is not None and abs(change_1d) >= Decimal(str(p.get("gamma_1d_change_threshold", 0.02))):
            selected_change = change_1d
            selected_window = "1d"
        elif change_1w is not None and abs(change_1w) >= Decimal(str(p.get("gamma_1w_change_threshold", 0.04))):
            selected_change = change_1w
            selected_window = "1w"

        if selected_change is None:
            return None

        side = "SELL" if selected_change > 0 else "BUY"
        reversion_fraction = Decimal(str(p.get("reversion_fraction", 0.50)))
        target = self._clamp_probability(current - (selected_change * reversion_fraction))
        stop = self._clamp_probability(current + (selected_change * Decimal("0.60")))
        confidence = min(78, 52 + int(abs(selected_change) * 1000))

        return {
            "signal_id": str(uuid.uuid4())[:8],
            "strategy": "C: Mean Reversion",
            "strategy_code": "MR",
            "side": side,
            "token": fallback_token,
            "entry_price": str(current),
            "target_price": str(target),
            "stop_loss": str(stop),
            "size": "100",
            "confidence": confidence,
            "urgency": "HIGH" if abs(selected_change) >= Decimal("0.08") else "MEDIUM",
            "expected_profit_bps": self._safe_profit_bps(current, target),
            "trigger_reason": f"Cambio Gamma {selected_window}: {float(selected_change)*100:.1f}¢ en mercado líquido. Fallback mean-reversion hacia {target}.",
            "status": "ACTIVE",
            "dedupe_key": f"MR:gamma:{m.market_id}:{fallback_token}:{selected_window}:{side}",
            "metrics": {
                "source": "gamma_price_change",
                "change_window": selected_window,
                "price_change": float(selected_change),
                "liquidity": str(m.liquidity),
                "volume_24h": str(m.volume_24h),
            }
        }
        return None

    def _favorite_longshot(self, m: MarketSnapshot) -> Optional[Dict]:
        """Estrategia D: Favorite-Longshot Bias"""
        p = self.params["favorite_longshot"]

        if m.volume_24h < Decimal(str(p["min_volume_24h"])):
            return None

        for outcome in m.outcomes:
            price = m.mid_price.get(outcome)
            if not price:
                continue

            hist = m.price_history.get(outcome)
            z_score = hist.z_score(15, price) if hist else None

            # FAVORITE: precio > 0.85, comprar si infravalorado temporalmente
            if price > Decimal(str(p["favorite_threshold"])):
                if z_score and z_score < Decimal("-1.5"):
                    return {
                        "signal_id": str(uuid.uuid4())[:8],
                        "strategy": "D: Favorite-Longshot",
                        "strategy_code": "FLB",
                        "side": "BUY",
                        "token": outcome,
                        "entry_price": str(price),
                        "target_price": str(price * Decimal("1.02")),
                        "stop_loss": str(price * Decimal("0.97")),
                        "size": "200",
                        "confidence": 60,
                        "urgency": "MEDIUM",
                        "expected_profit_bps": 200,
                        "trigger_reason": f"Favorito {outcome} a ${price} con z-score {float(z_score):.2f} (infravalorado temporal). Sesgo FLB.",
                        "status": "ACTIVE",
                        "metrics": {
                            "price": str(price),
                            "z_score": float(z_score),
                            "type": "favorite_undervalued",
                        }
                    }

            # LONGSHOT: precio < 0.15, vender si sobrevalorado
            if price < Decimal(str(p["longshot_threshold"])):
                if z_score and z_score > Decimal("2.0"):
                    return {
                        "signal_id": str(uuid.uuid4())[:8],
                        "strategy": "D: Favorite-Longshot",
                        "strategy_code": "FLB",
                        "side": "SELL",
                        "token": outcome,
                        "entry_price": str(price),
                        "target_price": str(price * Decimal("0.80")),
                        "stop_loss": str(price * Decimal("1.50")),
                        "size": "500",
                        "confidence": 55,
                        "urgency": "MEDIUM",
                        "expected_profit_bps": 2000,
                        "trigger_reason": f"Longshot {outcome} a ${price} con z-score {float(z_score):.2f} (sobrevalorado por euforia). Sesgo FLB.",
                        "status": "ACTIVE",
                        "metrics": {
                            "price": str(price),
                            "z_score": float(z_score),
                            "type": "longshot_overvalued",
                        }
                    }
        return None

    def _external_data(self, m: MarketSnapshot) -> Optional[Dict]:
        """Estrategia E: Latencia en datos externos (placeholder)"""
        # Esta estrategia requiere integración con fuentes externas (NOAA, ESPN, FRED)
        # Por ahora retorna None - se puede implementar conectando APIs externas
        return None

    def _whale_tracking(self, m: MarketSnapshot) -> Optional[Dict]:
        """Estrategia F: Whale Tracking con trades grandes enriquecidos."""
        p = self.params["whale_tracking"]
        min_position = Decimal(str(p.get("min_whale_position", 5000)))
        min_winrate = Decimal(str(p.get("min_whale_winrate", 0.60)))

        # El backend llena m.whale_signals desde Data API /trades + /positions.
        # Aquí solo convertimos la mejor señal smart-money en una señal operable.
        candidates = []
        for ws in m.whale_signals:
            try:
                winrate = Decimal(str(ws.get("wallet_winrate", 0)))
                max_position = Decimal(str(ws.get("wallet_max_position_size", 0)))
                current_value = Decimal(str(ws.get("wallet_current_value", 0)))
            except Exception:
                continue

            if max_position < min_position and current_value < min_position:
                continue
            if winrate < min_winrate:
                continue
            candidates.append(ws)

        if not candidates:
            return None

        best = sorted(
            candidates,
            key=lambda x: (
                float(x.get("wallet_winrate", 0)),
                float(x.get("wallet_max_position_size", 0)),
                float(x.get("notional", 0) or 0),
            ),
            reverse=True,
        )[0]

        token = str(best.get("outcome") or "Yes")
        side = str(best.get("side") or "BUY").upper()
        entry = m.mid_price.get(token) or m.prices.get(token) or Decimal(str(best.get("price", 0)))
        if entry <= 0:
            return None

        if side == "BUY":
            target = self._clamp_probability(entry * Decimal("1.08"))
            stop = self._clamp_probability(entry * Decimal("0.92"))
        else:
            target = self._clamp_probability(entry * Decimal("0.92"))
            stop = self._clamp_probability(entry * Decimal("1.08"))

        wallet = str(best.get("wallet") or "")
        winrate = float(best.get("wallet_winrate", 0))
        max_position = Decimal(str(best.get("wallet_max_position_size", 0)))
        notional = Decimal(str(best.get("notional", 0)))
        source = str(best.get("source") or "data_api_trades_positions")
        observed_size = Decimal(str(best.get("size") or best.get("amount") or 0))
        confidence = min(88, 55 + int(winrate * 25) + min(10, int(max_position / Decimal("10000"))))
        action_text = "mantiene" if "holders" in source else "hizo"

        return {
            "signal_id": str(uuid.uuid4())[:8],
            "strategy": "F: Whale Tracking",
            "strategy_code": "WT",
            "side": side,
            "token": token,
            "entry_price": str(entry),
            "target_price": str(target),
            "stop_loss": str(stop),
            "size": str(min(max(observed_size, max_position), Decimal("1000"))),
            "confidence": confidence,
            "urgency": "HIGH" if notional >= Decimal("1000") else "MEDIUM",
            "expected_profit_bps": self._safe_profit_bps(entry, target),
            "trigger_reason": f"Wallet {wallet[:8]}... con win-rate {winrate*100:.0f}% y posición máx {max_position:.0f} shares {action_text} {observed_size:.0f} shares en {token}. Señal smart-money.",
            "status": "ACTIVE",
            "dedupe_key": f"WT:{m.market_id}:{wallet}:{token}:{side}",
            "metrics": {
                "wallet": wallet,
                "wallet_winrate": winrate,
                "wallet_positions_checked": best.get("wallet_positions_checked", 0),
                "wallet_max_position_size": str(max_position),
                "wallet_current_value": str(best.get("wallet_current_value", 0)),
                "trade_notional": str(notional),
                "observed_size": str(observed_size),
                "source": source,
            }
        }


# Instancia global
engine = StrategyEngine()
