"""strategy_ranking.py — Ranking cuantitativo de estrategias con validación ML y estadística.

Cruza tres fuentes:
  1. paper_tracker       -> trades, PnL realizado/flotante, win rate.
  2. ai_learning_engine  -> Brier Score y multiplicador Kelly por estrategia.
  3. quant_ml (LinUCB)   -> multiplicador contextual y UCB por estrategia.

Aporta por estrategia: Quant Score compuesto, significancia estadística (Wilson vs
breakeven implícito), edge real, Kelly óptimo, métricas de riesgo (Sharpe/Sortino/MaxDD),
reloj de significancia (ETA en días), desglose por horizonte y categoría, ventanas móviles
y Tier automático A/B/C.
"""
from datetime import UTC, datetime
from math import sqrt
from typing import Any, Dict, List, Optional, Tuple

try:
    from strategy_governor import governor as _governor
except Exception:
    _governor = None

MIN_CLOSED_FOR_ML = 15
MIN_CLOSED_FOR_FILTER = 20
BRIER_BAD = 0.20
WINRATE_BAD = 50.0
Z95 = 1.96

SCORE_W_PNL = 1.0
SCORE_W_WINRATE = 0.25
SCORE_W_PF = 8.0
SCORE_W_SAMPLE = 0.3
SCORE_SAMPLE_CAP = 40
SCORE_PENALTY = 0.8
SCORE_PENALTY_THRESH = 15
SCORE_W_ML = 20.0


def compute_quant_score(total_pnl_usd, win_rate_pct, profit_factor, closed_trades_count, ai_kelly_multiplier) -> float:
    """Quant Score compuesto: rentabilidad + consistencia + muestra + validación ML."""
    closed = max(0, int(closed_trades_count or 0))
    return round(
        float(total_pnl_usd or 0) * SCORE_W_PNL
        + (float(win_rate_pct or 0) - 50.0) * SCORE_W_WINRATE
        + (float(profit_factor or 1.0) - 1.0) * SCORE_W_PF
        + min(closed, SCORE_SAMPLE_CAP) * SCORE_W_SAMPLE
        - max(0, SCORE_PENALTY_THRESH - closed) * SCORE_PENALTY
        + (float(ai_kelly_multiplier or 1.0) - 1.0) * SCORE_W_ML,
        2,
    )


def _f(v, d=0.0):
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _i(v, d=0):
    return int(_f(v, float(d)))


def _ts(v) -> float:
    if not v:
        return 0.0
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def wilson_interval(wins: int, n: int, z: float = Z95) -> Tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def breakeven_for_trade(t: Dict[str, Any]) -> float:
    entry = _f(t.get("entry_price"), 0.5)
    side = str(t.get("side") or "BUY").upper()
    if side == "BUY":
        return min(0.99, max(0.01, entry))
    return min(0.99, max(0.01, 1.0 - entry))


def sample_size_for_edge(edge: float, breakeven: float, z: float = Z95) -> int:
    e = max(0.005, abs(edge))
    p = min(0.99, max(0.01, breakeven))
    return int((z * z * p * (1 - p)) / (e * e)) + 1


def sharpe_ratio(returns: List[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    sd = sqrt(sum((r - mean) ** 2 for r in returns) / n)
    if sd <= 0:
        return 2.0 if mean > 0 else 0.0
    return (mean / sd) * sqrt(n)


def sortino_ratio(returns: List[float]) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    downside = [min(0.0, r) for r in returns]
    ds = sqrt(sum(r * r for r in downside) / n)
    if ds <= 0:
        return 2.0 if mean > 0 else 0.0
    return (mean / ds) * sqrt(n)


def max_drawdown(pnl_series: List[float]) -> float:
    peak = 0.0
    mdd = 0.0
    for x in pnl_series:
        peak = max(peak, x)
        if peak > 0:
            mdd = max(mdd, (peak - x) / peak)
    return round(mdd * 100.0, 2)


def kelly_optimal(win_rate: float, breakeven: float) -> float:
    wr = win_rate / 100.0
    b = min(0.99, max(0.01, breakeven))
    reward = 1.0 - b
    if reward <= 0:
        return 0.0
    ev = wr * reward - (1.0 - wr) * b
    return round(max(0.0, min(1.0, ev / reward)), 4)


def pearson(xs: List[float], ys: List[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    vx = sqrt(sum((x - mx) ** 2 for x in xs))
    vy = sqrt(sum((y - my) ** 2 for y in ys))
    if vx == 0 or vy == 0:
        return 0.0
    return round(cov / (vx * vy), 3)


def avg_holding_hours(closed: List[Dict[str, Any]]) -> float:
    hs = []
    for t in closed:
        o, c = _ts(t.get("opened_at")), _ts(t.get("closed_at"))
        if o > 0 and c > o:
            hs.append((c - o) / 3600.0)
    return round(sum(hs) / len(hs), 1) if hs else 0.0


def ml_badge(kelly):
    if kelly is None:
        return "SIN DATOS"
    if kelly > 1.10:
        return "BOOSTED"
    if kelly < 0.85:
        return "REDUCED"
    return "OPTIMAL"


def ml_effect(kelly):
    if kelly is None:
        return {"pct": 0.0, "label": "Sin datos del ML todavía para esta estrategia", "direction": "neutral"}
    pct = round((kelly - 1.0) * 100.0, 1)
    if pct > 2.0:
        label = f"El ML amplía el tamaño de las posiciones un {pct:.0f}% (buena calibración)"
        direction = "boost"
    elif pct < -2.0:
        label = f"El ML reduce el tamaño de las posiciones un {abs(pct):.0f}% (calibración débil)"
        direction = "reduce"
    else:
        label = "El ML mantiene el tamaño neutro (calibración óptima)"
        direction = "neutral"
    return {"pct": pct, "label": label, "direction": direction}


def build_strategy_ranking(paper_tracker, ai_learning_engine, quant_ml, mode: str = "realistic") -> Dict[str, Any]:
    """Construye el ranking completo cruzando paper trading + IA + Quant ML."""
    trades = list(getattr(paper_tracker, "trades", {}).values()) or []
    if mode == "research":
        budget = getattr(paper_tracker, "budget_per_strategy", 1000.0)
    else:
        budget = getattr(paper_tracker, "initial_balance", 1000.0)

    try:
        ai_status = ai_learning_engine.get_status() or {}
    except Exception:
        ai_status = {}
    ai_metrics = ai_status.get("strategy_metrics", {}) or {}
    ai_weights = getattr(ai_learning_engine, "strategy_weights", {}) or {}
    reflections = ai_status.get("reflections", []) or []

    try:
        qml_status = quant_ml.get_status() if hasattr(quant_ml, "get_status") else {}
    except Exception:
        qml_status = {}
    bandit = (qml_status or {}).get("bandit_strategies", {}) or {}

    lessons: Dict[str, List[Dict[str, Any]]] = {}
    for r in reflections:
        sid = r.get("strategy_id") or "GENERIC"
        lessons.setdefault(sid, []).append({
            "market_title": r.get("market_title", ""),
            "outcome": "WIN" if _i(r.get("outcome")) == 1 else "LOSS",
            "pnl": round(_f(r.get("pnl")), 2),
            "root_cause": r.get("root_cause", ""),
            "actionable_rule": r.get("actionable_rule", ""),
        })

    perf = paper_tracker.get_strategy_performance() or {}
    meta = {s.get("code"): s for s in (perf.get("strategies") or [])}

    by_strat: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        code = t.get("strategy_code") or t.get("strategy") or "GEN"
        by_strat.setdefault(code, []).append(t)

    now = datetime.now(UTC).timestamp()
    rows: List[Dict[str, Any]] = []

    # Mostrar TODAS las estrategias conocidas (con y sin trades)
    all_codes = sorted(set(meta.keys()) | set(by_strat.keys()), key=lambda c: (c not in by_strat, c))

    for code in all_codes:
        tlist = by_strat.get(code, [])
        closed = [t for t in tlist if t.get("status") in ("WON", "LOST")]
        open_ = [t for t in tlist if t.get("status") == "OPEN"]
        wins = [t for t in closed if t.get("status") == "WON"]
        losses = [t for t in closed if t.get("status") == "LOST"]

        n = len(tlist)
        nc = len(closed)
        nw = len(wins)

        realized = sum(_f(t.get("realized_pnl_usd")) for t in closed)
        unrealized = sum(_f(t.get("unrealized_pnl_usd")) for t in open_)
        total = realized + unrealized
        win_rate = (nw / nc * 100.0) if nc > 0 else 0.0

        gross_win = sum(_f(t.get("realized_pnl_usd")) for t in wins)
        gross_loss = abs(sum(_f(t.get("realized_pnl_usd")) for t in losses))
        pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 1.0)

        breakevens = [breakeven_for_trade(t) for t in (closed or tlist)]
        breakeven = (sum(breakevens) / len(breakevens)) if breakevens else 0.5
        edge = win_rate - breakeven * 100.0
        below_breakeven = (nc > 0) and (win_rate < breakeven * 100.0)

        lo, hi = wilson_interval(nw, nc)
        significant_better = (nc >= MIN_CLOSED_FOR_ML) and (lo > breakeven)
        significant_worse = (nc >= MIN_CLOSED_FOR_ML) and (hi < breakeven)
        if significant_better:
            significance = "BETTER"
        elif significant_worse:
            significance = "WORSE"
        elif nc < MIN_CLOSED_FOR_ML:
            significance = "INSUFFICIENT"
        else:
            significance = "NO_EDGE"

        n_required = sample_size_for_edge(edge / 100.0, breakeven)
        opened_ts = [_ts(t.get("opened_at")) for t in tlist if _ts(t.get("opened_at")) > 0]
        days_elapsed = max(1.0, (now - min(opened_ts)) / 86400.0) if opened_ts else 1.0
        velocity = nc / days_elapsed
        eta = max(0.0, (n_required - nc) / velocity) if velocity > 0 else None

        rets = [_f(t.get("realized_pnl_pct"), 0.0) for t in closed]
        sharpe = sharpe_ratio(rets)
        sortino = sortino_ratio(rets)
        cum = []
        run = 0.0
        for t in sorted(closed, key=lambda x: str(x.get("closed_at") or "")):
            run += _f(t.get("realized_pnl_usd"))
            cum.append(run)
        mdd = max_drawdown(cum)
        kelly = kelly_optimal(win_rate, breakeven)

        def pnl_since(hours):
            cutoff = now - hours * 3600.0
            return round(sum(_f(t.get("realized_pnl_usd")) for t in closed if _ts(t.get("closed_at")) >= cutoff), 2)

        horizon = {}
        for t in closed:
            h = t.get("horizon") or "medium_long"
            horizon[h] = round(horizon.get(h, 0.0) + _f(t.get("realized_pnl_usd")), 2)
        category = {}
        for t in closed:
            c = t.get("category") or "General"
            category[c] = round(category.get(c, 0.0) + _f(t.get("realized_pnl_usd")), 2)

        ai = ai_metrics.get(code, {}) or {}
        ai_kelly_raw = ai_weights.get(code, ai.get("kelly_multiplier"))
        ai_kelly = _f(ai_kelly_raw, 1.0) if ai_kelly_raw is not None else None
        bandit_info = bandit.get(code, {}) or {}
        bm_raw = bandit_info.get("capital_multiplier")
        bandit_mult = _f(bm_raw) if bm_raw is not None else None
        ml_active = (code in ai_weights) or (code in bandit)

        brier = ai.get("brier_score")
        brier = _f(brier) if brier is not None else None
        candidate_to_filter = (nc >= MIN_CLOSED_FOR_FILTER and brier is not None and brier > BRIER_BAD and win_rate < WINRATE_BAD)

        if significant_better and total > 0:
            tier = "A"
        elif total > 0 or nc >= MIN_CLOSED_FOR_ML:
            tier = "B"
        else:
            tier = "C"
        if candidate_to_filter or significant_worse:
            tier = "C"
        tier_action = {"A": "Mantener / escalar", "B": "Observar", "C": "Pausar / filtrar"}[tier]

        score = (
            total * SCORE_W_PNL
            + (win_rate - 50.0) * SCORE_W_WINRATE
            + (pf - 1.0) * SCORE_W_PF
            + min(nc, SCORE_SAMPLE_CAP) * SCORE_W_SAMPLE
            - max(0, SCORE_PENALTY_THRESH - nc) * SCORE_PENALTY
            + ((ai_kelly or 1.0) - 1.0) * SCORE_W_ML
        )

        flags = []
        if n == 0:
            flags.append("SIN OPERAR")
        if nc < MIN_CLOSED_FOR_ML:
            flags.append("MUESTRA INSUFICIENTE")
        if candidate_to_filter:
            flags.append("CANDIDATA A FILTRAR")

        m = meta.get(code, {})
        rows.append({
            "code": code,
            "name": m.get("name", code),
            "tag": m.get("tag", ""),
            "category_meta": m.get("category", ""),
            "icon": m.get("icon", ""),
            "description": m.get("description", ""),
            "status": m.get("status", "SCANNING"),
            "status_label": m.get("status_label", ""),
            "total_trades": n,
            "open_trades_count": len(open_),
            "closed_trades_count": nc,
            "won_count": nw,
            "lost_count": len(losses),
            "realized_pnl_usd": round(realized, 2),
            "unrealized_pnl_usd": round(unrealized, 2),
            "total_pnl_usd": round(total, 2),
            "roi_pct": round(total / budget * 100.0, 2) if budget > 0 else 0.0,
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": pf,
            "breakeven_pct": round(breakeven * 100.0, 1),
            "edge_pct": round(edge, 1),
            "below_breakeven": below_breakeven,
            "wilson_lower_pct": round(lo * 100.0, 1),
            "wilson_upper_pct": round(hi * 100.0, 1),
            "significance": significance,
            "significant": significant_better,
            "sample_required": n_required,
            "closing_velocity_per_day": round(velocity, 2),
            "eta_days": round(eta, 1) if eta is not None else None,
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "max_drawdown_pct": mdd,
            "kelly_optimal": kelly,
            "expectancy_per_trade_usd": round(realized / nc, 2) if nc > 0 else 0.0,
            "avg_holding_hours": avg_holding_hours(closed),
            "pnl_24h": pnl_since(24),
            "pnl_7d": pnl_since(168),
            "pnl_30d": pnl_since(720),
            "horizon_breakdown": horizon,
            "category_breakdown": category,
            "ml_active": ml_active,
            "ml_badge": ml_badge(ai_kelly),
            "ai_kelly_multiplier": ai_kelly,
            "ai_brier_score": brier,
            "ai_brier_status": ai.get("status"),
            "ai_trades_seen": _i(ai.get("trades_count")),
            "bandit_capital_multiplier": bandit_mult,
            "bandit_ucb_score": bandit_info.get("ucb_score"),
            "bandit_trades": _i(bandit_info.get("trades_count")),
            "ml_effect": ml_effect(ai_kelly),
            "sample_sufficient": nc >= MIN_CLOSED_FOR_ML,
            "candidate_to_filter": candidate_to_filter,
            "flags": flags,
            "lessons_count": len(lessons.get(code, [])),
            "lessons": lessons.get(code, [])[:5],
            "tier": tier,
            "tier_action": tier_action,
            "paused": bool(_governor and _governor.is_paused(code)),
            "governor_rules": _governor.get_rules(code) if _governor else {},
            "score": round(score, 2),
        })

    rows.sort(key=lambda r: (r["score"], r["total_pnl_usd"]), reverse=True)
    for idx, r in enumerate(rows, start=1):
        r["rank"] = idx
        if idx == 1:
            r["rank_badge"] = "🥇 #1"
        elif idx == 2:
            r["rank_badge"] = "🥈 #2"
        elif idx == 3:
            r["rank_badge"] = "🥉 #3"
        else:
            r["rank_badge"] = f"#{idx}"

    operating = [r for r in rows if r["total_trades"] > 0]
    ml_rows = [r for r in rows if r["ml_active"]]
    closed_rows = [r for r in rows if r["closed_trades_count"] > 0]
    candidates = [r for r in rows if r["candidate_to_filter"]]
    tiers = {"A": 0, "B": 0, "C": 0}
    for r in rows:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1

    best_score = rows[0] if rows else None
    best_pnl = max(rows, key=lambda r: r["total_pnl_usd"]) if rows else None
    best_wr = max(closed_rows, key=lambda r: r["win_rate_pct"]) if closed_rows else None

    def _brief(r, key):
        if not r:
            return None
        return {"code": r["code"], "name": r["name"], "icon": r["icon"], key: r.get(key)}

    # Correlación entre estrategias con suficientes trades cerrados
    corr_rows = [r for r in rows if r["closed_trades_count"] >= 3][:8]
    daily: Dict[str, Dict[str, float]] = {}
    for r in corr_rows:
        for t in by_strat.get(r["code"], []):
            if t.get("status") in ("WON", "LOST") and _ts(t.get("closed_at")) > 0:
                day = str(t.get("closed_at", ""))[:10]
                daily.setdefault(r["code"], {})[day] = daily.setdefault(r["code"], {}).get(day, 0.0) + _f(t.get("realized_pnl_usd"))
    days_set = sorted({d for series in daily.values() for d in series})
    corr = []
    codes = [r["code"] for r in corr_rows]
    for i, a in enumerate(codes):
        for j, b in enumerate(codes):
            if j <= i:
                continue
            xa = [daily.get(a, {}).get(d, 0.0) for d in days_set]
            xb = [daily.get(b, {}).get(d, 0.0) for d in days_set]
            c = pearson(xa, xb)
            if c != 0.0:
                corr.append({"a": a, "b": b, "correlation": c})
    corr.sort(key=lambda x: abs(x["correlation"]), reverse=True)

    return {
        "mode": mode,
        "budget": round(budget, 2),
        "total_strategies": len(rows),
        "operating_count": len(operating),
        "ml_active_count": len(ml_rows),
        "total_trades": sum(r["total_trades"] for r in rows),
        "total_closed_trades": sum(r["closed_trades_count"] for r in rows),
        "candidates_to_filter_count": len(candidates),
        "tiers": tiers,
        "best_by_score": _brief(best_score, "score"),
        "best_by_pnl": _brief(best_pnl, "total_pnl_usd"),
        "best_by_winrate": _brief(best_wr, "win_rate_pct"),
        "correlations": corr[:15],
        "score_formula": "pnl + (winrate-50)*0.25 + (profit_factor-1)*8 + min(closed,40)*0.3 - max(0,15-closed)*0.8 + (kelly-1)*20",
        "thresholds": {
            "min_closed_for_ml": MIN_CLOSED_FOR_ML,
            "min_closed_for_filter": MIN_CLOSED_FOR_FILTER,
            "brier_bad": BRIER_BAD,
            "winrate_bad": WINRATE_BAD,
        },
        "strategies": rows,
        "updated_at": datetime.now(UTC).isoformat(),
    }
