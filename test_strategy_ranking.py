"""Tests del ranking cuantitativo de estrategias."""
from strategy_ranking import (
    compute_quant_score,
    wilson_interval,
    sample_size_for_edge,
    kelly_optimal,
)


def test_wilson():
    lo, hi = wilson_interval(8, 10)
    assert 0.0 <= lo <= 0.8 <= hi <= 1.0, f"Wilson fuera de rango: {lo}, {hi}"
    print(f"[OK] Wilson(8/10) -> [{lo:.3f}, {hi:.3f}]")


def test_sample_size():
    n = sample_size_for_edge(0.10, 0.50)
    assert n > 50
    print(f"[OK] sample_size(edge=10%) -> {n} trades")


def test_score():
    s1 = compute_quant_score(50, 60, 2.0, 40, 1.2)
    s2 = compute_quant_score(20, 55, 1.2, 3, 1.0)
    assert s1 > s2, f"La estrategia sólida debería puntuar más: {s1} vs {s2}"
    print(f"[OK] score sólida={s1} vs débil={s2}")


def test_kelly():
    k = kelly_optimal(60.0, 0.5)
    assert 0 <= k <= 1
    print(f"[OK] kelly(60%, breakeven 50%) -> {k}")


if __name__ == "__main__":
    test_wilson()
    test_sample_size()
    test_score()
    test_kelly()
    print("TODOS LOS TESTS PASARON")
