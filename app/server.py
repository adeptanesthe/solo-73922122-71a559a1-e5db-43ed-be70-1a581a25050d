"""期货跨期保证金演练 —— Flask 应用。

单进程内存状态（带线程锁），重启或调用 /api/reset 即恢复默认演示场景。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from flask import Flask, jsonify, render_template, request

from . import margin

MAX_ACTIVE_ORDERS = 12          # 最多同时挂 12 笔
MAX_LOTS_PER_ORDER = 10         # 每笔至多 10 手
MAX_LOG_ENTRIES = 50

DEFAULT_STATE = {
    "cash": 2000,               # 账户资金（元）
    "near_position": 2,         # 期初近月净持仓（手，带方向）
    "far_position": -1,         # 期初远月净持仓（手，带方向）
}


class ApiError(Exception):
    """携带 HTTP 状态码的业务校验错误。"""

    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


def _new_state() -> dict[str, Any]:
    return {
        "cash": DEFAULT_STATE["cash"],
        "near_position": DEFAULT_STATE["near_position"],
        "far_position": DEFAULT_STATE["far_position"],
        "orders": [],          # 全部单据（含已成交/已撤），新单 append 到末尾
        "logs": [],
        "order_seq": 0,
    }


class OrderStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state = _new_state()

    # ---- 通用工具 -------------------------------------------------------
    def _log(self, text: str) -> None:
        self.state["logs"].append(text)
        if len(self.state["logs"]) > MAX_LOG_ENTRIES:
            del self.state["logs"][: -MAX_LOG_ENTRIES]

    def _active_orders(self) -> list[dict]:
        return [o for o in self.state["orders"] if o["status"] == "active"]

    def _get_order(self, order_id: str) -> dict:
        for order in self.state["orders"]:
            if order["id"] == order_id:
                return order
        raise ApiError(f"找不到单据 {order_id}", 404)

    def _recalc(self, extra_orders: list[dict] | None = None) -> dict:
        """以当前持仓 + 活动挂单（可附加一张试算单）重算上限。"""
        active = self._active_orders()
        orders = active + (extra_orders or [])
        worst = margin.worst_case(
            self.state["near_position"], self.state["far_position"], orders
        )
        current = margin.margin_requirement(
            self.state["near_position"], self.state["far_position"]
        )
        return {
            "current_margin": current,
            "max_margin": worst["max_margin"],
            "fills": worst["fills"],
            "near_position": worst["near_position"],
            "far_position": worst["far_position"],
            "pairs": worst["pairs"],
            "combinations": worst["combinations"],
            "order_ids": [o["id"] for o in orders],
        }

    # ---- 公开操作 -------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            calc = self._recalc()
            cash = self.state["cash"]
            return {
                "cash": cash,
                "near_position": self.state["near_position"],
                "far_position": self.state["far_position"],
                "current_margin": calc["current_margin"],
                "max_margin": calc["max_margin"],
                "headroom": cash - calc["max_margin"],
                "worst_fills": self._format_worst(calc),
                "worst_meta": {
                    "combinations": calc["combinations"],
                    "pairs": calc["pairs"],
                    "near_position": calc["near_position"],
                    "far_position": calc["far_position"],
                },
                "limits": {
                    "max_active_orders": MAX_ACTIVE_ORDERS,
                    "max_lots_per_order": MAX_LOTS_PER_ORDER,
                },
                "orders": [self._public_order(o) for o in self.state["orders"]],
                "logs": list(self.state["logs"]),
            }

    def _format_worst(self, calc: dict) -> list[dict]:
        rows = []
        for order_id, qty in zip(calc["order_ids"], calc["fills"]):
            order = next(o for o in self.state["orders"] if o["id"] == order_id)
            if qty <= 0:
                continue
            rows.append(
                {
                    "order_id": order_id,
                    "seq": order["seq"],
                    "kind": order["kind"],
                    "leg": order["leg"],
                    "direction": order["direction"],
                    "fill_lots": qty,
                    "remaining": order["remaining"],
                    "legs_text": order["legs_text"],
                }
            )
        return rows

    @staticmethod
    def _public_order(order: dict) -> dict:
        return {
            "id": order["id"],
            "seq": order["seq"],
            "kind": order["kind"],
            "leg": order["leg"],
            "direction": order["direction"],
            "lots": order["lots"],
            "filled": order["filled"],
            "remaining": order["remaining"],
            "status": order["status"],
            "legs_text": order["legs_text"],
        }

    def reset(self, payload: dict) -> dict:
        with self._lock:
            cash = _parse_int(payload.get("cash"), "账户资金", minimum=0)
            near = _parse_int(payload.get("near_position"), "期初近月净持仓")
            far = _parse_int(payload.get("far_position"), "期初远月净持仓")
            self.state = _new_state()
            self.state["cash"] = cash
            self.state["near_position"] = near
            self.state["far_position"] = far
            self._log(
                f"初始化：资金 {cash} 元，近月净持仓 {near:+d} 手，"
                f"远月净持仓 {far:+d} 手"
            )
            return self.snapshot()

    def place_order(self, payload: dict, trial: bool = False) -> dict:
        """挂单；trial=True 时只做资金试算，不写入任何状态。"""
        with self._lock:
            kind = str(payload.get("kind", "")).strip()
            if kind not in ("single", "spread"):
                raise ApiError("单据类型 kind 必须是 single 或 spread")
            direction = str(payload.get("direction", "")).strip()
            if direction not in ("buy", "sell"):
                raise ApiError("方向 direction 必须是 buy 或 sell")
            leg = None
            if kind == "single":
                leg = str(payload.get("leg", "")).strip()
                if leg not in ("near", "far"):
                    raise ApiError("单腿单必须指定 leg=near 或 far")
            lots = _parse_int(
                payload.get("lots"), "手数", minimum=1, maximum=MAX_LOTS_PER_ORDER
            )

            if kind == "spread":
                near_delta, far_delta = margin.order_delta(kind, direction)
            else:
                near_delta, far_delta = margin.single_leg_delta(leg, direction)

            active = self._active_orders()
            if len(active) >= MAX_ACTIVE_ORDERS:
                raise ApiError(
                    f"活动挂单已达上限 {MAX_ACTIVE_ORDERS} 笔，"
                    "请先成交或撤销部分单据"
                )

            candidate = {
                "id": "trial" if trial else uuid.uuid4().hex[:8],
                "seq": (self.state["order_seq"] + 1) if not trial else -1,
                "kind": kind,
                "leg": leg,
                "direction": direction,
                "lots": lots,
                "filled": 0,
                "remaining": lots,
                "status": "active",
                "near_delta": near_delta,
                "far_delta": far_delta,
                "legs_text": margin.describe_legs(kind, leg, direction),
            }

            calc = self._recalc(extra_orders=[candidate])
            trial_result = {
                "accepted": calc["max_margin"] <= self.state["cash"],
                "cash": self.state["cash"],
                "projected_max_margin": calc["max_margin"],
                "projected_current_margin": calc["current_margin"],
                "shortfall": max(0, calc["max_margin"] - self.state["cash"]),
                "projected_near": calc["near_position"],
                "projected_far": calc["far_position"],
                "projected_pairs": calc["pairs"],
                "combinations": calc["combinations"],
                "fills": [
                    {
                        "seq": active[i]["seq"] if i < len(active) else None,
                        "order_id": calc["order_ids"][i],
                        "fill_lots": qty,
                        "remaining": (
                            active[i]["remaining"]
                            if i < len(active)
                            else candidate["remaining"]
                        ),
                        "legs_text": (
                            active[i]["legs_text"]
                            if i < len(active)
                            else candidate["legs_text"]
                        ),
                        "is_candidate": i == len(active),
                    }
                    for i, qty in enumerate(calc["fills"])
                    if qty > 0
                ],
            }

            if trial:
                # 纯试算：不触碰持仓与挂单
                return {"trial": trial_result}

            if not trial_result["accepted"]:
                # 拒单：原持仓和挂单照旧
                self._log(
                    f"拒单：{candidate['legs_text']} {lots} 手，"
                    f"试算上限 {calc['max_margin']} 元 > 资金 "
                    f"{self.state['cash']} 元（缺口 {trial_result['shortfall']} 元）"
                )
                raise ApiError(
                    "试挂超出资金，拒单（原持仓和挂单保持不变）",
                    422,
                    extra={"trial": trial_result},
                )

            self.state["order_seq"] += 1
            candidate["id"] = uuid.uuid4().hex[:8]
            candidate["seq"] = self.state["order_seq"]
            self.state["orders"].append(candidate)
            self._log(
                f"挂单 #{candidate['seq']}：{candidate['legs_text']} {lots} 手"
            )
            return {"snapshot": self.snapshot(), "trial": trial_result}

    def fill_order(self, order_id: str, payload: dict) -> dict:
        """登记成交：单腿独立成交；跨期两腿等量同步成交。"""
        with self._lock:
            order = self._get_order(order_id)
            if order["status"] != "active":
                raise ApiError(f"单据 #{order['seq']} 已{order['status']}，不可成交")
            qty = _parse_int(
                payload.get("lots"),
                "成交手数",
                minimum=1,
                maximum=order["remaining"],
            )

            self.state["near_position"] += order["near_delta"] * qty
            self.state["far_position"] += order["far_delta"] * qty
            order["filled"] += qty
            order["remaining"] -= qty
            if order["remaining"] == 0:
                order["status"] = "filled"

            if order["kind"] == "spread":
                leg_note = (
                    f"近月 {order['near_delta'] * qty:+d} / "
                    f"远月 {order['far_delta'] * qty:+d}，两腿同步"
                )
            else:
                month = "近月" if order["leg"] == "near" else "远月"
                delta = order["near_delta"] if order["leg"] == "near" else order["far_delta"]
                leg_note = f"{month} {delta * qty:+d}"
            self._log(
                f"成交 #{order['seq']} {qty} 手（{leg_note}），"
                f"剩余 {order['remaining']} 手"
            )
            return self.snapshot()

    def cancel_order(self, order_id: str) -> dict:
        """撤单：只撤未成交部分，已成交部分保留。"""
        with self._lock:
            order = self._get_order(order_id)
            if order["status"] != "active":
                raise ApiError(f"单据 #{order['seq']} 已{order['status']}，不可撤销")
            cancelled = order["remaining"]
            if cancelled == 0:
                raise ApiError("该单没有可撤的未成交部分")
            order["remaining"] = 0
            order["status"] = "cancelled"
            self._log(
                f"撤单 #{order['seq']}：撤销未成交 {cancelled} 手"
                f"（已成交 {order['filled']} 手保留）"
            )
            return self.snapshot()


def _parse_int(
    value: Any, field: str, minimum: int | None = None, maximum: int | None = None
) -> int:
    """严格整数解析：拒绝浮点、空白、非整数字符串。"""
    if isinstance(value, bool):
        raise ApiError(f"{field}必须是整数")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        result = int(value.strip())
    else:
        raise ApiError(f"{field}必须是整数")
    if minimum is not None and result < minimum:
        raise ApiError(f"{field}不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ApiError(f"{field}不能大于 {maximum}")
    return result


def create_app() -> Flask:
    app = Flask(__name__)
    store = OrderStore()
    app.extensions["order_store"] = store

    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        return jsonify({"error": err.message, **err.extra}), err.status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def get_state():
        return jsonify(store.snapshot())

    @app.post("/api/setup")
    def setup():
        return jsonify(store.reset(request.get_json(silent=True) or {}))

    @app.post("/api/orders")
    def place_order():
        result = store.place_order(request.get_json(silent=True) or {})
        return jsonify(result)

    @app.post("/api/orders/trial")
    def trial_order():
        result = store.place_order(request.get_json(silent=True) or {}, trial=True)
        return jsonify(result)

    @app.post("/api/orders/<order_id>/fill")
    def fill_order(order_id: str):
        return jsonify(store.fill_order(order_id, request.get_json(silent=True) or {}))

    @app.post("/api/orders/<order_id>/cancel")
    def cancel_order(order_id: str):
        return jsonify(store.cancel_order(order_id))

    return app
