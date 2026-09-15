const STATUS_LABEL = { red: "红色", yellow: "黄色", green: "绿色" };
const STATUS_COLOR = { red: "#c93232", yellow: "#c18a16", green: "#168052" };
const IS_LOCAL_SERVICE = ["localhost", "127.0.0.1"].includes(window.location.hostname);
const COMPONENT_LABEL = {
  production: "产量效率",
  quality: "剔除质量",
  inspection: "巡检寻访",
  diagnosis: "诊断维护",
  ncr: "外部NCR",
  debug: "调试异常",
  factoryScore: "出厂前评分",
};

let dataset = null;
let currentRows = [];
const isCompactViewport = () => window.matchMedia("(max-width: 620px)").matches;
const machinePageSize = () => isCompactViewport() ? 8 : 24;
let visibleLimit = machinePageSize();
let selectedMachine = null;
let selectedDetailTab = "overview";
let mapPoints = [];

const el = (id) => document.getElementById(id);
const fmt = (value, fallback = "--") => value === null || value === undefined || value === "" ? fallback : value;
const pct = (value) => value === null || value === undefined ? "--" : `${(value * 100).toFixed(2)}%`;
const num = (value) => Number(value || 0).toLocaleString("zh-CN");

async function loadData() {
  const response = await fetch(`./data/machine_watch_data.json?ts=${Date.now()}`);
  if (!response.ok) throw new Error(`数据请求失败：${response.status}`);
  dataset = await response.json();
  initFilters();
  renderAll();
}

function initFilters() {
  fillSelect("modelFilter", "全部机型", dataset.machines.map((m) => m.model || "未知"));
  fillSelect("provinceFilter", "全部省份", dataset.machines.map((m) => m.province || "未知"));
}

function fillSelect(id, firstLabel, values) {
  const unique = [...new Set(values)].filter(Boolean).sort((a, b) => String(a).localeCompare(String(b), "zh-CN"));
  el(id).innerHTML = `<option value="">${firstLabel}</option>${unique.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}`;
}

function renderAll() {
  renderSummary();
  renderCharts();
  renderAlerts();
  renderMethodology();
  renderMachines();
}

function renderSummary() {
  const s = dataset.summary;
  el("machineCount").textContent = num(s.machineCount);
  el("redCount").textContent = num(s.redCount);
  el("yellowCount").textContent = num(s.yellowCount);
  el("greenCount").textContent = num(s.greenCount);
  el("highAlertCount").textContent = num(s.highRiskAlerts);
  el("factoryScoreMetric").textContent = s.avgFactoryScore === undefined ? "--" : s.avgFactoryScore;
  el("factoryScoreCoverage").textContent = `${s.machinesWithFactoryScore || 0} 台覆盖 · ${dataset.sourceFiles.length} 个数据源`;
  el("coverageText").textContent = `巡检 ${s.machinesWithInspectionData} · 诊断 ${s.machinesWithDiagnosisData} · NCR ${s.machinesWithNcrData}`;
  const validation = dataset.dataQuality?.factoryScoring;
  const validationText = validation
    ? `评分表校验通过${validation.warningCount ? ` · ${validation.warningCount} 项警告` : ""}`
    : "数据已载入";
  el("generatedAt").textContent = `${validationText} · ${new Date(dataset.generatedAt).toLocaleString("zh-CN")}`;
}

function filteredRows() {
  const keyword = el("searchInput").value.trim().toLowerCase();
  const status = el("statusFilter").value;
  const model = el("modelFilter").value;
  const province = el("provinceFilter").value;
  const sort = el("sortSelect").value;
  const rows = dataset.machines.filter((m) => {
    const haystack = `${m.machineId} ${m.factory || ""} ${m.model || ""} ${m.region || ""} ${m.province || ""} ${m.city || ""}`.toLowerCase();
    return (!keyword || haystack.includes(keyword))
      && (!status || m.status === status)
      && (!model || (m.model || "未知") === model)
      && (!province || (m.province || "未知") === province);
  });
  return rows.sort((a, b) => {
    if (sort === "scoreDesc") return b.healthScore - a.healthScore;
    if (sort === "idAsc") return String(a.machineId).localeCompare(String(b.machineId));
    return b.riskScore - a.riskScore;
  });
}

function renderMachines(resetLimit = false) {
  if (resetLimit) visibleLimit = machinePageSize();
  currentRows = filteredRows();
  const visible = currentRows.slice(0, visibleLimit);
  el("rowCount").textContent = `${currentRows.length} 台`;
  el("activeFilterText").textContent = activeFilterText();
  el("machineGrid").innerHTML = visible.length
    ? visible.map(machineCard).join("")
    : '<div class="empty-state"><strong>没有匹配的机组</strong><p>请调整筛选条件后再试。</p></div>';
  el("loadMoreBtn").hidden = visible.length >= currentRows.length;

  document.querySelectorAll(".machine-card").forEach((card) => {
    const open = () => openDrawer(currentRows[Number(card.dataset.index)]);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
}

function machineCard(m, index) {
  const warning = m.warnings?.[0]?.title || "当前无显著规则预警";
  const barColor = STATUS_COLOR[m.status];
  return `
    <article class="machine-card ${m.status}" data-index="${index}" tabindex="0" aria-label="查看机组 ${escapeHtml(m.machineId)} 详情">
      <div class="card-head">
        <div class="machine-id">
          <strong>${escapeHtml(m.machineId)}</strong>
          <span>${escapeHtml(m.factory || "未知烟厂")} · ${escapeHtml(m.model || "未知机型")}</span>
        </div>
        <span class="status-pill ${m.status}">${STATUS_LABEL[m.status]}</span>
      </div>
      <div class="score-row">
        <div><strong>${fmt(m.healthScore)}</strong><small> / 100 健康分</small></div>
        <small>风险 ${fmt(m.riskScore)}</small>
      </div>
      <div class="risk-track"><span style="width:${Math.min(100, m.riskScore || 0)}%;background:${barColor}"></span></div>
      <div class="card-facts">
        <div class="card-fact"><span>地区</span><strong>${escapeHtml(m.region || m.province || "--")}</strong></div>
        <div class="card-fact"><span>巡检红 / 黄</span><strong>${m.redCount || 0} / ${m.yellowCount || 0}</strong></div>
        <div class="card-fact"><span>未闭环诊断</span><strong>${m.openDiagnosisIssues || 0} 项</strong></div>
        <div class="card-fact"><span>出厂前评分</span><strong>${m.factoryScore === undefined ? "--" : `${m.factoryScore} 分`}</strong></div>
      </div>
      <p class="warning-line">${escapeHtml(warning)}</p>
    </article>`;
}

function activeFilterText() {
  const parts = [];
  if (el("statusFilter").value) parts.push(STATUS_LABEL[el("statusFilter").value]);
  if (el("modelFilter").value) parts.push(el("modelFilter").value);
  if (el("provinceFilter").value) parts.push(el("provinceFilter").value);
  if (el("searchInput").value.trim()) parts.push(`搜索“${el("searchInput").value.trim()}”`);
  return parts.length ? parts.join(" · ") : "全部机组";
}

function renderAlerts() {
  el("alertCount").textContent = `${dataset.alerts.length} 条`;
  el("alerts").innerHTML = dataset.alerts.slice(0, isCompactViewport() ? 5 : 8).map((a) => `
    <article class="alert ${a.severity}" data-machine="${escapeHtml(a.machineId)}">
      <strong>${escapeHtml(a.title)}</strong>
      <span>${escapeHtml(a.machineId)} · ${escapeHtml(a.factory || "未知")} · ${escapeHtml(a.source)}</span>
      <p>${escapeHtml(a.detail || "")}</p>
    </article>`).join("");
  document.querySelectorAll(".alert").forEach((item) => {
    item.addEventListener("click", () => {
      const machine = dataset.machines.find((m) => m.machineId === item.dataset.machine);
      if (machine) openDrawer(machine);
    });
  });
}

function renderMethodology() {
  const m = dataset.methodology;
  el("methodology").innerHTML = `
    <p class="methodology-copy"><strong>${escapeHtml(m.healthScore)}</strong><br>
    风险输入覆盖产量效率、剔除质量、巡检寻访、诊断维护、调试异常与外部 NCR。${escapeHtml(m.caveats.join(" "))}</p>`;
}

function renderCharts() {
  if (!dataset) return;
  drawDonut(el("statusChart"), dataset.summary);
  drawHorizontalBars(el("riskModelChart"), dataset.charts.riskByModel, "model", "avgRisk", "count", true);
  drawHorizontalBars(el("componentChart"), dataset.charts.riskComponents, "name", "value", null, false, COMPONENT_LABEL);
  drawHorizontalBars(el("provinceRiskChart"), dataset.charts.riskByProvince, "province", "avgRisk", "count", true);
  drawColumnChart(el("inspectionChart"), dataset.charts.inspectionStatus, "status", "count", ["#c93232", "#c18a16", "#168052"]);
  drawColumnChart(el("diagnosisChart"), dataset.charts.diagnosisPriority, "priority", "count", ["#c93232", "#c18a16", "#2563a8"]);
  drawColumnChart(el("ncrChart"), dataset.charts.ncrLevels, "level", "count", ["#c93232", "#c18a16", "#2563a8"]);
  drawHorizontalBars(el("issueChart"), dataset.charts.issueTypes, "name", "count");
  drawColumnChart(el("factoryBandChart"), dataset.charts.factoryScoreBands, "band", "count", ["#c93232", "#c18a16", "#2563a8", "#168052"]);
  drawHorizontalBars(el("factoryStageChart"), dataset.charts.factoryDeductionsByStage, "stage", "deduction");
  drawHorizontalBars(el("factoryModelChart"), dataset.charts.factoryScoreByModel, "model", "avgScore", "count");
  drawHorizontalBars(el("factoryDeptChart"), dataset.charts.factoryScoreByDepartment, "department", "avgScore", "count");
  drawProvinceMap();
}

function prepareCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  return { ctx, width: rect.width, height: rect.height };
}

function drawDonut(canvas, summary) {
  const { ctx, width, height } = prepareCanvas(canvas);
  const values = [
    { label: "红色", value: summary.redCount, color: STATUS_COLOR.red },
    { label: "黄色", value: summary.yellowCount, color: STATUS_COLOR.yellow },
    { label: "绿色", value: summary.greenCount, color: STATUS_COLOR.green },
  ];
  const total = values.reduce((sum, row) => sum + row.value, 0) || 1;
  const cx = Math.min(width * .36, 105);
  const cy = height * .49;
  const radius = Math.min(72, height * .34, width * .24);
  let start = -Math.PI / 2;
  values.forEach((row) => {
    const angle = row.value / total * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, start, start + angle);
    ctx.arc(cx, cy, radius * .63, start + angle, start, true);
    ctx.closePath();
    ctx.fillStyle = row.color;
    ctx.fill();
    start += angle;
  });
  ctx.textAlign = "center";
  ctx.fillStyle = "#16202a";
  ctx.font = "700 25px Microsoft YaHei";
  ctx.fillText(total, cx, cy + 4);
  ctx.fillStyle = "#687482";
  ctx.font = "11px Microsoft YaHei";
  ctx.fillText("台设备", cx, cy + 23);

  const legendX = Math.max(cx + radius + 26, width * .64);
  values.forEach((row, i) => {
    const y = cy - 44 + i * 43;
    ctx.fillStyle = row.color;
    ctx.beginPath();
    ctx.arc(legendX, y, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.textAlign = "left";
    ctx.fillStyle = "#687482";
    ctx.font = "11px Microsoft YaHei";
    ctx.fillText(row.label, legendX + 12, y + 4);
    ctx.fillStyle = "#16202a";
    ctx.font = "700 14px Microsoft YaHei";
    ctx.fillText(`${row.value} 台`, legendX + 12, y + 21);
  });
}

function drawHorizontalBars(canvas, rows, labelKey, valueKey, countKey = null, riskColor = false, labels = null) {
  const { ctx, width, height } = prepareCanvas(canvas);
  const data = (rows || []).slice(0, Math.max(5, Math.floor((height - 18) / 27)));
  if (!data.length) return drawEmpty(ctx, width, height);
  const values = data.map((row) => Number(row[valueKey] || 0));
  const max = Math.max(...values, 1);
  const labelWidth = Math.min(120, width * .34);
  const rightPad = 48;
  const rowHeight = (height - 12) / data.length;
  data.forEach((row, index) => {
    const value = Number(row[valueKey] || 0);
    const y = 7 + index * rowHeight;
    const barY = y + rowHeight * .45;
    const barWidth = Math.max(2, (width - labelWidth - rightPad) * value / max);
    const rawLabel = labels?.[row[labelKey]] || row[labelKey] || "未知";
    const label = String(rawLabel).length > 9 ? `${String(rawLabel).slice(0, 9)}…` : rawLabel;
    ctx.fillStyle = "#536171";
    ctx.font = "11px Microsoft YaHei";
    ctx.textAlign = "left";
    ctx.fillText(label, 0, barY + 4);
    ctx.fillStyle = "#e7ecef";
    ctx.fillRect(labelWidth, barY - 6, width - labelWidth - rightPad, 11);
    ctx.fillStyle = riskColor ? riskColorFor(value) : "#2d6d9f";
    ctx.fillRect(labelWidth, barY - 6, barWidth, 11);
    ctx.fillStyle = "#16202a";
    ctx.font = "700 10px Microsoft YaHei";
    const suffix = countKey ? `${value} · ${row[countKey]}台` : num(value);
    ctx.fillText(suffix, labelWidth + barWidth + 6, barY + 4);
  });
}

function drawColumnChart(canvas, rows, labelKey, valueKey, colors) {
  const { ctx, width, height } = prepareCanvas(canvas);
  if (!rows?.length) return drawEmpty(ctx, width, height);
  const max = Math.max(...rows.map((row) => Number(row[valueKey] || 0)), 1);
  const chartTop = 20;
  const chartBottom = height - 35;
  const slot = width / rows.length;
  rows.forEach((row, index) => {
    const value = Number(row[valueKey] || 0);
    const barWidth = Math.min(58, slot * .48);
    const barHeight = (chartBottom - chartTop) * value / max;
    const x = slot * index + (slot - barWidth) / 2;
    const y = chartBottom - barHeight;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x, y, barWidth, barHeight);
    ctx.textAlign = "center";
    ctx.fillStyle = "#16202a";
    ctx.font = "700 12px Microsoft YaHei";
    ctx.fillText(num(value), x + barWidth / 2, Math.max(13, y - 7));
    ctx.fillStyle = "#687482";
    ctx.font = "11px Microsoft YaHei";
    ctx.fillText(row[labelKey], x + barWidth / 2, chartBottom + 20);
  });
  ctx.strokeStyle = "#d9e0e6";
  ctx.beginPath();
  ctx.moveTo(8, chartBottom + .5);
  ctx.lineTo(width - 8, chartBottom + .5);
  ctx.stroke();
}

function drawEmpty(ctx, width, height) {
  ctx.fillStyle = "#8a96a3";
  ctx.font = "12px Microsoft YaHei";
  ctx.textAlign = "center";
  ctx.fillText("暂无可用数据", width / 2, height / 2);
}

function riskColorFor(value) {
  if (value >= 60) return STATUS_COLOR.red;
  if (value >= 30) return STATUS_COLOR.yellow;
  return STATUS_COLOR.green;
}

function drawProvinceMap() {
  const canvas = el("provinceMap");
  const { ctx, width, height } = prepareCanvas(canvas);
  const rows = dataset.charts.provinceMap || [];
  const margin = { left: 28, right: 24, top: 18, bottom: 22 };
  const lonMin = 78;
  const lonMax = 132;
  const latMin = 18;
  const latMax = 52;
  const project = (lon, lat) => ({
    x: margin.left + (lon - lonMin) / (lonMax - lonMin) * (width - margin.left - margin.right),
    y: margin.top + (latMax - lat) / (latMax - latMin) * (height - margin.top - margin.bottom),
  });

  ctx.strokeStyle = "#dfe6ea";
  ctx.lineWidth = 1;
  for (let i = 0; i < 6; i += 1) {
    const y = margin.top + i * (height - margin.top - margin.bottom) / 5;
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(width - margin.right, y);
    ctx.stroke();
  }
  for (let i = 0; i < 8; i += 1) {
    const x = margin.left + i * (width - margin.left - margin.right) / 7;
    ctx.beginPath();
    ctx.moveTo(x, margin.top);
    ctx.lineTo(x, height - margin.bottom);
    ctx.stroke();
  }

  mapPoints = rows.map((row) => {
    const point = project(row.lon, row.lat);
    const radius = Math.min(19, 5 + Math.sqrt(row.count) * 1.3);
    const selected = el("provinceFilter").value === row.province;
    ctx.beginPath();
    ctx.arc(point.x, point.y, radius + (selected ? 3 : 0), 0, Math.PI * 2);
    ctx.fillStyle = `${riskColorFor(row.avgRisk)}${selected ? "e8" : "b8"}`;
    ctx.fill();
    ctx.strokeStyle = selected ? "#17324d" : "#ffffff";
    ctx.lineWidth = selected ? 2 : 1;
    ctx.stroke();
    if (row.count >= 15 || selected) {
      ctx.fillStyle = "#334252";
      ctx.font = "10px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText(row.province, point.x, point.y - radius - 5);
    }
    return { ...row, ...point, radius: radius + 5 };
  });
}

function mapHit(event) {
  const rect = el("provinceMap").getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  return mapPoints.find((point) => Math.hypot(point.x - x, point.y - y) <= point.radius);
}

function handleMapMove(event) {
  const hit = mapHit(event);
  const tooltip = el("mapTooltip");
  if (!hit) {
    tooltip.hidden = true;
    return;
  }
  tooltip.hidden = false;
  tooltip.style.left = `${Math.min(event.offsetX + 12, el("provinceMap").clientWidth - 150)}px`;
  tooltip.style.top = `${Math.max(6, event.offsetY - 50)}px`;
  tooltip.innerHTML = `<strong>${escapeHtml(hit.province)}</strong><br>${hit.count} 台 · 平均风险 ${hit.avgRisk}<br>红 ${hit.red} / 黄 ${hit.yellow} / 绿 ${hit.green}`;
}

function handleMapClick(event) {
  const hit = mapHit(event);
  if (!hit) return;
  el("provinceFilter").value = hit.province;
  el("clearProvince").hidden = false;
  renderMachines(true);
  drawProvinceMap();
}

function openDrawer(machine) {
  selectedMachine = machine;
  selectedDetailTab = "overview";
  el("drawerFactory").textContent = `${machine.factory || "未知烟厂"} · ${machine.region || machine.province || "地区未知"}`;
  el("drawerTitle").textContent = `${machine.machineId} · ${machine.model || "未知机型"}`;
  renderDrawerHero(machine);
  renderDetailTabs();
  renderDrawerContent();
  el("drawerBackdrop").hidden = false;
  el("machineDrawer").classList.add("open");
  el("machineDrawer").setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
}

function closeDrawer() {
  el("machineDrawer").classList.remove("open");
  el("machineDrawer").setAttribute("aria-hidden", "true");
  el("drawerBackdrop").hidden = true;
  document.body.classList.remove("drawer-open");
}

function renderDrawerHero(m) {
  const degree = Math.max(0, Math.min(360, m.healthScore * 3.6));
  const color = STATUS_COLOR[m.status];
  const warning = m.warnings?.[0]?.title || "当前未命中显著风险规则";
  el("drawerHero").innerHTML = `
    <div class="drawer-score">
      <div class="score-disc" style="background:conic-gradient(${color} 0deg,${color} ${degree}deg,#e8edf1 ${degree}deg)"><span>${fmt(m.healthScore)}</span></div>
      <div class="drawer-summary">
        <h3><span class="status-pill ${m.status}">${STATUS_LABEL[m.status]}</span> 风险分 ${fmt(m.riskScore)}</h3>
        <p>${escapeHtml(warning)}</p>
        <div class="tag-row">
          <span class="tag">${escapeHtml(m.province || "地区未知")}</span>
          <span class="tag">${escapeHtml(m.baseStatus || "台账状态未知")}</span>
          <span class="tag">下次诊断 ${escapeHtml(m.nextDiagnosisDate || "--")}</span>
        </div>
      </div>
    </div>`;
}

function renderDetailTabs() {
  document.querySelectorAll("[data-detail-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.detailTab === selectedDetailTab);
  });
}

function renderDrawerContent() {
  const m = selectedMachine;
  if (!m) return;
  const renderers = {
    overview: detailOverview,
    operation: detailOperation,
    maintenance: detailMaintenance,
    factory: detailFactoryScore,
    events: detailEvents,
  };
  el("drawerContent").innerHTML = renderers[selectedDetailTab](m);
}

function fields(items) {
  return `<div class="detail-grid">${items.map(([label, value]) => `<div class="detail-field"><span>${label}</span><strong>${escapeHtml(fmt(value))}</strong></div>`).join("")}</div>`;
}

function detailOverview(m) {
  const components = Object.entries(m.riskComponents || {});
  return `
    <section class="detail-section">
      <h3>设备档案</h3>
      ${fields([
        ["机组号", m.machineId], ["烟厂", m.factory], ["用户编号", m.userCode],
        ["地区", m.region], ["省份 / 城市", `${m.province || "--"} / ${m.city || "--"}`], ["机型", m.model],
        ["项目号", m.projectNo], ["台账状态", m.baseStatus], ["模型版本", dataset.modelVersion],
      ])}
    </section>
    <section class="detail-section">
      <h3>风险构成</h3>
      ${components.length ? components.map(([key, value]) => `
        <div class="component-row"><span>${COMPONENT_LABEL[key] || key}</span><div class="risk-track"><span style="width:${Math.min(100, value * 3)}%;background:${riskColorFor(value * 2)}"></span></div><strong>${value}</strong></div>`).join("") : "<p>暂无明显风险项。</p>"}
    </section>
    <section class="detail-section">
      <h3>关键时间</h3>
      ${fields([
        ["诊断日期", m.diagnosisDate], ["下次诊断", m.nextDiagnosisDate], ["距下次诊断", m.daysToNextDiagnosis === undefined ? "--" : `${m.daysToNextDiagnosis} 天`],
        ["最近运行", m.latestRun], ["最近巡检报告", m.latestReportDate], ["最近调试异常", m.latestDebugDate],
      ])}
    </section>`;
}

function detailOperation(m) {
  const reasons = m.topReasons || [];
  return `
    <section class="detail-section">
      <h3>近期运行</h3>
      ${fields([
        ["统计班次", m.shifts], ["总产量", num(m.totalOutput)], ["平均有效作业率", m.avgEffectiveRate === undefined ? "--" : `${m.avgEffectiveRate}%`],
        ["总有效作业率", m.avgTotalEffectiveRate === undefined ? "--" : `${m.avgTotalEffectiveRate}%`], ["停机时长", m.downtimeMinutes === undefined ? "--" : `${m.downtimeMinutes} 分钟`], ["停机次数", m.stopCount],
        ["每班停机次数", m.stopCountPerShift], ["内部停机", m.internalDowntimeMinutes === undefined ? "--" : `${m.internalDowntimeMinutes} 分钟`], ["外部停机", m.externalDowntimeMinutes === undefined ? "--" : `${m.externalDowntimeMinutes} 分钟`],
      ])}
    </section>
    <section class="detail-section">
      <h3>剔除质量</h3>
      ${fields([["剔除量", m.rejectQty], ["产量基数", m.rejectBaseQty], ["剔除率", pct(m.rejectRate)]])}
      <div class="detail-list">${reasons.length ? reasons.map((row) => `<div class="detail-item"><strong>${escapeHtml(row.reason)}</strong>${num(row.qty)} 件</div>`).join("") : '<div class="detail-item">暂无剔除原因数据。</div>'}</div>
    </section>
    <section class="detail-section">
      <h3>项目节点原始信息</h3>
      ${fields([["发运", m.shipmentInfo], ["调试开始", m.commissionInfo], ["交验", m.acceptanceInfo]])}
    </section>`;
}

function detailMaintenance(m) {
  const inspectionDetails = m.inspectionDetails || [];
  const inspectionIssues = inspectionDetails.filter((row) => row.status === "红" || row.status === "黄");
  const diagnosisDetails = m.diagnosisDetails || [];
  return `
    <section class="detail-section">
      <h3>最新巡检报告</h3>
      ${fields([
        ["报告编号", m.latestReportNo], ["报告日期", m.latestReportDate], ["运转情况", m.operationText],
        ["记录点总数", m.latestInspectionTotal || 0], ["合格点", m.latestInspectionQualified || 0], ["不合格/警戒点", m.latestInspectionUnqualified || 0],
        ["未判定", m.latestInspectionUnknown || 0], ["历史报告数", m.reportCount || 0], ["历史记录点", m.inspectionRows || 0],
      ])}
      <div class="point-summary">
        <span class="point-count qualified">合格 ${m.latestInspectionQualified || 0}</span>
        <span class="point-count warning">警戒/不合格 ${m.latestInspectionUnqualified || 0}</span>
        <span class="point-count unknown">未判定 ${m.latestInspectionUnknown || 0}</span>
      </div>
    </section>
    <section class="detail-section">
      <h3>巡检问题点明细</h3>
      ${inspectionIssues.length
        ? inspectionIssues.map(inspectionPoint).join("")
        : '<div class="detail-item">最新报告全部检查点均为绿色合格。</div>'}
      <details class="all-points">
        <summary>展开全部 ${inspectionDetails.length} 个检查点</summary>
        <div class="point-list">${inspectionDetails.map(inspectionPoint).join("") || "<p>暂无巡检明细。</p>"}</div>
      </details>
    </section>
    <section class="detail-section">
      <h3>诊断问题汇总</h3>
      ${fields([
        ["问题总数", m.diagnosisIssues || 0], ["未闭环", m.openDiagnosisIssues || 0], ["A / B / C", `${m.priorityA || 0} / ${m.priorityB || 0} / ${m.priorityC || 0}`],
        ["未闭环 A", m.openPriorityA || 0], ["未闭环 B", m.openPriorityB || 0], ["最近诊断", m.latestDiagnosisDate],
      ])}
      <div class="issue-records">${diagnosisDetails.length
        ? diagnosisDetails.map(diagnosisRecord).join("")
        : '<div class="detail-item">暂无诊断问题明细。</div>'}</div>
    </section>`;
}

function inspectionPoint(row) {
  const tone = row.status === "红" ? "red" : row.status === "黄" ? "yellow" : row.status === "绿" ? "green" : "unknown";
  const standard = [row.qualifiedValue && `合格 ${row.qualifiedValue}`, row.warningValue && `警戒 ${row.warningValue}`, row.unit].filter(Boolean).join(" · ");
  return `
    <article class="point-record ${tone}">
      <div class="point-record-head">
        <span class="status-pill ${tone === "red" ? "red" : tone === "yellow" ? "yellow" : "green"}">${escapeHtml(row.status)}</span>
        <strong>${escapeHtml(row.location || "位置未填写")} / ${escapeHtml(row.part || "部件未填写")}</strong>
      </div>
      <p><b>检查项：</b>${escapeHtml(row.checkItem || "--")}</p>
      <p><b>检查方法：</b>${escapeHtml(row.method || "--")} ${row.pointName ? `· ${escapeHtml(row.pointName)}` : ""}</p>
      <p><b>实测结果：</b>${escapeHtml(row.result || "--")}${standard ? `　<span class="muted-text">${escapeHtml(standard)}</span>` : ""}</p>
    </article>`;
}

function diagnosisRecord(row) {
  const priority = row.priority || "未分级";
  return `
    <article class="issue-record ${row.closed ? "closed" : "open"}">
      <div class="issue-record-head">
        <span class="priority priority-${escapeHtml(priority.toLowerCase())}">${escapeHtml(priority)}级</span>
        <strong>${escapeHtml(row.part || "部件未填写")} / ${escapeHtml(row.location || "位置未填写")}</strong>
        <span>${escapeHtml(row.status || (row.closed ? "已闭环" : "待处理"))}</span>
      </div>
      <div class="issue-record-body">
        <p><b>检查项：</b>${escapeHtml(row.checkItem || "--")}</p>
        <p><b>具体问题：</b>${escapeHtml(row.content || "--")}</p>
        <p><b>处理方案：</b>${escapeHtml(row.plan || "暂未填写")}</p>
        <p class="muted-text">${escapeHtml(row.date || "--")} ${row.owner ? `· 负责人 ${escapeHtml(row.owner)}` : ""}</p>
      </div>
    </article>`;
}

function detailFactoryScore(m) {
  if (m.factoryScore === undefined || m.factoryScore === null) {
    return `<section class="detail-section"><h3>整机出厂前评分</h3><p>该机组暂未在评分工作簿中找到匹配记录。</p></section>`;
  }
  const sections = Object.entries(m.factoryScoreSections || {});
  const details = m.factoryDeductionDetails || [];
  return `
    <section class="detail-section">
      <h3>评分摘要</h3>
      ${fields([
        ["总分", `${m.factoryScore} 分`], ["出厂日期", m.factoryDate], ["调试部门", m.commissioningDept],
        ["评分用户", m.factoryScoreUser], ["评分机型", m.factoryScoreModel], ["评分记录数", m.factoryScoreRecordCount || 1],
        ["扣分项数量", m.factoryDeductionItemCount || 0], ["明细累计扣分", m.factoryDeductionTotal || 0], ["附件数量", m.factoryScoreAttachmentCount || 0],
      ])}
    </section>
    <section class="detail-section">
      <h3>分项得分</h3>
      ${sections.length ? sections.map(([name, value]) => `
        <div class="component-row"><span>${escapeHtml(name)}</span><div class="risk-track"><span style="width:${Math.min(100, value * 5)}%;background:#2563a8"></span></div><strong>${value}</strong></div>`).join("") : "<p>暂无分项得分。</p>"}
    </section>
    <section class="detail-section">
      <h3>扣分明细</h3>
      ${details.length ? details.map((row) => `
        <div class="deduction-row">
          <strong>${escapeHtml(row.stage || row.sequence || "未分类")}</strong>
          <span>${escapeHtml(row.reason || row.description || "未填写扣分原因")}${row.rectification ? `<br>整改：${escapeHtml(row.rectification)}` : ""}</span>
          <strong>-${fmt(row.deduction, 0)}</strong>
        </div>`).join("") : "<p>暂无扣分明细。</p>"}
    </section>`;
}

function detailEvents(m) {
  const warnings = m.warnings || [];
  const debugDetails = m.debugDetails || [];
  const ncrDetails = m.ncrDetails || [];
  return `
    <section class="detail-section">
      <h3>故障预警解释</h3>
      <div class="detail-list">${warnings.length ? warnings.map((w) => `<div class="detail-item ${w.severity}"><strong>${escapeHtml(w.title)}</strong>${escapeHtml(w.source)} · ${escapeHtml(w.detail || "")}</div>`).join("") : '<div class="detail-item">暂无规则预警。</div>'}</div>
    </section>
    <section class="detail-section">
      <h3>历史调试问题明细</h3>
      ${fields([
        ["调试异常总数", m.debugIssues || 0], ["近 180 天", m.recentDebugIssues || 0], ["未闭环调试", m.openDebugIssues || 0],
        ["严重异常", m.severeDebugIssues || 0], ["最近调试日期", m.latestDebugDate], ["问题类型数", (m.topIssueTypes || []).length],
      ])}
      <div class="issue-records">${debugDetails.length
        ? debugDetails.map(debugRecord).join("")
        : '<div class="detail-item">暂无历史调试问题。</div>'}</div>
    </section>
    <section class="detail-section">
      <h3>外部 NCR</h3>
      ${fields([
        ["NCR 总数", m.ncrCount || 0], ["未结束 NCR", m.openNcrCount || 0], ["NCR A / B / C", `${m.ncrLevelA || 0} / ${m.ncrLevelB || 0} / ${m.ncrLevelC || 0}`],
        ["NCR 成本", m.ncrCost === undefined ? "--" : `¥${num(m.ncrCost)}`], ["最近 NCR", m.latestNcrDate],
      ])}
      <div class="issue-records">${ncrDetails.length
        ? ncrDetails.map(ncrRecord).join("")
        : '<div class="detail-item">暂无外部 NCR 明细。</div>'}</div>
    </section>
    <section class="detail-section">
      <h3>数据来源</h3>
      <table class="source-table"><thead><tr><th>数据集</th><th>作用</th><th>记录数</th><th>更新时间</th></tr></thead><tbody>
        ${dataset.sourceFiles.map((source) => `<tr><td>${escapeHtml(source.name)}</td><td>${escapeHtml(source.role)}</td><td>${num(source.rows)}</td><td>${escapeHtml(source.modified)}</td></tr>`).join("")}
      </tbody></table>
    </section>`;
}

function debugRecord(row) {
  return `
    <article class="issue-record ${row.closed ? "closed" : "open"}">
      <div class="issue-record-head">
        <span class="priority">${escapeHtml(row.type || "未分类")}</span>
        <strong>${escapeHtml(row.content || "未填写问题内容")}</strong>
        <span>${escapeHtml(row.status || "--")}</span>
      </div>
      <div class="issue-record-body">
        <p><b>处理情况：</b>${escapeHtml(row.handling || "暂未填写")}</p>
        <p class="muted-text">${escapeHtml(row.date || "--")} ${row.owner ? `· 处理人 ${escapeHtml(row.owner)}` : ""} ${row.handledDate ? `· 处理日期 ${escapeHtml(row.handledDate)}` : ""}</p>
      </div>
    </article>`;
}

function ncrRecord(row) {
  const level = row.level || "未分级";
  const title = row.title || row.description || `NCR ${row.reportNo || ""}`;
  return `
    <article class="issue-record ${row.closed ? "closed" : "open"}">
      <div class="issue-record-head">
        <span class="priority priority-${escapeHtml(level.toLowerCase())}">${escapeHtml(level)}级</span>
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(row.status || "--")}</span>
      </div>
      <div class="issue-record-body">
        ${row.description && row.description !== title ? `<p><b>问题描述：</b>${escapeHtml(row.description)}</p>` : ""}
        <p><b>原因分析：</b>${escapeHtml(row.cause || "暂未填写")}</p>
        <p><b>处理方案：</b>${escapeHtml(row.plan || "暂未填写")}</p>
        ${row.result ? `<p><b>处理结果：</b>${escapeHtml(row.result)}</p>` : ""}
        <p class="muted-text">
          报告 ${escapeHtml(row.reportNo || "--")} · ${escapeHtml(row.date || "--")}
          ${row.closeDate ? ` · 结束 ${escapeHtml(row.closeDate)}` : ""}
          ${row.department ? ` · 责任部门 ${escapeHtml(row.department)}` : ""}
          ${row.owner ? ` · 责任人 ${escapeHtml(row.owner)}` : ""}
          ${row.cost ? ` · 成本 ¥${num(row.cost)}` : ""}
        </p>
      </div>
    </article>`;
}

function resetFilters() {
  ["searchInput", "statusFilter", "modelFilter", "provinceFilter"].forEach((id) => { el(id).value = ""; });
  el("sortSelect").value = "riskDesc";
  el("clearProvince").hidden = true;
  renderMachines(true);
  drawProvinceMap();
}

async function refreshDataSources() {
  const button = el("refreshBtn");
  button.disabled = true;
  button.textContent = "校验中…";
  showToast("正在校验工作表结构和新增数据…");
  try {
    const response = await fetch("/api/refresh", { method: "POST" });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.message || "刷新失败");
    await loadData();
    const validation = result.dataQuality?.factoryScoring;
    const warningText = validation?.warningCount ? `，有 ${validation.warningCount} 项数据警告` : "";
    showToast(`刷新成功${warningText}。`);
  } catch (error) {
    showToast(`刷新未发布：${error.message}。上一版数据已保留。`, true);
  } finally {
    button.disabled = false;
    button.textContent = "校验刷新";
  }
}

function showToast(message, isError = false) {
  const toast = el("refreshToast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 5200);
}

function debounce(fn, wait = 120) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

["searchInput", "statusFilter", "modelFilter", "provinceFilter", "sortSelect"].forEach((id) => {
  el(id).addEventListener(id === "searchInput" ? "input" : "change", () => {
    if (id === "provinceFilter") el("clearProvince").hidden = !el("provinceFilter").value;
    renderMachines(true);
    if (id === "provinceFilter") drawProvinceMap();
  });
});

document.querySelectorAll("[data-status-filter]").forEach((card) => {
  const apply = () => {
    el("statusFilter").value = el("statusFilter").value === card.dataset.statusFilter ? "" : card.dataset.statusFilter;
    renderMachines(true);
  };
  card.addEventListener("click", apply);
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") apply();
  });
});

document.querySelectorAll("[data-analysis]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-analysis]").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll(".analysis-view").forEach((view) => view.classList.remove("active"));
    const viewId = {
      risk: "riskAnalysis",
      quality: "qualityAnalysis",
      factory: "factoryAnalysis",
    }[button.dataset.analysis];
    el(viewId).classList.add("active");
    requestAnimationFrame(renderCharts);
  });
});

document.querySelectorAll("[data-detail-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    selectedDetailTab = button.dataset.detailTab;
    renderDetailTabs();
    renderDrawerContent();
  });
});

el("provinceMap").addEventListener("mousemove", handleMapMove);
el("provinceMap").addEventListener("mouseleave", () => { el("mapTooltip").hidden = true; });
el("provinceMap").addEventListener("click", handleMapClick);
el("clearProvince").addEventListener("click", () => {
  el("provinceFilter").value = "";
  el("clearProvince").hidden = true;
  renderMachines(true);
  drawProvinceMap();
});
el("loadMoreBtn").addEventListener("click", () => { visibleLimit += machinePageSize(); renderMachines(); });
el("toggleFilters").addEventListener("click", () => {
  const expanded = el("toolbar")?.classList.toggle("expanded") ?? document.querySelector(".toolbar").classList.toggle("expanded");
  el("toggleFilters").setAttribute("aria-expanded", String(expanded));
  el("toggleFilters").textContent = expanded ? "收起筛选" : "更多筛选";
});
el("toggleAnalysis").addEventListener("click", () => {
  const section = document.querySelector(".analysis-section");
  const expanded = section.classList.toggle("expanded");
  el("toggleAnalysis").setAttribute("aria-expanded", String(expanded));
  el("toggleAnalysis").textContent = expanded ? "收起分析图表" : "展开分析图表";
  if (expanded) requestAnimationFrame(renderCharts);
});
el("resetFilters").addEventListener("click", resetFilters);
el("refreshBtn").addEventListener("click", refreshDataSources);
if (!IS_LOCAL_SERVICE) {
  el("refreshBtn").disabled = true;
  el("refreshBtn").textContent = "数据快照";
  el("refreshBtn").title = "线上页面展示最近一次校验通过的数据快照";
}
el("closeDrawer").addEventListener("click", closeDrawer);
el("drawerBackdrop").addEventListener("click", closeDrawer);
window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
window.addEventListener("resize", debounce(() => {
  renderCharts();
  renderAlerts();
}, 180));

loadData().catch((error) => {
  document.body.innerHTML = `<main><section class="panel"><h1>数据载入失败</h1><p>${escapeHtml(error.message)}</p></section></main>`;
});
