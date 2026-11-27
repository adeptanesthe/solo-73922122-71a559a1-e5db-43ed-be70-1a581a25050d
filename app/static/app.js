/* 期货跨期保证金演练台前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);

let state = null;
let messageTimer = null;

async function api(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || `请求失败（HTTP ${resp.status}）`);
    err.payload = data;
    throw err;
  }
  return data;
}

function showMessage(text, kind = "error") {
  const box = $("#message");
  box.className = `message ${kind}`;
  box.textContent = text;
  box.classList.remove("hidden");
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => box.classList.add("hidden"), kind === "error" ? 9000 : 4000);
}

function fmt(n) {
  return Number(n).toLocaleString("zh-CN");
}

function signed(n) {
  return (n > 0 ? "+" : "") + n;
}

const KIND_CN = { single: "单腿", spread: "跨期" };
const DIR_CN = { buy: "买入", sell: "卖出" };
const STATUS_CN = { active: "活动", filled: "全部成交", cancelled: "已撤销" };

function render() {
  if (!state) return;

  $("#stat-cash").textContent = fmt(state.cash);
  $("#stat-current").textContent = fmt(state.current_margin);
  $("#stat-max").textContent = fmt(state.max_margin);
  $("#stat-headroom").textContent = fmt(state.headroom);
  $("#stat-near").textContent = signed(state.near_position);
  $("#stat-far").textContent = signed(state.far_position);

  const maxCard = $("#stat-max").closest(".stat");
  const headCard = $("#stat-headroom").closest(".stat");
  maxCard.classList.toggle("over", state.max_margin > state.cash);
  headCard.classList.toggle("over", state.headroom < 0);
  headCard.classList.toggle("ok", state.headroom >= 0);

  const active = state.orders.filter((o) => o.status === "active");
  $("#active-count").textContent = active.length;
  $("#active-limit").textContent = state.limits.max_active_orders;
  $("#lot-limit").textContent = state.limits.max_lots_per_order;
  $("#combo-count").textContent = fmt(state.worst_meta.combinations);

  // 单据表
  const tbody = $("#orders-tbody");
  tbody.innerHTML = "";
  if (state.orders.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="hint">暂无单据，先在上方挂单。</td></tr>';
  }
  for (const o of state.orders) {
    const tr = document.createElement("tr");
    const ops =
      o.status === "active"
        ? `<form class="fill-form" data-action="fill" data-id="${o.id}">
             <input type="number" min="1" max="${o.remaining}" step="1" value="${o.remaining}"
                    name="lots" aria-label="成交手数">
             <button class="btn btn-sm btn-primary" type="submit">报成交</button>
           </form>
           <button class="btn btn-sm btn-danger" data-action="cancel" data-id="${o.id}">撤余单</button>`
        : '<span class="hint">—</span>';
    tr.innerHTML = `
      <td>#${o.seq}</td>
      <td>${KIND_CN[o.kind]}${o.kind === "spread" ? "（同步）" : ""}</td>
      <td>${o.legs_text}</td>
      <td>${DIR_CN[o.direction]}</td>
      <td class="num">${o.lots}</td>
      <td class="num">${o.filled}</td>
      <td class="num">${o.remaining}</td>
      <td><span class="badge badge-${o.status}">${STATUS_CN[o.status]}</span></td>
      <td><div class="fill-form">${ops}</div></td>`;
    tbody.appendChild(tr);
  }

  // 上限组合
  const meta = state.worst_meta;
  const over = state.max_margin > state.cash;
  $("#worst-summary").innerHTML = `
    <span>最高保证金：<b class="${over ? "over-limit" : ""}">${fmt(state.max_margin)} 元</b></span>
    <span>组合数（顶点枚举）：<b>${fmt(meta.combinations)}</b></span>
    <span>达成时近月净持仓：<b>${signed(meta.near_position)} 手</b></span>
    <span>达成时远月净持仓：<b>${signed(meta.far_position)} 手</b></span>
    <span>对冲配对：<b>${meta.pairs} 对（每对减 80 元）</b></span>
    <span>资金缺口：<b class="${over ? "over-limit" : ""}">${fmt(Math.max(0, state.max_margin - state.cash))} 元</b></span>`;

  const wt = $("#worst-tbody");
  wt.innerHTML = "";
  $("#worst-empty").style.display = active.length === 0 ? "block" : "none";
  for (const f of state.worst_fills) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>#${f.seq}（${KIND_CN[f.kind]}）</td>
      <td>${f.legs_text}</td>
      <td class="num">${f.fill_lots}</td>
      <td class="num">${f.remaining}</td>`;
    wt.appendChild(tr);
  }

  // 日志
  const logList = $("#log-list");
  logList.innerHTML = "";
  for (const line of state.logs.slice().reverse()) {
    const li = document.createElement("li");
    li.textContent = line;
    logList.appendChild(li);
  }
}

function orderPreview() {
  const kind = $("#order-kind").value;
  const dir = $("#order-direction").value;
  const leg = $("#order-leg").value;
  $("#leg-wrap").style.display = kind === "single" ? "" : "none";
  let text;
  if (kind === "spread") {
    const near = dir === "buy" ? "买 +1" : "卖 -1";
    const far = dir === "buy" ? "卖 -1" : "买 +1";
    text = `跨期单：近月${near} / 远月${far}（每手，两腿等量同步成交）`;
  } else {
    const month = leg === "near" ? "近月" : "远月";
    text = `单腿单：${month}${DIR_CN[dir]}（每手，可独立成交）`;
  }
  $("#order-preview").textContent = text;
}

function readOrderForm() {
  return {
    kind: $("#order-kind").value,
    leg: $("#order-leg").value,
    direction: $("#order-direction").value,
    lots: Number($("#order-lots").value),
  };
}

async function refresh() {
  state = await api("/api/state");
  render();
}

// 期初设置
$("#setup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const payload = {
    cash: Number($("#setup-cash").value),
    near_position: Number($("#setup-near").value),
    far_position: Number($("#setup-far").value),
  };
  try {
    state = await api("/api/setup", { method: "POST", body: JSON.stringify(payload) });
    render();
    showMessage("已按新参数重置演练。", "success");
  } catch (err) {
    showMessage(err.message);
  }
});

// 挂单类型/方向联动提示
$("#order-kind").addEventListener("change", orderPreview);
$("#order-direction").addEventListener("change", orderPreview);
$("#order-leg").addEventListener("change", orderPreview);

// 提交挂单
$("#order-form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.id === "btn-trial") return;
  ev.preventDefault();
  try {
    const data = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify(readOrderForm()),
    });
    state = data.snapshot;
    render();
    showMessage("挂单成功，已按新挂单重算上限。", "success");
  } catch (err) {
    if (err.payload && err.payload.trial) {
      showTrialModal(err.payload.trial, /*rejected=*/ true, err.message);
      showMessage(err.message, "error");
    } else {
      showMessage(err.message);
    }
    refresh(); // 拒单后原状态不变，拉回确认
  }
});

// 仅试算
$("#btn-trial").addEventListener("click", async () => {
  try {
    const data = await api("/api/orders/trial", {
      method: "POST",
      body: JSON.stringify(readOrderForm()),
    });
    showTrialModal(data.trial, /*rejected=*/ false, null);
  } catch (err) {
    showMessage(err.message);
  }
});

function showTrialModal(trial, rejected, errorText) {
  const head = rejected
    ? `<div class="trial-head reject">✗ ${errorText || "试挂超出资金，拒单"}（原持仓和挂单不变）</div>`
    : `<div class="trial-head accept">✓ 试算通过：该单挂出后上限仍不超过账户资金（未实际挂单）</div>`;
  const rows = trial.fills
    .map(
      (f) => `<tr>
        <td>${f.is_candidate ? "本次试挂单" : "活动挂单"}${f.seq ? " #" + f.seq : ""}</td>
        <td>${f.legs_text}</td>
        <td class="num">${f.fill_lots}</td>
        <td class="num">${f.remaining}</td>
      </tr>`
    )
    .join("");
  $("#trial-body").innerHTML = `
    ${head}
    <dl class="kv">
      <dt>账户资金</dt><dd>${fmt(trial.cash)} 元</dd>
      <dt>试算最高保证金</dt><dd class="${trial.accepted ? "" : "over-limit"}">${fmt(trial.projected_max_margin)} 元</dd>
      <dt>当前持仓保证金</dt><dd>${fmt(trial.projected_current_margin)} 元</dd>
      <dt>资金缺口</dt><dd class="${trial.shortfall > 0 ? "over-limit" : ""}">${fmt(trial.shortfall)} 元</dd>
      <dt>达成时近月 / 远月净持仓</dt><dd>${signed(trial.projected_near)} / ${signed(trial.projected_far)} 手</dd>
      <dt>对冲配对 / 枚举组合数</dt><dd>${trial.projected_pairs} 对 / ${fmt(trial.combinations)} 组</dd>
    </dl>
    <h3>达到最高保证金的一组成交手数</h3>
    <table class="table">
      <thead><tr><th>单据</th><th>合约腿</th><th>成交手数</th><th>剩余手数</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="hint">该组合下没有单据需要成交。</td></tr>'}</tbody>
    </table>`;
  $("#trial-modal").classList.remove("hidden");
}
$("#trial-close").addEventListener("click", () => $("#trial-modal").classList.add("hidden"));
$("#trial-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "trial-modal") $("#trial-modal").classList.add("hidden");
});

// 成交 / 撤单（事件委托）
$("#orders-tbody").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-action='cancel']");
  if (!btn) return;
  ev.preventDefault();
  try {
    state = await api(`/api/orders/${btn.dataset.id}/cancel`, { method: "POST", body: "{}" });
    render();
    showMessage("已撤销未成交部分，并重新计算上限。", "success");
  } catch (err) {
    showMessage(err.message);
  }
});

$("#orders-tbody").addEventListener("submit", async (ev) => {
  const form = ev.target.closest("form[data-action='fill']");
  if (!form) return;
  ev.preventDefault();
  const input = form.querySelector("input[name='lots']");
  try {
    state = await api(`/api/orders/${form.dataset.id}/fill`, {
      method: "POST",
      body: JSON.stringify({ lots: Number(input.value) }),
    });
    render();
    showMessage("成交已登记，净持仓与剩余手数已更新，并重算上限。", "success");
  } catch (err) {
    showMessage(err.message);
  }
});

async function init() {
  orderPreview();
  try {
    state = await api("/api/state");
    $("#setup-cash").value = state.cash;
    $("#setup-near").value = state.near_position;
    $("#setup-far").value = state.far_position;
    render();
  } catch (err) {
    showMessage(err.message);
  }
}

init();
