"use strict";
const $ = (s) => document.querySelector(s);
const api = async (p, o) => { const r = await fetch(p, o); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`); return d; };
const post = (p, b) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
const del = (p) => api(p, { method: "DELETE" });
function toast(m, e) { const t = document.createElement("div"); t.className = "toast" + (e ? " err" : ""); t.textContent = m; document.body.appendChild(t); setTimeout(() => t.remove(), 4500); }
const fmtDur = (s) => (s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${(s / 60).toFixed(1)}m`);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tag = (s) => `<span class="tag ${esc(s || "")}">${esc(s || "?")}</span>`;
let META = { gpu_specs: [], regions: [], default: {} };

document.querySelectorAll("nav a").forEach((a) => a.addEventListener("click", () => {
  document.querySelectorAll("nav a").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll("section").forEach((x) => x.classList.remove("active"));
  a.classList.add("active"); $("#" + a.dataset.tab).classList.add("active");
  if (a.dataset.tab === "experiments") { loadExperiments(); loadRuns(); loadTags(); }
  if (a.dataset.tab === "board") loadBoard();
  if (a.dataset.tab === "instances") loadStock();
}));

let charts = {};
function drawChart(id, type, labels, datasets, opts) {
  if (!window.Chart) return;
  if (charts[id]) charts[id].destroy();
  const ds = Array.isArray(datasets) ? datasets : [{ data: datasets, backgroundColor: opts && opts.colors }];
  charts[id] = new Chart($("#" + id), { type, data: { labels, datasets: ds },
    options: { plugins: { legend: { display: type !== "bar", labels: { color: "#9aa3b2" } } },
      scales: type === "doughnut" ? {} : { x: { ticks: { color: "#9aa3b2" } }, y: { ticks: { color: "#9aa3b2" } } } } });
}
const SC = { running: "#37c871", succeeded: "#37c871", failed: "#ff5d5d", shutdown: "#f5b942", starting: "#f5b942", creating: "#f5b942", power_off: "#f5b942", removed: "#ff5d5d", not_run: "#9aa3b2", skipped: "#9aa3b2" };

// ---------- meta ----------
async function loadMeta() {
  META = await api("/api/meta");
  const regOpts = META.regions.map((r) => `<option value="${r.sign}">${r.name} (${r.sign})</option>`).join("");
  const gpuOpts = META.gpu_specs.map((g) => `<option value="${g.uuid}">${g.label} · ${g.uuid} · ${g.category}</option>`).join("");
  $("#e-region").innerHTML = `<option value="">（默认/自动）</option>` + regOpts;
  $("#e-gpu").innerHTML = gpuOpts;
  if (META.default.gpu_spec_uuid) $("#e-gpu").value = META.default.gpu_spec_uuid;
  // stock 区域下拉
  const stockReg = $("#stock-region");
  stockReg.innerHTML = `<option value="">全部偏好区</option>` + regOpts;
  stockReg.value = META.default.regions && META.default.regions[0] || "";
}

// ---------- 概览 ----------
async function loadOverview() {
  const s = await api("/api/summary");
  $("#balance").textContent = s.balance_yuan == null ? "—" : "¥" + s.balance_yuan.toFixed(2);
  $("#active-inst").textContent = s.active_instance ? "活动实例 " + s.active_instance : "";
  const cards = [["余额", s.balance_yuan == null ? "—" : "¥" + s.balance_yuan.toFixed(2)],
    ["实例", s.instances_total], ["实验", s.experiments_total], ["运行", s.runs_total], ["平均用时", fmtDur(s.avg_duration_sec)]];
  $("#summary-cards").innerHTML = cards.map((c) => `<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div></div>`).join("");
  const rb = s.runs_by_status || {}, ib = s.instances_by_status || {};
  // 文字徽章：始终可见，不依赖 Chart.js（CDN 失败也能看状态）
  $("#inst-status-text").innerHTML = statusChips(ib, "暂无实例");
  $("#run-status-text").innerHTML = statusChips(rb, "暂无任务");
  drawChart("chart-runs", "doughnut", Object.keys(rb), Object.values(rb), { colors: Object.keys(rb).map((k) => SC[k] || "#4f8cff") });
  drawChart("chart-inst", "doughnut", Object.keys(ib), Object.values(ib), { colors: Object.keys(ib).map((k) => SC[k] || "#4f8cff") });
  if (!window.Chart) { $("#run-status-text").innerHTML += ` <span class="muted">(图表库未加载)</span>`; }
}
function statusChips(obj, emptyText) {
  const ks = Object.keys(obj);
  if (!ks.length) return `<span class="muted">${esc(emptyText || "暂无")}</span>`;
  return ks.map((k) => `<span class="chip">${tag(k)} × <b>${obj[k]}</b></span>`).join("");
}

// ---------- 实例 ----------
async function loadInstances() {
  const { instances } = await api("/api/instances");
  $("#inst-rows").innerHTML = instances.length ? instances.map((it) => `
    <tr><td>${esc(it.instance_uuid)}${it.active ? " ⭐" : ""}</td><td>${esc(it.gpu)}${it.gpu_spec ? ` <span class="muted">(${esc(it.gpu_spec)})</span>` : ""}</td><td>${it.gpu_amount ?? "—"}</td>
    <td>${esc(it.region || "")}</td><td>${tag(it.status)}</td><td>${esc(it.billing)}</td>
    <td><button class="sm" onclick="instAction('${esc(it.instance_uuid)}','power_on')" ${it.status === "running" ? "disabled" : ""}>开机</button>
    <button class="sm" onclick="instAction('${esc(it.instance_uuid)}','power_off')" ${it.status !== "running" ? "disabled" : ""}>关机</button>
    <button class="sm danger" onclick="instAction('${esc(it.instance_uuid)}','release')">释放</button></td></tr>`).join("")
    : `<tr><td colspan="7" class="muted">无实例，点右上「创建实例」</td></tr>`;
}
async function instAction(uuid, action) {
  const n = { power_on: "开机", power_off: "关机", release: "释放（不可逆，磁盘数据会清空）" };
  if (!confirm(`确认对 ${uuid}：${n[action]}？`)) return;
  try { await post(`/api/instances/${uuid}/${action}`); toast(`${n[action]} 已触发`); setTimeout(refreshAll, 900); }
  catch (e) { toast("失败: " + e.message, true); }
}
function openCreate() {
  const d = META.default;
  openModal(`<h2>创建实例</h2>
    <div class="row"><div><label>区域</label><select id="c-region"><option value="">（自动调度）</option>
      ${META.regions.map((r) => `<option value="${r.sign}">${r.name} (${r.sign})</option>`).join("")}</select></div>
      <div><label>GPU 类型</label><select id="c-gpu">${META.gpu_specs.map((g) => `<option value="${g.uuid}" ${g.uuid === d.gpu_spec_uuid ? "selected" : ""}>${g.label} · ${g.uuid} · ${g.category}</option>`).join("")}</select></div></div>
    <div class="row"><div><label>GPU 数量 (1–4)</label><select id="c-num"><option>1</option><option>2</option><option>3</option><option>4</option></select></div>
      <div><label>系统盘扩容 GB (0–500)</label><input id="c-disk" type="number" value="${d.expand_disk_gb ?? 10}" min="0" max="500"></div></div>
    <div class="row"><div><label>镜像 image_uuid</label><input id="c-image" value="${esc(d.image_uuid || "")}"></div>
      <div><label>最低 CUDA (如 113=11.3)</label><input id="c-cuda" type="number" value="${d.cuda_v_from ?? 113}"></div></div>
    <label>实例名</label><input id="c-name" value="task-runner">
    <p class="hint">创建是按量计费、后台进行；可在库存表先确认目标区域有空闲卡。</p>
    <div class="right"><button onclick="closeModal()">取消</button> <button class="primary" onclick="doCreate()">创建</button></div>`);
}
async function doCreate() {
  const body = { region: $("#c-region").value || undefined, gpu_spec_uuid: $("#c-gpu").value,
    req_gpu_amount: +$("#c-num").value, expand_disk_gb: +$("#c-disk").value,
    image_uuid: $("#c-image").value.trim() || undefined, cuda_v_from: +$("#c-cuda").value,
    instance_name: $("#c-name").value.trim() || undefined };
  try { const { job_id } = await post("/api/create", body); closeModal(); toast("创建中…（后台）"); pollJob(job_id); }
  catch (e) { toast("创建失败: " + e.message, true); }
}
async function pollJob(jid) {
  try {
    const j = await api(`/api/jobs/${jid}`);
    if (j.status === "running") { $("#active-inst").textContent = "⏳ " + (j.log.slice(-1)[0] || "进行中…"); return setTimeout(() => pollJob(jid), 2500); }
    if (j.status === "done") { toast(`就绪: ${j.result.instance}（${j.result.ssh}）`); refreshAll(); }
    else toast("失败: " + j.error, true);
  } catch (e) { toast("轮询失败: " + e.message, true); }
}
let STOCK = {};
async function loadStock() {
  const rg = $("#stock-region").value;
  try { STOCK = await api("/api/stock" + (rg ? `?region=${rg}` : "")); renderStock(); }
  catch (e) { toast("库存失败: " + e.message, true); }
}
function renderStock() {
  const f = ($("#stock-filter").value || "").toLowerCase();
  const rows = [];
  for (const [rg, items] of Object.entries(STOCK))
    for (const [name, st] of Object.entries(items)) {
      if (st && st.idle_gpu_num != null && (!f || name.toLowerCase().includes(f)))
        rows.push(`<tr><td>${esc(rg)}</td><td>${esc(name)}</td><td style="color:${st.idle_gpu_num > 0 ? "var(--green)" : "var(--red)"}">${st.idle_gpu_num}</td><td>${st.total_gpu_num}</td><td class="muted">${esc((st.chip_corp || "") + " " + (st.cpu_arch || ""))}</td></tr>`);
    }
  $("#stock-rows").innerHTML = rows.join("") || `<tr><td colspan="5" class="muted">无数据</td></tr>`;
}

// ---------- 实验 ----------
function onModeChange() {
  const m = $("#e-mode").value;
  $("#e-value-label").textContent = m === "remote_script" ? "脚本路径，如 ~/proj/submit_exec.sh"
    : m === "remote" ? "远端命令，如 cd ~/proj && bash run.sh" : "粘贴要上传执行的 bash 脚本内容";
}
function loadYamlFile(ev) { const f = ev.target.files[0]; if (!f) return; const r = new FileReader(); r.onload = () => { $("#e-yaml").value = r.result; }; r.readAsText(f); }
function addMetricRow(name = "", source = "auto", pattern = "") {
  const div = document.createElement("div"); div.className = "metric-row";
  div.innerHTML = `<input placeholder="指标名 如 ASR / Accuracy" value="${esc(name)}">
    <select><option value="auto">auto</option><option value="json">json键</option><option value="regex">日志正则</option></select>
    <input placeholder="正则(可选) 如 ASR[:=]\\s*([0-9.]+)" value="${esc(pattern)}">
    <button class="sm danger" onclick="this.parentNode.remove()">×</button>`;
  div.querySelector("select").value = source;
  $("#e-metrics").appendChild(div);
}
function collectMetrics() {
  return [...document.querySelectorAll("#e-metrics .metric-row")].map((d) => {
    const [n, , p] = d.querySelectorAll("input"); const s = d.querySelector("select");
    return n.value.trim() ? { name: n.value.trim(), source: s.value, pattern: p.value.trim() || undefined } : null;
  }).filter(Boolean);
}
function resetExpForm() {
  $("#e-id").value = ""; $("#e-name").value = ""; $("#e-tag").value = ""; $("#e-mode").value = "remote_script";
  $("#e-value").value = ""; $("#e-yaml").value = ""; $("#e-metrics").innerHTML = ""; onModeChange();
  $("#e-gpunum").value = "1"; $("#e-region").value = ""; if (META.default.gpu_spec_uuid) $("#e-gpu").value = META.default.gpu_spec_uuid;
  $("#exp-form-title").firstChild.textContent = "初始化实验 ";
}
async function saveExp(asNew) {
  const name = $("#e-name").value.trim(); if (!name) return toast("实验名必填", true);
  const body = { name, tag: $("#e-tag").value.trim(), config_yaml: $("#e-yaml").value,
    exec_mode: $("#e-mode").value, exec_value: $("#e-value").value.trim(), metrics_spec: collectMetrics(),
    instance_pref: { region: $("#e-region").value || undefined, gpu_spec_uuid: $("#e-gpu").value, req_gpu_amount: +$("#e-gpunum").value } };
  if (!asNew && $("#e-id").value) body.experiment_id = $("#e-id").value;
  try { const r = await post("/api/experiments", body); $("#e-id").value = r.experiment_id; toast("已保存: " + r.experiment_id); loadExperiments(); loadTags(); }
  catch (e) { toast("保存失败: " + e.message, true); }
}
async function loadTags() {
  const { tags } = await api("/api/tags");
  $("#tag-list").innerHTML = tags.map((t) => `<option value="${esc(t)}">`).join("");
}
async function loadExperiments() {
  const { experiments } = await api("/api/experiments");
  $("#exp-rows").innerHTML = experiments.length ? experiments.map((e) => {
    const mk = Object.entries(e.last_metrics).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(", ");
    return `<tr><td><b>${esc(e.name)}</b></td><td>${e.tag ? `<span class="tagchip">${esc(e.tag)}</span>` : "—"}</td>
      <td class="muted">${esc(e.exec_mode)}</td><td>${e.last_status ? tag(e.last_status) : "—"}</td><td class="muted">${esc(mk) || "—"}</td>
      <td><button class="sm" onclick="editExp('${esc(e.experiment_id)}')">编辑/复用</button>
      <button class="sm primary" onclick="runExp('${esc(e.experiment_id)}')">运行</button>
      <button class="sm" onclick="genScript('${esc(e.experiment_id)}')">submit脚本</button>
      <button class="sm danger" onclick="delExp('${esc(e.experiment_id)}')">删</button></td></tr>`;
  }).join("") : `<tr><td colspan="6" class="muted">还没有实验，上面填写后「保存实验」</td></tr>`;
}
async function editExp(id) {
  const e = await api(`/api/experiments/${encodeURIComponent(id)}`);
  $("#e-id").value = e.experiment_id; $("#e-name").value = e.name || ""; $("#e-tag").value = e.tag || "";
  $("#e-mode").value = e.exec_mode || "remote_script"; $("#e-value").value = e.exec_value || ""; $("#e-yaml").value = e.config_yaml || "";
  $("#e-metrics").innerHTML = ""; (e.metrics_spec || []).forEach((m) => addMetricRow(m.name, m.source, m.pattern || ""));
  const p = e.instance_pref || {}; $("#e-region").value = p.region || ""; if (p.gpu_spec_uuid) $("#e-gpu").value = p.gpu_spec_uuid; $("#e-gpunum").value = p.req_gpu_amount || 1;
  onModeChange(); $("#exp-form-title").firstChild.textContent = `编辑实验：${e.name} `;
  window.scrollTo({ top: 0, behavior: "smooth" });
  toast("已载入配置，可改后「保存」覆盖，或「另存为新实验」");
}
async function runExp(id) {
  if (!confirm("在当前活动实例上运行该实验？（需已有 running 实例；没有请先创建/开机）")) return;
  try { const r = await post(`/api/experiments/${encodeURIComponent(id)}/run`, {}); toast("已启动: " + r.run_id); loadRuns(); }
  catch (e) { toast("运行失败: " + e.message, true); }
}
function genScript(id) { window.open(`/api/experiments/${encodeURIComponent(id)}/submit_script`, "_blank"); }
async function delExp(id) { if (!confirm("删除该实验定义？(运行记录与指标保留)")) return; try { await del(`/api/experiments/${encodeURIComponent(id)}`); toast("已删除"); loadExperiments(); } catch (e) { toast("失败: " + e.message, true); } }

async function loadRuns() {
  const { runs } = await api("/api/runs");
  $("#run-rows").innerHTML = runs.length ? runs.map((r) => {
    const mk = Object.entries(r.metrics).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(", ");
    return `<tr style="cursor:pointer" onclick="showRun('${esc(r.run_id)}')"><td>${esc(r.run_id)}</td><td class="muted">${esc(r.name)}</td>
      <td>${r.tag ? `<span class="tagchip">${esc(r.tag)}</span>` : "—"}</td><td>${tag(r.status)}</td><td>${r.exit_code ?? "—"}</td>
      <td>${fmtDur(r.duration_sec)}</td><td class="muted">${esc(mk) || "—"}</td></tr>`;
  }).join("") : `<tr><td colspan="7" class="muted">还没有运行</td></tr>`;
}
async function showRun(rid) {
  openModal(`<h2>${esc(rid)}</h2><p class="muted">加载中…</p>`);
  try {
    const d = await api(`/api/runs/${encodeURIComponent(rid)}`);
    const ms = Object.entries(d.metrics);
    const mrows = ms.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") || `<tr><td colspan="2" class="muted">暂无标量指标（完成后自动抓取）</td></tr>`;
    const hasSeries = d.series && Object.keys(d.series).length;
    openModal(`<h2>${esc(d.config.name || rid)} ${tag(d.status)} ${d.tag ? `<span class="tagchip">${esc(d.tag)}</span>` : ""}</h2>
      ${d.note ? `<p class="muted">⚠️ ${esc(d.note)}</p>` : ""}
      <p class="muted">实例 ${esc(d.instance)} · 退出码 ${d.exit_code ?? "—"} · <code>${esc(d.config.command || "")}</code></p>
      <h3 style="font-size:13px">指标</h3><table>${mrows}</table>
      ${hasSeries ? `<h3 style="font-size:13px;margin-top:12px">趋势</h3><canvas id="run-series"></canvas>` : ""}
      <h3 style="font-size:13px;margin-top:12px">手动记录指标 (JSON，可含 "_step":N 记曲线点)</h3>
      <input id="m-input" placeholder='{"ASR":0.83,"Accuracy":0.91}'>
      <button class="primary sm" style="margin-top:8px" onclick="recMetrics('${esc(rid)}')">保存指标</button>
      <h3 style="font-size:13px;margin-top:12px">日志</h3><pre>${esc(d.log) || "（无日志或实例已关机）"}</pre>
      <div class="right"><button onclick="closeModal()">关闭</button></div>`);
    if (hasSeries) {
      const keys = Object.keys(d.series);
      const palette = ["#4f8cff", "#37c871", "#f5b942", "#ff5d5d", "#b07cff"];
      const dsets = keys.map((k, i) => ({ label: k, data: d.series[k].map((p) => ({ x: p[0], y: p[1] })), borderColor: palette[i % 5], backgroundColor: palette[i % 5], tension: .2 }));
      if (window.Chart) { if (charts["run-series"]) charts["run-series"].destroy(); charts["run-series"] = new Chart($("#run-series"), { type: "line", data: { datasets: dsets }, options: { parsing: false, scales: { x: { type: "linear", ticks: { color: "#9aa3b2" } }, y: { ticks: { color: "#9aa3b2" } } }, plugins: { legend: { labels: { color: "#9aa3b2" } } } } }); }
    }
  } catch (e) { openModal(`<p class="muted">加载失败: ${esc(e.message)}</p><div class="right"><button onclick="closeModal()">关闭</button></div>`); }
}
async function recMetrics(rid) {
  const raw = $("#m-input").value.trim(); if (!raw) return; let o; try { o = JSON.parse(raw); } catch { return toast("JSON 格式错误", true); }
  try { await post(`/api/runs/${encodeURIComponent(rid)}/metrics`, o); toast("已保存"); showRun(rid); loadRuns(); } catch (e) { toast("失败: " + e.message, true); }
}

// ---------- 大盘 ----------
let BOARD = { keys: [], rows: [], tags: [] };
async function loadBoard() {
  BOARD = await api("/api/metrics");
  $("#board-tag").innerHTML = `<option value="">全部标签</option>` + BOARD.tags.map((t) => `<option>${esc(t)}</option>`).join("");
  $("#board-key").innerHTML = BOARD.keys.map((k) => `<option>${esc(k)}</option>`).join("") || `<option value="">（无数值指标）</option>`;
  renderBoard();
}
function renderBoard() {
  const tg = $("#board-tag").value, key = $("#board-key").value;
  const rows = BOARD.rows.filter((r) => !tg || r.tag === tg);
  const cols = BOARD.keys;
  const head = `<thead><tr><th>实验</th><th>标签</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>`;
  const body = rows.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.tag ? `<span class="tagchip">${esc(r.tag)}</span>` : "—"}</td>${cols.map((c) => `<td>${r.metrics[c] != null ? esc(r.metrics[c]) : "—"}</td>`).join("")}</tr>`).join("")
    || `<tr><td class="muted">无数据</td></tr>`;
  $("#board-table").innerHTML = head + "<tbody>" + body + "</tbody>";
  if (key) {
    const pts = rows.filter((r) => typeof r.metrics[key] === "number").sort((a, b) => b.metrics[key] - a.metrics[key]);
    drawChart("chart-board", "bar", pts.map((p) => p.name), pts.map((p) => p.metrics[key]), { colors: "#4f8cff" });
  }
}

function openModal(h) { $("#modal").innerHTML = h; $("#modal-bg").classList.add("show"); }
function closeModal() { $("#modal-bg").classList.remove("show"); }

async function refreshAll() {
  try { await Promise.all([loadOverview(), loadInstances(), loadStock()]); if ($("#experiments").classList.contains("active")) { loadExperiments(); loadRuns(); } }
  catch (e) { toast("刷新失败: " + e.message, true); }
}
(async () => {
  try { await loadMeta(); } catch (e) { toast("加载元信息失败: " + e.message, true); }  // 不阻塞概览
  onModeChange(); addMetricRow("Accuracy", "auto");
  await refreshAll();
  try { await loadTags(); } catch (e) { /* 忽略 */ }
})();
setInterval(() => { if ($("#overview").classList.contains("active")) loadOverview(); }, 15000);
