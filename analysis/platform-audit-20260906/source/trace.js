const state = {
  bootstrap: null,
  selectedPackage: null,
  selectedComponentTraceExcel: null,
  selectedSrmPartsExcel: null,
  selectedUserExcel: null,
  user: null,
  csrfToken: "",
  storage: null,
  selectedSite: sessionStorage.getItem("traceSelectedSite") || "HQ",
  masterSite: sessionStorage.getItem("traceMasterSite") || "",
  batches: { issue: [], bind: [] },
  bindSession: null,
  selectedUnbindIds: new Set(),
  detailStatus: null,
  masterSearch: "",
  masterWarningsOnly: false,
  masterPage: 1,
  adminData: {
    catalogLoaded: false,
    tables: [],
    recentAudit: [],
    tableName: "",
    directoryQuery: "",
    query: "",
    site: "",
    page: 1,
    pageSize: 20,
    total: 0,
    totalPages: 1,
    records: [],
    requestSerial: 0,
    loading: false,
    recordAction: "",
    record: null,
    proposedValues: null,
    differences: [],
    dirty: false,
    returnFocus: null,
    searchTimer: null,
  },
  scanner: {
    request: null, stream: null, frameRequest: null, torchOn: false, lastFrameAt: 0,
    processing: false, lastDecodedValue: "", lastSeenAt: 0, successCount: 0, failureCount: 0,
    detector: null, detecting: false, scanAttempt: 0,
  },
  scanTyping: { startedAt: 0, lastAt: 0, keyCount: 0, pendingMethod: "MANUAL" },
};

const viewNames = {
  dashboard: "运行总览",
  board: "追溯看板",
  trace: "追溯查询",
  issue: "仓库发放",
  bind: "现场绑定",
  access: "身份同步",
  "admin-data": "业务数据管理",
  exchange: "系统维护",
  master: "追溯清单",
};

const eventLabels = {
  CATALOG_REPLACED: "替换追溯清单",
  COMPONENT_TRACE_IMPORTED: "更新部件追溯清单",
  SRM_PARTS_IMPORTED: "更新SRM零件清单",
  ISSUE: "仓库确认发放",
  BIND: "现场确认绑定",
  UNBIND: "解除现场绑定",
  PACKAGE_IMPORT: "导入备份数据包",
};

const statusLabels = { PLANNED: "未绑定", BOUND: "已绑定" };
const roleLabels = {
  ADMIN: "系统管理员",
  WAREHOUSE_OPERATOR: "仓库操作员",
  ASSEMBLY_OPERATOR: "现场装配操作员",
  VIEWER: "只读查询",
};

function canManageAdminData(user = state.user) {
  return Boolean(
    user && user.role === "ADMIN" && user.site_code === "HQ" && !user.must_change_password
  );
}

function canAccess(view, role = state.user?.role, user = state.user) {
  if (["dashboard", "board", "trace"].includes(view)) return true;
  if (view === "admin-data") return role === "ADMIN" && canManageAdminData(user);
  if (["access", "exchange", "master"].includes(view)) return role === "ADMIN";
  if (view === "issue") return false;
  if (view === "bind") {
    if (role === "ADMIN") return true;
    return ["XC", "JC"].includes(state.user?.site_code) &&
      role === "ASSEMBLY_OPERATOR";
  }
  return false;
}

function userCanUnbindBinding(user, bindingSite) {
  const role = String(user?.role || "").toUpperCase();
  const userSite = String(user?.site_code || "").toUpperCase();
  const site = String(bindingSite || "").toUpperCase();
  if (role === "ADMIN" && userSite === "HQ") return ["XC", "JC"].includes(site);
  return ["ADMIN", "ASSEMBLY_OPERATOR"].includes(role) &&
    ["XC", "JC"].includes(userSite) && site === userSite;
}

async function api(path, options = {}) {
  const response = await fetch(path.startsWith("/") ? path.slice(1) : path, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(state.csrfToken ? { "X-CSRF-Token": state.csrfToken } : {}),
      ...(state.selectedSite ? { "X-Site-Code": state.selectedSite } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `请求失败（${response.status}）`);
    error.status = response.status;
    if (response.status === 401 && !path.includes("/auth/login")) showLogin();
    throw error;
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / (1024 ** index)).toFixed(index >= 3 ? 2 : 1)} ${units[index]}`;
}

function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("无法读取文件"));
    reader.readAsDataURL(file);
  });
}

function toast(title, message = "", type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.innerHTML = `<strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}`;
  document.querySelector("#toastRegion").append(node);
  setTimeout(() => node.remove(), 4600);
}

function resetAdminDataSession() {
  clearTimeout(state.adminData.searchTimer);
  state.adminData.catalogLoaded = false;
  state.adminData.tables = [];
  state.adminData.recentAudit = [];
  state.adminData.tableName = "";
  state.adminData.directoryQuery = "";
  state.adminData.query = "";
  state.adminData.site = "";
  state.adminData.page = 1;
  state.adminData.total = 0;
  state.adminData.totalPages = 1;
  state.adminData.records = [];
  state.adminData.requestSerial += 1;
  state.adminData.loading = false;
  state.adminData.recordAction = "";
  state.adminData.record = null;
  state.adminData.proposedValues = null;
  state.adminData.differences = [];
  state.adminData.dirty = false;
  state.adminData.returnFocus = null;
  state.adminData.searchTimer = null;
  ["#adminReviewDialog", "#adminRecordDialog"].forEach((selector) => {
    const dialog = document.querySelector(selector);
    if (dialog?.open) dialog.close();
  });
  const directory = document.querySelector("#adminEntityDirectory");
  if (directory) directory.innerHTML = '<div class="admin-directory-empty">登录后读取目录</div>';
  document.querySelector("#adminRecordFields")?.replaceChildren();
  document.querySelector("#adminReviewDiff")?.replaceChildren();
  const auditList = document.querySelector("#adminAuditList");
  if (auditList) auditList.innerHTML = '<div class="admin-directory-empty">暂无变更记录</div>';
  const catalogMeta = document.querySelector("#adminCatalogMeta");
  if (catalogMeta) catalogMeta.textContent = "登录后读取可管理业务实体";
  document.querySelector("#adminDataTableHead")?.replaceChildren();
  document.querySelector("#adminDataTableBody")?.replaceChildren();
  document.querySelector("#adminDataTableWrap")?.classList.add("hidden");
  document.querySelector("#adminPagination")?.classList.add("hidden");
}

function showLogin() {
  resetAdminDataSession();
  state.user = null;
  state.csrfToken = "";
  document.body.className = "auth-required";
  document.querySelector("#loginForm [name='username']")?.focus();
}

function refreshNavigationPermissions(user = state.user) {
  document.querySelectorAll(".nav-item[data-nav]").forEach((node) => {
    node.classList.toggle("hidden", !canAccess(node.dataset.nav, user?.role, user));
  });
  document.querySelectorAll(".nav-item[data-nav]:not(.hidden)").forEach((node, index) => {
    const marker = node.querySelector(".nav-icon");
    if (marker) marker.textContent = String(index + 1).padStart(2, "0");
  });
}

function applyAuthenticatedUser(user, csrfToken) {
  state.user = user;
  state.csrfToken = csrfToken;
  state.selectedSite = user.site_code;
  document.body.className = `authenticated ${user.role === "VIEWER" ? "read-only" : ""}`;
  document.querySelector("#passwordUsername").value = user.username;
  document.querySelector("#accountName").textContent = user.display_name;
  document.querySelector("#accountAvatar").textContent = user.display_name.slice(0, 1);
  document.querySelector("#accountMeta").textContent = `${user.site_name} · ${roleLabels[user.role] || user.role}`;
  refreshNavigationPermissions(user);
  const createSiteSelect = document.querySelector("#createUserForm [name='site_code']");
  if (createSiteSelect && user.role === "ADMIN") {
    const siteCodes = user.site_code === "HQ" ? ["HQ", "XC", "JC"] : [user.site_code];
    createSiteSelect.innerHTML = siteCodes.map((code) =>
      `<option value="${code}">${({ HQ: "总厂", XC: "新场", JC: "锦晨" })[code]}</option>`
    ).join("");
  }
  if (user.must_change_password) document.querySelector("#passwordDialog").showModal();
}

function navigate(view, updateHash = true) {
  if (!viewNames[view]) view = "dashboard";
  if (!canAccess(view)) view = ["issue", "bind"].includes(view) ? "trace" : "dashboard";
  const activeView = document.querySelector(".view.active")?.id.replace("view-", "");
  if (activeView === "admin-data" && view !== "admin-data" && state.adminData.dirty) {
    const discard = window.confirm("当前业务记录有未保存的修改，确定放弃并离开吗？");
    if (!discard) return false;
    closeAdminRecord({ force: true, restoreFocus: false });
  }
  document.querySelectorAll(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  document.querySelectorAll(".nav-item").forEach((node) => {
    const active = node.dataset.nav === view;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  document.querySelector("#viewTitle").textContent = viewNames[view];
  document.querySelector("#viewCrumb").textContent = viewNames[view];
  if (updateHash) history.replaceState(null, "", `#${view}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
  setTimeout(() => {
    if (view === "trace") document.querySelector("#tracePartCode")?.focus();
    if (view === "issue") document.querySelector("#issueOrderInput")?.focus();
    if (view === "bind") {
      document.querySelector(state.bindSession ? "#bindPartInput" : "#bindOrderInput")?.focus();
    }
    if (view === "access") loadUsers();
    if (view === "admin-data") {
      document.querySelector("#adminDataTitle")?.focus({ preventScroll: true });
      if (!state.adminData.catalogLoaded) loadAdminCatalog();
    }
    if (view === "exchange") {
      loadExchangeLogs();
      loadStorageStatus();
    }
    if (view === "board") renderAnalyticsBoard();
  }, 100);
  return true;
}

async function loadBootstrap() {
  const data = await api("/api/bootstrap");
  state.bootstrap = data;
  state.selectedSite = data.site.code;
  sessionStorage.setItem("traceSelectedSite", state.selectedSite);
  document.querySelector("#siteName").textContent = data.site.name;
  document.querySelector("#siteCode").textContent = data.site.code;
  document.querySelector("#heroSiteCode").textContent = data.site.code;
  document.querySelector("#bindSiteContext").textContent =
    data.user.role === "ADMIN" && data.user.site_code === "HQ" && data.site.code === "HQ"
      ? "管理员全权限 · 请在右上角选择新场或锦晨后执行现场绑定"
      : `${data.site.name} · 当前绑定写入本站并全程留痕`;
  document.querySelector("#exchangeWarning").classList.toggle("hidden", data.site.exchange_key_configured);
  const switcher = document.querySelector("#siteSwitcher");
  document.querySelector("#siteSwitcherWrap").classList.toggle("hidden", data.sites.length < 2);
  switcher.innerHTML = data.sites.map((site) => `<option value="${escapeHtml(site.code)}">${escapeHtml(site.name)}</option>`).join("");
  switcher.value = data.site.code;
  const masterSites = data.master_sites || [];
  if (!masterSites.some((site) => site.code === state.masterSite)) {
    state.masterSite = masterSites[0]?.code || "";
  }
  sessionStorage.setItem("traceMasterSite", state.masterSite);
  const masterSwitcher = document.querySelector("#masterSiteSwitcher");
  masterSwitcher.innerHTML = masterSites.map((site) =>
    `<option value="${escapeHtml(site.code)}">${escapeHtml(site.name)}</option>`
  ).join("");
  masterSwitcher.value = state.masterSite;
  document.querySelector("#masterSiteSwitcherWrap").classList.toggle("hidden", masterSites.length < 2);
  document.querySelectorAll("[data-master-site-name]").forEach((node) => {
    node.textContent = masterSites.find((site) => site.code === state.masterSite)?.name || data.site.name;
  });
  document.querySelectorAll("[data-bind-entry]").forEach((node) => {
    node.classList.toggle("hidden", !canAccess("bind"));
  });
  renderStats(data.stats);
  renderRecentEvents(data.recent_events);
  renderCatalog(data);
  renderAnalyticsBoard();
  hydrateOrders(data.component_orders);
  if (data.user.role === "ADMIN") await loadUsers();
  if (data.user.role === "ADMIN" && data.user.site_code === "HQ") {
    loadStorageStatus({ silent: true });
  }
}

function renderStats(stats) {
  const cards = [
    ["未绑定", Number(stats.not_bound || 0), "尚未开始扫码的装配订单", "01", "NOT_BOUND"],
    ["部分绑定", Number(stats.partial || 0), "已有核销但清单未完成", "02", "PARTIAL"],
    ["已全部绑定", Number(stats.complete || 0), "质量追溯零件零遗漏", "03", "COMPLETE"],
  ];
  document.querySelector("#statGrid").innerHTML = cards.map(([label, value, hint, index, status]) =>
    status
      ? `<button class="stat-card is-actionable" type="button" data-index="${index}" data-status-detail="${status}" aria-label="查看${label}明细">
          <span>${label}</span><strong>${value}</strong><small>${hint}</small><em>查看明细 →</em>
        </button>`
      : `<article class="stat-card" data-index="${index}"><span>${label}</span><strong>${value}</strong><small>${hint}</small></article>`
  ).join("");
  document.querySelector("#catalogPartCount").textContent = Number(stats.total_requirements || 0);
}

function statusDetailParts() {
  if (!state.detailStatus || !state.bootstrap) return [];
  const query = document.querySelector("#statusDetailSearch").value.trim().toLowerCase();
  return (state.bootstrap.order_statuses || []).filter((part) => {
    if (part.binding_status !== state.detailStatus) return false;
    if (!query) return true;
    return [
      part.order_no,
      part.component_name,
      part.wbs,
    ].some((value) => String(value || "").toLowerCase().includes(query));
  });
}

function visibleUnbindBindings(parts) {
  return parts.flatMap((part) => (part.bound_parts || []).map((binding) => ({
    ...binding,
    component_order_no: part.order_no,
  })));
}

function updateUnbindSelectionBar(bindings) {
  const eligible = bindings.filter((binding) => userCanUnbindBinding(state.user, binding.site_code));
  const eligibleIds = new Set(eligible.map((binding) => Number(binding.binding_id)));
  state.selectedUnbindIds.forEach((bindingId) => {
    if (!eligibleIds.has(bindingId)) state.selectedUnbindIds.delete(bindingId);
  });
  const selectedCount = state.selectedUnbindIds.size;
  const bar = document.querySelector("#unbindSelectionBar");
  bar.classList.toggle("hidden", !eligible.length);
  document.querySelector("#selectedUnbindCount").textContent = selectedCount;
  document.querySelector("#clearUnbindSelection").disabled = !selectedCount;
  document.querySelector("#batchUnbindSelected").disabled = !selectedCount;
  const selectAll = document.querySelector("#selectVisibleBindings");
  selectAll.checked = Boolean(eligible.length) && selectedCount === eligible.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < eligible.length;
}

function renderStatusDetailList() {
  const parts = statusDetailParts();
  const target = document.querySelector("#statusDetailList");
  const bindings = visibleUnbindBindings(parts);
  document.querySelector("#statusDetailCount").textContent = parts.length;
  target.className = parts.length ? "status-detail-list" : "status-detail-list empty-state";
  target.innerHTML = parts.length ? parts.map((part, index) => `
    <article class="status-detail-order">
      <div class="status-detail-row">
        <span>${String(index + 1).padStart(2, "0")}</span>
        <div class="detail-part-code"><small>装配订单</small><b>${escapeHtml(part.order_no)}</b><i>${escapeHtml(part.wbs)}</i></div>
        <div><small>功能部件</small><b>${escapeHtml(part.component_name)}</b><i>${escapeHtml(part.component_code)}</i></div>
        <div><small>绑定进度</small><b>${Number(part.bound_count || 0)} / ${Number(part.part_count || 0)}</b><i>还需 ${Math.max(0, Number(part.part_count || 0) - Number(part.bound_count || 0))} 件</i></div>
        <div class="detail-site-time"><small>站点 / 当前状态</small><b>${escapeHtml(({XC:"新场",JC:"锦晨",HQ:"总厂"})[part.site_code] || part.site_code || "—")} · ${escapeHtml(({NOT_BOUND:"未绑定",PARTIAL:"部分绑定",COMPLETE:"已全部绑定"})[part.binding_status])}</b><i>${Math.round((Number(part.bound_count || 0) / Math.max(1, Number(part.part_count || 0))) * 100)}%</i></div>
        ${part.binding_status === "COMPLETE" || !canAccess("bind") ? "<span class=\"detail-complete\">只读</span>" : `<button type="button" data-detail-bind="${escapeHtml(part.order_no)}" data-detail-site="${escapeHtml(part.site_code)}">进入绑定</button>`}
      </div>
      ${["PARTIAL", "COMPLETE"].includes(part.binding_status) && (part.bound_parts || []).length ? `
        <div class="bound-detail-list">
          <header><span>已绑定实物零件</span><b>${part.bound_parts.length} 件</b><small>按零件订单号 / 行号 + 序列号逐条管理</small></header>
          ${(part.bound_parts || []).map((binding) => {
            const canUnbind = userCanUnbindBinding(state.user, binding.site_code);
            const bindingId = Number(binding.binding_id);
            return `
            <div class="bound-detail-item">
              ${canUnbind ? `<label class="unbind-select" title="选择 ${escapeHtml(binding.display_code)}"><input type="checkbox" data-select-unbind="${bindingId}" ${state.selectedUnbindIds.has(bindingId) ? "checked" : ""}><span aria-hidden="true">✓</span></label>` : `<span class="unbind-lock" aria-hidden="true">⌁</span>`}
              <span class="bound-origin">${binding.source_type === "PURCHASE" ? "采购" : "生产"}</span>
              <div><small>订单号 / 行号 + 序列号</small><b>${escapeHtml(binding.display_code)}</b><i>${escapeHtml(binding.part_code)} · ${escapeHtml(binding.process_key || `工序${binding.process_no || "缺失"}`)}</i></div>
              <div><small>绑定记录</small><b>${escapeHtml(binding.site_code)} · ${escapeHtml(binding.operator)}</b><i>${formatDate(binding.confirmed_at)}</i></div>
              <button class="unbind-part-button ${canUnbind ? "" : "is-denied"}" type="button" ${canUnbind ? `data-unbind-binding="${bindingId}"` : "data-unbind-denied"} data-unbind-code="${escapeHtml(binding.display_code)}" data-unbind-order="${escapeHtml(part.order_no)}">${canUnbind ? "解除绑定" : "无权限 · 解绑"}</button>
            </div>
          `}).join("")}
        </div>
      ` : ""}
    </article>
  `).join("") : `<div class="detail-empty"><span>∅</span><h3>没有匹配明细</h3><p>请调整搜索关键词或切换右上角站点视图。</p></div>`;
  updateUnbindSelectionBar(bindings);
}

function openStatusDetail(status) {
  const config = {
    NOT_BOUND: { title: "未绑定装配订单", eyebrow: "NOT STARTED · ASSEMBLY ORDERS" },
    PARTIAL: { title: "部分绑定装配订单", eyebrow: "IN PROGRESS · ASSEMBLY ORDERS" },
    COMPLETE: { title: "已全部绑定装配订单", eyebrow: "COMPLETE · RELEASED" },
  }[status];
  state.detailStatus = status;
  state.selectedUnbindIds.clear();
  document.querySelector("#statusDetailTitle").textContent = config.title;
  document.querySelector("#statusDetailEyebrow").textContent = config.eyebrow;
  document.querySelector("#statusDetailMeta").textContent =
    `${state.bootstrap.site.name} · ${(state.bootstrap.order_statuses || []).filter((part) => part.binding_status === status).length} 个装配订单`;
  document.querySelector("#statusDetailSearch").value = "";
  document.querySelector("#statusDetailDialog").dataset.status = status;
  renderStatusDetailList();
  document.querySelector("#statusDetailDialog").showModal();
  setTimeout(() => document.querySelector("#statusDetailSearch").focus(), 80);
}

function closeStatusDetail() {
  const dialog = document.querySelector("#statusDetailDialog");
  if (dialog.open) dialog.close();
  state.detailStatus = null;
}

function renderRecentEvents(events) {
  const target = document.querySelector("#recentEvents");
  target.className = events.length ? "event-list" : "event-list empty-state";
  target.innerHTML = events.length ? events.map((event) => `
    <div class="event-item">
      <i></i><div><strong>${escapeHtml(eventLabels[event.event_type] || event.event_type)}</strong>
      <small>${escapeHtml(event.object_no || "系统记录")} · ${escapeHtml(event.operator)}</small></div>
      <time>${formatDate(event.created_at)}</time>
    </div>`).join("") : "暂无操作记录";
}

function hydrateOrders(orders) {
  document.querySelector("#componentOrderOptions").innerHTML = orders
    .filter((item) => Number(item.part_count || 0) > 0 && item.binding_status !== "COMPLETE")
    .map((item) =>
    `<option value="${escapeHtml(item.order_no)}">${escapeHtml(item.component_name)} · ${escapeHtml(item.wbs)}</option>`
  ).join("");
}

function renderCatalog(data) {
  const orders = data.component_orders || [];
  const imports = data.latest_master_imports || [];
  const siteOrders = orders.filter((item) => item.site_code === state.masterSite);
  const keyword = state.masterSearch.trim().toLowerCase();
  const selectedOrders = siteOrders.filter((item) => !keyword || `${item.order_no} ${item.component_name} ${item.wbs}`.toLowerCase().includes(keyword));
  const pageSize = 12;
  const totalPages = Math.max(1, Math.ceil(selectedOrders.length / pageSize));
  state.masterPage = Math.min(state.masterPage, totalPages);
  const visibleOrders = selectedOrders.slice((state.masterPage - 1) * pageSize, state.masterPage * pageSize);
  document.querySelector("#componentOrderCount").textContent = keyword ? `${selectedOrders.length}/${siteOrders.length}` : siteOrders.length;
  const target = document.querySelector("#orderList");
  target.className = selectedOrders.length ? "master-list catalog-order-list" : "master-list empty-state";
  target.innerHTML = selectedOrders.length ? visibleOrders.map((item) => `
    <div class="master-item"><div><strong>${escapeHtml(item.order_no)}</strong>
    <span>${escapeHtml(item.component_name)} · ${escapeHtml(item.wbs)}</span></div>
    <b>${Number(item.part_count || 0)} 件</b></div>
  `).join("") : (keyword ? "没有符合条件的部件订单" : "暂无部件订单");
  const pagination = document.querySelector("#masterPagination");
  pagination.classList.toggle("hidden", selectedOrders.length <= pageSize);
  pagination.innerHTML = `<button type="button" data-master-page="prev" ${state.masterPage === 1 ? "disabled" : ""}>上一页</button><span>第 ${state.masterPage} / ${totalPages} 页</span><button type="button" data-master-page="next" ${state.masterPage === totalPages ? "disabled" : ""}>下一页</button>`;
  const audit = document.querySelector("#catalogAudit");
  if (!imports.length) {
    document.querySelector("#catalogImportMeta").textContent = "尚未导入两份主数据清单";
    document.querySelector("#catalogWarningCount").textContent = "0";
    audit.className = "catalog-audit empty-state";
    audit.textContent = "尚无导入记录";
    return;
  }
  const parsed = imports.map((item) => {
    let warnings = [];
    try { warnings = JSON.parse(item.warnings_json || "[]"); } catch {}
    return { ...item, warnings };
  });
  const selectedImports = parsed.filter((item) => item.site_code === state.masterSite);
  const warningCount = selectedImports.reduce((sum, item) => sum + item.warnings.length, 0);
  document.querySelector("#catalogWarningCount").textContent = warningCount;
  const componentImport = selectedImports.find((item) => item.list_type === "COMPONENT_TRACE");
  const srmImport = selectedImports.find((item) => item.list_type === "SRM_PARTS");
  document.querySelector("#catalogImportMeta").textContent =
    `需求 ${componentImport?.valid_count || 0} 件 · SRM实物 ${srmImport?.valid_count || 0} 件`;
  audit.className = "catalog-audit";
  const visibleImports = state.masterWarningsOnly ? selectedImports.filter((item) => item.warnings.length) : selectedImports;
  audit.innerHTML = visibleImports.map((item) => `
    <section class="audit-source">
      <div><b>${item.list_type === "COMPONENT_TRACE" ? "部件追溯清单" : "SRM零件清单"}</b><span>${({XC:"新场",JC:"锦晨"})[item.site_code] || item.site_code} · ${item.valid_count} 件</span></div>
      <p>${escapeHtml(item.filename)} · ${escapeHtml(item.imported_by)} · ${formatDate(item.imported_at)}</p>
      ${item.warnings.length ? `<small>${item.warnings.length} 条提示</small><details><summary>查看告警详情</summary><ul>${item.warnings.slice(0,8).map((warning) => `<li>${escapeHtml(typeof warning === "string" ? warning : JSON.stringify(warning))}</li>`).join("")}</ul>${item.warnings.length > 8 ? `<p>另有 ${item.warnings.length - 8} 条，请修正源文件后重新校验。</p>` : ""}</details>` : "<strong>校验通过，无导入告警</strong>"}
    </section>
  `).join("") || `<div class="empty-state">${state.masterWarningsOnly ? "当前没有导入告警" : "本站尚未导入清单"}</div>`;
}

function renderHorizontalBars(targetSelector, rows, valueKey = "value", secondaryKey = "") {
  const target = document.querySelector(targetSelector);
  const max = Math.max(1, ...rows.map((row) => Number(row[valueKey] || 0)));
  const toneFor = (row, index) => {
    if (targetSelector === "#componentChart") {
      const required = Number(row[valueKey] || 0);
      const bound = Number(row[secondaryKey] || 0);
      if (!bound) return "completion-empty";
      return bound >= required ? "completion-complete" : "completion-partial";
    }
    if (targetSelector === "#siteChart") {
      const label = String(row.label).toUpperCase();
      if (label.includes("总厂") || label === "HQ") return "hq";
      if (label.includes("新场") || label === "XC") return "xc";
      if (label.includes("锦晨") || label === "JC") return "jc";
      return ["hq", "xc", "jc"][index % 3];
    }
    if (targetSelector === "#methodChart") {
      const label = String(row.label).toUpperCase();
      if (label.includes("摄像") || label === "CAMERA") return "camera";
      if (label.includes("手工") || label === "MANUAL") return "manual";
      return "scanner";
    }
    return ["operator-a", "operator-b", "operator-c"][index % 3];
  };
  const displayLabelFor = (row) => {
    const raw = String(row.label || "");
    const upper = raw.toUpperCase();
    if (targetSelector === "#siteChart") {
      return ({ HQ: "总厂", XC: "新场", JC: "锦晨" })[upper] || raw;
    }
    if (targetSelector === "#methodChart") {
      return ({ SCANNER: "扫码枪", CAMERA: "手机摄像头", MANUAL: "手工输入" })[upper] || raw;
    }
    return raw;
  };
  target.innerHTML = rows.length ? rows.map((row, index) => {
    const value = Number(row[valueKey] || 0);
    const secondary = Number(row[secondaryKey] || 0);
    const displayLabel = displayLabelFor(row);
    const progressValue = secondaryKey ? secondary : value;
    const progressMax = secondaryKey ? Math.max(1, value) : max;
    return `<div class="chart-bar-row tone-${toneFor(row, index)}">
      <span title="${escapeHtml(displayLabel)}">${escapeHtml(displayLabel)}</span>
      <div class="chart-bar-track">
        <progress class="chart-bar-primary" value="${progressValue}" max="${progressMax}" aria-label="${escapeHtml(displayLabel)} ${secondaryKey ? `已完成 ${secondary}，共 ${value}` : value}"></progress>
      </div>
      <b>${secondaryKey ? `${secondary} / ${value}` : value}</b>
    </div>`;
  }).join("") : '<div class="chart-empty">暂无已完成绑定数据</div>';
}

function renderStatusDonut(rows) {
  const values = Object.fromEntries(rows.map((row) => [row.label, Number(row.value || 0)]));
  const total = Object.values(values).reduce((sum, value) => sum + value, 0);
  const notBound = values["未绑定"] || 0;
  const partial = values["部分绑定"] || 0;
  const complete = values["已全部绑定"] || 0;
  const segments = [];
  let cursor = 0;
  [
    [notBound, "a"],
    [partial, "b"],
    [complete, "c"],
  ].forEach(([value, tone]) => {
    if (!value || !total) return;
    const start = cursor;
    const length = value / total * 100;
    cursor += length;
    segments.push(`<circle class="donut-segment donut-segment-${tone}" cx="50" cy="50" r="38" pathLength="100" stroke-dasharray="${length} ${100 - length}" stroke-dashoffset="${-start}" transform="rotate(-90 50 50)"/>`);
  });
  document.querySelector("#statusChart").innerHTML = `
    <div class="donut-visual">
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <circle class="donut-track" cx="50" cy="50" r="38"/>
        ${segments.join("")}
      </svg>
      <strong>${total}</strong><span>装配订单</span>
    </div>
    <div class="donut-legend">
      <span data-tone="a"><i></i>未绑定<b>${notBound}</b></span>
      <span data-tone="b"><i></i>部分绑定<b>${partial}</b></span>
      <span data-tone="c"><i></i>已全部绑定<b>${complete}</b></span>
    </div>`;
}

function renderDailyLine(rows) {
  const target = document.querySelector("#dailyChart");
  if (!rows.length) {
    target.innerHTML = '<div class="chart-empty">完成首个现场绑定后，这里将显示日趋势</div>';
    return;
  }
  const width = 760;
  const height = 220;
  const padding = 34;
  const max = Math.max(1, ...rows.map((row) => Number(row.value || 0)));
  const points = rows.map((row, index) => {
    const x = padding + index * ((width - padding * 2) / Math.max(1, rows.length - 1));
    const y = height - padding - Number(row.value || 0) / max * (height - padding * 2);
    return { ...row, x, y };
  });
  const path = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="近14日现场绑定零件趋势">
    <title>近14日现场绑定零件趋势</title>
    <line x1="${padding}" x2="${width - padding}" y1="${height - padding}" y2="${height - padding}" class="chart-axis"/>
    <path d="${path}" class="trend-path"/>
    ${points.map((point) => `<g><circle cx="${point.x}" cy="${point.y}" r="5"/><text x="${point.x}" y="${point.y - 12}" text-anchor="middle">${Number(point.value)}</text><text x="${point.x}" y="${height - 10}" text-anchor="middle">${escapeHtml(point.label.slice(5))}</text></g>`).join("")}
  </svg>`;
}

function renderAnalyticsBoard() {
  const analytics = state.bootstrap?.analytics || {};
  const stats = state.bootstrap?.stats || {};
  document.querySelector("#boardKpis").innerHTML = [
    ["当前追溯需求", stats.total_requirements || 0, "有效需求行"],
    ["已核销零件", stats.bound_requirements || 0, "已完成关联"],
    ["装配订单完成率", `${Math.round((stats.complete || 0) / Math.max(1, (stats.not_bound || 0) + (stats.partial || 0) + (stats.complete || 0)) * 100)}%`, "按有追溯需求订单"],
  ].map(([label, value, hint]) => `<article><span>${label}</span><strong>${value}</strong><small>${hint}</small></article>`).join("");
  const hasBindings = Number(stats.bound_requirements || 0) > 0;
  document.querySelector("#boardEmptyGuide").classList.toggle("hidden", hasBindings);
  document.querySelector("#boardChartGrid").classList.toggle("hidden", !hasBindings);
  renderStatusDonut(analytics.status || []);
  renderHorizontalBars("#componentChart", analytics.components || [], "required", "bound");
  renderHorizontalBars("#siteChart", analytics.sites || []);
  renderHorizontalBars("#methodChart", analytics.input_methods || []);
  renderHorizontalBars("#operatorChart", analytics.operators || []);
  renderDailyLine(analytics.daily || []);
}

async function loadUsers() {
  const data = await api("/api/users");
  const target = document.querySelector("#userList");
  const counts = data.users.reduce((result, user) => {
    result[user.role] = (result[user.role] || 0) + 1;
    return result;
  }, {});
  document.querySelector("#accountSummary").textContent =
    `${data.users.filter((user) => user.active).length} 个启用 · ${counts.ADMIN || 0} 个管理员 · ${counts.ASSEMBLY_OPERATOR || 0} 个现场装配员`;
  target.className = data.users.length ? "user-list synced-user-list" : "user-list empty-state";
  target.innerHTML = data.users.length ? data.users.map((user) => `
    <article class="user-item synced-user ${user.active ? "" : "is-disabled"}" data-user-id="${user.id}">
      <div class="user-identity">
        <span>${escapeHtml(user.display_name.slice(0, 1))}</span>
        <div><b>${escapeHtml(user.username)}</b><small>${escapeHtml(user.site_name)} · ${escapeHtml(roleLabels[user.role] || user.role)}</small></div>
      </div>
      <span class="sync-source">质检平台同步</span>
      <i>${user.active ? "已启用" : "已停用"}</i>
    </article>
  `).join("") : "暂无账号";
}

function adminApi(path, options = {}) {
  return api(path, {
    ...options,
    headers: { ...(options.headers || {}), "X-Site-Code": "HQ" },
  });
}

function currentAdminTable() {
  return state.adminData.tables.find((table) => table.name === state.adminData.tableName) || null;
}

function adminActionLabel(action) {
  return ({ create: "新增", update: "修改", delete: "删除" })[String(action || "").toLowerCase()] || action || "变更";
}

function adminBooleanValue(value) {
  return value === true || value === 1 || value === "1" || String(value).toLowerCase() === "true";
}

function normalizeAdminValue(value, field = {}) {
  const type = String(field.input_type || "text").toLowerCase();
  if (value == null || value === "") return null;
  if (["boolean", "checkbox"].includes(type)) return adminBooleanValue(value);
  if (type === "number") return Number(value);
  if (typeof value === "object") {
    try { return JSON.stringify(value); } catch { return String(value); }
  }
  return String(value);
}

function adminValuesEqual(left, right, field) {
  return JSON.stringify(normalizeAdminValue(left, field)) === JSON.stringify(normalizeAdminValue(right, field));
}

function adminDisplayValue(value, field = {}, limit = 240) {
  const type = String(field.input_type || "text").toLowerCase();
  if (value == null || value === "") return "—";
  if (["boolean", "checkbox"].includes(type)) return adminBooleanValue(value) ? "是" : "否";
  let text;
  if (typeof value === "object") {
    try { text = JSON.stringify(value); } catch { text = String(value); }
  } else {
    text = String(value);
  }
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function adminPrimaryKeyNames(primaryKey, key = {}) {
  if (Array.isArray(primaryKey)) {
    return primaryKey.map((item) => typeof item === "object" ? item.name : item).filter(Boolean);
  }
  if (typeof primaryKey === "string" && primaryKey) return [primaryKey];
  if (primaryKey && typeof primaryKey === "object") {
    if (typeof primaryKey.name === "string") return [primaryKey.name];
    if (Array.isArray(primaryKey.fields)) {
      return primaryKey.fields.map((item) => typeof item === "object" ? item.name : item).filter(Boolean);
    }
    const matchingNames = Object.keys(primaryKey).filter((name) => Object.hasOwn(key, name));
    if (matchingNames.length) return matchingNames;
  }
  return key && typeof key === "object" && !Array.isArray(key) ? Object.keys(key) : [];
}

function adminKeyLabel(key, tableName = "") {
  if (key == null || key === "") return "—";
  if (typeof key !== "object") return adminDisplayValue(key, {}, 240);
  if (Array.isArray(key)) return key.map((value) => adminDisplayValue(value, {}, 120)).join(" / ") || "—";
  const table = state.adminData.tables.find((item) => item.name === tableName);
  const names = adminPrimaryKeyNames(table?.primary_key, key).filter((name) => Object.hasOwn(key, name));
  if (!names.length) return adminDisplayValue(key, {}, 240);
  if (names.length === 1) return adminDisplayValue(key[names[0]], {}, 240);
  return names.map((name) => `${name}=${adminDisplayValue(key[name], {}, 120)}`).join(" / ");
}

function adminAuditText(value, fallback = "—", limit = 360) {
  return value == null || value === "" ? fallback : adminDisplayValue(value, {}, limit);
}

function setAdminDataState(title, message = "", mode = "", retryKind = "records") {
  const target = document.querySelector("#adminDataState");
  target.className = `admin-data-state ${mode}`.trim();
  target.setAttribute("role", mode === "error" ? "alert" : "status");
  const marker = mode === "error" ? "!" : mode === "loading" ? "···" : "⌁";
  target.innerHTML = `<span aria-hidden="true">${marker}</span><b>${escapeHtml(title)}</b>${message ? `<small>${escapeHtml(message)}</small>` : ""}${mode === "error" ? `<button type="button" data-admin-retry="${escapeHtml(retryKind)}">重新加载</button>` : ""}`;
}

function renderAdminDirectory() {
  const query = state.adminData.directoryQuery.trim().toLowerCase();
  const tables = state.adminData.tables.filter((table) => {
    if (!query) return true;
    return [table.label, table.name, table.group, table.description]
      .some((value) => String(value || "").toLowerCase().includes(query));
  });
  const groups = new Map();
  tables.forEach((table) => {
    const group = table.group || "其他业务数据";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(table);
  });
  document.querySelector("#adminEntityCount").textContent = tables.length;
  const target = document.querySelector("#adminEntityDirectory");
  target.innerHTML = groups.size ? Array.from(groups.entries()).map(([group, items]) => `
    <section class="admin-entity-group">
      <h4>${escapeHtml(group)}</h4>
      ${items.map((table) => {
        const selected = table.name === state.adminData.tableName;
        const editable = Boolean(table.editable && (table.allow_create || table.allow_update || table.allow_delete));
        return `<button type="button" data-admin-table="${escapeHtml(table.name)}" ${selected ? 'aria-current="page"' : ""} class="${selected ? "active" : ""}">
          <span><b>${escapeHtml(table.label || table.name)}</b><small>${escapeHtml(table.description || table.name)}</small></span>
          <em>${Number(table.count || 0)}</em><i class="${editable ? "editable" : "readonly"}">${editable ? "可维护" : "只读"}</i>
        </button>`;
      }).join("")}
    </section>
  `).join("") : `<div class="admin-directory-empty">${query ? "没有匹配的业务实体" : "暂无可查看业务实体"}</div>`;
}

function renderAdminAudit() {
  const target = document.querySelector("#adminAuditList");
  const items = Array.isArray(state.adminData.recentAudit) ? state.adminData.recentAudit : [];
  target.className = items.length ? "admin-audit-list" : "admin-audit-list is-empty";
  target.innerHTML = items.length ? items.map((item) => {
    const rawAction = adminAuditText(item.action ?? item.event_type, "update", 40).toLowerCase();
    const action = ["create", "update", "delete"].includes(rawAction) ? rawAction : "update";
    const tableName = adminAuditText(item.table_name ?? item.table ?? item.entity, "");
    const entity = adminAuditText(item.table_label ?? item.entity_label ?? item.table_name ?? item.table ?? item.entity, "业务数据");
    const rawKey = item.key_label ?? item.record_key ?? item.object_no ?? item.key;
    const keyLabel = adminKeyLabel(rawKey, tableName);
    const operator = adminAuditText(item.operator ?? item.actor ?? item.changed_by, "管理员");
    const reason = adminAuditText(item.reason ?? item.description, "未提供说明", 1000);
    const timestamp = item.created_at || item.changed_at || item.timestamp;
    const timestampLabel = typeof timestamp === "string" || typeof timestamp === "number"
      ? formatDate(timestamp)
      : adminAuditText(timestamp);
    const auditId = adminAuditText(item.audit_id ?? item.id);
    return `<article class="admin-audit-item tone-${action}">
      <span>${escapeHtml(adminActionLabel(action))}</span>
      <div><b>${escapeHtml(entity)} · ${escapeHtml(keyLabel)}</b><small>${escapeHtml(operator)} · ${escapeHtml(timestampLabel)}</small><p>${escapeHtml(reason)}</p></div>
      <em>审计 #${escapeHtml(auditId)}</em>
    </article>`;
  }).join("") : '<div class="admin-directory-empty">暂无管理员变更记录</div>';
}

function renderAdminCatalogMeta() {
  const total = state.adminData.tables.reduce((sum, table) => sum + Number(table.count || 0), 0);
  const editable = state.adminData.tables.filter((table) => table.editable).length;
  document.querySelector("#adminCatalogMeta").textContent =
    `${state.adminData.tables.length} 个业务实体 · ${total} 条记录 · ${editable} 个实体允许受控维护`;
}

function renderAdminTableMeta() {
  const table = currentAdminTable();
  const panel = document.querySelector("#adminRecordsPanel");
  const createButton = document.querySelector("#createAdminRecord");
  if (!table) {
    document.querySelector("#adminTableGroup").textContent = "BUSINESS ENTITY";
    document.querySelector("#adminTableTitle").textContent = "请选择业务实体";
    document.querySelector("#adminTableDescription").textContent = "从左侧目录选择需要查看或维护的数据。";
    document.querySelector("#adminRecordCount").textContent = "0 条记录";
    createButton.classList.add("hidden");
    panel.classList.remove("is-readonly");
    return;
  }
  document.querySelector("#adminTableGroup").textContent = `${table.group || "业务数据"} · ${table.name}`;
  document.querySelector("#adminTableTitle").textContent = table.label || table.name;
  document.querySelector("#adminTableDescription").textContent = table.description || "查看中心数据库业务记录。";
  document.querySelector("#adminRecordCount").textContent = `${Number(state.adminData.total || table.count || 0)} 条记录`;
  document.querySelector("#adminDataCaption").textContent = `${table.label || table.name}数据记录`;
  createButton.classList.toggle("hidden", !(table.editable && table.allow_create));
  panel.classList.toggle("is-readonly", !table.editable);
  const siteWrap = document.querySelector("#adminSiteFilterWrap");
  siteWrap.classList.toggle("hidden", !table.site_scoped);
  if (!table.site_scoped) {
    state.adminData.site = "";
    document.querySelector("#adminSiteFilter").value = "";
  }
}

async function loadAdminCatalog({ loadRecords = true } = {}) {
  if (!canManageAdminData()) return false;
  const requestSerial = ++state.adminData.requestSerial;
  const refresh = document.querySelector("#refreshAdminCatalog");
  refresh.disabled = true;
  refresh.textContent = "正在读取…";
  if (!state.adminData.catalogLoaded) {
    document.querySelector("#adminEntityDirectory").innerHTML = '<div class="admin-directory-empty">正在加载目录…</div>';
    setAdminDataState("正在读取业务目录", "正在核对可查看与可维护范围。", "loading");
  }
  try {
    const data = await adminApi("/api/admin/data/catalog");
    if (requestSerial !== state.adminData.requestSerial || !canManageAdminData()) return false;
    state.adminData.tables = Array.isArray(data.tables) ? data.tables : [];
    state.adminData.recentAudit = Array.isArray(data.recent_audit) ? data.recent_audit : [];
    if (!state.adminData.tables.some((table) => table.name === state.adminData.tableName)) {
      state.adminData.tableName = state.adminData.tables[0]?.name || "";
      state.adminData.page = 1;
      state.adminData.query = "";
      state.adminData.site = "";
      document.querySelector("#adminRecordSearch").value = "";
      document.querySelector("#adminSiteFilter").value = "";
    }
    state.adminData.catalogLoaded = true;
    renderAdminCatalogMeta();
    renderAdminDirectory();
    renderAdminAudit();
    renderAdminTableMeta();
    if (state.adminData.tableName && loadRecords) await loadAdminRecords();
    else if (!state.adminData.tableName) {
      document.querySelector("#adminDataTableWrap").classList.add("hidden");
      document.querySelector("#adminPagination").classList.add("hidden");
      setAdminDataState("没有可用业务实体", "当前目录未返回任何数据表。", "empty");
    }
    return true;
  } catch (error) {
    if (requestSerial !== state.adminData.requestSerial || !canManageAdminData()) return false;
    state.adminData.catalogLoaded = false;
    document.querySelector("#adminEntityDirectory").innerHTML = '<div class="admin-directory-empty is-error">目录加载失败<br><button type="button" data-admin-retry="catalog">重新加载</button></div>';
    setAdminDataState("业务目录加载失败", error.message, "error", "catalog");
    return false;
  } finally {
    refresh.disabled = false;
    refresh.textContent = "刷新目录";
  }
}

function renderAdminRecords() {
  const table = currentAdminTable();
  const wrap = document.querySelector("#adminDataTableWrap");
  const pagination = document.querySelector("#adminPagination");
  if (!table || !state.adminData.records.length) {
    wrap.classList.add("hidden");
    pagination.classList.add("hidden");
    document.querySelector("#adminRecordCount").textContent = `${Number(state.adminData.total || 0)} 条记录`;
    setAdminDataState(
      state.adminData.query || state.adminData.site ? "没有匹配记录" : "当前实体暂无记录",
      state.adminData.query || state.adminData.site ? "请调整搜索词或站点范围。" : "如该实体允许新增，可从右上角创建第一条记录。",
      "empty",
    );
    return;
  }
  const fields = Array.isArray(table.fields) ? table.fields : [];
  const hasActions = Boolean(table.editable && (table.allow_update || table.allow_delete));
  document.querySelector("#adminDataTableHead").innerHTML = `<tr>${fields.map((field) => `<th scope="col">${escapeHtml(field.label || field.name)}${field.primary_key ? '<small>主键</small>' : ""}</th>`).join("")}${hasActions ? '<th scope="col" class="admin-actions-column">操作</th>' : ""}</tr>`;
  document.querySelector("#adminDataTableBody").innerHTML = state.adminData.records.map((record, index) => {
    const values = record.values || {};
    const keyLabel = adminKeyLabel(record.key_label ?? record.key, table.name);
    const cells = fields.map((field) => {
      const display = adminDisplayValue(values[field.name], field, 360);
      const booleanType = ["boolean", "checkbox"].includes(String(field.input_type || "").toLowerCase());
      return `<td data-label="${escapeHtml(field.label || field.name)}"><span class="admin-cell-value ${booleanType ? "is-boolean" : ""}" title="${escapeHtml(display)}">${escapeHtml(display)}</span></td>`;
    }).join("");
    const actions = hasActions ? `<td data-label="操作" class="admin-row-actions">
      ${table.allow_update ? `<button type="button" data-admin-edit="${index}" aria-label="编辑 ${escapeHtml(keyLabel)}">编辑</button>` : ""}
      ${table.allow_delete ? `<button class="is-danger" type="button" data-admin-delete="${index}" aria-label="删除 ${escapeHtml(keyLabel)}">删除</button>` : ""}
    </td>` : "";
    return `<tr>${cells}${actions}</tr>`;
  }).join("");
  document.querySelector("#adminRecordCount").textContent = `${Number(state.adminData.total || 0)} 条记录`;
  document.querySelector("#adminPageMeta").textContent = `第 ${state.adminData.page} / ${state.adminData.totalPages} 页 · 共 ${state.adminData.total} 条`;
  document.querySelector("#adminPreviousPage").disabled = state.adminData.page <= 1;
  document.querySelector("#adminNextPage").disabled = state.adminData.page >= state.adminData.totalPages;
  document.querySelector("#adminDataState").classList.add("hidden");
  wrap.classList.remove("hidden");
  pagination.classList.toggle("hidden", state.adminData.totalPages <= 1);
}

async function loadAdminRecords() {
  const table = currentAdminTable();
  if (!table || !canManageAdminData()) return false;
  const requestSerial = ++state.adminData.requestSerial;
  const requestedTable = table.name;
  state.adminData.loading = true;
  const panel = document.querySelector("#adminRecordsPanel");
  panel.setAttribute("aria-busy", "true");
  panel.classList.add("is-loading");
  if (!state.adminData.records.length) setAdminDataState("正在读取记录", `${table.label || table.name} · 中心数据库`, "loading");
  const params = new URLSearchParams({
    table: table.name,
    page: String(state.adminData.page),
    page_size: String(state.adminData.pageSize),
  });
  if (state.adminData.query) params.set("q", state.adminData.query);
  if (table.site_scoped && state.adminData.site) params.set("site", state.adminData.site);
  try {
    const data = await adminApi(`/api/admin/data/records?${params.toString()}`);
    if (requestSerial !== state.adminData.requestSerial || requestedTable !== state.adminData.tableName) return false;
    const receivedTable = data.table_meta || (typeof data.table === "object" ? data.table : null);
    if (receivedTable) {
      const index = state.adminData.tables.findIndex((item) => item.name === requestedTable);
      if (index >= 0) state.adminData.tables[index] = { ...state.adminData.tables[index], ...receivedTable, count: Number(data.total || 0) };
    }
    state.adminData.records = Array.isArray(data.records) ? data.records : [];
    state.adminData.total = Number(data.total || 0);
    state.adminData.page = Math.max(1, Number(data.page || state.adminData.page));
    state.adminData.pageSize = Math.max(1, Number(data.page_size || state.adminData.pageSize));
    state.adminData.totalPages = Math.max(1, Number(data.total_pages || 1));
    renderAdminDirectory();
    renderAdminTableMeta();
    renderAdminRecords();
    return true;
  } catch (error) {
    if (requestSerial !== state.adminData.requestSerial || requestedTable !== state.adminData.tableName) return false;
    state.adminData.records = [];
    document.querySelector("#adminDataTableWrap").classList.add("hidden");
    document.querySelector("#adminPagination").classList.add("hidden");
    setAdminDataState("记录加载失败", error.message, "error");
    return false;
  } finally {
    if (requestSerial === state.adminData.requestSerial) {
      state.adminData.loading = false;
      panel.setAttribute("aria-busy", "false");
      panel.classList.remove("is-loading");
    }
  }
}

async function selectAdminTable(tableName) {
  if (!state.adminData.tables.some((table) => table.name === tableName)) return;
  clearTimeout(state.adminData.searchTimer);
  state.adminData.searchTimer = null;
  state.adminData.tableName = tableName;
  state.adminData.query = "";
  state.adminData.site = "";
  state.adminData.page = 1;
  state.adminData.records = [];
  document.querySelector("#adminRecordSearch").value = "";
  document.querySelector("#adminSiteFilter").value = "";
  renderAdminDirectory();
  renderAdminTableMeta();
  await loadAdminRecords();
  const title = document.querySelector("#adminTableTitle");
  title?.focus?.({ preventScroll: true });
  if (window.matchMedia("(max-width: 900px)").matches) {
    document.querySelector("#adminRecordsPanel")?.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function adminFieldIsEditable(field, action) {
  if (action === "create" && typeof field.writable_on_create === "boolean") {
    return field.writable_on_create;
  }
  if (action === "update" && typeof field.writable_on_update === "boolean") {
    return field.writable_on_update && !field.primary_key;
  }
  return !field.read_only && !(action === "update" && field.primary_key);
}

function adminControlValue(value, field) {
  if (value == null) return "";
  if (typeof value === "object") {
    try { return String(field.input_type).toLowerCase() === "json" ? JSON.stringify(value, null, 2) : JSON.stringify(value); }
    catch { return String(value); }
  }
  const text = String(value);
  return String(field.input_type || "").toLowerCase() === "datetime-local"
    ? text.slice(0, 16)
    : text;
}

function renderAdminField(field, value, action, index) {
  const id = `admin-record-field-${index}`;
  const helpId = `${id}-help`;
  const label = field.label || field.name;
  const type = String(field.input_type || "text").toLowerCase();
  const editable = adminFieldIsEditable(field, action);
  const badges = `${field.required ? '<i class="is-required">必填</i>' : ""}${field.primary_key ? '<i>主键</i>' : ""}${!editable ? '<i>只读</i>' : ""}`;
  const help = field.help ? `<small id="${helpId}">${escapeHtml(field.help)}</small>` : "";
  const wideClass = ["textarea", "json"].includes(type) ? " is-wide" : "";
  if (!editable) {
    return `<div class="admin-record-field is-readonly${wideClass}"><span>${escapeHtml(label)}${badges}</span><output id="${id}">${escapeHtml(adminDisplayValue(value, field, 1200))}</output>${help}</div>`;
  }
  const describedBy = field.help ? ` aria-describedby="${helpId}"` : "";
  const required = field.required && !["boolean", "checkbox"].includes(type) ? " required" : "";
  const rawValue = adminControlValue(value, field);
  const options = Array.isArray(field.options) ? field.options : [];
  let control;
  if (options.length || type === "select") {
    const normalizedOptions = options.map((option) => typeof option === "object"
      ? { value: option.value ?? option.name ?? "", label: option.label ?? option.name ?? option.value ?? "" }
      : { value: option, label: option });
    if (rawValue && !normalizedOptions.some((option) => String(option.value) === rawValue)) {
      normalizedOptions.unshift({ value: rawValue, label: rawValue });
    }
    control = `<select id="${id}" name="${escapeHtml(field.name)}"${required}${describedBy}><option value="">请选择</option>${normalizedOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${String(option.value) === rawValue ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select>`;
  } else if (["boolean", "checkbox"].includes(type)) {
    control = `<span class="admin-boolean-control"><input id="${id}" name="${escapeHtml(field.name)}" type="checkbox" ${adminBooleanValue(value) ? "checked" : ""}${describedBy}><span>启用 / 是</span></span>`;
  } else if (["textarea", "json"].includes(type)) {
    control = `<textarea id="${id}" name="${escapeHtml(field.name)}" rows="${type === "json" ? 8 : 4}"${required}${describedBy}>${escapeHtml(rawValue)}</textarea>`;
  } else {
    const inputType = ["number", "date", "datetime-local", "email", "url", "password", "search", "tel"].includes(type) ? type : "text";
    control = `<input id="${id}" name="${escapeHtml(field.name)}" type="${inputType}" value="${escapeHtml(rawValue)}" autocomplete="off"${required}${describedBy}>`;
  }
  return `<label class="admin-record-field${wideClass}"><span>${escapeHtml(label)}${badges}</span>${control}${help}</label>`;
}

function openAdminRecord(action, record = null, trigger = document.activeElement) {
  const table = currentAdminTable();
  if (!table || !table.editable) return;
  if (action === "create" && !table.allow_create) return;
  if (action === "update" && !table.allow_update) return;
  const fields = Array.isArray(table.fields) ? table.fields : [];
  state.adminData.recordAction = action;
  state.adminData.record = record;
  state.adminData.proposedValues = null;
  state.adminData.differences = [];
  state.adminData.dirty = false;
  state.adminData.returnFocus = trigger;
  const keyLabel = record ? adminKeyLabel(record.key_label ?? record.key, table.name) : "新记录";
  document.querySelector("#adminRecordEyebrow").textContent = `${table.group || "BUSINESS DATA"} · ${table.name}`;
  document.querySelector("#adminRecordTitle").textContent = action === "create" ? `新增${table.label || table.name}` : `编辑${table.label || table.name}`;
  document.querySelector("#adminRecordDescription").textContent = action === "create"
    ? "填写业务字段后进入差异审查；当前步骤不会写入数据库。"
    : `${keyLabel} · 版本校验将在最终写入时执行。`;
  document.querySelector("#reviewAdminRecord").textContent = action === "create" ? "审查新增内容" : "审查字段差异";
  document.querySelector("#adminRecordFields").innerHTML = fields.map((field, index) =>
    renderAdminField(
      field,
      action === "create" && record?.values?.[field.name] === undefined
        ? field.default_value
        : record?.values?.[field.name],
      action,
      index,
    )
  ).join("") || '<div class="admin-directory-empty">该实体没有可展示字段</div>';
  const dialog = document.querySelector("#adminRecordDialog");
  dialog.showModal();
  setTimeout(() => dialog.querySelector("input:not([readonly]),select,textarea")?.focus(), 60);
}

function closeAdminRecord({ force = false, restoreFocus = true } = {}) {
  const dialog = document.querySelector("#adminRecordDialog");
  if (!force && state.adminData.dirty) {
    const discard = window.confirm("当前业务记录有未保存的修改，确定放弃吗？");
    if (!discard) return false;
  }
  const reviewDialog = document.querySelector("#adminReviewDialog");
  if (reviewDialog.open) reviewDialog.close();
  if (dialog.open) dialog.close();
  state.adminData.dirty = false;
  state.adminData.recordAction = "";
  state.adminData.record = null;
  state.adminData.proposedValues = null;
  state.adminData.differences = [];
  const returnFocus = state.adminData.returnFocus;
  state.adminData.returnFocus = null;
  if (restoreFocus && returnFocus?.isConnected) setTimeout(() => returnFocus.focus(), 20);
  return true;
}

function collectAdminRecordValues() {
  const table = currentAdminTable();
  const form = document.querySelector("#adminRecordForm");
  const action = state.adminData.recordAction;
  const values = {};
  (table?.fields || []).forEach((field) => {
    if (!adminFieldIsEditable(field, action)) return;
    const control = form.elements.namedItem(field.name);
    if (!control) return;
    const type = String(field.input_type || "text").toLowerCase();
    if (["boolean", "checkbox"].includes(type)) values[field.name] = Boolean(control.checked);
    else if (type === "number") values[field.name] = control.value === "" ? "" : Number(control.value);
    else {
      if (type === "json") {
        control.setCustomValidity("");
        if (control.value.trim()) {
          try { JSON.parse(control.value); }
          catch { control.setCustomValidity("请输入有效的 JSON 数据"); }
        }
      }
      values[field.name] = control.value;
    }
  });
  return values;
}

function buildAdminDifferences(values) {
  const table = currentAdminTable();
  const action = state.adminData.recordAction;
  return (table?.fields || []).filter((field) => Object.hasOwn(values, field.name)).flatMap((field) => {
    const before = action === "create" ? undefined : state.adminData.record?.values?.[field.name];
    const after = values[field.name];
    if (action === "update" && adminValuesEqual(before, after, field)) return [];
    return [{ field, before, after }];
  });
}

function renderAdminReview(action) {
  const table = currentAdminTable();
  const record = state.adminData.record;
  const deleting = action === "delete";
  const keyLabel = record ? adminKeyLabel(record.key_label ?? record.key, table?.name) : "新记录";
  document.querySelector("#adminReviewMark").textContent = deleting ? "!" : "Δ";
  document.querySelector("#adminReviewEyebrow").textContent = deleting ? "HARD DELETE · VERSION CHECK" : "FIELD DIFF · AUDITED WRITE";
  document.querySelector("#adminReviewTitle").textContent = deleting ? "确认删除业务记录" : `${adminActionLabel(action)}前差异审查`;
  document.querySelector("#adminReviewDescription").textContent = deleting
    ? "删除会校验当前版本；请确认记录标识并说明删除依据。"
    : "请逐项核对修改前后内容，并填写可追溯的变更原因。";
  document.querySelector("#adminReviewSummary").innerHTML = `<span>${escapeHtml(table?.group || "业务数据")}</span><b>${escapeHtml(table?.label || table?.name || "业务实体")}</b><em>${escapeHtml(keyLabel)}</em>`;
  const diffTarget = document.querySelector("#adminReviewDiff");
  diffTarget.innerHTML = deleting ? `<div class="admin-delete-impact" role="listitem"><span>DELETE</span><div><b>将删除 ${escapeHtml(keyLabel)}</b><p>删除后无法在当前页面直接恢复，请确认该记录不再被业务链路引用。</p></div></div>` : state.adminData.differences.map(({ field, before, after }) => `
    <div class="admin-diff-row" role="listitem">
      <b>${escapeHtml(field.label || field.name)}</b>
      <div><small>修改前</small><span>${escapeHtml(adminDisplayValue(before, field, 1000))}</span></div>
      <i aria-hidden="true">→</i>
      <div><small>修改后</small><span>${escapeHtml(adminDisplayValue(after, field, 1000))}</span></div>
    </div>
  `).join("");
  const deleteWrap = document.querySelector("#adminDeleteConfirmWrap");
  const deleteInput = document.querySelector("#adminDeleteConfirm");
  deleteWrap.classList.toggle("hidden", !deleting);
  deleteInput.required = deleting;
  deleteInput.value = "";
  document.querySelector("#adminDeleteConfirmHint").textContent = deleting ? `请输入“${keyLabel}”以确认目标记录` : "";
  const reason = document.querySelector("#adminChangeReason");
  reason.value = "";
  document.querySelector("#adminReasonCount").textContent = "0 / 200";
  document.querySelector("#adminReviewError").classList.add("hidden");
  const confirm = document.querySelector("#confirmAdminChange");
  confirm.className = deleting ? "danger-button" : "primary-button";
  confirm.textContent = deleting ? "确认删除并留痕" : "确认并写入数据库";
  document.querySelector("#cancelAdminReview").textContent = deleting ? "取消删除" : "返回修改";
  const dialog = document.querySelector("#adminReviewDialog");
  dialog.classList.toggle("is-delete", deleting);
  dialog.showModal();
  setTimeout(() => (deleting ? deleteInput : reason).focus(), 60);
}

function reviewAdminRecordChanges(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = collectAdminRecordValues();
  if (!form.reportValidity()) return;
  const differences = buildAdminDifferences(values);
  if (state.adminData.recordAction === "update" && !differences.length) {
    toast("没有需要保存的字段变化", "请先修改至少一个可编辑字段");
    return;
  }
  state.adminData.proposedValues = values;
  state.adminData.differences = differences;
  renderAdminReview(state.adminData.recordAction);
}

function openAdminDelete(record, trigger = document.activeElement) {
  const table = currentAdminTable();
  if (!table?.editable || !table.allow_delete) return;
  state.adminData.recordAction = "delete";
  state.adminData.record = record;
  state.adminData.proposedValues = null;
  state.adminData.differences = [];
  state.adminData.returnFocus = trigger;
  renderAdminReview("delete");
}

function closeAdminReview() {
  const action = state.adminData.recordAction;
  const dialog = document.querySelector("#adminReviewDialog");
  if (dialog.open) dialog.close();
  if (action === "delete") {
    const returnFocus = state.adminData.returnFocus;
    state.adminData.recordAction = "";
    state.adminData.record = null;
    state.adminData.returnFocus = null;
    if (returnFocus?.isConnected) setTimeout(() => returnFocus.focus(), 20);
  } else {
    setTimeout(() => document.querySelector("#adminRecordDialog input:not([readonly]), #adminRecordDialog select, #adminRecordDialog textarea")?.focus(), 20);
  }
}

async function submitAdminChange(event) {
  event.preventDefault();
  const action = state.adminData.recordAction;
  const table = currentAdminTable();
  const record = state.adminData.record;
  const reason = document.querySelector("#adminChangeReason").value.trim();
  const errorTarget = document.querySelector("#adminReviewError");
  errorTarget.classList.add("hidden");
  if (reason.length < 4 || reason.length > 200) {
    errorTarget.textContent = "变更原因必须为 4–200 个字符。";
    errorTarget.classList.remove("hidden");
    document.querySelector("#adminChangeReason").focus();
    return;
  }
  const payload = { action, table: table?.name, reason };
  if (["create", "update"].includes(action)) payload.values = state.adminData.proposedValues;
  if (["update", "delete"].includes(action)) {
    payload.key = record?.key;
    payload.version = record?.version;
  }
  if (action === "delete") {
    const expected = adminKeyLabel(record?.key_label ?? record?.key, table?.name);
    const confirmation = document.querySelector("#adminDeleteConfirm").value.trim();
    if (confirmation !== expected) {
      errorTarget.textContent = `记录标识不匹配，请完整输入“${expected}”。`;
      errorTarget.classList.remove("hidden");
      document.querySelector("#adminDeleteConfirm").focus();
      return;
    }
    payload.confirm_key = confirmation;
  }
  const button = document.querySelector("#confirmAdminChange");
  const oldLabel = button.textContent;
  button.disabled = true;
  button.textContent = "正在校验并写入…";
  try {
    const result = await adminApi("/api/admin/data/records", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.adminData.dirty = false;
    const reviewDialog = document.querySelector("#adminReviewDialog");
    const recordDialog = document.querySelector("#adminRecordDialog");
    if (reviewDialog.open) reviewDialog.close();
    if (recordDialog.open) recordDialog.close();
    state.adminData.recordAction = "";
    state.adminData.record = null;
    state.adminData.proposedValues = null;
    state.adminData.differences = [];
    state.adminData.returnFocus = null;
    toast(`${adminActionLabel(action)}已写入`, `${adminKeyLabel(result.key_label ?? result.key, table?.name)} · 审计 #${adminAuditText(result.audit_id)}`);
    await loadAdminCatalog({ loadRecords: false });
    await loadAdminRecords();
    document.querySelector("#adminTableTitle")?.focus?.({ preventScroll: true });
  } catch (error) {
    errorTarget.textContent = error.status === 409
      ? `数据版本已变化，请关闭窗口并刷新后重试。${error.message ? ` ${error.message}` : ""}`
      : error.message;
    errorTarget.classList.remove("hidden");
    errorTarget.focus?.();
  } finally {
    button.disabled = false;
    button.textContent = oldLabel;
  }
}

function wireAdminDataEvents() {
  document.querySelector("#refreshAdminCatalog").addEventListener("click", () => loadAdminCatalog());
  document.querySelector("#adminEntitySearch").addEventListener("input", (event) => {
    state.adminData.directoryQuery = event.currentTarget.value;
    renderAdminDirectory();
  });
  document.querySelector("#adminEntityDirectory").addEventListener("click", (event) => {
    const button = event.target.closest("[data-admin-table]");
    if (button) selectAdminTable(button.dataset.adminTable);
  });
  document.querySelector("#adminRecordSearch").addEventListener("input", (event) => {
    clearTimeout(state.adminData.searchTimer);
    state.adminData.query = event.currentTarget.value.trim();
    state.adminData.page = 1;
    state.adminData.searchTimer = setTimeout(loadAdminRecords, 360);
  });
  document.querySelector("#adminSiteFilter").addEventListener("change", (event) => {
    state.adminData.site = event.currentTarget.value;
    state.adminData.page = 1;
    loadAdminRecords();
  });
  document.querySelector("#adminPageSize").addEventListener("change", (event) => {
    state.adminData.pageSize = Number(event.currentTarget.value);
    state.adminData.page = 1;
    loadAdminRecords();
  });
  document.querySelector("#refreshAdminRecords").addEventListener("click", loadAdminRecords);
  document.querySelector("#adminPreviousPage").addEventListener("click", () => {
    if (state.adminData.page <= 1 || state.adminData.loading) return;
    state.adminData.page -= 1;
    loadAdminRecords();
  });
  document.querySelector("#adminNextPage").addEventListener("click", () => {
    if (state.adminData.page >= state.adminData.totalPages || state.adminData.loading) return;
    state.adminData.page += 1;
    loadAdminRecords();
  });
  document.querySelector("#createAdminRecord").addEventListener("click", (event) => openAdminRecord("create", null, event.currentTarget));
  document.querySelector("#adminDataTableBody").addEventListener("click", (event) => {
    const edit = event.target.closest("[data-admin-edit]");
    if (edit) return openAdminRecord("update", state.adminData.records[Number(edit.dataset.adminEdit)], edit);
    const remove = event.target.closest("[data-admin-delete]");
    if (remove) openAdminDelete(state.adminData.records[Number(remove.dataset.adminDelete)], remove);
  });
  document.querySelector("#adminDataState").addEventListener("click", (event) => {
    const retry = event.target.closest("[data-admin-retry]");
    if (!retry) return;
    if (retry.dataset.adminRetry === "catalog") loadAdminCatalog();
    else loadAdminRecords();
  });
  document.querySelector("#adminEntityDirectory").addEventListener("click", (event) => {
    const retry = event.target.closest("[data-admin-retry='catalog']");
    if (retry) loadAdminCatalog();
  });
  const recordForm = document.querySelector("#adminRecordForm");
  recordForm.addEventListener("input", () => { state.adminData.dirty = true; });
  recordForm.addEventListener("change", () => { state.adminData.dirty = true; });
  recordForm.addEventListener("submit", reviewAdminRecordChanges);
  document.querySelector("#closeAdminRecord").addEventListener("click", () => closeAdminRecord());
  document.querySelector("#cancelAdminRecord").addEventListener("click", () => closeAdminRecord());
  document.querySelector("#adminRecordDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAdminRecord();
  });
  document.querySelector("#adminRecordDialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeAdminRecord();
  });
  document.querySelector("#adminChangeReason").addEventListener("input", (event) => {
    document.querySelector("#adminReasonCount").textContent = `${event.currentTarget.value.length} / 200`;
    document.querySelector("#adminReviewError").classList.add("hidden");
  });
  document.querySelector("#adminDeleteConfirm").addEventListener("input", () => document.querySelector("#adminReviewError").classList.add("hidden"));
  document.querySelector("#cancelAdminReview").addEventListener("click", closeAdminReview);
  document.querySelector("#adminReviewForm").addEventListener("submit", submitAdminChange);
  document.querySelector("#adminReviewDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAdminReview();
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.adminData.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function runTrace(partCode) {
  const target = document.querySelector("#traceResults");
  target.innerHTML = `<div class="blank-slate"><span>···</span><h3>正在查询中心数据库</h3></div>`;
  try {
    const data = await api(`/api/trace?part_code=${encodeURIComponent(partCode)}`);
    target.innerHTML = renderTraceCard(data.item);
  } catch (error) {
    target.innerHTML = `<div class="blank-slate"><span>∅</span><h3>未找到零件</h3><p>${escapeHtml(error.message)}</p></div>`;
    throw error;
  }
}

function renderTraceCard(part) {
  const source = part.source_type === "PRODUCTION" ? "生产订单零件" : "采购订单零件";
  const batches = (part.batches || []).map((batch) => `
    <div class="timeline-item"><header><h4>现场确认绑定</h4>
    <time>${formatDate(batch.confirmed_at)}</time></header>
    <p>${escapeHtml(batch.batch_no)} · ${escapeHtml(batch.site_code)} · ${escapeHtml(batch.operator)}</p></div>
  `).join("");
  return `<article class="trace-result-card">
    <header class="result-head"><div><p>${source}</p><h3>${escapeHtml(part.display_code)}</h3></div>
    <span class="status-pill">${escapeHtml(statusLabels[part.status] || part.status)}</span></header>
    <div class="result-meta trace-chain-grid">
      <div class="meta-item"><span>零件</span><strong>${escapeHtml(part.part_code || "清单未填写零件编码")}<br>${escapeHtml(part.display_code)}</strong></div>
      <div class="meta-item"><span>装配订单</span><strong>${escapeHtml(part.component_order_no || "尚未绑定")}<br>${escapeHtml(part.component_name || "等待现场关联")}</strong></div>
      <div class="meta-item"><span>工序号</span><strong>${escapeHtml(part.process_no || "允许缺失 / 尚未绑定")}<br>${escapeHtml(part.component_code || "—")}</strong></div>
      <div class="meta-item"><span>WBS</span><strong>${escapeHtml(part.wbs || "尚未绑定")}</strong></div>
    </div>
    <div class="chain-banner"><span>SRM实物唯一键</span><i>→</i><span>零件号 ${escapeHtml(part.part_code)}</span><i>→</i><span>${part.component_order_no ? `装配订单 ${escapeHtml(part.component_order_no)}` : "等待现场绑定"}</span></div>
    <div class="timeline">${batches || '<div class="empty-state">该SRM实物尚未完成现场绑定</div>'}</div>
  </article>`;
}

function batchUi(kind) {
  return {
    list: document.querySelector(`#${kind}BatchList`),
    count: document.querySelector(`#${kind}BatchCount`),
    confirm: document.querySelector(`#confirm${kind[0].toUpperCase()}${kind.slice(1)}Batch`),
    orderInput: document.querySelector(`#${kind}OrderInput`),
    partInput: document.querySelector(`#${kind}PartInput`),
  };
}

function renderBatch(kind) {
  if (kind === "bind") return renderBindSession();
  const items = state.batches[kind];
  const ui = batchUi(kind);
  ui.count.textContent = items.length;
  ui.confirm.disabled = !items.length;
  ui.confirm.querySelector("span").textContent = `${items.length} 件`;
  ui.list.className = items.length ? "batch-items" : "batch-items empty-state";
  ui.list.innerHTML = items.length ? items.map(({ part }, index) => `
    <div class="batch-item">
      <span>${String(index + 1).padStart(2, "0")}</span>
      <div><b>${escapeHtml(part.display_code)}</b><small>${escapeHtml(part.part_code || "零件编码未填写")} · 工序 ${escapeHtml(part.process_no || "—")}</small></div>
      <em>${escapeHtml(part.component_name)}</em>
      <button type="button" data-remove-batch="${kind}" data-index="${index}" aria-label="移除">×</button>
    </div>
  `).join("") : "尚未扫描零件";
}

function bindRemainingRequirements() {
  if (!state.bindSession) return [];
  const stagedIds = new Set(state.batches.bind.map((item) => Number(item.required_part.id)));
  return state.bindSession.requirements.filter((part) => !stagedIds.has(Number(part.id)));
}

function groupBindRequirements(requirements) {
  const grouped = new Map();
  requirements.forEach((part) => {
    const key = `${part.process_no || ""}\u0000${part.part_code || ""}`;
    const current = grouped.get(key);
    if (current) current.quantity += 1;
    else grouped.set(key, { ...part, quantity: 1 });
  });
  return [...grouped.values()];
}

function renderBindSession() {
  const session = state.bindSession;
  const workspace = document.querySelector("#bindSessionWorkspace");
  workspace.classList.toggle("hidden", !session);
  if (!session) {
    document.querySelector("#bindOrderInput").disabled = false;
    document.querySelector("#startBindOrder").disabled = false;
    document.querySelector("#bindRequirementList").innerHTML = "开始绑定后自动载入清单";
    document.querySelector("#bindVerifiedList").innerHTML = "尚未核验零件";
    document.querySelector("#confirmBindBatch").disabled = true;
    return;
  }
  document.querySelector("#bindOrderInput").disabled = true;
  document.querySelector("#startBindOrder").disabled = true;
  document.querySelector("#activeBindOrder").textContent = session.order.order_no;
  document.querySelector("#activeBindMeta").textContent =
    `${session.order.component_name} · 部件号 ${session.order.component_code} · ${session.order.wbs}`;
  const items = state.batches.bind;
  const remaining = bindRemainingRequirements();
  const groupedRemaining = groupBindRequirements(remaining);
  const completed = session.fulfilled_count + items.length;
  document.querySelector("#bindProgressText").textContent = `${completed} / ${session.total_count}`;
  document.querySelector("#bindProgress").max = Math.max(1, session.total_count);
  document.querySelector("#bindProgress").value = completed;
  document.querySelector("#bindBatchCount").textContent = items.length;
  document.querySelector("#bindRemainingCount").textContent = remaining.length;

  const requiredTarget = document.querySelector("#bindRequirementList");
  requiredTarget.className = remaining.length ? "requirement-list" : "requirement-list complete";
  requiredTarget.innerHTML = remaining.length ? groupedRemaining.map((part, index) => `
    <div class="requirement-row">
      <span>${String(index + 1).padStart(2, "0")}</span>
      <div><b>${escapeHtml(part.part_code)}</b><small>${escapeHtml(part.process_key || `工序 ${part.process_no || "允许缺失"}`)}</small></div>
      <i class="requirement-status planned">待扫码 ×${part.quantity}</i>
    </div>
  `).join("") : `<div class="list-complete-mark"><span>✓</span><h4>应绑清单已全部核验</h4><p>现在可以提交绑定并放行装配。</p></div>`;

  const verifiedTarget = document.querySelector("#bindVerifiedList");
  verifiedTarget.className = items.length ? "verified-parts" : "verified-parts empty-state";
  verifiedTarget.innerHTML = items.length ? items.slice().reverse().map((item, reverseIndex) => {
    const index = items.length - reverseIndex - 1;
    return `<div class="verified-part ${item.requires_confirmation ? "is-override" : ""}">
      <span>✓</span><div><b>${escapeHtml(item.part.display_code)}</b><small>消减：${escapeHtml(item.required_part.part_code || item.required_part.display_code)}</small></div>
      <i>${({SCANNER:"扫码枪",CAMERA:"摄像头",MANUAL:"手工"})[item.input_method] || "已识别"} · 已保存</i>
    </div>`;
  }).join("") : "尚未核验零件";

  const confirm = document.querySelector("#confirmBindBatch");
  confirm.disabled = !items.length;
  confirm.firstChild.textContent = remaining.length ? "结束本轮扫码 " : "全部完成，可开始装配 ";
  confirm.querySelector("span").textContent = `已保存 ${items.length} 件`;
}

function showBindDecision({ title, message, confirmLabel = "确认绑定", confirmOnly = false, warning = false, hardStop = false, eyebrow = "" }) {
  const dialog = document.querySelector("#bindDecisionDialog");
  document.querySelector("#bindDecisionTitle").textContent = title;
  document.querySelector("#bindDecisionMessage").textContent = message;
  document.querySelector("#confirmBindDecision").textContent = confirmLabel;
  document.querySelector("#cancelBindDecision").classList.toggle("hidden", confirmOnly);
  document.querySelector("#bindDecisionMark").textContent = warning ? "!" : "✓";
  document.querySelector("#bindDecisionEyebrow").textContent =
    eyebrow || (warning ? "TRACE DIFFERENCE · MANUAL CONFIRM" : "COMPLETENESS CHECK");
  dialog.classList.toggle("is-warning", warning);
  dialog.classList.toggle("is-hard-stop", hardStop);
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

async function showDuplicateBindingAlert(message) {
  await showBindDecision({
    title: "重复绑定已拒绝",
    message,
    confirmLabel: "我已知晓",
    confirmOnly: true,
    warning: true,
    hardStop: true,
    eyebrow: "HARD STOP · DUPLICATE PART",
  });
  document.querySelector("#bindPartInput")?.focus();
}

async function showUnbindPermissionAlert() {
  await showBindDecision({
    title: "没有解除绑定权限",
    message: "当前追溯角色只能查看绑定记录。请联系总厂系统管理员或本站现场负责人处理解绑。",
    confirmLabel: "我已知晓",
    confirmOnly: true,
    warning: true,
    hardStop: true,
    eyebrow: "ACCESS DENIED · UNBIND",
  });
}

async function batchUnbindSelected() {
  const bindingIds = [...state.selectedUnbindIds];
  if (!bindingIds.length) return;
  const confirmed = await showBindDecision({
    title: `确认解除 ${bindingIds.length} 件零件？`,
    message: "本次选择会在同一事务中处理：任意一件已被他人解除或不属于当前站点，整批都会取消，不会出现部分成功。",
    confirmLabel: `确认批量解绑 ${bindingIds.length} 件`,
    warning: true,
    eyebrow: "AUDITED ACTION · BATCH UNBIND",
  });
  if (!confirmed) return;
  const button = document.querySelector("#batchUnbindSelected");
  button.disabled = true;
  try {
    const result = await api("/api/bindings/unbind-batch", {
      method: "POST",
      body: JSON.stringify({ binding_ids: bindingIds, reason: "运行总览批量解绑" }),
    });
    state.selectedUnbindIds.clear();
    await loadBootstrap();
    renderStatusDetailList();
    document.querySelector("#statusDetailMeta").textContent =
      `${state.bootstrap.site.name} · ${statusDetailParts().length} 个装配订单`;
    toast("批量解除绑定完成", `已解除 ${result.count} 件零件，审计记录已逐件保存`);
  } catch (error) {
    if (error.status === 403) await showUnbindPermissionAlert();
    else toast("批量解除绑定失败", error.message, "error");
    renderStatusDetailList();
  }
}

async function unbindDetailBinding(button) {
  const bindingId = button.dataset.unbindBinding;
  const displayCode = button.dataset.unbindCode;
  const orderNo = button.dataset.unbindOrder;
  const confirmed = await showBindDecision({
    title: "确认解除这件零件？",
    message: `零件 ${displayCode} 将从装配订单 ${orderNo} 解除。解除后，该零件可以绑定到其他符合零件号要求的装配订单。`,
    confirmLabel: "确认解除绑定",
    warning: true,
    eyebrow: "AUDITED ACTION · UNBIND PART",
  });
  if (!confirmed) return;
  button.disabled = true;
  try {
    const result = await api(`/api/bindings/${encodeURIComponent(bindingId)}/unbind`, {
      method: "POST",
      body: JSON.stringify({ reason: "运行总览逐条解绑" }),
    });
    state.selectedUnbindIds.delete(Number(bindingId));
    await loadBootstrap();
    renderStatusDetailList();
    document.querySelector("#statusDetailMeta").textContent =
      `${state.bootstrap.site.name} · ${statusDetailParts().length} 个装配订单`;
    toast("零件已解除绑定", `${result.display_code} · 原订单 ${result.previous_component_order_no}`);
  } catch (error) {
    if (error.status === 403) await showUnbindPermissionAlert();
    else toast("解除绑定失败", error.message, "error");
    button.disabled = false;
  }
}

async function startBindOrder(form) {
  ensureOperationSite();
  const orderNo = formObject(form).component_order_no;
  const data = await api("/api/bind-orders/prepare", {
    method: "POST",
    body: JSON.stringify({ component_order_no: orderNo }),
  });
  state.batches.bind = [];
  state.bindSession = data;
  document.querySelector("#bindOrderInput").value = data.order.order_no;
  renderBindSession();
  if (data.remaining_count) {
    document.querySelector("#bindPartInput").focus();
    toast("装配订单已锁定", `载入${data.remaining_count}件待绑定质量追溯零件`);
  } else {
    toast("该订单已完成绑定", "追溯清单中没有待绑定零件");
  }
}

function clientMetadata() {
  return {
    operating_system: navigator.userAgentData?.platform || navigator.platform || "未知",
    platform: navigator.platform || "",
    language: navigator.language || "",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    screen: `${screen.width}x${screen.height}`,
    touch_points: navigator.maxTouchPoints || 0,
  };
}

async function stageBindItem(form) {
  ensureOperationSite();
  if (!state.bindSession) throw new Error("请先输入装配订单号并点击开始绑定");
  const rawCode = formObject(form).part_code;
  const inputMethod = form.dataset.inputMethod || state.scanTyping.pendingMethod || "MANUAL";
  const scannedAt = new Date().toISOString();
  const items = state.batches.bind;
  const result = await api("/api/operations/validate", {
    method: "POST",
    body: JSON.stringify({
      operation_type: "BIND",
      component_order_no: state.bindSession.order.order_no,
      part_code: rawCode,
      reserved_required_ids: items.map((item) => item.required_part.id),
    }),
  });
  if (items.some((item) => item.part.id === result.part.id)) {
    throw new Error("该订单+序列号已在本轮完成核验");
  }
  let overrideConfirmed = false;
  if (result.requires_confirmation) {
    overrideConfirmed = await showBindDecision({
      title: "现场信息与追溯清单不一致",
      message: `${result.override_reason} 是否确认使用该零件完成本次绑定？`,
      confirmLabel: "确认本次绑定",
      warning: true,
    });
    if (!overrideConfirmed) return;
  }
  const stagedItem = {
    ...result,
    rawCode,
    override_confirmed: overrideConfirmed,
    input_method: inputMethod,
    scanned_at: scannedAt,
  };
  const saved = await api("/api/bindings/scan", {
    method: "POST",
    body: JSON.stringify({
      component_order_no: state.bindSession.order.order_no,
      items: [{
        part_code: rawCode,
        override_confirmed: overrideConfirmed,
        input_method: inputMethod,
        scanned_at: scannedAt,
      }],
      client_metadata: clientMetadata(),
    }),
  });
  items.push({ ...stagedItem, batch_no: saved.batch_no });
  document.querySelector("#lastScanFeedback").textContent =
    `${result.part.part_code} · 已自动保存`;
  form.dataset.inputMethod = "";
  state.scanTyping.pendingMethod = "MANUAL";
  form.reset();
  renderBindSession();
  document.querySelector("#bindPartInput").focus();
  navigator.vibrate?.(45);
  return result;
}

function resetBindSession() {
  state.batches.bind = [];
  state.bindSession = null;
  document.querySelector("#bindOrderForm").reset();
  document.querySelector("#bindStageForm").reset();
  renderBindSession();
  document.querySelector("#bindOrderInput").focus();
}

async function confirmBindSession() {
  ensureOperationSite();
  if (!state.bindSession) throw new Error("请先开始一个装配订单");
  const items = state.batches.bind;
  if (!items.length) throw new Error("本轮没有新增绑定零件");
  const remaining = bindRemainingRequirements();
  if (remaining.length) {
    const finishPartial = await showBindDecision({
      title: "结束本轮扫码？",
      message: `本轮已自动保存 ${items.length} 件，仍有 ${remaining.length} 件未绑定。结束后可稍后继续扫码。`,
      confirmLabel: "确认结束本轮",
      warning: true,
      eyebrow: "PARTIAL BINDING · SAVED",
    });
    if (!finishPartial) {
      document.querySelector("#bindPartInput").focus();
      return;
    }
  }
  const button = document.querySelector("#confirmBindBatch");
  button.disabled = true;
  const savedCount = items.length;
  const completed = remaining.length === 0;
  resetBindSession();
  await loadBootstrap();
  toast(
    completed ? "该装配订单已全部绑定，可开始装配" : "本轮扫码已结束",
    `${savedCount} 件零件均已自动保存`,
  );
}

function ensureOperationSite() {
  if (!canAccess("bind")) throw new Error("当前账号没有现场绑定权限");
  if (!["XC", "JC"].includes(state.selectedSite)) {
    throw new Error("管理员执行绑定前，请先在右上角数据视图选择新场或锦晨");
  }
}

async function stageBatchItem(kind, form) {
  ensureOperationSite();
  const values = formObject(form);
  const items = state.batches[kind];
  if (items.length && items[0].order.order_no !== values.component_order_no.trim()) {
    throw new Error(`当前批次已锁定订单${items[0].order.order_no}；请确认本批或清空后切换订单`);
  }
  const result = await api("/api/operations/validate", {
    method: "POST",
    body: JSON.stringify({
      operation_type: kind === "issue" ? "ISSUE" : "BIND",
      component_order_no: values.component_order_no,
      part_code: values.part_code,
    }),
  });
  if (items.some((item) => item.part.id === result.part.id)) throw new Error("该零件已在当前批次中");
  items.push({ ...result, rawCode: values.part_code });
  batchUi(kind).orderInput.value = result.order.order_no;
  batchUi(kind).partInput.value = "";
  renderBatch(kind);
  batchUi(kind).partInput.focus();
  navigator.vibrate?.(45);
}

async function confirmBatch(kind) {
  ensureOperationSite();
  const items = state.batches[kind];
  if (!items.length) throw new Error("请至少扫描一个零件");
  const ui = batchUi(kind);
  const old = ui.confirm.innerHTML;
  ui.confirm.disabled = true;
  ui.confirm.innerHTML = `正在确认并写入… <span>${items.length} 件</span>`;
  try {
    const result = await api(kind === "issue" ? "/api/issue-batches" : "/api/bind-batches", {
      method: "POST",
      body: JSON.stringify({
        component_order_no: items[0].order.order_no,
        items: items.map((item) => ({ part_code: item.rawCode })),
      }),
    });
    state.batches[kind] = [];
    ui.orderInput.value = "";
    ui.partInput.value = "";
    renderBatch(kind);
    await loadBootstrap();
    toast(
      kind === "issue" ? "仓库发放已确认" : "现场绑定完成，可开始装配",
      `${result.batch_no} · ${result.item_count} 件`,
    );
  } finally {
    ui.confirm.innerHTML = old;
    ui.confirm.disabled = !state.batches[kind].length;
    ui.confirm.querySelector("span").textContent = `${state.batches[kind].length} 件`;
  }
}

async function loadExchangeLogs() {
  try {
    const data = await api("/api/exchange/logs");
    document.querySelector("#unexportedCount").textContent = `${data.unexported_events} 条待导出`;
    const combined = [
      ...data.imports.map((item) => ({ ...item, direction: "导入", at: item.imported_at })),
      ...data.exports.map((item) => ({ ...item, direction: "导出", at: item.exported_at })),
    ].sort((a, b) => String(b.at).localeCompare(String(a.at))).slice(0, 30);
    const target = document.querySelector("#exchangeLogs");
    target.className = combined.length ? "log-list" : "log-list empty-state";
    target.innerHTML = combined.length ? combined.map((item) => `
      <div class="log-item"><b>${item.direction}</b><span>${escapeHtml(item.package_id)} · ${escapeHtml(item.package_type)} · ${item.record_count} 条</span><time>${formatDate(item.at)}</time></div>
    `).join("") : "暂无交换记录";
  } catch (error) {
    toast("交换记录加载失败", error.message, "error");
  }
}

function renderStorageStatus(data) {
  state.storage = data;
  const panel = document.querySelector("#storagePanel");
  const alert = document.querySelector("#capacityAlert");
  const isHqAdmin = state.user?.role === "ADMIN" && state.user?.site_code === "HQ";
  panel.classList.toggle("hidden", !isHqAdmin);
  if (!isHqAdmin) {
    alert.classList.add("hidden");
    return;
  }
  const levelLabels = {
    NORMAL: "容量正常",
    NOTICE: "容量提醒",
    WARNING: "容量警告",
    CRITICAL: "容量严重不足",
  };
  const years = Number(data.estimated_days || 0) / 365;
  const runway = data.estimated_days >= 730
    ? `约 ${years.toFixed(1)} 年`
    : `约 ${Math.round(data.estimated_days || 0)} 天`;
  panel.dataset.level = data.level;
  document.querySelector("#storageLevel").textContent = levelLabels[data.level] || data.level;
  document.querySelector("#storageSummary").textContent =
    `当前已用 ${formatBytes(data.used_bytes)}，按 ${formatBytes(data.daily_growth_bytes)}/天测算`;
  document.querySelector("#storageUsedPercent").textContent = `${Number(data.used_percent).toFixed(1)}%`;
  document.querySelector("#storageProgress").value = Number(data.used_percent || 0);
  document.querySelector("#storageFree").textContent = formatBytes(data.free_bytes);
  document.querySelector("#storageTotal").textContent = `总容量 ${formatBytes(data.total_bytes)}`;
  document.querySelector("#storageRunway").textContent = runway;
  document.querySelector("#storageForecastBasis").textContent =
    data.forecast_basis === "MEASURED"
      ? `依据 ${data.measurement_days} 天服务器实际增长`
      : "首日按保守值 20 MiB/天，运行24小时后转为实测";
  document.querySelector("#storageDatabase").textContent = formatBytes(data.database_bytes);
  document.querySelector("#storagePlatform").textContent = `平台文件 ${formatBytes(data.platform_bytes)}`;
  document.querySelector("#storageBackupMeta").textContent = data.last_full_backup_download_at
    ? `最近完整备份：${formatDate(data.last_full_backup_download_at)}`
    : "尚未下载完整备份";
  document.querySelector("#storageCleanupMeta").textContent = data.cleanup_allowed
    ? `备份已确认，本次安全清理窗口开放至下载后24小时`
    : data.last_cache_cleanup_at
      ? `最近清理：${formatDate(data.last_cache_cleanup_at)}；再次清理前请重新下载备份`
      : "下载完成后，24小时内允许执行安全缓存清理";
  document.querySelector("#clearServerCache").disabled = !data.cleanup_allowed;
  alert.classList.toggle("hidden", data.level === "NORMAL");
  document.querySelector("#capacityAlertText").textContent =
    `${levelLabels[data.level]}：已用 ${data.used_percent}%，可用 ${formatBytes(data.free_bytes)}，预计可运行 ${runway.replace("约 ", "")}`;
}

async function loadStorageStatus({ silent = false } = {}) {
  const isHqAdmin = state.user?.role === "ADMIN" && state.user?.site_code === "HQ";
  if (!isHqAdmin) {
    document.querySelector("#storagePanel")?.classList.add("hidden");
    document.querySelector("#capacityAlert")?.classList.add("hidden");
    return null;
  }
  try {
    const data = await api("/api/admin/storage");
    renderStorageStatus(data);
    return data;
  } catch (error) {
    if (!silent) toast("容量测算失败", error.message, "error");
    return null;
  }
}

async function saveBackupBlob(blob, filename) {
  if (window.showSaveFilePicker) {
    const handle = await window.showSaveFilePicker({
      suggestedName: filename,
      types: [{ description: "SQLite 数据库备份", accept: { "application/x-sqlite3": [".db"] } }],
    });
    const writable = await handle.createWritable();
    await writable.write(blob);
    await writable.close();
    return;
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function downloadFullBackup() {
  const button = document.querySelector("#downloadFullBackup");
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = "正在生成并下载…";
  try {
    const response = await fetch("api/admin/full-backup.db", {
      credentials: "same-origin",
      headers: { "X-Site-Code": state.selectedSite },
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `完整备份下载失败（${response.status}）`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1]
      || `quality-trace-full-backup-${Date.now()}.db`;
    const sha256 = response.headers.get("X-Backup-SHA256") || "";
    const blob = await response.blob();
    await saveBackupBlob(blob, filename);
    await api("/api/admin/full-backup/confirm", {
      method: "POST",
      body: JSON.stringify({ filename, sha256, size_bytes: blob.size }),
    });
    await loadStorageStatus({ silent: true });
    const clearNow = await showBindDecision({
      title: "完整服务器数据已下载",
      message: `备份文件 ${filename} 已保存（${formatBytes(blob.size)}）。建议现在清除安全缓存，释放历史备份和发布包占用；业务主数据与追溯记录不会删除。`,
      confirmLabel: "清除安全缓存",
      warning: true,
      eyebrow: "BACKUP COMPLETE · SAFE MAINTENANCE",
    });
    if (clearNow) await clearServerCache();
  } catch (error) {
    if (error?.name !== "AbortError") toast("完整备份未完成", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}

async function clearServerCache() {
  if (!state.storage?.cleanup_allowed) {
    toast("暂不能清理", "请先下载完整服务器数据", "error");
    return;
  }
  const confirmed = await showBindDecision({
    title: "确认执行安全缓存清理？",
    message: "仅清理过期会话、停用且从未绑定的旧清单行、旧容量快照及超额历史备份/发布包。现场绑定、解绑、主数据和操作审计记录会完整保留。",
    confirmLabel: "确认清理",
    warning: true,
    eyebrow: "SAFE CACHE CLEANUP · AUDITED ACTION",
  });
  if (!confirmed) return;
  const button = document.querySelector("#clearServerCache");
  button.disabled = true;
  button.textContent = "正在安全清理…";
  try {
    const result = await api("/api/admin/cache/clear", {
      method: "POST",
      body: JSON.stringify({ confirm_text: "清理缓存" }),
    });
    renderStorageStatus(result.storage);
    const count = Object.values(result.deleted || {}).reduce((sum, value) => sum + Number(value || 0), 0);
    toast("安全缓存清理完成", `共清理 ${count} 项；追溯业务数据已保留`);
  } catch (error) {
    toast("安全缓存清理失败", error.message, "error");
    await loadStorageStatus({ silent: true });
  } finally {
    button.textContent = "清除安全缓存";
  }
}

function scannerElements() {
  return {
    dialog: document.querySelector("#scannerDialog"), video: document.querySelector("#scannerVideo"),
    canvas: document.querySelector("#scannerCanvas"), idle: document.querySelector("#scannerIdle"),
    feedback: document.querySelector(".scanner-feedback"), status: document.querySelector("#scannerStatus"),
    hint: document.querySelector("#scannerHint"), liveButton: document.querySelector("#startLiveScanner"),
    photoInput: document.querySelector("#scanPhotoInput"), torchButton: document.querySelector("#toggleTorch"),
  };
}

function setScannerStatus(message, mode = "") {
  const { feedback, status } = scannerElements();
  feedback.className = `scanner-feedback ${mode}`.trim();
  status.textContent = message;
}

function liveScannerSupport() {
  if (!window.jsQR) {
    return { available: false, message: "二维码解析库未加载，请刷新页面后重试" };
  }
  if (navigator.mediaDevices?.getUserMedia) {
    return { available: true, message: "打开实时摄像头" };
  }
  if (!window.isSecureContext) {
    return {
      available: false,
      message: "实时扫码需要 HTTPS",
      hint: "当前页面不是安全连接。请使用 https 地址，并确认手机已信任站点证书；普通 HTTP 只能使用拍照识码。",
    };
  }
  return {
    available: false,
    message: "浏览器不支持实时扫码",
    hint: "当前浏览器没有提供网页摄像头接口。请升级 Chrome/Edge/Safari，或暂用拍照识码。",
  };
}

function cameraConstraints() {
  return [
    { audio: false, video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 960 }, frameRate: { ideal: 30 } } },
    { audio: false, video: { facingMode: { ideal: "environment" } } },
    { audio: false, video: true },
  ];
}

async function requestCameraStream() {
  let lastError;
  for (const constraints of cameraConstraints()) {
    try {
      return await navigator.mediaDevices.getUserMedia(constraints);
    } catch (error) {
      lastError = error;
      if (["NotAllowedError", "SecurityError", "NotFoundError", "NotReadableError"].includes(error.name)) {
        break;
      }
    }
  }
  throw lastError;
}

function cameraErrorMessage(error) {
  if (!window.isSecureContext) return "实时扫码需要 HTTPS 安全连接；请使用 https 地址并信任站点证书";
  if (error.name === "NotAllowedError" || error.name === "SecurityError") return "摄像头权限被拒绝，请在浏览器站点设置中允许摄像头后重试";
  if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") return "未检测到可用摄像头，请检查设备或改用拍照识码";
  if (error.name === "NotReadableError" || error.name === "TrackStartError") return "摄像头正被其他应用占用，请关闭占用摄像头的应用后重试";
  if (error.name === "OverconstrainedError" || error.name === "ConstraintNotSatisfiedError") return "当前设备不支持指定摄像头参数，已尝试兼容模式但仍无法打开";
  if (error.name === "NotSupportedError") return "当前浏览器不支持实时摄像头调用，请升级浏览器或使用拍照识码";
  return `无法打开摄像头：${error.message || error.name || "未知错误"}`;
}

function createNativeQrDetector() {
  if (typeof window.BarcodeDetector !== "function") return null;
  try {
    return new window.BarcodeDetector({ formats: ["qr_code"] });
  } catch {
    return null;
  }
}

async function optimizeScannerTrack(track) {
  if (!track?.getCapabilities || !track?.applyConstraints) return;
  const capabilities = track.getCapabilities();
  const advanced = {};
  if (capabilities.focusMode?.includes?.("continuous")) advanced.focusMode = "continuous";
  if (capabilities.exposureMode?.includes?.("continuous")) advanced.exposureMode = "continuous";
  if (!Object.keys(advanced).length) return;
  try {
    await track.applyConstraints({ advanced: [advanced] });
  } catch {
    // Some mobile browsers advertise controls that the active camera cannot apply.
  }
}

function stopScannerStream() {
  const { video, torchButton } = scannerElements();
  if (state.scanner.frameRequest) cancelAnimationFrame(state.scanner.frameRequest);
  state.scanner.frameRequest = null;
  state.scanner.stream?.getTracks().forEach((track) => track.stop());
  state.scanner.stream = null;
  state.scanner.torchOn = false;
  state.scanner.processing = false;
  state.scanner.detector = null;
  state.scanner.detecting = false;
  state.scanner.scanAttempt = 0;
  video.srcObject = null;
  torchButton.classList.add("hidden");
  torchButton.textContent = "打开闪光灯";
}

function closeScanner() {
  const { dialog, idle, photoInput } = scannerElements();
  stopScannerStream();
  idle.classList.remove("hidden");
  photoInput.value = "";
  state.scanner.request = null;
  if (dialog.open) dialog.close();
}

function openScanner(button) {
  const { dialog, liveButton, hint, idle } = scannerElements();
  state.scanner.request = { kind: button.dataset.scanKind, input: button.dataset.scanInput };
  state.scanner.processing = false;
  state.scanner.lastDecodedValue = "";
  state.scanner.lastSeenAt = 0;
  state.scanner.successCount = 0;
  state.scanner.failureCount = 0;
  const support = liveScannerSupport();
  liveButton.disabled = !support.available;
  liveButton.textContent = support.message;
  const continuousBind = button.dataset.scanInput === "#bindPartInput" && Boolean(state.bindSession);
  hint.textContent = continuousBind
    ? "实时取景会一直保持；每件识别后立即后台保存，成功或失败均无需点确认，直接扫描下一件。"
    : support.available
      ? "可使用实时扫码，也可调用系统相机拍照识码；识别过程全部在本机完成。"
      : support.hint;
  idle.classList.remove("hidden");
  setScannerStatus("请选择实时扫码或拍照识码");
  if (!dialog.open) dialog.showModal();
}

function setScannedValue(selector, value) {
  const field = document.querySelector(selector);
  if (!field) throw new Error("未找到需要回填的字段");
  field.value = value;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  field.dispatchEvent(new Event("change", { bubbles: true }));
  return field;
}

function applyScanPayload(payload) {
  const request = state.scanner.request;
  if (!request) throw new Error("扫码目标已失效，请重新点击扫码按钮");
  let value = payload.raw;
  if (request.kind === "order") {
    value = payload.assemblyOrder || String(payload.raw || "").match(/2000\d+/)?.[0] || "";
    if (!value) throw new Error("未识别到2000开头的部件/装配订单号");
  }
  if (!value) throw new Error("二维码内容为空");
  return { field: setScannedValue(request.input, value), display: value };
}

function scannerIsContinuousBind() {
  return state.scanner.request?.input === "#bindPartInput" && Boolean(state.bindSession);
}

async function handleDecodedScan(rawValue) {
  if (state.scanner.processing) return;
  const normalizedRaw = String(rawValue || "").trim();
  const now = performance.now();
  if (
    normalizedRaw &&
    normalizedRaw === state.scanner.lastDecodedValue &&
    now - state.scanner.lastSeenAt < 1800
  ) {
    state.scanner.lastSeenAt = now;
    return;
  }
  state.scanner.processing = true;
  state.scanner.lastDecodedValue = normalizedRaw;
  state.scanner.lastSeenAt = now;
  try {
    const payload = window.TraceScanUtils.parseScanPayload(normalizedRaw);
    const result = applyScanPayload(payload);
    const submitBindPart = result.field.id === "bindPartInput" && Boolean(state.bindSession);
    if (submitBindPart) {
      const form = result.field.closest("form");
      form.dataset.inputMethod = "CAMERA";
      const bound = await stageBindItem(form);
      state.scanner.successCount += 1;
      setScannerStatus(
        `已保存 ${state.scanner.successCount} 件 · ${bound.part.display_code} · 请继续扫描`,
        "success",
      );
      navigator.vibrate?.(45);
    } else {
      setScannerStatus(`识别成功：${result.display}`, "success");
      navigator.vibrate?.(70);
      stopScannerStream();
      setTimeout(() => {
        closeScanner();
        result.field.focus();
      }, 420);
      return;
    }
  } catch (error) {
    state.scanner.failureCount += 1;
    document.querySelector("#bindPartInput")?.closest("form")?.reset();
    setScannerStatus(
      scannerIsContinuousBind()
        ? `未保存：${error.message} · 请继续扫描下一件`
        : error.message,
      "error",
    );
    navigator.vibrate?.([60, 45, 60]);
  } finally {
    state.scanner.processing = false;
  }
}

function decodeCanvas(canvas, inversionAttempts = "attemptBoth") {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  return window.jsQR(image.data, image.width, image.height, { inversionAttempts });
}

function scannerFramePlan(attempt, hasNativeDetector) {
  const fullFrame = attempt % 6 === 0;
  return {
    runJsQr: !hasNativeDetector || attempt % 3 === 0,
    fullFrame,
    cropRatio: fullFrame ? 1 : 0.72,
    maxSide: fullFrame ? 960 : 640,
    inversionAttempts: fullFrame ? "attemptBoth" : "dontInvert",
  };
}

function drawScannerFrame(video, canvas, plan) {
  const sourceWidth = video.videoWidth;
  const sourceHeight = video.videoHeight;
  let sourceX = 0;
  let sourceY = 0;
  let cropWidth = sourceWidth;
  let cropHeight = sourceHeight;
  if (!plan.fullFrame) {
    const cropSize = Math.max(1, Math.round(Math.min(sourceWidth, sourceHeight) * plan.cropRatio));
    cropWidth = cropSize;
    cropHeight = cropSize;
    sourceX = Math.max(0, Math.round((sourceWidth - cropSize) / 2));
    sourceY = Math.max(0, Math.round((sourceHeight - cropSize) / 2));
  }
  const scale = Math.min(1, plan.maxSide / Math.max(cropWidth, cropHeight));
  const targetWidth = Math.max(1, Math.round(cropWidth * scale));
  const targetHeight = Math.max(1, Math.round(cropHeight * scale));
  if (canvas.width !== targetWidth) canvas.width = targetWidth;
  if (canvas.height !== targetHeight) canvas.height = targetHeight;
  canvas.getContext("2d", { willReadFrequently: true }).drawImage(
    video,
    sourceX, sourceY, cropWidth, cropHeight,
    0, 0, targetWidth, targetHeight,
  );
}

async function decodeScannerFrame(video, canvas) {
  state.scanner.scanAttempt += 1;
  let detector = state.scanner.detector;
  let plan = scannerFramePlan(state.scanner.scanAttempt, Boolean(detector));
  if (detector) {
    try {
      const codes = await detector.detect(video);
      const rawValue = String(codes?.[0]?.rawValue || "").trim();
      if (rawValue) return rawValue;
    } catch {
      state.scanner.detector = null;
      detector = null;
      plan = scannerFramePlan(state.scanner.scanAttempt, false);
    }
  }
  if (!plan.runJsQr) return "";
  drawScannerFrame(video, canvas, plan);
  return String(decodeCanvas(canvas, plan.inversionAttempts)?.data || "").trim();
}

function scheduleScannerFrame() {
  if (state.scanner.stream && !state.scanner.frameRequest) {
    state.scanner.frameRequest = requestAnimationFrame(scanVideoFrame);
  }
}

function scanVideoFrame(timestamp) {
  const { video, canvas } = scannerElements();
  state.scanner.frameRequest = null;
  const activeStream = state.scanner.stream;
  if (!activeStream) return;
  if (
    !state.scanner.processing &&
    !state.scanner.detecting &&
    timestamp - state.scanner.lastFrameAt >= 80 &&
    video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
  ) {
    state.scanner.lastFrameAt = timestamp;
    state.scanner.detecting = true;
    void decodeScannerFrame(video, canvas)
      .then((rawValue) => {
        if (activeStream !== state.scanner.stream) return;
        if (rawValue) {
          void handleDecodedScan(rawValue);
        } else if (
          state.scanner.lastDecodedValue &&
          performance.now() - state.scanner.lastSeenAt > 500
        ) state.scanner.lastDecodedValue = "";
      })
      .catch(() => {})
      .finally(() => {
        state.scanner.detecting = false;
        scheduleScannerFrame();
      });
    return;
  }
  scheduleScannerFrame();
}

async function startLiveScanner() {
  const { video, idle, torchButton } = scannerElements();
  const support = liveScannerSupport();
  if (!support.available) return setScannerStatus(support.hint || support.message, "error");
  stopScannerStream();
  setScannerStatus("正在申请后置摄像头权限…", "active");
  try {
    const stream = await requestCameraStream();
    state.scanner.stream = stream;
    video.srcObject = stream;
    await video.play();
    idle.classList.add("hidden");
    setScannerStatus("快速识别已开启，请将二维码置于框内", "active");
    const track = stream.getVideoTracks()[0];
    if (track?.getCapabilities?.().torch) torchButton.classList.remove("hidden");
    state.scanner.detector = createNativeQrDetector();
    state.scanner.detecting = false;
    state.scanner.scanAttempt = 0;
    state.scanner.lastFrameAt = 0;
    void optimizeScannerTrack(track);
    scheduleScannerFrame();
  } catch (error) {
    setScannerStatus(cameraErrorMessage(error), "error");
  }
}

function loadPhoto(file) {
  if (typeof createImageBitmap === "function") return createImageBitmap(file);
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(file);
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error("无法读取照片")); };
    image.src = url;
  });
}

async function scanPhoto(file) {
  if (!file) return;
  const { canvas, idle } = scannerElements();
  setScannerStatus("正在本机解析照片…", "active");
  try {
    const image = await loadPhoto(file);
    const width = image.width || image.naturalWidth;
    const height = image.height || image.naturalHeight;
    const scale = Math.min(1, 1800 / Math.max(width, height));
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    canvas.getContext("2d", { willReadFrequently: true }).drawImage(image, 0, 0, canvas.width, canvas.height);
    image.close?.();
    idle.classList.add("hidden");
    const result = decodeCanvas(canvas);
    if (!result?.data) throw new Error("照片中未识别到二维码，请对准并保持清晰后重拍");
    await handleDecodedScan(result.data);
  } catch (error) {
    setScannerStatus(error.message, "error");
  }
}

async function toggleTorch() {
  const track = state.scanner.stream?.getVideoTracks()[0];
  if (!track) return;
  state.scanner.torchOn = !state.scanner.torchOn;
  try {
    await track.applyConstraints({ advanced: [{ torch: state.scanner.torchOn }] });
    document.querySelector("#toggleTorch").textContent = state.scanner.torchOn ? "关闭闪光灯" : "打开闪光灯";
  } catch {
    state.scanner.torchOn = false;
    setScannerStatus("该设备不支持网页控制闪光灯", "error");
  }
}

function wireAuthAndAccounts() {
  document.querySelector("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    const old = button.textContent;
    button.disabled = true;
    button.textContent = "正在验证身份…";
    try {
      const result = await api("/api/auth/login", { method: "POST", body: JSON.stringify(formObject(form)) });
      applyAuthenticatedUser(result.user, result.csrf_token);
      form.reset();
      await loadBootstrap();
      navigate(location.hash.slice(1) || "dashboard", false);
    } catch (error) {
      toast("登录失败", error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = old;
    }
  });
  document.querySelector("#logoutButton").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST", body: "{}" }); } catch {}
    showLogin();
  });
  document.querySelector("#passwordForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api("/api/auth/change-password", { method: "POST", body: JSON.stringify(formObject(form)) });
      state.user.must_change_password = false;
      refreshNavigationPermissions(state.user);
      document.querySelector("#passwordDialog").close();
      form.reset();
      toast("密码修改成功");
    } catch (error) { toast("密码修改失败", error.message, "error"); }
  });
}

function wireCatalogAndExchange() {
  const masterSearch = document.querySelector("#masterOrderSearch");
  masterSearch.addEventListener("input", () => {
    state.masterSearch = masterSearch.value;
    state.masterPage = 1;
    if (state.bootstrap) renderCatalog(state.bootstrap);
  });
  document.querySelector("#clearMasterSearch").addEventListener("click", () => {
    masterSearch.value = "";
    state.masterSearch = "";
    state.masterPage = 1;
    if (state.bootstrap) renderCatalog(state.bootstrap);
    masterSearch.focus();
  });
  document.querySelector("#catalogWarningsOnly").addEventListener("change", (event) => {
    state.masterWarningsOnly = event.currentTarget.checked;
    if (state.bootstrap) renderCatalog(state.bootstrap);
  });
  document.querySelector("#masterPagination").addEventListener("click", (event) => {
    const button = event.target.closest("[data-master-page]");
    if (!button || button.disabled) return;
    state.masterPage += button.dataset.masterPage === "next" ? 1 : -1;
    if (state.bootstrap) renderCatalog(state.bootstrap);
    document.querySelector("#orderList").scrollIntoView({ block: "start", behavior: "smooth" });
  });
  const wireListImport = ({ inputId, buttonId, stateKey, endpoint, emptyLabel, successLabel }) => {
    const input = document.querySelector(inputId);
    const button = document.querySelector(buttonId);
    input.addEventListener("change", () => {
      const file = input.files[0];
      state[stateKey] = file?.name.toLowerCase().endsWith(".xlsx") && file.size <= 15 * 1024 * 1024 ? file : null;
      button.disabled = !state[stateKey];
      input.closest(".file-drop").querySelector("span").textContent = state[stateKey]?.name || emptyLabel;
      if (file && !state[stateKey]) toast("清单文件无效", "请选择不超过15MB的.xlsx文件", "error");
    });
    button.addEventListener("click", async () => {
      const old = button.textContent;
      button.disabled = true;
      button.textContent = "正在校验并更新…";
      try {
        const file = state[stateKey];
        const result = await api(endpoint, {
          method: "POST",
          body: JSON.stringify({
            filename: file.name,
            content_base64: await fileToBase64(file),
            target_site: state.masterSite,
          }),
        });
        toast(result.skipped ? "清单版本未变化" : successLabel,
          `${result.valid_count}件有效数据 · ${result.warnings.length}条提示`);
        input.value = "";
        state[stateKey] = null;
        input.closest(".file-drop").querySelector("span").textContent = emptyLabel;
        await loadBootstrap();
      } catch (error) {
        toast("清单导入失败", error.message, "error");
      } finally {
        button.textContent = old;
        button.disabled = !state[stateKey];
      }
    });
  };
  wireListImport({
    inputId: "#componentTraceFile",
    buttonId: "#importComponentTrace",
    stateKey: "selectedComponentTraceExcel",
    endpoint: "/api/master/component-trace/import-excel",
    emptyLabel: "选择 部件追溯清单.xlsx",
    successLabel: "部件追溯清单已更新",
  });
  wireListImport({
    inputId: "#srmPartsFile",
    buttonId: "#importSrmParts",
    stateKey: "selectedSrmPartsExcel",
    endpoint: "/api/master/srm-parts/import-excel",
    emptyLabel: "选择 SRM零件清单.xlsx",
    successLabel: "SRM零件清单已更新",
  });
  const packageFile = document.querySelector("#packageFile");
  packageFile.addEventListener("change", async () => {
    const file = packageFile.files[0];
    state.selectedPackage = null;
    document.querySelector("#importPackage").disabled = true;
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) return toast("文件过大", "数据包不能超过10MB", "error");
    try {
      state.selectedPackage = JSON.parse(await file.text());
      document.querySelector("#importPackage").disabled = false;
      packageFile.closest(".file-drop").querySelector("span").textContent = file.name;
    } catch { toast("文件无效", "无法解析JSON数据包", "error"); }
  });
  document.querySelector("#importPackage").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      const result = await api("/api/exchange/import", { method: "POST", body: JSON.stringify({ package: state.selectedPackage }) });
      toast("数据包导入成功", `${result.source_site} · ${result.record_count}条记录`);
      packageFile.value = "";
      state.selectedPackage = null;
      await loadBootstrap();
      await loadExchangeLogs();
    } catch (error) { toast("导入失败", error.message, "error"); }
  });
}

function wireEvents() {
  wireAuthAndAccounts();
  wireCatalogAndExchange();
  wireAdminDataEvents();
  document.querySelector("#siteSwitcher").addEventListener("change", async (event) => {
    state.selectedSite = event.currentTarget.value;
    sessionStorage.setItem("traceSelectedSite", state.selectedSite);
    try { await loadBootstrap(); toast("数据视图已切换", state.bootstrap.site.name); }
    catch (error) { toast("切换失败", error.message, "error"); }
  });
  document.querySelector("#masterSiteSwitcher").addEventListener("change", (event) => {
    state.masterSite = event.currentTarget.value;
    sessionStorage.setItem("traceMasterSite", state.masterSite);
    document.querySelectorAll("[data-master-site-name]").forEach((node) => {
      node.textContent = ({ XC: "新场", JC: "锦晨" })[state.masterSite] || state.masterSite;
    });
    renderCatalog(state.bootstrap);
  });
  document.addEventListener("click", async (event) => {
    const nav = event.target.closest("[data-nav]");
    if (nav) navigate(nav.dataset.nav);
    const statusDetail = event.target.closest("[data-status-detail]");
    if (statusDetail) openStatusDetail(statusDetail.dataset.statusDetail);
    const detailTrace = event.target.closest("[data-detail-trace]");
    if (detailTrace) {
      const code = detailTrace.dataset.detailTrace;
      closeStatusDetail();
      navigate("trace");
      document.querySelector("#tracePartCode").value = code;
      runTrace(code).catch((error) => toast("追溯查询失败", error.message, "error"));
    }
    const detailBind = event.target.closest("[data-detail-bind]");
    if (detailBind) {
      closeStatusDetail();
      const targetSite = detailBind.dataset.detailSite;
      if (state.user?.role === "ADMIN" && state.user?.site_code === "HQ" && ["XC", "JC"].includes(targetSite)) {
        state.selectedSite = targetSite;
        sessionStorage.setItem("traceSelectedSite", targetSite);
        try { await loadBootstrap(); }
        catch (error) { toast("站点切换失败", error.message, "error"); return; }
      }
      navigate("bind");
      resetBindSession();
      document.querySelector("#bindOrderInput").value = detailBind.dataset.detailBind;
      document.querySelector("#bindOrderForm").requestSubmit();
    }
    const unbindPart = event.target.closest("[data-unbind-binding]");
    if (unbindPart) unbindDetailBinding(unbindPart);
    const unbindDenied = event.target.closest("[data-unbind-denied]");
    if (unbindDenied) await showUnbindPermissionAlert();
    const scan = event.target.closest("[data-scan-kind]");
    if (scan) openScanner(scan);
    const remove = event.target.closest("[data-remove-batch]");
    if (remove) {
      state.batches[remove.dataset.removeBatch].splice(Number(remove.dataset.index), 1);
      renderBatch(remove.dataset.removeBatch);
    }
    const clear = event.target.closest("[data-clear-batch]");
    if (clear) {
      const kind = clear.dataset.clearBatch;
      if (kind === "bind") return resetBindSession();
      state.batches[kind] = [];
      batchUi(kind).orderInput.value = "";
      batchUi(kind).partInput.value = "";
      renderBatch(kind);
    }
  });
  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
    if (event.key === "/" && !typing) { event.preventDefault(); navigate("trace"); }
  });
  document.querySelector("#refreshDashboard").addEventListener("click", () => loadBootstrap().then(() => toast("已刷新")));
  document.querySelector("#refreshBoard").addEventListener("click", () => loadBootstrap().then(() => {
    renderAnalyticsBoard();
    toast("追溯看板已刷新");
  }));
  document.querySelector("#refreshStorage").addEventListener("click", () =>
    loadStorageStatus().then((result) => {
      if (result) toast("服务器容量已重新测算");
    })
  );
  document.querySelector("#downloadFullBackup").addEventListener("click", downloadFullBackup);
  document.querySelector("#clearServerCache").addEventListener("click", clearServerCache);
  document.querySelector("#statusDetailSearch").addEventListener("input", renderStatusDetailList);
  document.querySelector("#selectVisibleBindings").addEventListener("change", (event) => {
    const eligibleIds = visibleUnbindBindings(statusDetailParts())
      .filter((binding) => userCanUnbindBinding(state.user, binding.site_code))
      .map((binding) => Number(binding.binding_id));
    if (event.currentTarget.checked) eligibleIds.forEach((bindingId) => state.selectedUnbindIds.add(bindingId));
    else eligibleIds.forEach((bindingId) => state.selectedUnbindIds.delete(bindingId));
    renderStatusDetailList();
  });
  document.querySelector("#clearUnbindSelection").addEventListener("click", () => {
    state.selectedUnbindIds.clear();
    renderStatusDetailList();
  });
  document.querySelector("#batchUnbindSelected").addEventListener("click", batchUnbindSelected);
  document.querySelector("#statusDetailList").addEventListener("change", (event) => {
    const checkbox = event.target.closest("[data-select-unbind]");
    if (!checkbox) return;
    const bindingId = Number(checkbox.dataset.selectUnbind);
    if (checkbox.checked) state.selectedUnbindIds.add(bindingId);
    else state.selectedUnbindIds.delete(bindingId);
    updateUnbindSelectionBar(visibleUnbindBindings(statusDetailParts()));
  });
  document.querySelector("#closeStatusDetail").addEventListener("click", closeStatusDetail);
  document.querySelector("#closeStatusDetailFooter").addEventListener("click", closeStatusDetail);
  document.querySelector("#statusDetailDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeStatusDetail();
  });
  document.querySelector("#statusDetailDialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeStatusDetail();
  });
  document.querySelector("#traceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await runTrace(formObject(event.currentTarget).part_code); }
    catch (error) { toast("查询失败", error.message, "error"); }
  });
  document.querySelector("#issueStageForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await stageBatchItem("issue", event.currentTarget); }
    catch (error) { toast("未加入批次", error.message, "error"); }
  });
  batchUi("issue").confirm.addEventListener("click", async () => {
    try { await confirmBatch("issue"); }
    catch (error) { toast("批次确认失败", error.message, "error"); renderBatch("issue"); }
  });
  document.querySelector("#bindOrderForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await startBindOrder(event.currentTarget); }
    catch (error) { toast("装配订单载入失败", error.message, "error"); }
  });
  document.querySelector("#bindStageForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await stageBindItem(event.currentTarget); }
    catch (error) {
      if (error.message.includes("重复绑定已拒绝")) await showDuplicateBindingAlert(error.message);
      else toast("零件核验失败", error.message, "error");
    }
  });
  document.querySelector("#bindPartInput").addEventListener("keydown", (event) => {
    const typing = state.scanTyping;
    const now = performance.now();
    if (event.key === "Enter") {
      event.preventDefault();
      const duration = Math.max(1, typing.lastAt - typing.startedAt);
      typing.pendingMethod =
        typing.keyCount >= 8 && duration <= Math.max(900, typing.keyCount * 90)
          ? "SCANNER"
          : "MANUAL";
      event.currentTarget.closest("form").dataset.inputMethod = typing.pendingMethod;
      typing.startedAt = 0;
      typing.lastAt = 0;
      typing.keyCount = 0;
      event.currentTarget.closest("form").requestSubmit();
      return;
    }
    if (event.key.length !== 1) return;
    if (!typing.lastAt || now - typing.lastAt > 180) {
      typing.startedAt = now;
      typing.keyCount = 0;
    }
    typing.lastAt = now;
    typing.keyCount += 1;
  });
  document.querySelector("#confirmBindBatch").addEventListener("click", async () => {
    try { await confirmBindSession(); }
    catch (error) {
      if (error.message.includes("有未绑定的质量追溯零件")) {
        await showBindDecision({
          title: "绑定清单尚未完成",
          message: "有未绑定的质量追溯零件，请继续绑定。",
          confirmLabel: "继续绑定",
          confirmOnly: true,
        });
      } else if (error.message.includes("重复绑定已拒绝")) {
        await showDuplicateBindingAlert(error.message);
      } else {
        toast("绑定确认失败", error.message, "error");
      }
      renderBindSession();
    }
  });
  document.querySelector("#closeScanner").addEventListener("click", closeScanner);
  document.querySelector("#scannerDialog").addEventListener("cancel", (event) => { event.preventDefault(); closeScanner(); });
  document.querySelector("#scannerDialog").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeScanner(); });
  document.querySelector("#startLiveScanner").addEventListener("click", startLiveScanner);
  document.querySelector("#scanPhotoInput").addEventListener("change", (event) => scanPhoto(event.currentTarget.files[0]));
  document.querySelector("#toggleTorch").addEventListener("click", toggleTorch);
  document.addEventListener("visibilitychange", () => { if (document.hidden && state.scanner.stream) closeScanner(); });
  document.querySelectorAll(".download-link").forEach((link) => link.addEventListener("click", () => setTimeout(loadExchangeLogs, 900)));
  renderBatch("issue");
  renderBatch("bind");
}

async function init() {
  wireEvents();
  try {
    const auth = await api("/api/auth/me");
    if (!auth.user) return showLogin();
    applyAuthenticatedUser(auth.user, auth.csrf_token);
    await loadBootstrap();
    navigate(location.hash.slice(1) || "dashboard", false);
  } catch (error) {
    showLogin();
    if (error.status !== 401) toast("无法连接中心服务", error.message, "error");
  }
}

init();
