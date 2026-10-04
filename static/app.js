/* 港区冷藏箱供电台账：台账视图 + 操作入口 + 审计来源追溯。 */
const $ = (sel) => document.querySelector(sel);

const state = { role: "electrician", ledger: null };

function headers() {
  return { "X-User-Id": "web-" + state.role, "X-Role": state.role, "X-Org": "yard" };
}

function toast(message, kind = "info") {
  const box = $("#toast");
  const el = document.createElement("div");
  el.className = "toast-item toast-" + kind;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
}

function pill(s) {
  return `<span class="pill s-${esc(s)}">${esc(stateLabel(s))}</span>`;
}

function stateLabel(s) {
  return {
    waiting: "待安排", connected: "已接电", queued: "排队中",
    pending_circuit: "待补回路", pending_recovery: "待补(恢复)",
    loaded: "已装船", cancelled: "已取消",
    active: "投用", tripped: "跳闸", maintenance: "检修",
    complete: "完成", failed: "失败", running: "进行中",
    candidate: "候选", discarded: "已弃单", queue: "已转排队",
    connected_x: "已失效", loaded_frozen: "装船冻结",
  }[s] || s;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { ...headers(), "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || ("HTTP " + res.status));
    err.payload = data;
    throw err;
  }
  return data;
}

function formObject(form) {
  const out = {};
  new FormData(form).forEach((v, k) => { out[k] = v; });
  return out;
}

function num(v, d = null) {
  if (v === "" || v === null || v === undefined) return d;
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

async function loadLedger() {
  try {
    state.ledger = await api("/api/ledger");
    render();
  } catch (e) {
    toast("台账加载失败：" + e.message, "err");
  }
}

function render() {
  const L = state.ledger;
  if (!L) return;
  renderStats(L);
  renderReefers(L);
  renderCircuits(L);
  renderQueue(L);
  renderConflicts(L);
  renderTrips(L);
  renderVoyagesBatches(L);
  renderEvents(L);
}

function renderStats(L) {
  const rs = L.reefers.reduce((m, r) => (m[r.state] = (m[r.state] || 0) + 1, m), {});
  $("#stat-line").textContent =
    `冷藏箱 ${L.reefers.length}（接电 ${rs.connected || 0}｜排队 ${(rs.queued || 0) + (rs.pending_circuit || 0) + (rs.pending_recovery || 0)}｜装船 ${rs.loaded || 0}）`
    + ` 回路 ${L.circuits.length} 跳闸未恢复 ${L.trips.filter((t) => !t.recovered_at).length}`
    + ` 队列 ${L.queue.length} 冲突候选 ${L.conflicts.filter((x) => x.state === "candidate").length}`;
  $("#cnt-reefers").textContent = "共 " + L.reefers.length;
}

function renderReefers(L) {
  const byConn = {};
  L.reefers.forEach((r) => {
    if (r.connection) byConn[r.connection.id] = r;
  });
  $("#t-reefers tbody").innerHTML = L.reefers.map((r) => {
    const conn = r.connection;
    const connText = conn
      ? `${esc(r.circuit_code || "?")} <span class="muted">#${conn.id} · ${esc(conn.state === "loaded_frozen" ? "装船冻结" : "在用")}</span>`
      : '<span class="muted">—</span>';
    const gap = r.queue ? `<span class="gap">${r.queue.gap_kw}</span>` : "";
    const voyage = r.voyage_id ? `#${r.voyage_id}` : "";
    const actions = [];
    if (["waiting", "queued", "pending_circuit", "pending_recovery"].includes(r.state)) {
      actions.push(`<button data-act="connect" data-id="${r.id}">接电</button>`);
    }
    if (r.state === "connected") {
      actions.push(`<button class="primary" data-act="load" data-id="${r.id}">装船</button>`);
    }
    return `<tr class="actable" data-audit="reefer:${r.id}">
      <td>${r.id}</td><td>${esc(r.reefer_no)}<div class="muted">${r.required_kw}kW · ${r.temp_setpoint_c}±${r.temp_tolerance_c}℃</div></td>
      <td>${pill(r.state)}</td><td>${connText}</td><td>${gap}</td><td>${voyage}</td>
      <td>${actions.join(" ")}</td></tr>`;
  }).join("");
}

function renderCircuits(L) {
  $("#t-circuits tbody").innerHTML = L.circuits.map((c) => {
    const ratio = c.capacity_kw ? Math.min(100, Math.round((c.used_kw / c.capacity_kw) * 100)) : 0;
    const barColor = ratio >= 100 ? "#c9352c" : ratio >= 80 ? "#b26a00" : "#1a7f37";
    const ops = c.state === "active"
      ? `<button class="danger" data-act="trip" data-id="${c.id}">跳闸</button>
         <button data-act="maintenance" data-id="${c.id}">检修</button>`
      : `<button class="primary" data-act="recover-circuit" data-id="${c.id}">恢复投用</button>`;
    return `<tr class="actable" data-audit="circuit:${c.id}">
      <td>${c.id}</td><td>${esc(c.circuit_code)}<div class="muted">${esc(c.bay || "")}</div></td>
      <td>${pill(c.state)}</td>
      <td><div style="width:110px;background:#eef1f4;border-radius:5px;height:8px;overflow:hidden">
        <div style="width:${ratio}%;height:100%;background:${barColor}"></div></div>
        ${c.used_kw}/${c.capacity_kw} kW</td>
      <td>${c.temp_min_c}~${c.temp_max_c}</td><td>${ops}</td></tr>`;
  }).join("");
}

function renderQueue(L) {
  $("#t-queue tbody").innerHTML = L.queue.length ? L.queue.map((q) =>
    `<tr><td>${q.reefer_id}</td><td>${pill(q.reason)}</td>
     <td class="${q.gap_kw > 0 ? "gap" : ""}">${q.gap_kw}</td>
     <td><span class="src">${esc(q.source || "")}</span></td></tr>`).join("")
    : '<tr><td colspan="4" class="muted">队列为空</td></tr>';
}

function renderConflicts(L) {
  const open = L.conflicts.filter((x) => x.state === "candidate");
  $("#t-conflicts tbody").innerHTML = open.length ? open.map((x) =>
    `<tr><td>${x.id}</td><td>${x.reefer_id}</td><td>${esc(x.actor_id)}</td>
     <td><button data-act="resolve-queue" data-id="${x.id}">转排队</button>
         <button data-act="resolve-discard" data-id="${x.id}">弃单</button></td></tr>`).join("")
    : '<tr><td colspan="4" class="muted">无未处理冲突候选</td></tr>';
}

function renderTrips(L) {
  $("#t-trips tbody").innerHTML = L.trips.map((t) =>
    `<tr class="actable" data-audit="circuit:${t.circuit_id}">
      <td>${t.id}</td><td>${t.circuit_id}</td><td>${esc(t.reason)}</td>
      <td class="muted">${esc((t.occurred_at || "").replace("T", " ").slice(0, 19))}</td>
      <td>${t.recovered_at ? '<span class="muted">已恢复</span>' : pill("tripped")}</td></tr>`).join("");
}

function renderVoyagesBatches(L) {
  $("#t-voyages tbody").innerHTML = L.voyages.map((v) =>
    `<tr class="actable" data-audit="voyage:${v.id}">
      <td>${v.id}</td><td>${esc(v.vessel)} · ${esc(v.voyage_no)}</td><td>${v.sail_hour}</td>
      <td><button data-act="revise" data-id="${v.id}">改期</button></td></tr>`).join("");
  $("#t-batches tbody").innerHTML = L.batches.map((b) =>
    `<tr class="actable" data-audit="batch:${b.id}">
      <td>${b.id}</td><td>${esc(b.batch_no)}</td><td>${pill(b.state)}</td>
      <td>${b.size}<div class="muted">${esc(b.note || "")}</div></td></tr>`).join("");
}

function renderEvents(L) {
  const interesting = new Set(["connected", "queued", "tripped", "restored", "invalidated",
    "rescheduled", "restored", "pending_recovery", "loaded", "basis_kept", "conflict_candidate",
    "revised", "maintenance", "recovery_applied", "failed", "complete"]);
  const rows = (window._events || []);
  $("#t-events tbody").innerHTML = rows.length ? rows.slice(0, 60).map((e) =>
    `<tr class="actable" data-audit="${e.entity_type}:${e.entity_id}">
      <td>${esc(e.entity_type)}#${e.entity_id}</td><td>${esc(e.action)}</td>
      <td>${esc((e.details && e.details.summary) || "")}</td>
      <td>${e.details && e.details.source ? `<span class="src">${esc(e.details.source)}</span>` : ""}</td></tr>`).join("")
    : '<tr><td colspan="4" class="muted">加载中…</td></tr>';
}

async function loadEvents() {
  try {
    const data = await api("/api/events?limit=100");
    window._events = data.items || [];
    if (state.ledger) renderEvents(state.ledger);
  } catch (e) { /* 审计加载失败不阻塞台账 */ }
}

async function openTimeline(type, id) {
  $("#dlg-title").textContent = `审计时间线 · ${type} #${id}`;
  const plural = { reefer: "reefers", circuit: "circuits", voyage: "voyages", batch: "batches" }[type];
  try {
    const data = await api(`/api/${plural}/${id}/audit`);
    $("#t-timeline tbody").innerHTML = (data.items || []).map((e, i) =>
      `<tr><td>${i + 1}</td><td>${esc(e.action)}</td><td>${esc(e.actor_id)}</td>
       <td>${esc((e.details && e.details.summary) || "")}</td>
       <td>${e.details && e.details.source ? `<span class="src">${esc(e.details.source)}</span>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="5" class="muted">无事件</td></tr>';
    $("#dlg").showModal();
  } catch (e) {
    toast("审计查询失败：" + e.message, "err");
  }
}

async function act(kind, id, payload = {}) {
  const routes = {
    connect: ["POST", `/api/reefers/${id}/connect`],
    load: ["POST", `/api/reefers/${id}/load`],
    trip: ["POST", `/api/circuits/${id}/trip`],
    "recover-circuit": ["POST", `/api/circuits/${id}/recover`],
    maintenance: ["POST", `/api/circuits/${id}/maintenance`],
    revise: ["POST", `/api/voyages/${id}/revise`],
    "resolve-queue": ["POST", `/api/conflicts/${id}/resolve`, { decision: "queue" }],
    "resolve-discard": ["POST", `/api/conflicts/${id}/resolve`, { decision: "discard" }],
  };
  const [method, path, extra] = routes[kind];
  try {
    await api(path, { method, body: JSON.stringify({ data: { ...(extra || {}), ...payload } }) });
    toast("操作成功：" + stateLabel(kind), "ok");
  } catch (e) {
    const hint = e.payload && e.payload.data && e.payload.data.candidate_id
      ? `（冲突候选 #${e.payload.data.candidate_id}）` : "";
    toast("操作未生效：" + e.message + hint, "err");
  }
  await loadLedger(); await loadEvents();
}

document.addEventListener("click", async (ev) => {
  const auditEl = ev.target.closest("tr.actable");
  const btn = ev.target.closest("button[data-act]");
  if (btn) {
    ev.stopPropagation();
    const kind = btn.dataset.act;
    const id = btn.dataset.id ? Number(btn.dataset.id) : null;
    if (["trip", "maintenance"].includes(kind)) {
      const reason = prompt(kind === "trip" ? "跳闸原因？" : "检修原因？", kind === "trip" ? "现场跳闸" : "计划检修");
      if (reason === null) return;
      await act(kind, id, { reason });
    } else if (kind === "revise") {
      const value = prompt("新的开航时刻（小时序号）？", "48");
      if (value === null) return;
      await act(kind, id, { sail_hour: Number(value) });
    } else if (kind === "batch-connect" || kind === "batch-fail" || kind === "recover") {
      const form = $("#f-batch");
      const f = formObject(form);
      const ids = String(f.ids || "").split(",").map((x) => Number(x.trim())).filter(Number.isFinite);
      if (!ids.length) return toast("请填写冷藏箱ID", "err");
      const path = kind === "batch-connect" ? "/api/batches/connect"
        : kind === "batch-fail" ? "/api/batches/fail" : "/api/batches/recover";
      try {
        const r = await api(path, { method: "POST", body: JSON.stringify({ data: { reefer_ids: ids, reason: f.reason } }) });
        toast("批次操作完成：" + JSON.stringify(r.restored ? { 恢复: r.restored.length, 待补: r.pending.length } : r.state || "ok"), "ok");
      } catch (e) { toast("批次未生效：" + e.message, "err"); }
      await loadLedger(); await loadEvents();
    } else if (kind) {
      await act(kind, id);
    }
    return;
  }
  if (auditEl && auditEl.dataset.audit) {
    const [type, eid] = auditEl.dataset.audit.split(":");
    await openTimeline(type, Number(eid));
  }
});

async function submitCreate(path, body) {
  try {
    await api(path, { method: "POST", body: JSON.stringify(body) });
    toast("登记成功", "ok");
    await loadLedger();
  } catch (e) { toast("登记失败：" + e.message, "err"); }
}

$("#f-circuit").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = formObject(e.target);
  submitCreate("/api/circuits", { data: {
    circuit_code: f.circuit_code, bay: f.bay, capacity_kw: num(f.capacity_kw),
    temp_min_c: num(f.temp_min_c), temp_max_c: num(f.temp_max_c) } });
  e.target.reset();
});
$("#f-voyage").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = formObject(e.target);
  submitCreate("/api/voyages", { data: { vessel: f.vessel, voyage_no: f.voyage_no, sail_hour: num(f.sail_hour) } });
  e.target.reset();
});
$("#f-reefer").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = formObject(e.target);
  submitCreate("/api/reefers", { data: {
    reefer_no: f.reefer_no, required_kw: num(f.required_kw),
    temp_setpoint_c: num(f.temp_setpoint_c), temp_tolerance_c: num(f.temp_tolerance_c),
    voyage_id: f.voyage_id ? num(f.voyage_id) : null } });
  e.target.reset();
});
$("#pump").addEventListener("click", async () => {
  try {
    const r = await api("/api/queues/pump", { method: "POST", body: "{}" });
    toast(`重排完成：接电 ${r.connected.length}，仍等待 ${r.still_waiting.length}`, "ok");
  } catch (e2) { toast("重排失败：" + e2.message, "err"); }
  await loadLedger(); await loadEvents();
});
$("#refresh").addEventListener("click", async () => { await loadLedger(); await loadEvents(); });
$("#role").addEventListener("change", async (e) => {
  state.role = e.target.value;
  await loadLedger(); await loadEvents();
});

loadLedger();
loadEvents();
setInterval(() => { loadLedger(); loadEvents(); }, 15000);
