"""保证金引擎单元测试：公式、跨期配对、上限枚举与暴力枚举交叉验证。"""

import itertools
import random

import pytest

from app import margin


def make_order(near_delta, far_delta, remaining):
    return {
        "near_delta": near_delta,
        "far_delta": far_delta,
        "remaining": remaining,
    }


def test_margin_formula_basic():
    # 只有近月 3 手：3 * 100
    assert margin.margin_requirement(3, 0) == 300
    # 近月 +2、远月 -1：100*3 - 80*1 = 220
    assert margin.margin_requirement(2, -1) == 220
    # 近月 -2、远月 +1：同样 220
    assert margin.margin_requirement(-2, 1) == 220
    # 完全配对的反向持仓：+3 / -3 -> 600 - 240 = 360
    assert margin.margin_requirement(3, -3) == 360
    # 同向不配对：+3 / +2 -> 500
    assert margin.margin_requirement(3, 2) == 500
    # 空仓为 0
    assert margin.margin_requirement(0, 0) == 0


def test_spread_pairs():
    assert margin.spread_pairs(3, -2) == 2
    assert margin.spread_pairs(-3, 2) == 2
    assert margin.spread_pairs(3, 2) == 0
    assert margin.spread_pairs(0, -2) == 0
    assert margin.spread_pairs(2, 0) == 0


def test_deltas():
    # 跨期单：买 -> 近月 +1 远月 -1；卖 -> 近月 -1 远月 +1
    assert margin.order_delta("spread", "buy") == (1, -1)
    assert margin.order_delta("spread", "sell") == (-1, 1)
    # 单腿单
    assert margin.single_leg_delta("near", "buy") == (1, 0)
    assert margin.single_leg_delta("near", "sell") == (-1, 0)
    assert margin.single_leg_delta("far", "buy") == (0, 1)
    assert margin.single_leg_delta("far", "sell") == (0, -1)
    with pytest.raises(ValueError):
        margin.order_delta("single", "buy")


def test_worst_case_no_orders_equals_current():
    worst = margin.worst_case(2, -1, [])
    assert worst["max_margin"] == 220
    assert worst["fills"] == []
    assert worst["combinations"] == 1


def test_worst_case_single_leg_order():
    # 期初空仓，挂一张近月买单 3 手：不成交 0 元，全成交 300 元 -> 上限 300
    orders = [make_order(1, 0, 3)]
    worst = margin.worst_case(0, 0, orders)
    assert worst["max_margin"] == 300
    assert worst["fills"] == [3]
    assert worst["near_position"] == 3


def test_worst_case_spread_order():
    # 空仓 + 跨期买 4 手：全成交 => 近月 +4 远月 -4，800 - 320 = 480
    orders = [make_order(1, -1, 4)]
    worst = margin.worst_case(0, 0, orders)
    assert worst["max_margin"] == 480
    assert worst["fills"] == [4]
    assert worst["pairs"] == 4


def test_worst_case_mixed_scenario_manual():
    # 期初近月 +2、远月 -1（保证金 220）
    # 单 A：近月卖 2 手（delta -1, 0），全成交后 0/-1 => 100
    # 单 B：远月买 3 手（0, +1），全成交后 2/2  => 400（同向无配对）
    # A+B 全成交：0/2 => 200；只成交 B：2/2 => 400 为最高
    orders = [make_order(-1, 0, 2), make_order(0, 1, 3)]
    worst = margin.worst_case(2, -1, orders)
    assert worst["max_margin"] == 400
    assert worst["fills"] == [0, 3]
    assert (worst["near_position"], worst["far_position"]) == (2, 2)


def test_worst_case_vertex_matches_bruteforce_random():
    """大量随机小场景：顶点枚举的最大值必须等于逐手全枚举的最大值。"""
    rng = random.Random(20261127)
    deltas = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1)]
    for scenario in range(300):
        n = rng.randint(0, 4)
        orders = []
        for _ in range(n):
            d = rng.choice(deltas)
            orders.append(make_order(d[0], d[1], rng.randint(1, 4)))
        near0 = rng.randint(-4, 4)
        far0 = rng.randint(-4, 4)

        vertex = margin.worst_case(near0, far0, orders)
        brute = margin.worst_case_bruteforce(near0, far0, orders)
        assert vertex["max_margin"] == brute["max_margin"], (
            f"场景 {scenario} 不一致: orders={orders}, near0={near0}, far0={far0}"
        )
        # 顶点解给出的成交手数必须合法且确实达到该保证金
        assert len(vertex["fills"]) == n
        for qty, order in zip(vertex["fills"], orders):
            assert qty in (0, order["remaining"])
        near, far = margin.apply_fills(near0, far0, orders, vertex["fills"])
        assert margin.margin_requirement(near, far) == vertex["max_margin"]


def test_bruteforce_enumerates_every_combination():
    orders = [make_order(1, 0, 2), make_order(0, 1, 3)]
    brute = margin.worst_case_bruteforce(0, 0, orders)
    # (2+1) * (3+1) = 12 种
    assert brute["combinations"] == 12


def test_full_grid_max_is_at_vertex_exhaustive():
    """对一个固定小场景，直接枚举整格点并验证最大值仅在顶点出现/被顶点取到。"""
    orders = [make_order(1, -1, 3), make_order(-1, 0, 2)]
    near0, far0 = 1, -1
    values = {}
    for q1, q2 in itertools.product(range(4), range(3)):
        near, far = margin.apply_fills(near0, far0, orders, [q1, q2])
        values[(q1, q2)] = margin.margin_requirement(near, far)
    grid_max = max(values.values())
    vertex = margin.worst_case(near0, far0, orders)
    assert vertex["max_margin"] == grid_max
    # 顶点（每维取 0 或上限）中的最大值即整格最大值
    vertex_values = [values[(q1, q2)] for q1 in (0, 3) for q2 in (0, 2)]
    assert max(vertex_values) == grid_max


def test_max_12_orders_enumeration_size_and_speed():
    """12 笔挂单时组合数 4096，且计算在毫秒级完成。"""
    import time

    orders = [make_order(1, -1, 10) for _ in range(12)]
    start = time.perf_counter()
    worst = margin.worst_case(0, 0, orders)
    elapsed = time.perf_counter() - start
    assert worst["combinations"] == 4096
    assert elapsed < 0.5
    # 12 张跨期买 10 手全成交 => +120 / -120 => 24000 - 9600 = 14400
    assert worst["max_margin"] == 14400
    assert worst["fills"] == [10] * 12
