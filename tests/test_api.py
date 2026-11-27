"""Flask API 端到端测试：挂单、拒单试算、成交、撤单、上限重算与约束。"""

import pytest

from app import create_app
from app.server import MAX_ACTIVE_ORDERS, MAX_LOTS_PER_ORDER


@pytest.fixture()
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def post(client, url, payload=None):
    return client.post(url, json=payload or {})


def setup(client, cash=2000, near=2, far=-1):
    resp = post(client, "/api/setup", {"cash": cash, "near_position": near, "far_position": far})
    assert resp.status_code == 200
    return resp.get_json()


def test_initial_state(client):
    data = client.get("/api/state").get_json()
    assert data["cash"] == 2000
    assert data["near_position"] == 2
    assert data["far_position"] == -1
    # 220 = 100*3 - 80
    assert data["current_margin"] == 220
    assert data["max_margin"] == 220
    assert data["orders"] == []


def test_place_single_order_and_worst_case(client):
    setup(client)
    # 近月买 3 手：全成交 => 近月 +5 远月 -1，600-80=520
    resp = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 3,
    })
    assert resp.status_code == 200
    snap = resp.get_json()["snapshot"]
    order = snap["orders"][0]
    assert order["remaining"] == 3 and order["status"] == "active"
    assert snap["max_margin"] == 520
    assert snap["worst_fills"][0]["fill_lots"] == 3
    assert snap["worst_meta"]["combinations"] == 2


def test_spread_order_legs(client):
    setup(client, cash=5000, near=0, far=0)
    resp = post(client, "/api/orders", {
        "kind": "spread", "direction": "buy", "lots": 4,
    })
    snap = resp.get_json()["snapshot"]
    # 全成交 => +4 / -4 => 800 - 320 = 480
    assert snap["max_margin"] == 480
    order = snap["orders"][0]
    assert "近月" in order["legs_text"] and "远月" in order["legs_text"]


def test_reject_order_when_over_cash_keeps_state_unchanged(client):
    setup(client, cash=300, near=0, far=0)
    before = client.get("/api/state").get_json()
    # 近月买 10 手 => 上限 1000 > 300，应拒单
    resp = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 10,
    })
    assert resp.status_code == 422
    body = resp.get_json()
    trial = body["trial"]
    assert trial["accepted"] is False
    assert trial["projected_max_margin"] == 1000
    assert trial["shortfall"] == 700
    assert any(f["is_candidate"] for f in trial["fills"])

    after = client.get("/api/state").get_json()
    # 原持仓和挂单照旧
    assert after["orders"] == before["orders"]
    assert after["near_position"] == before["near_position"] == 0
    assert after["far_position"] == before["far_position"] == 0
    assert after["cash"] == before["cash"] == 300
    # 拒单事件写入日志
    assert any("拒单" in line for line in after["logs"])


def test_trial_does_not_mutate_state(client):
    setup(client, cash=300, near=0, far=0)
    before = client.get("/api/state").get_json()
    resp = post(client, "/api/orders/trial", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 10,
    })
    assert resp.status_code == 200
    trial = resp.get_json()["trial"]
    assert trial["accepted"] is False
    after = client.get("/api/state").get_json()
    assert after["orders"] == before["orders"]
    assert after["max_margin"] == before["max_margin"] == 0


def test_trial_accepted_preview(client):
    setup(client, cash=5000)
    resp = post(client, "/api/orders/trial", {
        "kind": "spread", "direction": "buy", "lots": 2,
    })
    trial = resp.get_json()["trial"]
    assert trial["accepted"] is True
    assert trial["shortfall"] == 0


def test_fill_single_updates_position_and_remaining(client):
    setup(client)
    oid = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 3,
    }).get_json()["snapshot"]["orders"][0]["id"]

    resp = post(client, f"/api/orders/{oid}/fill", {"lots": 2})
    snap = resp.get_json()
    assert snap["near_position"] == 4   # 2 + 2
    assert snap["far_position"] == -1
    order = snap["orders"][0]
    assert order["filled"] == 2 and order["remaining"] == 1
    assert order["status"] == "active"
    # 上限随剩余手数重算：再成交 1 手 => 5/-1 => 520
    assert snap["max_margin"] == 520

    # 余下 1 手成交后单据置为 filled
    snap = post(client, f"/api/orders/{oid}/fill", {"lots": 1}).get_json()
    assert snap["orders"][0]["status"] == "filled"
    assert snap["orders"][0]["remaining"] == 0
    assert snap["near_position"] == 5
    # 无活动挂单，上限回到当前保证金
    assert snap["max_margin"] == snap["current_margin"] == 520


def test_spread_fill_is_synchronized(client):
    setup(client, cash=5000, near=0, far=0)
    oid = post(client, "/api/orders", {
        "kind": "spread", "direction": "buy", "lots": 5,
    }).get_json()["snapshot"]["orders"][0]["id"]

    snap = post(client, f"/api/orders/{oid}/fill", {"lots": 3}).get_json()
    assert snap["near_position"] == 3
    assert snap["far_position"] == -3
    order = snap["orders"][0]
    assert order["filled"] == 3 and order["remaining"] == 2


def test_fill_validation(client):
    setup(client)
    oid = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 2,
    }).get_json()["snapshot"]["orders"][0]["id"]

    # 超过剩余手数
    resp = post(client, f"/api/orders/{oid}/fill", {"lots": 3})
    assert resp.status_code == 400
    # 零或负数
    assert post(client, f"/api/orders/{oid}/fill", {"lots": 0}).status_code == 400
    # 已成交完的单据不可再成交
    post(client, f"/api/orders/{oid}/fill", {"lots": 2})
    resp = post(client, f"/api/orders/{oid}/fill", {"lots": 1})
    assert resp.status_code == 400


def test_cancel_only_removes_unfilled_part(client):
    setup(client)
    oid = post(client, "/api/orders", {
        "kind": "single", "leg": "far", "direction": "sell", "lots": 4,
    }).get_json()["snapshot"]["orders"][0]["id"]
    # 先成交 1 手
    snap = post(client, f"/api/orders/{oid}/fill", {"lots": 1}).get_json()
    assert snap["far_position"] == -2  # -1 - 1

    # 撤余单：只撤剩余 3 手，已成交保留
    snap = post(client, f"/api/orders/{oid}/cancel").get_json()
    order = snap["orders"][0]
    assert order["status"] == "cancelled"
    assert order["filled"] == 1
    assert order["remaining"] == 0
    assert snap["far_position"] == -2
    # 撤单后重算：无活动挂单，上限 = 当前保证金；当前 2 / -2 => 400-160=240
    assert snap["max_margin"] == 240
    assert snap["current_margin"] == 240

    # 已撤单据不能再撤/再成交
    assert post(client, f"/api/orders/{oid}/cancel").status_code == 400
    assert post(client, f"/api/orders/{oid}/fill", {"lots": 1}).status_code == 400


def test_order_constraints(client):
    setup(client, cash=10_000_000, near=0, far=0)

    # 单笔超过 10 手
    resp = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": MAX_LOTS_PER_ORDER + 1,
    })
    assert resp.status_code == 400

    # 非法类型 / 方向 / 缺腿
    assert post(client, "/api/orders", {"kind": "futures", "direction": "buy", "lots": 1}).status_code == 400
    assert post(client, "/api/orders", {"kind": "single", "direction": "buy", "lots": 1}).status_code == 400
    assert post(client, "/api/orders", {"kind": "spread", "direction": "hold", "lots": 1}).status_code == 400
    # 浮点与字符串数字的严格校验
    assert post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 1.5,
    }).status_code == 400

    # 挂满 12 笔后第 13 笔被拒
    for i in range(MAX_ACTIVE_ORDERS):
        ok = post(client, "/api/orders", {
            "kind": "single", "leg": "near", "direction": "buy", "lots": 1,
        })
        assert ok.status_code == 200
    overflow = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 1,
    })
    assert overflow.status_code == 400
    assert "上限" in overflow.get_json()["error"]

    # 成交一笔释放名额后可再挂
    first_id = client.get("/api/state").get_json()["orders"][0]["id"]
    post(client, f"/api/orders/{first_id}/fill", {"lots": 1})
    again = post(client, "/api/orders", {
        "kind": "single", "leg": "near", "direction": "buy", "lots": 1,
    })
    assert again.status_code == 200


def test_setup_validation_and_reset(client):
    # 负资金不允许
    resp = post(client, "/api/setup", {"cash": -1, "near_position": 0, "far_position": 0})
    assert resp.status_code == 400
    # 非整数
    resp = post(client, "/api/setup", {"cash": 1.5, "near_position": 0, "far_position": 0})
    assert resp.status_code == 400

    snap = setup(client, cash=100, near=-3, far=3)
    assert snap["cash"] == 100
    assert snap["near_position"] == -3 and snap["far_position"] == 3
    # -3/+3 完全配对：600 - 240 = 360，期初已超资金 -> 页面可见缺口
    assert snap["current_margin"] == 360
    assert snap["headroom"] == 100 - 360


def test_index_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "期货跨期保证金演练" in html
    assert "app.js" in html


def test_full_workflow_invariant_margin_never_exceeds_cash_after_accept(client):
    """被接受的挂单，其任何部分成交后当前保证金都不应超过当时资金（上限约束的推论）。"""
    setup(client, cash=800, near=1, far=0)

    # 挂两张单：跨期买 2 手、近月卖 3 手
    r1 = post(client, "/api/orders", {"kind": "spread", "direction": "buy", "lots": 2})
    assert r1.status_code == 200
    r2 = post(client, "/api/orders", {"kind": "single", "leg": "near", "direction": "sell", "lots": 3})
    assert r2.status_code == 200
    ids = [o["id"] for o in client.get("/api/state").get_json()["orders"]]

    # 随意顺序、部分数量成交，每步检查资金约束
    post(client, f"/api/orders/{ids[0]}/fill", {"lots": 2})
    snap = client.get("/api/state").get_json()
    assert snap["current_margin"] <= snap["cash"]

    post(client, f"/api/orders/{ids[1]}/fill", {"lots": 1})
    snap = client.get("/api/state").get_json()
    assert snap["current_margin"] <= snap["cash"]

    post(client, f"/api/orders/{ids[1]}/fill", {"lots": 2})
    snap = client.get("/api/state").get_json()
    assert snap["current_margin"] <= snap["cash"]
    assert snap["max_margin"] == snap["current_margin"]
