import {
  Activity, ArrowLeft, ArrowRight, Blocks, BookOpenCheck, Box, CheckCircle2,
  CircleDashed, ClipboardCheck, Database, Fingerprint, GitBranch, Layers3,
  LoaderCircle, LockKeyhole, LogOut, Network, RefreshCw, Save, ScanSearch,
  ShieldCheck, UserCog, UserPlus, Users
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

type Permission = "failure" | "trace" | "machine_watch" | "assembly_sq" | "vehicle" | "user_admin";
type SystemPermission = Exclude<Permission, "user_admin">;
type TraceRole = "ADMIN" | "WAREHOUSE_OPERATOR" | "ASSEMBLY_OPERATOR" | "VIEWER";
type HealthState = "checking" | "online" | "degraded" | "offline" | "unknown";
type SystemHealth = Record<SystemPermission, HealthState>;
type PlatformUser = {
  id: number;
  username: string;
  display_name: string;
  role: "ADMIN" | "USER";
  permissions: Permission[];
  trace_username: string;
  trace_role: TraceRole;
  trace_site_code: "HQ" | "XC" | "JC";
  trace_sync_status: "PENDING" | "SYNCED" | "ERROR";
  trace_sync_error: string;
  active: boolean;
  created_at: string;
  updated_at: string;
  sync_warning?: string;
};

type Module = {
  index: string;
  name: string;
  english: string;
  description: string;
  delivery: "已上线" | "试运行";
  icon: typeof ScanSearch;
  metrics: Array<{ label: string; value: string }>;
  capabilities: string[];
  href?: string;
  permission: SystemPermission;
  accent: "copper" | "teal" | "blue" | "gold";
};

const modules: Module[] = [
  {
    index: "01",
    name: "失效分析平台",
    english: "FAILURE INTELLIGENCE",
    description: "融合 IMA 烟机质检知识库与多模态大模型，将零部件档案、现场图像和工况证据转化为可追溯的工程结论。",
    delivery: "已上线",
    icon: ScanSearch,
    metrics: [{ label: "分析链路", value: "知识检索 → 多模态" }, { label: "输出", value: "结构化报告 / PDF" }],
    capabilities: ["零部件结构化建档", "IMA 知识检索", "失效机理与根因排序", "历史报告搜索"],
    href: "/failure-analysis/",
    permission: "failure",
    accent: "copper"
  },
  {
    index: "02",
    name: "质量追溯",
    english: "QUALITY TRACEABILITY",
    description: "以生产订单/采购订单零件码精确定位实物，贯通部件订单、工序与WBS；仓库发放和现场绑定分段确认，支持总厂、新场、锦晨共享。",
    delivery: "已上线",
    icon: GitBranch,
    metrics: [{ label: "追溯主键", value: "订单号 + 行号 + 序列号" }, { label: "业务门禁", value: "发放 → 绑定 → 装配" }],
    capabilities: ["生产/采购零件码查询", "部件订单与WBS贯通", "最新Excel清单导入", "仓库整批发放", "现场整批绑定", "安卓摄像头扫码"],
    href: "/platform-api/auth/trace-sso",
    permission: "trace",
    accent: "teal"
  },
  {
    index: "03",
    name: "机组运行评价",
    english: "MACHINE WATCH",
    description: "汇集产量效率、剔除质量、巡检诊断、外部 NCR 与出厂评分，以红黄绿状态和规则预警识别优先处置机组。",
    delivery: "已上线",
    icon: Activity,
    metrics: [{ label: "评价对象", value: "机组 / 烟厂 / 区域" }, { label: "分析方式", value: "健康评分 / 风险预警" }],
    capabilities: ["红黄绿状态总览", "多源质量信号", "区域风险分布", "机组问题明细"],
    href: "/machine-watch/",
    permission: "machine_watch",
    accent: "blue"
  },
  {
    index: "04",
    name: "装配工单 SQ 质检看板",
    english: "ASSEMBLY SQ INSPECTION",
    description: "聚合装配工单的自检、专检、报工与审核状态，按制造中心查看检验覆盖、待检积压和生产订单明细，并支持导入最新 Excel 数据。",
    delivery: "已上线",
    icon: ClipboardCheck,
    metrics: [{ label: "评价对象", value: "装配工单 / 制造中心" }, { label: "数据方式", value: "内置快照 / Excel 更新" }],
    capabilities: ["自检与专检完成率", "制造中心进度对比", "未检订单清单", "已检订单导出"],
    href: "/assembly-sq-dashboard/",
    permission: "assembly_sq",
    accent: "gold"
  },
  {
    index: "05",
    name: "车载数据服务",
    english: "VEHICLE INTELLIGENCE",
    description: "按 CGI 设备 SN 汇集运行轨迹、惯导信号与定位质量，关联异常事件、行程统计和原始报文，支持多车多设备持续接入。",
    delivery: "试运行",
    icon: Activity,
    metrics: [{ label: "数据来源", value: "CGI-430 / GPCHCX" }, { label: "分析方式", value: "轨迹回放 / 惯导曲线" }],
    capabilities: ["历史轨迹与回放", "惯导曲线与异常点", "事件处置台账", "设备与规则管理", "采样数据导出"],
    href: "/vehicle/",
    permission: "vehicle",
    accent: "teal"
  }
];

const initialHealth: SystemHealth = {
  failure: "checking",
  trace: "checking",
  machine_watch: "checking",
  assembly_sq: "checking",
  vehicle: "checking",
};

const healthLabels: Record<HealthState, string> = {
  checking: "检测中",
  online: "正常",
  degraded: "数据过期",
  offline: "异常",
  unknown: "状态未知",
};

function traceAssignmentError(role: TraceRole, site: PlatformUser["trace_site_code"]) {
  if (["WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR"].includes(role) && !["XC", "JC"].includes(site)) {
    return "现场角色必须明确归属新场或锦晨；总部站点不会显示现场绑定入口。";
  }
  return "";
}

function healthState(value: unknown): HealthState {
  return value === "online" || value === "degraded" || value === "offline" ? value : "unknown";
}

function App() {
  const [user, setUser] = useState<PlatformUser | null>(null);
  const [csrf, setCsrf] = useState("");
  const [authLoading, setAuthLoading] = useState(true);
  const [view, setView] = useState<"home" | "users" | "info">("home");
  const [systemHealth, setSystemHealth] = useState<SystemHealth>(initialHealth);
  const [healthCheckedAt, setHealthCheckedAt] = useState("");

  useEffect(() => {
    fetch("/platform-api/auth/me", { credentials: "same-origin", cache: "no-store" })
      .then((response) => response.json())
      .then((data) => { setUser(data.user); setCsrf(data.csrf_token || ""); })
      .catch(() => setUser(null))
      .finally(() => setAuthLoading(false));
  }, []);

  useEffect(() => {
    if (!user) {
      setSystemHealth(initialHealth);
      setHealthCheckedAt("");
      return;
    }
    let cancelled = false;
    const refresh = () => fetch("/platform-api/auth/system-health", { credentials: "same-origin", cache: "no-store" })
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "运行状态检测失败");
        if (!cancelled) {
          setSystemHealth({
            failure: healthState(data.systems?.failure?.status),
            trace: healthState(data.systems?.trace?.status),
            machine_watch: healthState(data.systems?.machine_watch?.status),
            assembly_sq: healthState(data.systems?.assembly_sq?.status),
            vehicle: healthState(data.systems?.vehicle?.status),
          });
          setHealthCheckedAt(data.checked_at || "");
        }
      })
      .catch(() => {
        if (!cancelled) setSystemHealth({ failure: "unknown", trace: "unknown", machine_watch: "unknown", assembly_sq: "unknown", vehicle: "unknown" });
      });
    refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [user]);

  async function login(username: string, password: string) {
    const response = await fetch("/platform-api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "登录失败，请稍后重试");
    setUser(data.user);
    setCsrf(data.csrf_token);
    const next = new URLSearchParams(window.location.search).get("next");
    if (next?.startsWith("/") && !next.startsWith("//")) window.location.assign(next);
  }

  async function logout() {
    await fetch("/platform-api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf },
    });
    setUser(null);
    setCsrf("");
    setView("home");
  }

  if (authLoading) return <div className="auth-loading"><span className="brand-symbol"><Layers3 size={22}/></span><LoaderCircle className="spin" size={22}/><b>正在建立安全工作域</b></div>;
  if (!user) return <LoginPage onLogin={login}/>;

  const can = (permission: Permission) => user.permissions.includes(permission);
  const onlineCount = Object.values(systemHealth).filter((status) => status === "online").length;
  const degradedCount = Object.values(systemHealth).filter((status) => status === "degraded").length;
  const offlineCount = Object.values(systemHealth).filter((status) => status === "offline").length;
  const healthPending = Object.values(systemHealth).some((status) => status === "checking" || status === "unknown");
  if (view === "users" && can("user_admin")) {
    return <UserManagement currentUser={user} csrf={csrf} onBack={() => setView("home")} onLogout={logout}/>;
  }
  if (view === "info") {
    return <PlatformInfo user={user} can={can} health={systemHealth} onBack={() => setView("home")} onLogout={logout}/>;
  }

  return <div className="platform-shell">
    <header className="site-header home-header">
      <a className="brand" href="/" aria-label="CTMC Quality OS 首页">
        <span className="brand-symbol"><Layers3 size={22}/></span>
        <span><b>CTMC QUALITY</b><small>质量数字化工作台</small></span>
      </a>
      <nav className="home-tools" aria-label="平台工具">
        <button className="quiet-action" onClick={() => setView("info")} aria-label="平台说明"><BookOpenCheck size={16}/><span>平台说明</span></button>
        {can("user_admin") && <button className="quiet-action" onClick={() => setView("users")} aria-label="用户与权限"><UserCog size={16}/><span>用户与权限</span></button>}
        <span className="secure-pill"><ShieldCheck size={14}/>{user.display_name}</span>
        <button className="nav-logout" onClick={logout} title="退出登录" aria-label="退出登录"><LogOut size={15}/></button>
      </nav>
    </header>

    <main className="home-main">
      <section className="home-intro">
        <div>
          <span className="home-eyebrow">QUALITY WORKSPACE</span>
          <h1>质检平台</h1>
          <p>选择工作模块</p>
        </div>
        <div className={`home-health ${healthPending ? "checking" : offlineCount ? "offline" : degradedCount ? "degraded" : "online"}`}>
          <i/><span>{healthPending ? "正在检测" : offlineCount ? `${offlineCount} 异常 · ${degradedCount} 数据过期` : degradedCount ? `${onlineCount} 正常 · ${degradedCount} 数据过期` : `${onlineCount}/${modules.length} 正常`}</span>
          {healthCheckedAt && <time>{formatCheckedTime(healthCheckedAt)}</time>}
        </div>
      </section>

      <section className="entry-grid" aria-label="业务模块入口">
        {modules.map((module) => <HomeEntry key={module.index} module={module} canAccess={can(module.permission)} health={systemHealth[module.permission]}/>) }
      </section>

    </main>

    <footer className="home-footer">
      <div className="footer-brand"><span className="brand-symbol small"><Box size={16}/></span><div><b>CTMC QUALITY OS</b><small>质量检验部数字化能力入口</small></div></div>
      <span>quality-ctmc.cloud</span>
    </footer>
  </div>;
}

function formatCheckedTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "刚刚" : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function HomeEntry({ module, canAccess, health }: { module: Module; canAccess: boolean; health: HealthState }) {
  const Icon = module.icon;
  const available = Boolean(module.href && canAccess);
  const content = <>
    <span className="entry-number">{module.index}</span>
    <span className="entry-icon"><Icon size={42} strokeWidth={1.55}/></span>
    <div className="entry-name"><small>{module.english}</small><h2>{module.name}</h2></div>
    <span className={`entry-status ${canAccess ? health : "locked"}`}>
      {!canAccess ? <LockKeyhole size={13}/> : health === "online" ? <i/> : health === "checking" ? <LoaderCircle className="spin" size={14}/> : <CircleDashed size={14}/>}
      {!canAccess ? "未授权" : healthLabels[health]}
    </span>
    <span className="entry-arrow">{available ? <ArrowRight size={21}/> : <LockKeyhole size={18}/>}</span>
  </>;
  return available
    ? <a className={`home-entry accent-${module.accent}`} href={module.href}>{content}</a>
    : <div className={`home-entry accent-${module.accent} unavailable`} aria-disabled="true">{content}</div>;
}

function PlatformInfo({ user, can, health, onBack, onLogout }: {
  user: PlatformUser;
  can: (permission: Permission) => boolean;
  health: SystemHealth;
  onBack: () => void;
  onLogout: () => void;
}) {
  return <div className="platform-shell info-shell">
    <header className="site-header info-header">
      <button className="info-back" onClick={onBack} aria-label="返回工作台"><ArrowLeft size={18}/><span>返回工作台</span></button>
      <div className="brand info-brand"><span className="brand-symbol"><Layers3 size={22}/></span><span><b>PLATFORM INFO</b><small>平台说明</small></span></div>
      <div className="info-user"><span className="secure-pill"><ShieldCheck size={14}/>{user.display_name}</span><button className="nav-logout" onClick={onLogout} title="退出登录" aria-label="退出登录"><LogOut size={15}/></button></div>
    </header>
    <main className="info-main">
      <section className="info-hero">
        <span>CTMC QUALITY / PLATFORM INFO</span>
        <h1>平台说明</h1>
        <p>业务模块、平台架构与建设进展集中在此查看。</p>
      </section>
      <nav className="info-subnav" aria-label="平台说明目录"><a href="#modules">业务模块</a><a href="#architecture">平台架构</a><a href="#roadmap">建设进展</a></nav>

      <section className="section modules-section" id="modules">
        <SectionTitle number="01" eyebrow="BUSINESS MODULES" title="业务模块" copy="五套系统贯通质检业务与运输工况，车载数据服务已接入真实设备。"/>
        <div className="module-list">
          {modules.map((module) => <ModuleCard key={module.index} module={module} canAccess={can(module.permission)} health={health[module.permission]}/>) }
        </div>
      </section>

      <section className="architecture" id="architecture">
        <div className="architecture-copy">
          <div className="kicker light"><span>PLATFORM ARCHITECTURE</span><i/>统一质量数据语境</div>
          <h2>平台架构</h2>
          <p>业务应用专注现场场景，平台层管理身份与流程，数据层沉淀质量资产。</p>
          <div className="principles">
            <span><CheckCircle2 size={15}/>统一身份边界</span>
            <span><CheckCircle2 size={15}/>证据链可追溯</span>
          </div>
        </div>
        <div className="architecture-stack">
          <ArchitectureRow label="业务应用层" index="L3" icon={Blocks} items={["失效分析", "质量追溯", "运行监控", "工单 SQ 质检"]}/>
          <ArchitectureRow label="质量平台层" index="L2" icon={Network} items={["权限与身份", "对象管理", "报告中心", "消息与告警"]}/>
          <ArchitectureRow label="数据与知识层" index="L1" icon={Database} items={["主数据", "质量档案", "IMA 知识库", "时序数据"]}/>
        </div>
      </section>

      <section className="section roadmap-section" id="roadmap">
        <SectionTitle number="02" eyebrow="DELIVERY" title="建设进展" copy="四条质检业务主线已上线，车载数据服务进入试运行。"/>
        <div className="roadmap">
          <div className="roadmap-line"/>
          <RoadmapItem phase="01" status="已完成" title="失效分析" copy="知识检索、多模态分析与报告输出" active/>
          <RoadmapItem phase="02" status="已完成" title="机组运行评价" copy="健康评分、区域态势与风险预警" active/>
          <RoadmapItem phase="03" status="已完成" title="质量追溯" copy="三地共享与零件级追溯链" active/>
          <RoadmapItem phase="04" status="已完成" title="装配工单质检" copy="自检、专检与工单进度看板" active/>
        </div>
      </section>
    </main>
  </div>;
}

function ModuleCard({ module, canAccess, health }: { module: Module; canAccess: boolean; health: HealthState }) {
  const Icon = module.icon;
  const available = Boolean(module.href && canAccess);
  const content = <article className={`module-card accent-${module.accent} ${available ? "available" : ""} ${!canAccess ? "locked" : ""} ${canAccess && health === "offline" ? "service-offline" : ""}`}>
    <div className="module-rail"><span className="module-index">{module.index}</span><Icon size={24}/><i/></div>
    <div className="module-content">
      <header><div><small>{module.english} · {module.delivery}</small><h3>{module.name}</h3></div><span className={`status ${canAccess ? health : ""}`}>{!canAccess ? <LockKeyhole size={13}/> : health === "online" ? <CheckCircle2 size={13}/> : health === "checking" ? <LoaderCircle className="spin" size={13}/> : <CircleDashed size={13}/>} {!canAccess ? "未授权" : healthLabels[health]}</span></header>
      <p>{module.description}</p>
      <div className="module-metrics">{module.metrics.map((metric) => <div key={metric.label}><span>{metric.label}</span><b>{metric.value}</b></div>)}</div>
      <div className="capabilities">{module.capabilities.map((item) => <span key={item}><i/>{item}</span>)}</div>
    </div>
    <div className="module-action">{available ? <><span>OPEN MODULE</span><ArrowRight size={18}/></> : <><span>{!canAccess ? "NO ACCESS" : health === "checking" ? "CHECKING" : "SERVICE OFFLINE"}</span>{health === "checking" ? <LoaderCircle className="spin" size={18}/> : <LockKeyhole size={18}/>}</>}</div>
  </article>;
  return available ? <a className="module-link" href={module.href}>{content}</a> : content;
}

function SectionTitle({ number, eyebrow, title, copy }: { number: string; eyebrow: string; title: string; copy: string }) {
  return <div className="section-title"><span>{number}</span><div><small>{eyebrow}</small><h2>{title}</h2></div><p>{copy}</p></div>;
}

function ArchitectureRow({ label, index, icon: Icon, items }: { label: string; index: string; icon: typeof Database; items: string[] }) {
  return <div className="architecture-row"><span>{index}</span><div className="layer-name"><Icon size={18}/><b>{label}</b></div><div className="layer-items">{items.map(item => <i key={item}>{item}</i>)}</div></div>;
}

function RoadmapItem({ phase, status, title, copy, active = false }: { phase: string; status: string; title: string; copy: string; active?: boolean }) {
  return <article className={active ? "active" : ""}><span className="roadmap-dot">{active ? <CheckCircle2 size={17}/> : <CircleDashed size={17}/>}</span><small>{phase}<i>{status}</i></small><h3>{title}</h3><p>{copy}</p></article>;
}

function LoginPage({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      await onLogin(username, password);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return <main className="login-page">
    <div className="login-grid"/>
    <section className="login-story">
      <a className="login-brand" href="/" aria-label="CTMC Quality OS">
        <span className="brand-symbol"><Layers3 size={22}/></span>
        <span><b>CTMC QUALITY</b><small>QUALITY OPERATING SYSTEM</small></span>
      </a>
      <div>
        <span className="login-kicker">SECURE QUALITY WORKSPACE</span>
        <h1>一个身份，进入<br/>连续的质量证据链。</h1>
        <p>平台账号统一控制模块访问；进入质量追溯时无需再次登录。</p>
      </div>
      <div className="login-capabilities">
        <span><Fingerprint size={17}/>统一身份认证</span>
        <span><ShieldCheck size={17}/>模块级权限</span>
        <span><GitBranch size={17}/>追溯免重复登录</span>
      </div>
    </section>
    <section className="login-panel">
      <div className="login-card">
        <a className="login-panel-brand" href="/" aria-label="CTMC Quality OS">
          <span className="brand-symbol"><Layers3 size={20}/></span>
          <span><b>CTMC QUALITY</b><small>安全质量工作域</small></span>
        </a>
        <span className="login-sequence">Q / AUTH 01</span>
        <div className="login-icon"><LockKeyhole size={22}/></div>
        <h2>登录质检平台</h2>
        <p>使用平台管理员分配的账号进入工作域。</p>
        <form onSubmit={submit}>
          <label><span>用户名</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="请输入用户名"/></label>
          <label><span>密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" placeholder="请输入密码"/></label>
          {error && <div className="login-error" role="alert" aria-live="assertive"><CircleDashed size={16}/>{error}</div>}
          <button disabled={loading || !username || !password}>{loading ? <LoaderCircle className="spin" size={18}/> : <ShieldCheck size={18}/>} {loading ? "正在验证身份…" : "进入质量工作台"}<ArrowRight size={17}/></button>
        </form>
        <small>登录会话 12 小时有效 · 账号权限由平台管理员配置</small>
      </div>
    </section>
  </main>;
}

const permissionLabels: Record<Permission, { name: string; note: string }> = {
  failure: { name: "失效分析平台", note: "案例、图片、知识检索与报告" },
  trace: { name: "质量追溯", note: "跳转并关联追溯平台原账号" },
  machine_watch: { name: "机组运行评价", note: "机组状态、风险与质量数据" },
  assembly_sq: { name: "装配工单 SQ 质检看板", note: "自检、专检与工单进度看板" },
  vehicle: { name: "车载数据服务", note: "轨迹、惯导信号、事件和设备规则" },
  user_admin: { name: "用户与权限管理", note: "创建账号并分配模块权限" },
};

function UserManagement({ currentUser, csrf, onBack, onLogout }: { currentUser: PlatformUser; csrf: string; onBack: () => void; onLogout: () => void }) {
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [passwords, setPasswords] = useState<Record<number, string>>({});
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [query, setQuery] = useState("");
  const [siteFilter, setSiteFilter] = useState<"ALL" | PlatformUser["trace_site_code"]>("ALL");
  const [statusFilter, setStatusFilter] = useState<"ALL" | "ACTIVE" | "INACTIVE">("ALL");
  const [syncFilter, setSyncFilter] = useState<"ALL" | PlatformUser["trace_sync_status"]>("ALL");
  const [draft, setDraft] = useState({
    username: "",
    display_name: "",
    password: "",
    trace_role: "VIEWER" as PlatformUser["trace_role"],
    trace_site_code: "HQ" as PlatformUser["trace_site_code"],
    permissions: ["failure", "trace"] as Permission[],
  });

  async function loadUsers() {
    setLoading(true);
    try {
      const response = await fetch("/platform-api/auth/users", { credentials: "same-origin", cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "账号列表加载失败");
      setUsers(data.users);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "账号列表加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadUsers(); }, []);

  function togglePermission(list: Permission[], permission: Permission) {
    return list.includes(permission) ? list.filter((item) => item !== permission) : [...list, permission];
  }

  function updateLocal(id: number, patch: Partial<PlatformUser>) {
    setUsers((current) => current.map((user) => user.id === id ? { ...user, ...patch } : user));
  }

  async function saveUser(user: PlatformUser) {
    const assignmentError = traceAssignmentError(user.trace_role, user.trace_site_code);
    if (assignmentError) {
      setError(`${user.display_name}：${assignmentError}`);
      return;
    }
    setSavingId(user.id);
    setError("");
    setNotice("");
    try {
      const response = await fetch(`/platform-api/auth/users/${user.id}`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          display_name: user.display_name,
          trace_role: user.trace_role,
          trace_site_code: user.trace_site_code,
          active: user.active,
          permissions: user.permissions,
          password: passwords[user.id] || undefined,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "保存失败");
      setUsers((current) => current.map((item) => item.id === user.id ? data : item));
      setPasswords((current) => ({ ...current, [user.id]: "" }));
      setNotice(data.sync_warning ? `已保存 ${data.display_name}；追溯同步失败，可稍后重试` : `已保存 ${data.display_name}，追溯账号已自动同步`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSavingId(null);
    }
  }

  async function createUser(event: FormEvent) {
    event.preventDefault();
    const assignmentError = traceAssignmentError(draft.trace_role, draft.trace_site_code);
    if (assignmentError) {
      setError(assignmentError);
      return;
    }
    setCreating(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/platform-api/auth/users", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(draft),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "创建账号失败");
      setUsers((current) => [...current, data]);
      setDraft({ username: "", display_name: "", password: "", trace_role: "VIEWER", trace_site_code: "HQ", permissions: ["failure", "trace"] });
      setNotice(data.sync_warning ? `账号 ${data.username} 已在质检平台创建；追溯同步失败，可稍后重试` : `账号 ${data.username} 已在质检与追溯平台同步创建`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建账号失败");
    } finally {
      setCreating(false);
    }
  }

  async function syncHistoricalUsers() {
    setSyncing(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/platform-api/auth/users/reconcile", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "历史账号同步失败");
      if (data.errors?.length) throw new Error(`已同步 ${data.synced.length} 个，${data.errors.length} 个失败`);
      setNotice(`历史账号同步完成，共 ${data.synced.length} 个`);
      await loadUsers();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史账号同步失败");
    } finally {
      setSyncing(false);
    }
  }

  const filteredUsers = users.filter((user) => {
    const keyword = query.trim().toLowerCase();
    const matchesQuery = !keyword || `${user.username} ${user.display_name}`.toLowerCase().includes(keyword);
    const matchesSite = siteFilter === "ALL" || user.trace_site_code === siteFilter;
    const matchesStatus = statusFilter === "ALL" || (statusFilter === "ACTIVE" ? user.active : !user.active);
    const matchesSync = syncFilter === "ALL" || user.trace_sync_status === syncFilter;
    return matchesQuery && matchesSite && matchesStatus && matchesSync;
  });
  const draftAssignmentError = traceAssignmentError(draft.trace_role, draft.trace_site_code);

  return <div className="admin-shell">
    <header className="admin-header">
      <button className="admin-back" onClick={onBack}><Layers3 size={18}/><span><b>CTMC QUALITY</b><small>返回平台首页</small></span></button>
      <div><span><ShieldCheck size={14}/>管理员工作域</span><b>{currentUser.display_name}</b><button onClick={onLogout}><LogOut size={15}/>退出</button></div>
    </header>
    <main className="admin-main">
      <section className="admin-intro">
        <div><span className="admin-kicker">IDENTITY & ACCESS / 01</span><h1>用户与模块权限</h1><p>质检平台作为账号主数据源：账号、密码、姓名、启停状态以及追溯角色和站点会自动同步到质量追溯平台。</p></div>
        <div className="admin-summary"><Users size={22}/><strong>{users.length}</strong><span>平台账号</span><i>{users.filter((user) => user.active).length} 个启用</i></div>
      </section>

      <section className="create-user-card">
        <header><span><UserPlus size={19}/></span><div><h2>新建统一账号</h2><p>一次填写，同时创建质检平台与质量追溯平台账号，用户名和密码保持一致。</p></div></header>
        <form onSubmit={createUser}>
          <label><span>用户名</span><input required value={draft.username} onChange={(event) => setDraft({ ...draft, username: event.target.value })} placeholder="例如 zhangsan"/></label>
          <label><span>姓名</span><input required value={draft.display_name} onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} placeholder="显示姓名"/></label>
          <label><span>初始密码</span><input required minLength={12} type="password" value={draft.password} onChange={(event) => setDraft({ ...draft, password: event.target.value })} placeholder="至少 12 位"/></label>
          <label><span>追溯角色</span><select value={draft.trace_role} onChange={(event) => setDraft({ ...draft, trace_role: event.target.value as PlatformUser["trace_role"] })}><option value="VIEWER">只读用户</option><option value="WAREHOUSE_OPERATOR">仓库操作员</option><option value="ASSEMBLY_OPERATOR">现场装配操作员</option><option value="ADMIN">管理员</option></select></label>
          <label><span>追溯站点</span><select value={draft.trace_site_code} onChange={(event) => setDraft({ ...draft, trace_site_code: event.target.value as PlatformUser["trace_site_code"] })}><option value="HQ" disabled={["WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR"].includes(draft.trace_role)}>总厂</option><option value="XC">新场</option><option value="JC">锦晨</option></select></label>
          <fieldset><legend>模块权限</legend>{(Object.keys(permissionLabels) as Permission[]).map((permission) => <label key={permission}><input type="checkbox" checked={draft.permissions.includes(permission)} onChange={() => setDraft({ ...draft, permissions: togglePermission(draft.permissions, permission) })}/><span><b>{permissionLabels[permission].name}</b><small>{permissionLabels[permission].note}</small></span></label>)}</fieldset>
          <button disabled={creating || Boolean(draftAssignmentError)}>{creating ? <LoaderCircle className="spin" size={17}/> : <UserPlus size={17}/>}创建账号</button>
          <p className={`role-assignment-guide ${draftAssignmentError ? "error" : ""}`}>{draftAssignmentError || "现场角色只能归属新场或锦晨；总部仅用于管理员与只读用户。"}</p>
        </form>
      </section>

      {(error || notice) && <div className={`admin-notice ${error ? "error" : ""}`}>{error || notice}</div>}

      <section className="user-list-section">
        <div className="user-list-title"><div><span>ACCOUNT DIRECTORY</span><h2>账号目录</h2></div><div className="directory-actions"><p>保存后自动同步；停用或改密会注销追溯平台旧会话。</p><button onClick={syncHistoricalUsers} disabled={syncing}>{syncing ? <LoaderCircle className="spin" size={15}/> : <RefreshCw size={15}/>}同步历史账号</button></div></div>
        <div className="directory-toolbar" aria-label="账号筛选">
          <label className="directory-search"><ScanSearch size={16}/><input name="account-directory-search" autoComplete="off" inputMode="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索用户名或姓名" aria-label="搜索用户名或姓名"/></label>
          <label><span>站点</span><select value={siteFilter} onChange={(event) => setSiteFilter(event.target.value as typeof siteFilter)}><option value="ALL">全部站点</option><option value="HQ">总厂</option><option value="XC">新场</option><option value="JC">锦晨</option></select></label>
          <label><span>状态</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}><option value="ALL">全部状态</option><option value="ACTIVE">已启用</option><option value="INACTIVE">已停用</option></select></label>
          <label><span>同步</span><select value={syncFilter} onChange={(event) => setSyncFilter(event.target.value as typeof syncFilter)}><option value="ALL">全部同步状态</option><option value="SYNCED">已同步</option><option value="PENDING">待同步</option><option value="ERROR">同步失败</option></select></label>
          <b>{filteredUsers.length} / {users.length}</b>
        </div>
        {loading ? <div className="admin-loading"><LoaderCircle className="spin"/>正在加载账号…</div> : <div className="user-list">
          {filteredUsers.map((user) => <details className={`user-card ${!user.active ? "inactive" : ""}`} key={user.id}>
            <summary>
              <div className="user-avatar">{user.display_name.slice(0, 1).toUpperCase()}</div>
              <div><h3>{user.display_name}</h3><p>@{user.username} · {user.role === "ADMIN" ? "平台管理员" : "普通用户"}</p></div>
              <span className="user-site">{user.trace_site_code === "HQ" ? "总厂" : user.trace_site_code === "XC" ? "新场" : "锦晨"}</span>
              <span className={`sync-badge ${user.trace_sync_status.toLowerCase()}`}>{user.trace_sync_status === "SYNCED" ? <CheckCircle2 size={13}/> : user.trace_sync_status === "ERROR" ? <CircleDashed size={13}/> : <LoaderCircle size={13}/>} {user.trace_sync_status === "SYNCED" ? "追溯已同步" : user.trace_sync_status === "ERROR" ? "同步失败" : "待同步"}</span>
              <span className={`account-state ${user.active ? "active" : "inactive"}`}>{user.active ? "启用" : "停用"}</span>
              <span className="expand-label">展开编辑</span>
            </summary>
            <div className="user-editor-body">
              <div className="user-fields">
              <label><span>显示姓名</span><input value={user.display_name} onChange={(event) => updateLocal(user.id, { display_name: event.target.value })}/></label>
              <label><span>统一用户名</span><input value={user.username} disabled title="质检平台与追溯平台使用同一用户名"/></label>
              <label><span>追溯角色</span><select value={user.trace_role} onChange={(event) => updateLocal(user.id, { trace_role: event.target.value as PlatformUser["trace_role"] })}><option value="VIEWER">只读用户</option><option value="WAREHOUSE_OPERATOR">仓库操作员</option><option value="ASSEMBLY_OPERATOR">现场装配操作员</option><option value="ADMIN">管理员</option></select></label>
              <label><span>追溯站点</span><select value={user.trace_site_code} onChange={(event) => updateLocal(user.id, { trace_site_code: event.target.value as PlatformUser["trace_site_code"] })}><option value="HQ" disabled={["WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR"].includes(user.trace_role)}>总厂</option><option value="XC">新场</option><option value="JC">锦晨</option></select></label>
              <label className="active-toggle"><span>账号状态</span><span><input type="checkbox" checked={user.active} disabled={user.id === currentUser.id} onChange={(event) => updateLocal(user.id, { active: event.target.checked })}/>{user.active ? "启用" : "停用"}</span></label>
              </div>
              {traceAssignmentError(user.trace_role, user.trace_site_code) && <div className="sync-error">角色配置：{traceAssignmentError(user.trace_role, user.trace_site_code)}</div>}
              {user.trace_sync_error && <div className="sync-error">追溯同步：{user.trace_sync_error}</div>}
              <div className="permission-grid">
              {(Object.keys(permissionLabels) as Permission[]).map((permission) => <label key={permission} className={user.permissions.includes(permission) ? "selected" : ""}>
                <input type="checkbox" checked={user.permissions.includes(permission)} onChange={() => updateLocal(user.id, { permissions: togglePermission(user.permissions, permission) })}/>
                <span><b>{permissionLabels[permission].name}</b><small>{permissionLabels[permission].note}</small></span>
              </label>)}
              </div>
              <details className="password-reset"><summary><LockKeyhole size={14}/>需要重置密码时展开</summary><label><span>新密码</span><input type="password" minLength={12} value={passwords[user.id] || ""} onChange={(event) => setPasswords({ ...passwords, [user.id]: event.target.value })} placeholder="至少 12 位；不修改则留空"/></label></details>
              <footer><span>更新于 {new Date(user.updated_at).toLocaleString("zh-CN", { hour12: false })}</span><button onClick={() => saveUser(user)} disabled={savingId === user.id || Boolean(traceAssignmentError(user.trace_role, user.trace_site_code))}>{savingId === user.id ? <LoaderCircle className="spin" size={16}/> : <Save size={16}/>}保存并同步</button></footer>
            </div>
          </details>)}
          {!filteredUsers.length && <div className="directory-empty">没有符合当前筛选条件的账号</div>}
        </div>}
      </section>
    </main>
  </div>;
}

export default App;
