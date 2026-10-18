import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ArrowUpRight, BookOpen, CheckCheck, CircleAlert, ClipboardList, Download, FileSearch, FileText, History, LoaderCircle, RefreshCw, Save, Search, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { api, download, leaf, projectUrl } from "./api";
import type { AnalysisIssue, AnalysisMetrics, AnalysisReport, AnalysisSearch, Approval, ChapterBrief, DocumentItem, Project, StyleBaseline } from "./api";
import { Empty } from "./ui";

type Props = { project: Project; documents: DocumentItem[]; approvals: Approval[]; unsavedPath?: string; onOpen: (path: string, line?: number) => void; onRefresh: () => Promise<void> };
type Tab = "brief" | "review" | "baseline" | "search";
const tabs: { id: Tab; label: string; icon: typeof BookOpen }[] = [{ id: "brief", label: "写前包", icon: ClipboardList }, { id: "review", label: "本地审稿", icon: ShieldCheck }, { id: "baseline", label: "固定样章", icon: SlidersHorizontal }, { id: "search", label: "历史检索", icon: FileSearch }];
const metricLabels: Record<string, string> = { characters: "字符数", sentences: "句段数", paragraphs: "段落数", mean_sentence_characters: "平均句段字符", long_sentence_count: "长句段数", dialogue_marks_per_1000: "每千字符对话引号数", simile_terms_per_1000: "每千字符比拟词数" };
const statusLabels = { pending: "待处理", resolved: "已处理", ignored: "已忽略" };
const ruleLabels: Record<string, string> = { canned_phrase: "常见套话", long_sentence: "长句段", literal_overlap: "字面重复", baseline_sentence_shift: "样章句长差异" };
const reasonLabels: Record<string, string> = { baseline_missing_stale_or_self_reference: "样章未配置、版本已变化，或当前文稿本身被选作样章", catalog_limit_remaining_not_enumerated: "目录数量达到扫描上限，其余文件尚未逐一检查", file_limit: "达到本次文件数量上限", byte_limit: "达到本次文本大小上限", missing: "文件已缺失", unreadable_or_invalid_utf8: "文件无法读取或不是有效的 UTF-8 文本" };
const sectionLabels: Record<string, string> = { included: "已纳入", missing: "未查到", omitted: "本次省略" };
const message = (error: unknown) => error instanceof Error ? error.message : "操作未完成，请刷新后重试。";
function Limits({ items }: { items?: string[] }) { return items?.length ? <details className="analysis-limits"><summary>检查范围与限制（{items.length}）</summary><ul>{items.map((item, index) => <li key={index}>{item.replace(/\bcoverage\b/g, "覆盖记录").replace(/\bomitted\b/g, "未纳入清单")}</li>)}</ul></details> : null; }
function Metrics({ value }: { value: AnalysisMetrics | null | undefined }) { return value ? <div className="analysis-metrics">{Object.entries(value).map(([key, number]) => <div key={key}><strong>{typeof number === "number" ? Number(number.toFixed(2)).toLocaleString("zh-CN") : "—"}</strong><span>{metricLabels[key] || key}</span></div>)}</div> : null; }
function Warning({ children }: { children: ReactNode }) { return <div className="analysis-warning" role="status"><CircleAlert size={16} /><div>{children}</div></div>; }
function SourceButton({ path, line, onOpen }: { path: string; line?: number; onOpen: Props["onOpen"] }) { return <button type="button" className="analysis-source" onClick={() => onOpen(path, line)}><FileText size={13} /><span>{path}{line ? `:${line}` : ""}</span><ArrowUpRight size={12} /></button>; }
function records(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.filter((item) => !!item && typeof item === "object") : []; }
function Omitted({ items }: { items: unknown }) {
  const entries = records(items);
  return entries.length ? <ul>{entries.map((item, index) => <li key={index}>{typeof item.path === "string" ? `${item.path}：` : ""}{reasonLabels[String(item.reason)] || "未纳入本次检查，请检查来源文件"}</li>)}</ul> : null;
}
function Coverage({ value, onOpen }: { value: AnalysisReport["coverage"]; onOpen: Props["onOpen"] }) {
  const rules = Array.isArray(value.rules) ? value.rules.filter((item): item is string => typeof item === "string") : [];
  return <details className="analysis-limits"><summary>本次覆盖记录</summary>
    <p>检查文稿 {typeof value.source_characters === "number" ? value.source_characters.toLocaleString() : "—"} 字符；对照 {typeof value.history_scanned === "number" ? value.history_scanned : "—"} 份历史章节。</p>
    <p>已执行：{rules.map((rule) => ruleLabels[rule] || "其他文字规则").join("、") || "未记录"}。</p>
    {records(value.skipped).map((item, index) => <p key={index}>未执行{ruleLabels[String(item.rule)] || "部分检查"}：{reasonLabels[String(item.reason)] || "所需材料不足"}。</p>)}
    {value.issue_limit_reached === true && <p>已达到单次问题数量上限（{typeof value.issue_limit === "number" ? value.issue_limit : "—"} 项），清单可能未列出全部命中。</p>}
    {records(value.history_sources).length > 0 && <details><summary>本次对照的历史章节</summary><ul>{records(value.history_sources).map((item, index) => <li key={index}>{typeof item.path === "string" && <SourceButton path={item.path} onOpen={onOpen} />}{typeof item.hash === "string" && <span> · 版本 {item.hash.slice(0, 12)}</span>}</li>)}</ul></details>}
    {records(value.omitted).length > 0 && <details><summary>未纳入的文件</summary><Omitted items={value.omitted} /></details>}
  </details>;
}

export default function AnalysisWorkspace({ project, documents, approvals, unsavedPath, onOpen, onRefresh }: Props) {
  const base = `${projectUrl(project.novel_id)}/analysis`;
  const [tab, setTab] = useState<Tab>("brief");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [baseline, setBaseline] = useState<StyleBaseline | null>(null);
  const [samplePaths, setSamplePaths] = useState<string[]>([]);
  const [baselineBusy, setBaselineBusy] = useState(false);
  const [planPath, setPlanPath] = useState("");
  const [mode, setMode] = useState<"compact" | "full">("compact");
  const [brief, setBrief] = useState<ChapterBrief | null>(null);
  const [briefBusy, setBriefBusy] = useState(false);
  const [documentPath, setDocumentPath] = useState("");
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const [historyPath, setHistoryPath] = useState("");
  const [localHistory, setLocalHistory] = useState<string[]>([]);
  const [issueFilter, setIssueFilter] = useState<"all" | AnalysisIssue["status"]>("pending");
  const [markBusy, setMarkBusy] = useState("");
  const [query, setQuery] = useState("");
  const [searchedQuery, setSearchedQuery] = useState("");
  const [search, setSearch] = useState<AnalysisSearch | null>(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [refreshBusy, setRefreshBusy] = useState(false);
  const alive = useRef(true);
  const requests = useRef({ baseline: 0, report: 0, brief: 0, search: 0 });
  const samplePathsRef = useRef(samplePaths); samplePathsRef.current = samplePaths;
  const reportRef = useRef(report); reportRef.current = report;
  const sourceDocuments = useMemo(() => documents.filter((item) => /^(drafts|chapters)\/.+\.md$/.test(item.path)).sort((a, b) => a.path.localeCompare(b.path)), [documents]);
  const plans = documents.filter((item) => /^outlines\/approved\/ch[0-9]{3,}\.md$/.test(item.path) && approvals.some((proposal) => proposal.target === item.path && proposal.state === "applied" && proposal.after_hash === item.hash));
  const reportPaths = [...new Set([...localHistory, ...documents.filter((item) => /^reviews\/analysis-[0-9a-f]{32}\.json$/.test(item.path)).map((item) => item.path)])].sort().reverse();
  const selectedDocument = sourceDocuments.find((item) => item.path === documentPath);
  const selectedPlan = plans.find((item) => item.path === planPath);
  const reportCurrent = report ? documents.find((item) => item.path === report.source.path) : null;
  const reportStale = !!report && (report.stale || !reportCurrent || reportCurrent.hash !== report.source.hash || (!!baseline && report.baseline?.hash !== baseline.hash) || Object.entries(report.dependencies || {}).some(([path, hash]) => documents.find((item) => item.path === path)?.hash !== hash));
  const baselineStale = !!baseline && (baseline.stale || baseline.samples.some((item) => documents.find((document) => document.path === item.path)?.hash !== item.hash));

  useEffect(() => {
    alive.current = true;
    const request = ++requests.current.baseline;
    setBaselineBusy(true);
    void api<StyleBaseline>(`${base}/baseline`).then((value) => {
      if (!alive.current || request !== requests.current.baseline) return;
      setBaseline(value); setSamplePaths(value.samples.map((item) => item.path));
    }).catch((failure) => { if (alive.current && request === requests.current.baseline) setError(message(failure)); }).finally(() => { if (alive.current && request === requests.current.baseline) setBaselineBusy(false); });
    return () => { alive.current = false; for (const key of Object.keys(requests.current) as (keyof typeof requests.current)[]) requests.current[key] += 1; };
  }, [base]);

  async function saveBaseline() {
    if (!baseline || baselineBusy || refreshBusy || !samplePaths.length || samplePaths.length > 10) return;
    const snapshot = [...samplePaths];
    const selected = snapshot.map((path) => sourceDocuments.find((item) => item.path === path));
    if (selected.some((item) => !item)) { setError("选定样章已缺失，请先移除缺失文件，再保存。"); return; }
    if (snapshot.includes(unsavedPath || "")) { setError("样章有未保存修改，请先保存正文，再更新基线。"); return; }
    const request = ++requests.current.baseline;
    setBaselineBusy(true); setError(""); setNotice("");
    try {
      const value = await api<StyleBaseline>(`${base}/baseline`, "PUT", { samples: selected.map((item) => ({ path: item!.path, hash: item!.hash })), expected_hash: baseline.hash });
      if (!alive.current || request !== requests.current.baseline) return;
      setBaseline(value);
      if (JSON.stringify(samplePathsRef.current) === JSON.stringify(snapshot)) setSamplePaths(value.samples.map((item) => item.path));
      setNotice("固定样章已显式保存。后续文件变化不会自动改写基线。");
    } catch (failure) { if (alive.current && request === requests.current.baseline) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.baseline) setBaselineBusy(false); }
  }
  async function analyze() {
    if (!selectedDocument || reportBusy) return;
    if (unsavedPath === selectedDocument.path) { setError("请先保存当前文稿，再分析已保存版本。"); return; }
    const snapshot = selectedDocument;
    const request = ++requests.current.report;
    setReportBusy(true); setMarkBusy(""); setError(""); setNotice("");
    try {
      const value = await api<AnalysisReport>(`${base}/reports`, "POST", { path: snapshot.path, expected_hash: snapshot.hash });
      if (!alive.current || request !== requests.current.report) return;
      setReport(value); setHistoryPath(value.path); setLocalHistory((items) => [...new Set([value.path, ...items])]);
      await onRefresh();
    } catch (failure) { if (alive.current && request === requests.current.report) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.report) setReportBusy(false); }
  }
  async function loadReport(path: string) {
    const request = ++requests.current.report;
    setHistoryPath(path); setReport(null); setMarkBusy("");
    if (!path) { setReportBusy(false); return; }
    setReportBusy(true); setError("");
    try {
      const value = await api<AnalysisReport>(`${base}/report?path=${encodeURIComponent(path)}`);
      if (!alive.current || request !== requests.current.report) return;
      setReport(value);
    } catch (failure) { if (alive.current && request === requests.current.report) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.report) setReportBusy(false); }
  }
  async function mark(issue: AnalysisIssue, status: AnalysisIssue["status"]) {
    if (!report || markBusy || reportBusy || refreshBusy) return;
    if (status === "resolved" && reportStale) { setError("报告所用材料已变化，请重新分析后再标记已处理。"); return; }
    const snapshot = report;
    const request = requests.current.report;
    setMarkBusy(issue.id); setError("");
    try {
      const value = await api<AnalysisReport>(`${base}/mark`, "POST", { report_path: snapshot.path, report_hash: snapshot.hash, issue_id: issue.id, status });
      if (!alive.current || request !== requests.current.report || reportRef.current?.path !== snapshot.path) return;
      setReport(value);
    } catch (failure) { if (alive.current && request === requests.current.report) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.report) setMarkBusy(""); }
  }
  async function buildBrief() {
    if (!selectedPlan || briefBusy) return;
    const request = ++requests.current.brief;
    setBriefBusy(true); setBrief(null); setError("");
    try {
      const value = await api<ChapterBrief>(`${base}/brief`, "POST", { plan_path: selectedPlan.path, expected_hash: selectedPlan.hash, mode });
      if (alive.current && request === requests.current.brief) setBrief(value);
    } catch (failure) { if (alive.current && request === requests.current.brief) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.brief) setBriefBusy(false); }
  }
  async function searchHistory(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || searchBusy) return;
    const snapshot = query.trim();
    const request = ++requests.current.search;
    setSearchBusy(true); setSearch(null); setError(""); setSearchedQuery(snapshot);
    try {
      const value = await api<AnalysisSearch>(`${base}/search?query=${encodeURIComponent(snapshot)}&limit=10`);
      if (alive.current && request === requests.current.search) setSearch(value);
    } catch (failure) { if (alive.current && request === requests.current.search) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.search) setSearchBusy(false); }
  }
  async function refreshView() {
    if (refreshBusy || baselineBusy || reportBusy || markBusy) return;
    setRefreshBusy(true); setError("");
    const currentReport = reportRef.current;
    const reportRequest = requests.current.report;
    const baselineRequest = ++requests.current.baseline;
    try {
      await onRefresh();
      const value = await api<StyleBaseline>(`${base}/baseline`);
      if (!alive.current) return;
      if (baselineRequest === requests.current.baseline) setBaseline(value);
      if (currentReport) {
        const updated = await api<AnalysisReport>(`${base}/report?path=${encodeURIComponent(currentReport.path)}`);
        if (alive.current && reportRequest === requests.current.report && reportRef.current?.path === currentReport.path) setReport(updated);
      }
    } catch (failure) { if (alive.current) setError(message(failure)); }
    finally { if (alive.current) setRefreshBusy(false); }
  }
  const samples = [...sourceDocuments, ...((baseline?.samples || []).filter((item) => !sourceDocuments.some((document) => document.path === item.path)).map((item) => ({ path: item.path, hash: item.hash, kind: "missing", bytes: 0 })))];
  const shownIssues = report?.issues.filter((item) => issueFilter === "all" || item.status === issueFilter) || [];
  return <div className="analysis-workspace scroll-area">
    <div className="page-heading compact-heading"><div><div className="eyebrow">A CLOSER LOOK AT YOUR WORDS</div><h1>写作检查</h1><p>准备写前材料，核对已保存文稿，保留每一处证据。</p></div><button type="button" className="button secondary small" onClick={() => void refreshView()} disabled={refreshBusy || baselineBusy || reportBusy || !!markBusy}><RefreshCw size={14} className={refreshBusy ? "spin" : ""} />刷新状态</button></div>
    <div className="analysis-local-note"><CheckCheck size={15} />本工作区使用本地资料与规则，不调用模型；检查结果供作者判断。</div>
    <div className="analysis-tabs" role="tablist" aria-label="写作检查工具">{tabs.map(({ id, label, icon: Icon }) => <button type="button" key={id} id={`analysis-tab-${id}`} aria-controls={`analysis-panel-${id}`} role="tab" aria-selected={tab === id} className={tab === id ? "selected" : ""} onClick={() => { setTab(id); setError(""); setNotice(""); }}><Icon size={16} />{label}</button>)}</div>
    {error && <div className="analysis-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="关闭检查错误">×</button></div>}
    {notice && <p className="analysis-success" role="status">{notice}</p>}
    <section className="analysis-panel" role="tabpanel" id={`analysis-panel-${tab}`} aria-labelledby={`analysis-tab-${tab}`}>
      {tab === "brief" && <>
        <div className="analysis-section-heading"><div><h2>从已批准计划开始</h2><p>汇集当前章计划、承接状态、相关规划和检索证据，列出缺失及省略的材料。实际起草会按助手中选定的模式重建最新包。</p></div><ClipboardList size={21} /></div>
        <div className="analysis-controls"><label className="analysis-field grow">已批准并应用的章计划<select value={planPath} onChange={(event) => { setPlanPath(event.target.value); requests.current.brief += 1; setBrief(null); setBriefBusy(false); }}><option value="">明确选择一份章计划</option>{plans.map((item) => <option key={item.path} value={item.path}>{item.path}</option>)}</select></label><label className="analysis-field">材料范围<select value={mode} onChange={(event) => { setMode(event.target.value as "compact" | "full"); requests.current.brief += 1; setBrief(null); setBriefBusy(false); }}><option value="compact">精简写前包</option><option value="full">完整写前包</option></select></label><button type="button" className="button primary small" disabled={!selectedPlan || briefBusy} onClick={() => void buildBrief()}>{briefBusy ? <LoaderCircle className="spin" size={15} /> : <ClipboardList size={15} />}整理写前包</button></div>
        {!plans.length && <Warning>未查到可选的已应用章计划。请先到创作规划提取章计划，并在审批中心批准、应用。</Warning>}
        {brief && <><div className="analysis-result-heading"><span>{brief.chapter ? `第 ${brief.chapter} 章` : "本章"} · {brief.budget ? `${brief.budget.characters.toLocaleString()} / ${brief.budget.limit.toLocaleString()} 字符` : "写前材料"}</span><button type="button" className="button secondary small" onClick={() => download(`写前包-${brief.chapter || "本章"}.md`, brief.text)}><Download size={14} />导出 Markdown</button></div>
          {!!brief.missing.length && <Warning><strong>未查到的材料</strong><ul>{brief.missing.map((item) => <li key={item.id}>{item.label}：{item.reason}</li>)}</ul></Warning>}
          {!!brief.omitted.length && <details className="analysis-limits"><summary>本次省略（{brief.omitted.length}）</summary><ul>{brief.omitted.map((item) => <li key={`${item.id}-${item.paths.join()}`}>{item.label}：{item.reason}</li>)}</ul></details>}
          <div className="analysis-brief-sections">{brief.sections.map((section) => <details key={section.id} open={section.status === "included"}><summary><strong>{section.label}</strong><span className="tag">{sectionLabels[section.status] || section.status}{section.truncated ? " · 已截取" : ""}</span></summary><div>{section.paths.map((path) => <SourceButton key={path} path={path} onOpen={onOpen} />)}<pre>{section.content || "本次未纳入正文内容"}</pre></div></details>)}</div><Limits items={brief.limitations} /></>}
        {!brief && !briefBusy && <Empty compact icon={<ClipboardList size={26} />} title="先读清楚，再动笔" description="选择已应用章计划，生成本章材料清单。整理写前包不会批准新情节或写入正式记忆。" />}
      </>}
      {tab === "review" && <>
        <div className="analysis-section-heading"><div><h2>检查已保存的文字</h2><p>本地规则筛查句式与明显字面问题。处理标记只更新报告。</p></div><ShieldCheck size={21} /></div>
        <div className="analysis-controls"><label className="analysis-field grow">文稿<select value={documentPath} onChange={(event) => { setDocumentPath(event.target.value); requests.current.report += 1; setReport(null); setReportBusy(false); setMarkBusy(""); setHistoryPath(""); }}><option value="">选择已保存草稿或正式章节</option>{sourceDocuments.map((item) => <option key={item.path} value={item.path}>{item.path}{item.path === unsavedPath ? " · 有未保存修改" : ""}</option>)}</select></label><button type="button" className="button primary small" disabled={!selectedDocument || reportBusy || selectedDocument.path === unsavedPath} onClick={() => void analyze()}>{reportBusy ? <LoaderCircle size={15} className="spin" /> : <ShieldCheck size={15} />}分析已保存版本</button></div>
        {unsavedPath === documentPath && <Warning>当前文稿有未保存修改。请先回到编辑器保存，再进行分析。<SourceButton path={documentPath} onOpen={onOpen} /></Warning>}
        <label className="analysis-field analysis-history"><span><History size={14} />重开历史报告</span><select value={historyPath} onChange={(event) => void loadReport(event.target.value)}><option value="">选择已有报告</option>{reportPaths.map((path) => <option key={path} value={path}>{leaf(path)}</option>)}</select></label>
        {report && <><div className="analysis-report-source"><SourceButton path={report.source.path} onOpen={onOpen} /><code title={report.source.hash}>分析版本 {report.source.hash.slice(0, 12)}</code><span className="tag">{reportStale ? "报告已过期" : "已保存版本"}</span></div>
          {reportStale && <Warning>正文、固定样章或依赖版本已变化。下方原句属于报告生成时的版本，请重新分析后判断当前文字。{report.stale_reasons?.length > 0 && <ul>{report.stale_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}</Warning>}
          <Metrics value={report.metrics} /><p className="analysis-caption">句段依标点切分；对话与比拟词来自规则估计，数字不代表文学质量评分。{report.baseline?.comparison_available ? "本次已按固定样章进行文风指标对照。" : "本次未进行文风对照：样章未配置、已变化，或待检查文稿本身被选为样章。"}</p>
          <div className="analysis-issue-toolbar"><h3>原句与建议 <span>{report.issues.length}</span></h3><select aria-label="筛选问题状态" value={issueFilter} onChange={(event) => setIssueFilter(event.target.value as typeof issueFilter)}><option value="all">全部状态</option>{Object.entries(statusLabels).map(([id, label]) => <option key={id} value={id}>{label}（{report.issues.filter((item) => item.status === id).length}）</option>)}</select></div>
          {shownIssues.length ? <div className="analysis-issues">{shownIssues.map((issue) => <article className={`analysis-issue ${issue.status}`} key={issue.id}><div className="analysis-issue-heading"><span className="tag">{ruleLabels[issue.rule] || "文字检查"}</span><span>{issue.severity === "warning" ? "优先核对" : "审阅提示"}</span><SourceButton path={report.source.path} line={issue.start_line} onOpen={onOpen} /></div><p className="analysis-line-label">报告原文 · 第 {issue.start_line}{issue.end_line !== issue.start_line ? `–${issue.end_line}` : ""} 行</p><blockquote>{issue.quote}</blockquote><h4>{issue.message}</h4><p>{issue.suggestion}</p><div className="analysis-issue-actions"><label>处理状态<select value={issue.status} disabled={!!markBusy || refreshBusy || reportBusy} onChange={(event) => void mark(issue, event.target.value as AnalysisIssue["status"])}>{Object.entries(statusLabels).map(([id, label]) => <option key={id} value={id} disabled={id === "resolved" && reportStale}>{label}{id === "resolved" && reportStale ? "（需重新分析）" : ""}</option>)}</select></label>{markBusy === issue.id && <LoaderCircle size={14} className="spin" />}<small>仅标记报告，不修改正文</small></div></article>)}</div> : <Empty compact icon={<CheckCheck size={24} />} title={report.issues.length ? "当前筛选下没有问题" : "本次规则没有命中"} description="这不代表全文不存在连续性、情节重复或文风问题；请结合原文审阅。" />}
          <Coverage value={report.coverage} onOpen={onOpen} /><Limits items={report.limitations} />
        </>}
        {!report && !reportBusy && <Empty compact icon={<ShieldCheck size={26} />} title="保留原句的本地检查" description="选择一份已保存文稿。报告将单独保存，可标记问题并在以后重新打开。" />}
      </>}
      {tab === "baseline" && <>
        <div className="analysis-section-heading"><div><h2>由你选定文风参照</h2><p>明确选择章节或草稿作为样章；系统不会自动替换，文件变化后需再次保存。</p></div><SlidersHorizontal size={21} /></div>
        {baseline?.configured ? <><div className="analysis-result-heading"><span className="tag">{baselineStale ? "样章版本已变化" : "固定样章已配置"}</span><span>{baseline.samples.length} 份样章 · {baseline.hash?.slice(0, 12)}</span></div>{baselineStale && <Warning>至少一份样章已变更或缺失。原基线保留；下方勾选后点击保存，才会按当前版本更新。</Warning>}<Metrics value={baseline.metrics} /></> : <Warning>尚未选定固定样章。本地审稿可以继续，文风对照暂不执行。</Warning>}
        <div className="analysis-samples">{samples.length ? samples.map((item) => { const fixed = baseline?.samples.find((entry) => entry.path === item.path); const changed = !!fixed && fixed.hash !== documents.find((document) => document.path === item.path)?.hash; return <label key={item.path} className="analysis-sample"><input type="checkbox" checked={samplePaths.includes(item.path)} disabled={baselineBusy || (!samplePaths.includes(item.path) && samplePaths.length >= 10)} onChange={(event) => setSamplePaths((items) => event.target.checked ? [...items, item.path] : items.filter((path) => path !== item.path))} /><span><strong>{item.path}</strong><small>{item.kind === "missing" ? "文件已缺失，请取消选择" : fixed ? changed ? "当前内容已变化，保存后才会更新基线" : "与保存的样章版本一致" : "尚未设为样章"}{item.path === unsavedPath ? " · 有未保存修改" : ""}</small></span>{fixed && <code>{fixed.hash.slice(0, 10)}</code>}</label>; }) : <Empty compact title="还没有可选样章" description="先保存一份草稿或正式章节，再由你明确选定。" />}</div>
        <div className="analysis-save-row"><span>{samplePaths.length ? `已选择 ${samplePaths.length} / 10 份；保存时绑定当前文件版本` : "请明确选择 1–10 份样章"}</span><button type="button" className="button primary small" disabled={!baseline || baselineBusy || refreshBusy || !samplePaths.length || samplePaths.length > 10 || samplePaths.includes(unsavedPath || "")} onClick={() => void saveBaseline()}>{baselineBusy ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}保存样章选择</button></div><Limits items={baseline?.limitations} />
      </>}
      {tab === "search" && <>
        <div className="analysis-section-heading"><div><h2>检索正式章节</h2><p>按本地词项与字面相关程度查找历史原句，结果保留文件版本和行号。</p></div><FileSearch size={21} /></div>
        <form className="analysis-search-form" onSubmit={(event) => void searchHistory(event)}><label className="analysis-field grow">检索词<input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={200} placeholder="输入人物、地点、物品或需要核对的原句" required /></label><button className="button primary small" type="submit" disabled={searchBusy || !query.trim()}>{searchBusy ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}查找历史</button></form>
        <p className="analysis-caption">此工具不做语义检索，不能判断情节是否重复或事实是否矛盾。</p>
        {search && <><div className="analysis-result-heading"><span>“{searchedQuery}” · 扫描 {search.scanned} 份正式章节 · 命中 {search.results.length} 处</span></div><div className="analysis-search-results">{search.results.map((item, index) => <article key={`${item.path}-${item.start_line}-${index}`}><div><SourceButton path={item.path} line={item.start_line} onOpen={onOpen} /><span className="tag">字面相关 {Number(item.score.toFixed(2))}</span></div><blockquote>{item.quote}</blockquote><small>第 {item.start_line}–{item.end_line} 行 · 版本 {item.hash.slice(0, 12)}</small></article>)}</div>{!search.results.length && <Empty compact icon={<FileSearch size={24} />} title="本次未查到字面匹配" description="只检查了本次扫描范围；你可以尝试更短的原句、人物名或物品名。" />}{search.omitted.length > 0 && <details className="analysis-limits"><summary>未纳入本次检索（{search.omitted.length}）</summary><Omitted items={search.omitted} /></details>}<Limits items={search.limitations} /></>}
        {!search && !searchBusy && <Empty compact icon={<Search size={26} />} title="从历史原句核对" description="仅搜索正式章节，不把候选草稿当作已发生事实。" />}
      </>}
    </section><p className="analysis-footer"><ShieldCheck size={13} />写前材料与检查报告不会改写正式正文、章计划或故事记忆。</p>
  </div>;
}
