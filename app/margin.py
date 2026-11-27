"""期货跨期保证金演练 —— 保证金计算引擎（纯函数，无 Flask 依赖）。

规则（全部以人民币元为单位）：

1. 每个合约月（近月 near / 远月 far）的净持仓每手计 100 元，
   即单边保证金 = 100 * |净持仓手数|。
2. 近月与远月方向相反的持仓，每配成一对可减收 80 元（跨期对冲优惠）：
   配对数 = min(|近月净持仓|, |远月净持仓|)，但仅在两者符号相反时成立。
3. 总保证金 = 100*(|near| + |far|) - 80*配对数。

上限试算（找“最高保证金”）：
    给定当前净持仓与全部活动挂单，考虑每张单“从 0 手到剩余手数”的任意成交
    手数组合，返回其中的最高保证金及一组达到该值的成交手数。

    保证金 M(x, y) = 100*|x| + 100*|y| - 80*pairs(x, y) 是 (x, y) 上的
    凸函数（逐段线性，各段斜率在跨越坐标轴与对冲区边界时单调不减，
    详见 README 的证明）。而各单成交手数 q_i 在区间 [0, remaining_i] 内取值，
    最终持仓是 q_i 的线性函数，故复合后的保证金在“成交手数盒子”上仍为凸函数。
    凸函数在（整数）盒子上的最大值必在顶点取得，即每张单要么成交 0 手、
    要么成交全部剩余手数。因此只需枚举各单 0/全成交的 2^N 种组合
    （N <= 12，最多 4096 次），即可严格覆盖题目要求的“所有成交组合”的最大值；
    测试中另用逐手全枚举对小场景做交叉验证。
"""

from __future__ import annotations

from typing import Iterable, Sequence

#: 每手单边保证金（元）
MARGIN_PER_LOT = 100
#: 每对反向跨期持仓的减收金额（元）
SPREAD_DISCOUNT_PER_PAIR = 80


def spread_pairs(near: int, far: int) -> int:
    """近月、远月净持仓中可配成跨期对冲的对数（方向相反才配对）。"""
    if near == 0 or far == 0:
        return 0
    if (near > 0) == (far > 0):
        return 0  # 同向不配对
    return min(abs(near), abs(far))


def margin_requirement(near: int, far: int) -> int:
    """给定两月净持仓（手，带方向），返回应缴保证金（元）。"""
    pairs = spread_pairs(near, far)
    return MARGIN_PER_LOT * (abs(near) + abs(far)) - SPREAD_DISCOUNT_PER_PAIR * pairs


def apply_fills(
    near0: int,
    far0: int,
    orders: Sequence[dict],
    quantities: Sequence[int],
) -> tuple[int, int]:
    """在期初净持仓上叠加一组成交手数，返回新的 (近月, 远月) 净持仓。

    orders 中每张单为带 ``near_delta`` / ``far_delta``（每手对净持仓的带符号
    增量）与剩余手数字段的 dict；quantities 为与之一一对应的成交手数。
    """
    near, far = near0, far0
    for order, qty in zip(orders, quantities):
        near += order["near_delta"] * qty
        far += order["far_delta"] * qty
    return near, far


def worst_case(
    near0: int,
    far0: int,
    orders: Sequence[dict],
) -> dict:
    """枚举各活动挂单的顶点成交组合，求最高保证金。

    返回 dict::

        {
          "max_margin": int,            # 最高保证金（元）
          "fills": [int, ...],          # 一组达到该值的成交手数（与 orders 对齐）
          "near_position": int,         # 该组合下的近月净持仓
          "far_position": int,          # 该组合下的远月净持仓
          "pairs": int,                 # 该组合下的对冲配对数
          "combinations": int,          # 枚举的组合数（= 2^活动单数）
        }

    若没有活动挂单，fills 为空，结果就是当前持仓的保证金。
    """
    n = len(orders)
    best = {
        "max_margin": -1,
        "fills": [0] * n,
        "near_position": near0,
        "far_position": far0,
        "pairs": spread_pairs(near0, far0),
        "combinations": 1 << n,
    }

    # mask 的第 i 位为 1 表示第 i 张单全部成交，为 0 表示不成交（顶点枚举）
    for mask in range(1 << n):
        quantities = [
            orders[i]["remaining"] if (mask >> i) & 1 else 0 for i in range(n)
        ]
        near, far = apply_fills(near0, far0, orders, quantities)
        value = margin_requirement(near, far)
        if value > best["max_margin"]:
            best.update(
                max_margin=value,
                fills=quantities,
                near_position=near,
                far_position=far,
                pairs=spread_pairs(near, far),
            )
    return best


def worst_case_bruteforce(
    near0: int,
    far0: int,
    orders: Sequence[dict],
) -> dict:
    """逐手枚举每张单 0..remaining 的所有成交组合（仅用于测试交叉验证）。

    复杂度为乘积级，只应在小 remaining / 小单数场景下调用。
    """
    n = len(orders)
    quantities = [0] * n
    best = {
        "max_margin": -1,
        "fills": [0] * n,
        "near_position": near0,
        "far_position": far0,
        "pairs": spread_pairs(near0, far0),
        "combinations": 0,
    }

    def enumerate_recursive(index: int) -> None:
        if index == n:
            near, far = apply_fills(near0, far0, orders, quantities)
            value = margin_requirement(near, far)
            best["combinations"] += 1
            if value > best["max_margin"]:
                best.update(
                    max_margin=value,
                    fills=list(quantities),
                    near_position=near,
                    far_position=far,
                    pairs=spread_pairs(near, far),
                )
            return
        for lots in range(orders[index]["remaining"] + 1):
            quantities[index] = lots
            enumerate_recursive(index + 1)

    enumerate_recursive(0)
    return best


def order_delta(kind: str, direction: str) -> tuple[int, int]:
    """返回 (近月每手增量, 远月每手增量)，带符号。

    kind: ``single``（单腿单）或 ``spread``（跨期单）
    direction: ``buy``（买入，净持仓增加）或 ``sell``（卖出，净持仓减少）
    """
    sign = 1 if direction == "buy" else -1
    if kind == "single":
        # 挂单时通过 leg 字段指定 near/far，由调用方决定落点
        raise ValueError("单腿单请使用 single_leg_delta")
    # 跨期单一买一卖：近月与远月方向相反、等量
    return sign, -sign


def single_leg_delta(leg: str, direction: str) -> tuple[int, int]:
    """单腿单的每手 (近月增量, 远月增量)。"""
    sign = 1 if direction == "buy" else -1
    if leg == "near":
        return sign, 0
    if leg == "far":
        return 0, sign
    raise ValueError(f"未知合约月: {leg!r}")


def describe_legs(kind: str, leg: str | None, direction: str) -> str:
    """供界面展示的合约腿文字，例如“近月买 +1 / 远月卖 -1（每手）”。"""
    cn_dir = {"buy": "买", "sell": "卖"}
    sign = "+" if direction == "buy" else "-"
    if kind == "spread":
        # 跨期单一买一卖：近月与远月方向相反、等量
        if direction == "buy":
            return "近月买 +1 / 远月卖 -1（每手）"
        return "近月卖 -1 / 远月买 +1（每手）"
    month = "近月" if leg == "near" else "远月"
    return f"{month}{cn_dir[direction]} {sign}1（每手）"
