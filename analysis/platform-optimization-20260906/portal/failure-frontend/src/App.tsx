import { ChangeEvent, DragEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Archive, ArrowLeft, BookOpen, Bot, Box, Check, ChevronRight, ClipboardCheck, Clock3, Database, Download, FileImage, FileText, Gauge, History, ImagePlus, LoaderCircle, Menu, Microscope, Plus, Printer, Search, ShieldCheck, Sparkles, UploadCloud, X } from "lucide-react";
import type { Analysis, AnalysisJob } from "./types";

const API = import.meta.env.VITE_API_BASE || "/failure-analysis-api";
let csrfToken = "";
let csrfRequest: Promise<string> | null = null;

async function getCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (!csrfRequest) {
    csrfRequest = fetch("/platform-api/auth/me", { credentials: "same-origin", cache: "no-store" })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.user || !data.csrf_token) throw new Error("登录已失效，请返回质检平台重新登录");
        csrfToken = data.csrf_token;
        return csrfToken;
      })
      .finally(() => { csrfRequest = null; });
  }
  return csrfRequest;
}
const fields = [
  ["part_name", "零件 / 部件名称", "例如：商标纸输送滚轮", true],
  ["part_code", "图号 / 件号", "例如：YB55-03-214", false],
  ["part_category", "类别", "滚轮、凸轮、轴承、刀具…", false],
  ["machine_model", "设备机型", "例如：ZB45 / YB55", false],
  ["machine_position", "安装部位", "机构、工位与装配位置", false],
  ["material", "材料 / 表面处理", "已知则填写，不确定可留空", false],
  ["service_hours", "累计运行时间", "小时、班次或生产量", false],
  ["reporter", "提交人", "质量 / 维修 / 工艺", false],
] as const;
const coreFields = fields.slice(0, 5);
const optionalFields = fields.slice(5);

const initialForm: Record<string, string> = {
  case_no: `FA-${new Date().toISOString().slice(0, 10).replaceAll("-", "")}-01`, part_name: "", part_code: "", part_category: "", machine_model: "", machine_position: "", material: "", service_hours: "", reporter: "", failure_symptom: "", operating_condition: "", maintenance_history: ""
};

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

function App() {
  const [form, setForm] = useState(initialForm);
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<string[]>([]);
  const [history, setHistory] = useState<Analysis[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sidebar, setSidebar] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState<"new" | "history">("new");
  const [searching, setSearching] = useState(false);
  const [job, setJob] = useState<AnalysisJob | null>(null);

  const filtered = useMemo(() => history.filter((item) => `${item.case_no}${item.part_name}${item.failure_mode}`.toLowerCase().includes(query.toLowerCase())), [history, query]);

  useEffect(() => { apiFetch("/api/analyses").then((r) => r.ok ? r.json() : []).then(setHistory).catch(() => undefined); }, []);
  useEffect(() => { const urls = files.map((file) => URL.createObjectURL(file)); setPreviews(urls); return () => urls.forEach(URL.revokeObjectURL); }, [files]);
  useEffect(() => {
    const pendingJob = sessionStorage.getItem("failure-analysis-job");
    if (!pendingJob) return;
    setLoading(true);
    pollAnalysisJob(pendingJob)
      .then(completeAnalysis)
      .catch((reason) => setError(reason instanceof Error ? reason.message : "分析任务状态查询失败"))
      .finally(() => setLoading(false));
  }, []);

  async function apiFetch(path: string, init: RequestInit = {}) {
    const method = (init.method || "GET").toUpperCase();
    const headers = new Headers(init.headers);
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers.set("X-CSRF-Token", await getCsrfToken());
    return fetch(`${API}${path}`, { ...init, headers, credentials: "same-origin" });
  }

  function completeAnalysis(data: Analysis) {
    sessionStorage.removeItem("failure-analysis-job");
    setJob(null);
    setAnalysis(data);
    setHistory((current) => [data, ...current.filter((item) => item.id !== data.id)]);
  }

  async function pollAnalysisJob(jobId: string): Promise<Analysis> {
    const deadline = Date.now() + 20 * 60_000;
    let consecutiveNetworkErrors = 0;
    while (Date.now() < deadline) {
      await new Promise<void>((resolve) => {
        const finish = () => { window.clearTimeout(timer); document.removeEventListener("visibilitychange", visible); resolve(); };
        const visible = () => { if (!document.hidden) finish(); };
        const timer = window.setTimeout(finish, document.hidden ? 15_000 : 2_000);
        document.addEventListener("visibilitychange", visible);
      });
      let current: AnalysisJob;
      try {
        const response = await apiFetch(`/api/analyze-jobs/${jobId}`, { cache: "no-store" });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.detail || "无法查询分析任务状态");
        }
        current = await response.json() as AnalysisJob;
        consecutiveNetworkErrors = 0;
      } catch (reason) {
        consecutiveNetworkErrors += 1;
        if (consecutiveNetworkErrors >= 6) {
          throw new Error("网络暂时无法连接，但服务器任务仍会继续。稍后刷新页面即可自动恢复进度。");
        }
        setJob((current) => current ? { ...current, message: "网络短暂波动，正在重新连接任务…" } : current);
        continue;
      }
      setJob(current);
      if (current.status === "completed" && current.analysis) return current.analysis;
      if (current.status === "failed") {
        sessionStorage.removeItem("failure-analysis-job");
        throw new Error(current.error || "分析任务未完成，请检查输入后重试。");
      }
    }
    throw new Error("分析仍在后台运行，稍后刷新页面可继续查看进度。");
  }

  function addFiles(next: File[]) {
    setError("");
    const invalid = next.find((file) => !["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size === 0 || file.size > 10 * 1024 * 1024);
    if (invalid) {
      setError(`${invalid.name || "所选文件"} 不是有效的 JPG/PNG/WebP 图片，或超过 10MB。`);
      return;
    }
    const remaining = 6 - files.length;
    if (next.length > remaining) setError(`最多上传 6 张图片，本次仅加入前 ${remaining} 张。`);
    const accepted = next.slice(0, remaining);
    setFiles((current) => [...current, ...accepted]);
  }

  async function openAnalysis(id: string) {
    setError("");
    const response = await apiFetch(`/api/analyses/${id}`);
    if (!response.ok) return;
    setAnalysis(await response.json());
    setSidebar(false);
  }

  async function searchHistory(event?: FormEvent) {
    event?.preventDefault(); setSearching(true); setError("");
    try {
      const response = await apiFetch(`/api/analyses?q=${encodeURIComponent(query)}`);
      if (!response.ok) throw new Error("历史报告检索失败");
      setHistory(await response.json());
    } catch (reason) { setError(reason instanceof Error ? reason.message : "历史报告检索失败"); }
    finally { setSearching(false); }
  }

  async function downloadPdf(item: Analysis) {
    const response = await apiFetch(`/api/analyses/${item.id}/pdf`);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "PDF 生成失败");
    }
    const blob = await response.blob();
    if (blob.type !== "application/pdf" || blob.size < 500) throw new Error("服务器返回的 PDF 文件无效");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${item.case_no}-失效分析报告.pdf`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }

  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    if (!form.part_name.trim() || !form.case_no.trim()) { setError("请先填写案例编号和零件名称。"); return; }
    if (!files.length) { setError("请至少上传 1 张失效图片作为分析证据。"); return; }
    setLoading(true);
    const payload = new FormData();
    Object.entries(form).forEach(([key, value]) => payload.append(key, value));
    files.forEach((file) => payload.append("files", file));
    try {
      const response = await apiFetch(`/api/analyze`, { method: "POST", body: payload });
      const data = await response.json() as AnalysisJob & { detail?: string };
      if (!response.ok) throw new Error(data.detail || "分析失败");
      sessionStorage.setItem("failure-analysis-job", data.id);
      setJob(data);
      completeAnalysis(await pollAnalysisJob(data.id));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "分析失败，请稍后重试。"); }
    finally { setLoading(false); }
  }

  function reset() { setPage("new"); setAnalysis(null); setFiles([]); setForm({ ...initialForm, case_no: `FA-${new Date().toISOString().slice(0, 10).replaceAll("-", "")}-${String(history.length + 1).padStart(2, "0")}` }); setError(""); }

  return <div className="app-shell">
    <aside className={`sidebar ${sidebar ? "is-open" : ""}`}>
      <div className="brand"><div className="brand-mark"><Microscope size={20}/></div><div><b>析因</b><span>FAILURE LAB</span></div><button className="icon-btn sidebar-close" aria-label="关闭导航" onClick={() => setSidebar(false)}><X size={19}/></button></div>
      <button className="new-case" onClick={reset}><Plus size={17}/>新建失效分析</button>
      <button className={`archive-nav ${page === "history" && !analysis ? "active" : ""}`} onClick={() => {setPage("history");setAnalysis(null);setSidebar(false)}}><Archive size={16}/><span>历史失效报告</span><b>{history.length}</b></button>
      <div className="history-title"><span><History size={14}/>最近分析</span><span>{history.length}</span></div>
      <label className="search"><Search size={15}/><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索编号或零件"/></label>
      <div className="case-list">{filtered.map((item) => <button key={item.id} className={`case-item ${analysis?.id === item.id ? "active" : ""}`} onClick={() => openAnalysis(item.id)}><span className="case-icon"><Box size={16}/></span><span className="case-copy"><b>{item.part_name}</b><small>{item.case_no} · {formatTime(item.created_at)}</small></span><ChevronRight size={15}/></button>)}</div>
      <div className="sidebar-foot"><span className="online-dot"/>质量分析<span className="model-pill">Seed 2.0 Pro</span></div>
    </aside>
    {sidebar && <button className="backdrop" onClick={() => setSidebar(false)} aria-label="关闭导航"/>}
    <main className="workspace">
      <header className="topbar"><div className="top-left"><button className="icon-btn mobile-menu" aria-label="打开导航" onClick={() => setSidebar(true)}><Menu size={20}/></button><div><span className="crumb">质量工程 / 失效分析</span><h1>{analysis ? analysis.part_name : page === "history" ? "历史失效报告" : "新建分析任务"}</h1></div></div><div className="top-actions"><span className="secure"><ShieldCheck size={15}/>平台统一身份</span>{(analysis || page === "history") && <button className="ghost-btn" onClick={reset}><Plus size={16}/>新分析</button>}</div></header>
      {analysis?.report ? <ReportView analysis={analysis} onBack={() => {setAnalysis(null);setPage("history")}} onReanalyze={reset} onDownload={() => downloadPdf(analysis)}/> : page === "history" ? <HistoryPage history={history} query={query} setQuery={setQuery} searching={searching} error={error} onSearch={searchHistory} onOpen={openAnalysis}/> : <form className="content" onSubmit={submit}>
        <section className="intro-panel"><div><div className="eyebrow"><span>01</span> 建立证据包</div><h2>先录入关键事实，再生成工程结论。</h2><p>知识检索与图片分析会在后台连续完成，结果统一进入结构化报告。</p></div><div className="process-line four"><div className="process active"><span>1</span>资料录入</div><i/><div className="process"><span>2</span>知识检索</div><i/><div className="process"><span>3</span>综合分析</div><i/><div className="process"><span>4</span>生成报告</div></div></section>
        <section className="section-card"><div className="section-head"><div className="section-number">A</div><div><h3>零部件档案</h3><p>先填写识别零件和安装位置所需的信息。</p></div><label className="case-no"><span>案例编号</span><input required value={form.case_no} onChange={(e) => setForm({...form, case_no: e.target.value})}/></label></div><div className="fields-grid">{coreFields.map(([name, label, placeholder, required]) => <label className="field" key={name}><span>{label}{required && <em>*</em>}</span><input required={required} name={name} value={form[name]} placeholder={placeholder} onChange={(e) => setForm({...form, [name]: e.target.value})}/></label>)}</div><details className="optional-fields"><summary>补充材料、运行时间和提交人 <span>选填</span></summary><div className="fields-grid">{optionalFields.map(([name, label, placeholder]) => <label className="field" key={name}><span>{label}</span><input name={name} value={form[name]} placeholder={placeholder} onChange={(e) => setForm({...form, [name]: e.target.value})}/></label>)}</div></details></section>
        <section className="section-card"><div className="section-head"><div className="section-number">B</div><div><h3>失效现场</h3><p>先描述观察事实，工况与维修历史可按需补充。</p></div></div><div className="narrative-grid"><label className="field wide"><span>失效现象 <em>*</em></span><textarea required rows={4} value={form.failure_symptom} onChange={(e) => setForm({...form, failure_symptom: e.target.value})} placeholder="例如：运行中周期性异响；停机发现滚轮工作面局部剥落，并有金属粉末…"/></label></div><details className="optional-fields"><summary>补充工况与维护记录 <span>选填</span></summary><div className="narrative-grid"><label className="field"><span>失效时工况</span><textarea rows={4} value={form.operating_condition} onChange={(e) => setForm({...form, operating_condition: e.target.value})} placeholder="速度、载荷、温度、润滑、停机前异常、班次变化…"/></label><label className="field"><span>维护 / 更换历史</span><textarea rows={4} value={form.maintenance_history} onChange={(e) => setForm({...form, maintenance_history: e.target.value})} placeholder="最近一次保养、换件、调整、清洁或异常处理记录…"/></label></div></details></section>
        <section className="section-card"><div className="section-head"><div className="section-number copper">C</div><div><h3>图片证据</h3><p>建议包含整体、安装关系、失效区域近景和带标尺照片；最多 6 张。</p></div><span className="file-count">{files.length} / 6</span></div>
          <div className={`dropzone ${dragging ? "dragging" : ""}`} onDragOver={(e) => {e.preventDefault();setDragging(true)}} onDragLeave={() => setDragging(false)} onDrop={(e: DragEvent) => {e.preventDefault();setDragging(false);addFiles(Array.from(e.dataTransfer.files))}}><UploadCloud size={28}/><b>拖入失效图片，或点击选择</b><span>JPG / PNG / WebP · 单张不超过 10MB</span><input aria-label="选择失效图片" type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(e: ChangeEvent<HTMLInputElement>) => addFiles(Array.from(e.target.files || []))}/></div>
          {!!files.length && <div className="preview-grid">{files.map((file, index) => <figure key={`${file.name}-${index}`}><img src={previews[index]} alt={file.name}/><figcaption><FileImage size={14}/><span>{file.name}</span><button type="button" aria-label={`移除 ${file.name}`} onClick={() => setFiles(files.filter((_, i) => i !== index))}><X size={14}/></button></figcaption></figure>)}</div>}
        </section>
        {error && <div className="error-banner"><AlertTriangle size={18}/><span>{error}</span></div>}
        <footer className={`submit-bar ${loading ? "is-running" : ""}`}><div className="submit-status"><Database size={20}/><span><b>{job?.message || "知识检索与综合分析"}</b><small>{loading ? "任务在服务器后台运行，网络切换或刷新不会丢失" : "预计 40–300 秒 · 知识依据与最终结论分别留档"}</small>{loading && <span className="job-progress" aria-label={`分析进度 ${job?.progress || 5}%`}><i style={{ width: `${job?.progress || 5}%` }}/></span>}</span></div><button className="analyze-btn" disabled={loading}>{loading ? <><LoaderCircle className="spin" size={18}/>{job?.progress || 5}%</> : <><Sparkles size={18}/>检索知识并生成报告</>}</button></footer>
      </form>}
    </main>
  </div>;
}

function HistoryPage({ history, query, setQuery, searching, error, onSearch, onOpen }: { history: Analysis[]; query: string; setQuery: (value: string) => void; searching: boolean; error: string; onSearch: (event: FormEvent) => void; onOpen: (id: string) => void }) {
  const [confidenceBand, setConfidenceBand] = useState("ALL");
  const [sortOrder, setSortOrder] = useState("NEWEST");
  const avgConfidence = history.length ? Math.round(history.reduce((sum, item) => sum + item.confidence, 0) / history.length) : 0;
  const sourceCount = history.reduce((sum, item) => sum + (item.knowledge_source_count || 0), 0);
  const caseCounts = history.reduce<Record<string, number>>((counts, item) => ({ ...counts, [item.case_no]: (counts[item.case_no] || 0) + 1 }), {});
  const visibleHistory = [...history].filter((item) => {
    if (confidenceBand === "HIGH") return item.confidence >= 70;
    if (confidenceBand === "MEDIUM") return item.confidence >= 40 && item.confidence < 70;
    if (confidenceBand === "LOW") return item.confidence < 40;
    return true;
  }).sort((a, b) => sortOrder === "CONFIDENCE" ? b.confidence - a.confidence : +new Date(b.created_at) - +new Date(a.created_at));
  return <div className="history-page"><section className="history-hero"><div><div className="eyebrow"><span>ARCHIVE</span> FAILURE INTELLIGENCE</div><h2>历史失效报告库</h2><p>从编号、零件、机型、部位、失效模式和报告正文中检索，让已完成的分析成为下一次判断的工程资产。</p></div><div className="archive-glyph"><Archive size={32}/><span>{history.length}</span><small>当前结果</small></div></section>
    <form className="history-search" onSubmit={onSearch}><Search size={19}/><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索案例编号、零件、机型、部位或失效模式…"/><button disabled={searching}>{searching ? <LoaderCircle className="spin" size={17}/> : <Search size={17}/>}检索报告</button></form>
    <div className="history-filters"><label><span>置信度</span><select value={confidenceBand} onChange={(event) => setConfidenceBand(event.target.value)}><option value="ALL">全部</option><option value="HIGH">70% 及以上</option><option value="MEDIUM">40%–69%</option><option value="LOW">40% 以下</option></select></label><label><span>排序</span><select value={sortOrder} onChange={(event) => setSortOrder(event.target.value)}><option value="NEWEST">最新优先</option><option value="CONFIDENCE">置信度优先</option></select></label><b>{visibleHistory.length} 条</b></div>
    <div className="archive-stats"><article><span>REPORTS</span><strong>{visibleHistory.length}</strong><small>当前显示</small></article><article className={avgConfidence < 60 ? "needs-attention" : ""}><span>CONFIDENCE</span><strong>{avgConfidence}<sup>%</sup></strong><small>{avgConfidence < 60 ? "建议补充证据" : "平均置信度"}</small></article><article><span>KNOWLEDGE</span><strong>{sourceCount}</strong><small>IMA 命中依据</small></article></div>
    {error && <div className="error-banner"><AlertTriangle size={18}/>{error}</div>}
    <section className="archive-table"><div className="archive-row archive-head"><span>案例 / 时间</span><span>零部件</span><span>设备与部位</span><span>失效模式</span><span>知识依据</span><span/></div>{visibleHistory.map((item) => <button className="archive-row" onClick={() => onOpen(item.id)} key={item.id}><span><b>{item.case_no}{caseCounts[item.case_no] > 1 && <i className="version-badge">多版本</i>}</b><small>{formatTime(item.created_at)}</small></span><span><b>{item.part_name}</b></span><span><b>{item.machine_model || "—"}</b><small>{item.machine_position || "未填写部位"}</small></span><span><b>{item.failure_mode}</b><small className={item.confidence < 40 ? "low-confidence" : ""}>置信度 {item.confidence}%</small></span><span><i><BookOpen size={13}/>{item.knowledge_source_count || 0} 条</i></span><span><ChevronRight size={17}/></span></button>)}</section>
    {!visibleHistory.length && <div className="empty-archive"><Archive size={34}/><b>没有找到匹配报告</b><p>调整关键词或置信度筛选，或新建一次失效分析。</p></div>}
  </div>;
}

function ReportView({ analysis, onBack, onReanalyze, onDownload }: { analysis: Analysis; onBack: () => void; onReanalyze: () => void; onDownload: () => Promise<void> }) {
  const r = analysis.report!;
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  async function exportPdf() {
    setExporting(true); setExportError("");
    try { await onDownload(); }
    catch (reason) { setExportError(reason instanceof Error ? reason.message : "PDF 生成失败"); }
    finally { setExporting(false); }
  }
  return <div className="report-wrap">
    <div className="report-toolbar"><button className="back-link" onClick={onBack}><ArrowLeft size={16}/>返回历史报告</button><div><button className="print-btn" onClick={() => window.print()}><Printer size={16}/>打印</button><button className="download-btn primary" disabled={exporting} onClick={exportPdf}>{exporting ? <LoaderCircle className="spin" size={16}/> : <Download size={16}/>}下载 PDF</button></div></div>
    <section className="report-hero" id="report-summary"><div><div className="eyebrow"><span>REPORT</span> {analysis.case_no}</div><h2>{r.failure_mode}</h2><p>{r.executive_summary}</p><div className="report-meta"><span><Box size={15}/>{analysis.part_name}</span><span><Clock3 size={15}/>{formatTime(analysis.created_at)}</span><span><Bot size={15}/>{analysis.model}</span></div></div><div className={`confidence ${r.confidence < 40 ? "low" : ""}`}><Gauge size={22}/><strong>{r.confidence}<sup>%</sup></strong><span>{r.confidence < 40 ? "证据不足，建议补充" : "综合置信度"}</span></div></section>
    <div className="report-layout"><nav className="report-toc" aria-label="报告目录"><b>报告目录</b><a href="#report-summary">结论摘要</a><a href="#report-risk">风险与事实</a><a href="#report-actions">处置计划</a><a href="#report-mechanisms">失效机理</a><a href="#report-causes">根因证据</a><a href="#report-evidence">知识依据</a><a href="#report-missing">待补信息</a><button onClick={onReanalyze}><Plus size={14}/>补充证据后新建分析</button></nav><div className="report-body">
    {analysis.preliminary_report?.summary && <section className="report-card full knowledge-card" id="report-evidence"><header><span className="report-icon"><Database size={18}/></span><div><b>IMA 知识库初步报告</b><small>先于图片综合分析形成 · 适用置信度 {analysis.preliminary_report.confidence}%</small></div><span className="source-badge">{analysis.knowledge_sources?.length || 0} 条依据</span></header><p className="preliminary-summary">{analysis.preliminary_report.summary}</p><details className="evidence-details"><summary>展开知识命中详情</summary><div className="knowledge-findings">{analysis.preliminary_report.knowledge_findings?.map((item, i) => <article key={i}><BookOpen size={15}/><div><b>{item.finding}</b><p>{item.applicability}</p><small>来源：{item.source_title}</small></div></article>)}</div></details></section>}
    <div className="report-grid" id="report-risk"><section className="report-card observations"><header><span className="report-icon"><Microscope size={18}/></span><div><b>已知事实</b><small>来自图片或用户资料，不混入推断</small></div></header><ul className="check-list">{r.observations.map((item, i) => <li key={i}><Check size={15}/>{item}</li>)}</ul></section><section className="report-card risk"><header><span className="report-icon"><AlertTriangle size={18}/></span><div><b>继续使用风险</b><small>停机与质量风险提示</small></div></header><p>{r.risk_statement}</p></section></div>
    <section className="report-card full actions-card" id="report-actions"><header><span className="report-icon"><ClipboardCheck size={18}/></span><div><b>处置与验证计划</b><small>按风险优先级执行并关闭</small></div></header><div className="action-list">{r.actions.map((item, i) => <article key={i}><span className={`priority ${item.priority}`}>{item.priority}</span><div><b>{item.action}</b><p>责任：{item.owner}　·　关闭验证：{item.verification}</p></div></article>)}</div></section>
    <section className="report-card full" id="report-mechanisms"><header><span className="report-icon"><Sparkles size={18}/></span><div><b>失效机理排序</b><small>基于当前证据的概率判断</small></div></header><div className="mechanism-list">{r.mechanisms.map((item, i) => <article key={i}><span className={`prob ${item.probability}`}>{item.probability}</span><div><b>{item.name}</b><p>{item.rationale}</p></div></article>)}</div></section>
    <section className="report-card full" id="report-causes"><header><span className="report-icon"><FileText size={18}/></span><div><b>根因假设与证据</b><small>按人、机、料、法、环线索组织</small></div></header><div className="cause-table"><div className="table-row table-head"><span>类别</span><span>可能根因</span><span>证据与局限</span></div>{r.root_causes.map((item, i) => <div className="table-row" key={i}><span><i>{item.category}</i></span><b>{item.cause}</b><p>{item.evidence}</p></div>)}</div></section>
    {!!r.knowledge_references?.length && <details className="report-card full evidence-details"><summary><BookOpen size={17}/>展开最终结论引用的知识依据</summary><div className="knowledge-findings">{r.knowledge_references.map((item, i) => <article key={i}><BookOpen size={15}/><div><b>{item.source_title}</b><p>{item.used_for}</p><small>{item.caveat}</small></div></article>)}</div></details>}
    <div className="report-grid" id="report-missing"><section className="report-card"><header><span className="report-icon"><Microscope size={18}/></span><div><b>建议补充检测</b><small>用实测缩小不确定性</small></div></header>{r.tests_required.map((item, i) => <div className="test-item" key={i}><b>{item.test}</b><p>{item.purpose} · {item.method}</p></div>)}</section><section className="report-card missing-card"><header><span className="report-icon"><ImagePlus size={18}/></span><div><b>仍缺失的信息</b><small>补齐后建议重新分析</small></div></header><ul className="plain-list">{r.missing_information.map((item, i) => <li key={i}>{item}</li>)}</ul><button onClick={onReanalyze}><Plus size={15}/>补充证据后重新分析</button></section></div>
    {exportError && <div className="error-banner"><AlertTriangle size={18}/><span>{exportError}</span></div>}
    <section className="report-disclaimer"><ShieldCheck size={20}/><p><b>工程边界</b>{r.disclaimer} 本报告不替代材料、尺寸、硬度、金相、无损检测和责任工程师签署。</p><div className="report-export-actions"><button className="print-btn" onClick={() => window.print()}><Printer size={16}/>打印</button><button className="download-btn primary" disabled={exporting} onClick={exportPdf}>{exporting ? <LoaderCircle className="spin" size={16}/> : <Download size={16}/>}下载 PDF</button></div></section>
    </div></div></div>;
}

export default App;
