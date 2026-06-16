"use strict";
const $ = (s) => document.querySelector(s);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
};
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
function toast(msg, err) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
const fmtDur = (s) => (s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${(s / 60).toFixed(1)}m`);
const tag = (s) => `<span class="tag ${s || ""}">${s || "?"}</span>`;
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// ---- 导航 ----
document.querySelectorAll("nav a").forEach((a) =>
  a.addEventListener("click", () => {
    document.querySelectorAll("nav a").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll("section").forEach((x) => x.classList.remove("active"));
    a.classList.add("active");
    $("#" + a.dataset.tab).classList.add("active");
    if (a.dataset.tab === "board") loadBoard();
  })
);

// ---- 提交表单：随执行方式切换提示 ----
$("#f-mode").addEventListener("change", () => {
  const m = $("#f-mode").value;
  $("#f-value-label").textContent =
    m === "remote_script" ? "脚本路径，如 ~/proj/submit_exec.sh"
    : m === "remote" ? "远端命令，如 cd ~/proj && bash run.sh"
    : "粘贴要上传执行的 bash 脚本内容";
});

let charts = {};
function drawChart(id, type, labels, data, colors) {
  if (!window.Chart) return;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($("#" + id), {
    type,
    data: { labels, datasets: [{ data, backgroundColor: colors || "#4f8cff" }] },
    options: { plugins: { legend: { display: type !== "bar", labels: { color: "#9aa3b2" } } },
      scales: type === "bar" ? { x: { ticks: { color: "#9aa3b2" } }, y: { ticks: { color: "#9aa3b2" } } } : {} },
  });
}
const STATUS_COLORS = { running: "#37c871", succeeded: "#37c871", failed: "#ff5d5d",
  shutdown: "#f5b942", starting: "#f5b942", creating: "#f5b942", removed: "#ff5d5d", not_run: "#9aa3b2", skipped: "#9aa3b2" };

// ---- 概览 ----
async function loadOverview() {
  const s = await api("/api/summary");
  $("#balance").textContent = s.balance_yuan == null ? "—" : "¥" + s.balance_yuan.toFixed(2);
  $("#active-inst").textContent = s.active_instance ? "活动实例 " + s.active_instance : "";
  const cards = [
    ["余额", s.balance_yuan == null ? "—" : "¥" + s.balance_yuan.toFixed(2)],
    ["实例总数", s.instances_total],
    ["实验总数", s.runs_total],
    ["平均用时", fmtDur(s.avg_duration_sec)],
  ];
  $("#summary-cards").innerHTML = cards.map((c) => `<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div></div>`).join("");
  const rb = s.runs_by_status || {};
  drawChart("chart-runs", "doughnut", Object.keys(rb), Object.values(rb), Object.keys(rb).map((k) => STATUS_COLORS[k] || "#4f8cff"));
  const ib = s.instances_by_status || {};
  drawChart("chart-inst", "doughnut", Object.keys(ib), Object.values(ib), Object.keys(ib).map((k) => STATUS_COLORS[k] || "#4f8cff"));
}

// ---- 实例 ----
async function loadInstances() {
  const { instances } = await api("/api/instances");
  $("#inst-rows").innerHTML = instances.length ? instances.map((it) => `
    <tr><td>${esc(it.instance_uuid)}${it.active ? " ⭐" : ""}</td><td>${tag(it.status)}</td>
    <td>${esc(it.billing)}</td><td>${esc(it.region || "")}</td>
    <td>
      <button onclick="instAction('${it.instance_uuid}','power_on')" ${it.status === "running" ? "disabled" : ""}>开机</button>
      <button onclick="instAction('${it.instance_uuid}','power_off')" ${it.status !== "running" ? "disabled" : ""}>关机</button>
      <button class="danger" onclick="instAction('${it.instance_uuid}','release')">释放</button>
    </td></tr>`).join("") : `<tr><td colspan="5" class="muted">无实例，点右上「新建/开机实例」</td></tr>`;
  // 提交表单的实例下拉
  $("#f-instance").innerHTML = instances.map((it) =>
    `<option value="${it.instance_uuid}" ${it.active ? "selected" : ""}>${it.instance_uuid} (${it.status})</option>`).join("")
    || `<option value="">（无实例，请先开机）</option>`;
}
async function loadStock() {
  const data = await api("/api/stock");
  const chips = [];
  for (const [rg, items] of Object.entries(data))
    for (const [name, st] of Object.entries(items))
      if (st && st.idle_gpu_num != null) chips.push(`<span class="chip">${esc(rg)} · ${esc(name)}: <b>${st.idle_gpu_num}</b>/${st.total_gpu_num}</span>`);
  $("#stock").innerHTML = chips.join("") || `<span class="muted">无数据</span>`;
}
async function instAction(uuid, action) {
  const names = { power_on: "开机", power_off: "关机", release: "释放（不可逆）" };
  if (!confirm(`确认对 ${uuid} 执行：${names[action]}？`)) return;
  try { await post(`/api/instances/${uuid}/${action}`); toast(`${names[action]} 已触发`); setTimeout(refreshAll, 800); }
  catch (e) { toast("失败: " + e.message, true); }
}
async function doUp() {
  const sel = confirm("按库存自动选区创建？\n确定=自动选区，取消=用配置默认/复用活动实例");
  try {
    const { job_id } = await post("/api/up", { select_region: sel });
    toast("正在创建/开机实例…（后台进行）");
    pollJob(job_id);
  } catch (e) { toast("失败: " + e.message, true); }
}
async function pollJob(jid) {
  try {
    const j = await api(`/api/jobs/${jid}`);
    if (j.status === "running") { $("#active-inst").textContent = "⏳ " + (j.log.slice(-1)[0] || "创建中…"); return setTimeout(() => pollJob(jid), 2500); }
    if (j.status === "done") { toast(`实例就绪: ${j.result.instance}（${j.result.ssh}）`); refreshAll(); }
    else toast("实例任务失败: " + j.error, true);
  } catch (e) { toast("轮询失败: " + e.message, true); }
}

// ---- 提交 & 实验 ----
async function doSubmit() {
  const mode = $("#f-mode").value, value = $("#f-value").value.trim();
  if (!value) return toast("请填写脚本路径/命令/内容", true);
  const body = { instance: $("#f-instance").value, name: $("#f-name").value.trim() || undefined };
  body[mode] = value;
  const mf = $("#f-metrics").value.trim(); if (mf) body.metrics_file = mf;
  const cfgRaw = $("#f-config").value.trim();
  if (cfgRaw) { try { body.config = JSON.parse(cfgRaw); } catch { return toast("配置 JSON 格式错误", true); } }
  $("#submit-hint").textContent = "提交中…";
  try {
    const r = await post("/api/run", body);
    $("#submit-hint").textContent = "";
    toast(`任务已启动: ${r.run_id}`);
    loadRuns();
  } catch (e) { $("#submit-hint").textContent = ""; toast("提交失败: " + e.message, true); }
}
async function loadRuns() {
  const { runs } = await api("/api/runs");
  $("#run-rows").innerHTML = runs.length ? runs.map((r) => {
    const mk = Object.entries(r.metrics).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(", ");
    return `<tr style="cursor:pointer" onclick="showRun('${esc(r.run_id)}')">
      <td>${esc(r.name)}</td><td>${esc(r.instance)}</td><td>${tag(r.status)}</td>
      <td>${r.exit_code == null ? "—" : r.exit_code}</td><td>${fmtDur(r.duration_sec)}</td>
      <td class="muted">${esc(mk) || "—"}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="muted">还没有实验</td></tr>`;
}
async function showRun(rid) {
  openModal(`<h2>${esc(rid)}</h2><p class="muted">加载中…</p>`);
  try {
    const d = await api(`/api/runs/${encodeURIComponent(rid)}`);
    const metricsRows = Object.entries(d.metrics).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")
      || `<tr><td colspan="2" class="muted">暂无指标（完成后自动抓取 metrics.json，或手动记录）</td></tr>`;
    openModal(`
      <h2>${esc(d.config.name || rid)} ${tag(d.status)}</h2>
      ${d.note ? `<p class="muted">⚠️ ${esc(d.note)}</p>` : ""}
      <p class="muted">实例 ${esc(d.instance)} · 退出码 ${d.exit_code == null ? "—" : d.exit_code} · 命令 <code>${esc(d.config.command || "")}</code></p>
      <h3 style="font-size:13px">指标</h3>
      <table>${metricsRows}</table>
      <h3 style="font-size:13px;margin-top:14px">手动记录指标 (JSON)</h3>
      <input id="m-input" placeholder='{"acc":0.97,"loss":0.12}'>
      <button class="primary" style="margin-top:8px" onclick="recordMetrics('${esc(rid)}')">保存指标</button>
      <h3 style="font-size:13px;margin-top:14px">日志 (tail)</h3>
      <pre>${esc(d.log) || "（无日志或实例已关机）"}</pre>
      <div class="right"><button onclick="closeModal()">关闭</button></div>`);
  } catch (e) { openModal(`<p class="muted">加载失败: ${esc(e.message)}</p><div class="right"><button onclick="closeModal()">关闭</button></div>`); }
}
async function recordMetrics(rid) {
  const raw = $("#m-input").value.trim(); if (!raw) return;
  let obj; try { obj = JSON.parse(raw); } catch { return toast("JSON 格式错误", true); }
  try { await post(`/api/runs/${encodeURIComponent(rid)}/metrics`, obj); toast("指标已保存"); showRun(rid); loadRuns(); }
  catch (e) { toast("保存失败: " + e.message, true); }
}

// ---- 大盘 ----
let boardData = { keys: [], rows: [] };
async function loadBoard() {
  boardData = await api("/api/metrics");
  const sel = $("#board-key");
  sel.innerHTML = boardData.keys.map((k) => `<option>${esc(k)}</option>`).join("") || `<option value="">（暂无数值指标）</option>`;
  // 表格
  const cols = boardData.keys;
  const head = `<thead><tr><th>实验</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>`;
  const body = boardData.rows.map((r) =>
    `<tr><td>${esc(r.name)}</td>${cols.map((c) => `<td>${r.metrics[c] != null ? esc(r.metrics[c]) : "—"}</td>`).join("")}</tr>`).join("")
    || `<tr><td class="muted">还没有指标，去「提交&实验」跑实验或手动记录</td></tr>`;
  $("#board-table").innerHTML = head + "<tbody>" + body + "</tbody>";
  renderBoard();
}
function renderBoard() {
  const key = $("#board-key").value; if (!key) return;
  const pts = boardData.rows.filter((r) => typeof r.metrics[key] === "number")
    .sort((a, b) => b.metrics[key] - a.metrics[key]);
  drawChart("chart-board", "bar", pts.map((p) => p.name), pts.map((p) => p.metrics[key]), "#4f8cff");
}

// ---- modal ----
function openModal(html) { $("#modal").innerHTML = html; $("#modal-bg").classList.add("show"); }
function closeModal() { $("#modal-bg").classList.remove("show"); }

async function refreshAll() {
  try { await Promise.all([loadOverview(), loadInstances(), loadStock(), loadRuns()]); }
  catch (e) { toast("刷新失败: " + e.message, true); }
}
refreshAll();
setInterval(() => { if ($("#overview").classList.contains("active")) loadOverview(); }, 15000);
