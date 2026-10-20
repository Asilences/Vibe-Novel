import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ArrowUpRight, BookOpen, CheckCheck, CircleAlert, ClipboardList, Download, FileSearch, FileText, History, LoaderCircle, RefreshCw, Save, Search, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { api, download, leaf, projectUrl } from "./api";
import type { AnalysisIssue, AnalysisMetrics, AnalysisReport, AnalysisSearch, AnalysisVerification, Approval, ChapterBrief, DocumentItem, Project, SemanticStatus, StyleBaseline } from "./api";
import { Empty } from "./ui";
import AnalysisToolkitWorkspace from "./AnalysisToolkitWorkspace";
import { selectionChanged } from "./pendingEdits";

type Props = { project: Project; documents: DocumentItem[]; approvals: Approval[]; currentDocument?: DocumentItem; unsavedPath?: string; onOpen: (path: string, line?: number, expectedHash?: string) => void; onRefresh: () => Promise<void>; onDirtyChange?: (dirty: boolean) => void };
type Tab = "brief" | "review" | "baseline" | "search" | "toolkit" | "structure";
const tabs: { id: Tab; label: string; icon: typeof BookOpen }[] = [{ id: "brief", label: "写前包", icon: ClipboardList }, { id: "review", label: "本地审稿", icon: ShieldCheck }, { id: "baseline", label: "固定样章", icon: SlidersHorizontal }, { id: "search", label: "历史检索", icon: FileSearch }, { id: "toolkit", label: "用词与档案", icon: BookOpen }, { id: "structure", label: "跨章线索", icon: History }];
const metricLabels: Record<string, string> = { characters: "字符数", sentences: "句段数", paragraphs: "段落数", mean_sentence_characters: "平均句段字符", mean_paragraph_characters: "平均段落字符", sentence_length_variation: "句长变异系数", short_sentences_percent: "短句占比（%）", long_sentence_count: "长句段数", dialogue_marks_per_1000: "每千字符引号数", simile_terms_per_1000: "每千字符比拟词数" };
const statusLabels = { pending: "待处理", resolved: "已处理", ignored: "已忽略" };
const ruleLabels: Record<string, string> = { canned_phrase: "常见套话", long_sentence: "长句段", literal_overlap: "字面重复", baseline_sentence_shift: "样章句长差异", baseline_dialogue_shift: "样章引号密度差异", baseline_simile_shift: "样章比拟词密度差异", baseline_paragraph_shift: "样章段落长度差异", baseline_rhythm_shift: "样章句长变化差异", lexicon_alias: "统一用词核对", lexicon_forbidden: "不推荐用词", lexicon_required: "作者必需词项", style_avoid: "作者文风避用表达" };
const reasonLabels: Record<string, string> = { baseline_missing_stale_or_self_reference: "样章未配置、版本已变化，或当前文稿本身被选作样章", baseline_missing: "尚未配置固定样章", baseline_self_reference_only: "排除当前文稿后，没有剩余固定样章", baseline_remaining_sample_stale: "剩余固定样章已变化或缺失", catalog_limit_remaining_not_enumerated: "目录数量达到扫描上限，其余文件尚未逐一检查", file_limit: "达到本次文件数量上限", byte_limit: "达到本次文本大小上限", missing: "文件已缺失", unreadable_or_invalid_utf8: "文件无法读取或不是有效的 UTF-8 文本" };
const sectionLabels: Record<string, string> = { included: "已纳入", missing: "未查到", omitted: "本次省略" };
const message = (error: unknown) => error instanceof Error ? error.message : "操作未完成，请刷新后重试。";
function Limits({ items }: { items?: string[] }) { return items?.length ? <details className="analysis-limits"><summary>检查范围与限制（{items.length}）</summary><ul>{items.map((item, index) => <li key={index}>{item.replace(/\bcoverage\b/g, "覆盖记录").replace(/\bomitted\b/g, "未纳入清单")}</li>)}</ul></details> : null; }
function Metrics({ value }: { value: AnalysisMetrics | null | undefined }) { return value ? <div className="analysis-metrics">{Object.entries(value).map(([key, number]) => <div key={key}><strong>{typeof number === "number" ? Number(number.toFixed(2)).toLocaleString("zh-CN") : "—"}</strong><span>{metricLabels[key] || key}</span></div>)}</div> : null; }
function Warning({ children }: { children: ReactNode }) { return <div className="analysis-warning" role="status"><CircleAlert size={16} /><div>{children}</div></div>; }
function SourceButton({ path, line, expectedHash, stale, onOpen }: { path: string; line?: number; expectedHash?: string; stale?: boolean; onOpen: Props["onOpen"] }) { return <button type="button" className="analysis-source" disabled={stale} title={stale ? "来源版本已变化；请重新检查或检索后打开" : expectedHash ? `仅打开匹配版本 ${expectedHash.slice(0, 12)}` : "打开来源"} onClick={() => onOpen(path, line, expectedHash)}><FileText size={13} /><span>{path}{line ? `:${line}` : ""}{stale ? " · 来源已失效" : ""}</span><ArrowUpRight size={12} /></button>; }
function records(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.filter((item) => !!item && typeof item === "object") : []; }
function Omitted({ items }: { items: unknown }) {
  const entries = records(items);
  return entries.length ? <ul>{entries.map((item, index) => <li key={index}>{typeof item.path === "string" ? `${item.path}：` : ""}{reasonLabels[String(item.reason)] || "未纳入本次检查，请检查来源文件"}</li>)}</ul> : null;
}
function Coverage({ value, documents, onOpen }: { value: AnalysisReport["coverage"]; documents: DocumentItem[]; onOpen: Props["onOpen"] }) {
  const rules = Array.isArray(value.rules) ? value.rules.filter((item): item is string => typeof item === "string") : [];
  return <details className="analysis-limits"><summary>本次覆盖记录</summary>
    <p>检查文稿 {typeof value.source_characters === "number" ? value.source_characters.toLocaleString() : "—"} 字符；对照 {typeof value.history_scanned === "number" ? value.history_scanned : "—"} 份历史章节。</p>
    <p>已执行：{rules.map((rule) => ruleLabels[rule] || "其他文字规则").join("、") || "未记录"}。</p>
    {records(value.skipped).map((item, index) => <p key={index}>未执行{ruleLabels[String(item.rule)] || "部分检查"}：{reasonLabels[String(item.reason)] || "所需材料不足"}。</p>)}
    {value.issue_limit_reached === true && <p>已达到单次问题数量上限（{typeof value.issue_limit === "number" ? value.issue_limit : "—"} 项），清单可能未列出全部命中。</p>}
    {records(value.history_sources).length > 0 && <details><summary>本次对照的历史章节</summary><ul>{records(value.history_sources).map((item, index) => <li key={index}>{typeof item.path === "string" && <SourceButton path={item.path} expectedHash={typeof item.hash === "string" ? item.hash : undefined} stale={typeof item.hash !== "string" || documents.find((document) => document.path === item.path)?.hash !== item.hash} onOpen={onOpen} />}{typeof item.hash === "string" && <span> · 版本 {item.hash.slice(0, 12)}</span>}</li>)}</ul></details>}
    {!!value.rule_counts && typeof value.rule_counts === "object" && <details><summary>各规则的发现与截断</summary><ul>{Object.entries(value.rule_counts).map(([rule, raw]) => { const item = raw as Record<string, unknown>; return <li key={rule}>{ruleLabels[rule] || rule}：发现 {String(item.found ?? "—")} 项，展示 {String(item.returned ?? "—")} 项，截断 {String(item.truncated ?? "—")} 项</li>; })}</ul></details>}
    {records(value.omitted).length > 0 && <details><summary>未纳入的文件</summary><Omitted items={value.omitted} /></details>}
  </details>;
}
function Verification({ value }: { value: AnalysisVerification & { previous_issues: AnalysisIssue[] } }) {
  const labels = { remaining: "规则仍命中", not_found: "本次未再命中", unchecked: "本次未能验证" };
  return <details className="analysis-limits" open><summary>逐项规则复查 · {value.results.length} 条旧证据</summary><p>旧报告及作者标记保留。未再命中不能认定语义、连续性或情节问题已修好。</p><p>对照旧报告 {leaf(value.previous.path)} · 版本 {value.previous.hash.slice(0, 12)}</p><div className="analysis-verification-list">{value.results.map((item) => <article key={item.issue_id}><div><strong>{ruleLabels[item.rule] || "文字检查"}</strong><span className="tag">{labels[item.status]}</span></div><blockquote>{value.previous_issues.find((issue) => issue.id === item.issue_id)?.quote || "旧报告证据"}</blockquote><p>{item.reason}</p><small>旧作者标记：{statusLabels[item.previous_status]}</small></article>)}</div><Limits items={value.limitations} /></details>;
}
function BaselineComparison({ value, documents, onOpen }: { value: AnalysisReport["baseline"]; documents: DocumentItem[]; onOpen: Props["onOpen"] }) {
  const raw = value?.comparison;
  if (!raw || typeof raw !== "object") return value?.comparison_available ? <p className="analysis-caption">旧报告未记录本次实际使用的样章子集与均值；请重新检查以查看完整比较基准。</p> : null;
  const comparison = raw as { metrics?: AnalysisMetrics | null; excluded_paths?: string[]; samples?: { path: string; hash: string }[]; reason?: string };
  return <details className="analysis-limits"><summary>本次实际文风比较基准</summary>
    {!!comparison.excluded_paths?.length && <p>已排除待审文稿，避免与自身比较：{comparison.excluded_paths.join("、")}。</p>}
    {comparison.metrics ? <><p>下列统计仅来自本次剩余的作者固定样章快照，并非全部样章的均值。</p><Metrics value={comparison.metrics} /></> : <p>本次未执行文风比较：{reasonLabels[comparison.reason || ""] || "缺少有效的其他固定样章"}。</p>}
    {!!comparison.samples?.length && <ul>{comparison.samples.map((item) => <li key={item.path}><SourceButton path={item.path} expectedHash={item.hash} stale={documents.find((document) => document.path === item.path)?.hash !== item.hash} onOpen={onOpen} /><span> · 固定版本 {item.hash.slice(0, 12)}</span></li>)}</ul>}
  </details>;
}

export default function AnalysisWorkspace({ project, documents, approvals, currentDocument, unsavedPath, onOpen, onRefresh, onDirtyChange }: Props) {
  const base = `${projectUrl(project.novel_id)}/analysis`;
  const [tab, setTab] = useState<Tab>("brief");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [baseline, setBaseline] = useState<StyleBaseline | null>(null);
  const [samplePaths, setSamplePaths] = useState<string[]>([]);
  const [toolkitDirty, setToolkitDirty] = useState(false);
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
  const [searchSignature, setSearchSignature] = useState("");
  const [verification, setVerification] = useState<(AnalysisVerification & { previous_issues: AnalysisIssue[] }) | null>(null);
  const [searchMode, setSearchMode] = useState<"literal" | "semantic">("literal");
  const [semanticStatus, setSemanticStatus] = useState<SemanticStatus | null>(null);
  const [semanticBusy, setSemanticBusy] = useState(false);
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
  const historySignature = documents.filter((item) => item.path.startsWith("chapters/")).map((item) => `${item.path}:${item.hash}`).sort().join("|");
  const searchStale = !!search && ((searchSignature !== historySignature) || (search.canonical_revision !== undefined && project.canonical_revision !== undefined && search.canonical_revision !== project.canonical_revision) || search.results.some((item) => documents.find((document) => document.path === item.path)?.hash !== item.hash));
  const briefStale = !!brief && Object.entries(brief.sources).some(([path, hash]) => documents.find((item) => item.path === path)?.hash !== hash);
  const sourceStale = (path: string, hash: string) => documents.find((item) => item.path === path)?.hash !== hash;
  const baselineDirty = !!baseline && selectionChanged(samplePaths, baseline.samples.map((item) => item.path));
  const dirty = toolkitDirty || baselineDirty;
  const dirtyCallback = useRef(onDirtyChange); dirtyCallback.current = onDirtyChange;
  useEffect(() => { dirtyCallback.current?.(dirty); }, [dirty]);
  useEffect(() => () => dirtyCallback.current?.(false), []);

  function chooseTab(next: Tab) {
    if (next === tab) return;
    if (toolkitDirty && !window.confirm("检查配置尚未保存。切换工具会放弃这份修改，仍要继续吗？")) return;
    setToolkitDirty(false); setTab(next); setError(""); setNotice("");
  }

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
      requests.current.brief += 1; setBrief(null); setBriefBusy(false);
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
    setReportBusy(true); setMarkBusy(""); setError(""); setNotice(""); setVerification(null);
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
    setHistoryPath(path); setReport(null); setMarkBusy(""); setVerification(null);
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
  async function verifyReport() {
    if (!report || reportBusy || markBusy || refreshBusy) return;
    if (unsavedPath === report.source.path) { setError("请先保存文稿，再复查已保存版本。"); return; }
    const snapshot = report;
    const request = ++requests.current.report;
    setReportBusy(true); setVerification(null); setError(""); setNotice("");
    try {
      const value = await api<AnalysisVerification>(`${base}/verify`, "POST", { report_path: snapshot.path, report_hash: snapshot.hash });
      if (!alive.current || request !== requests.current.report) return;
      setVerification({ ...value, previous_issues: snapshot.issues });
      if (value.report) {
        setReport(value.report); setHistoryPath(value.report.path);
        setLocalHistory((items) => [...new Set([value.report!.path, ...items])]);
      }
      setNotice(value.report ? "已按当前保存版本重新运行规则。未再命中仅代表本次规则结果，不能认定语义或情节问题已修好；新报告供重新审阅，旧作者标记保留在旧报告。" : "来源已缺失或无法读取，本次未能生成新报告；请查看逐项复查原因。");
      await onRefresh();
    } catch (failure) { if (alive.current && request === requests.current.report) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.report) setReportBusy(false); }
  }
  async function buildBrief() {
    if (!selectedPlan || briefBusy) return;
    if (currentDocument && currentDocument.path === unsavedPath) { setError("额外作者来源有未保存修改，请先保存再整理写前包。"); return; }
    const request = ++requests.current.brief;
    setBriefBusy(true); setBrief(null); setError("");
    try {
      const value = await api<ChapterBrief>(`${base}/brief`, "POST", { plan_path: selectedPlan.path, expected_hash: selectedPlan.hash, mode, ...(currentDocument ? { document_path: currentDocument.path, document_hash: currentDocument.hash } : {}) });
      if (alive.current && request === requests.current.brief) setBrief(value);
    } catch (failure) { if (alive.current && request === requests.current.brief) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.brief) setBriefBusy(false); }
  }
  async function searchHistory(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || searchBusy) return;
    const snapshot = query.trim();
    const signature = historySignature;
    const request = ++requests.current.search;
    setSearchBusy(true); setSearch(null); setError(""); setSearchedQuery(snapshot);
    try {
      const value = searchMode === "semantic" ? await api<AnalysisSearch>(`${base}/semantic/search`, "POST", { query: snapshot, limit: 10 }) : await api<AnalysisSearch>(`${base}/search?query=${encodeURIComponent(snapshot)}&limit=10`);
      if (alive.current && request === requests.current.search) { setSearch(value); setSearchSignature(signature); }
    } catch (failure) { if (alive.current && request === requests.current.search) setError(message(failure)); }
    finally { if (alive.current && request === requests.current.search) setSearchBusy(false); }
  }
  async function checkSemantic() {
    if (semanticBusy) return;
    setSemanticBusy(true); setError("");
    try { const value = await api<SemanticStatus>(`${base}/semantic/status`); if (alive.current) setSemanticStatus(value); }
    catch (failure) { if (alive.current) setError(message(failure)); }
    finally { if (alive.current) setSemanticBusy(false); }
  }
  async function refreshView() {
    if (refreshBusy || baselineBusy || reportBusy || markBusy) return;
    requests.current.brief += 1; setBrief(null); setBriefBusy(false);
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
    <div className="analysis-local-note"><CheckCheck size={15} />本工作区使用本地资料；语义检索在本机运行，不调用生成 API。检查结果供作者判断。</div>
    <div className="analysis-tabs" role="tablist" aria-label="写作检查工具">{tabs.map(({ id, label, icon: Icon }) => <button type="button" key={id} id={`analysis-tab-${id}`} aria-controls={`analysis-panel-${id}`} role="tab" aria-selected={tab === id} className={tab === id ? "selected" : ""} onClick={() => chooseTab(id)}><Icon size={16} />{label}</button>)}</div>
    {error && <div className="analysis-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="关闭检查错误">×</button></div>}
    {notice && <p className="analysis-success" role="status">{notice}</p>}
    <section className="analysis-panel" role="tabpanel" id={`analysis-panel-${tab}`} aria-labelledby={`analysis-tab-${tab}`}>
      {(tab === "toolkit" || tab === "structure") && <AnalysisToolkitWorkspace key={`${project.novel_id}-${tab}`} project={project} documents={documents} onOpen={onOpen} onRefresh={refreshView} onDirtyChange={setToolkitDirty} view={tab} />}
      {tab === "brief" && <>
        <div className="analysis-section-heading"><div><h2>从已批准计划开始</h2><p>汇集当前章计划、承接状态、相关规划和检索证据，列出缺失及省略的材料。实际起草会按助手中选定的模式重建最新包。</p></div><ClipboardList size={21} /></div>
        <div className="analysis-controls"><label className="analysis-field grow">已批准并应用的章计划<select value={planPath} onChange={(event) => { setPlanPath(event.target.value); requests.current.brief += 1; setBrief(null); setBriefBusy(false); }}><option value="">明确选择一份章计划</option>{plans.map((item) => <option key={item.path} value={item.path}>{item.path}</option>)}</select></label><label className="analysis-field">材料范围<select value={mode} onChange={(event) => { setMode(event.target.value as "compact" | "full"); requests.current.brief += 1; setBrief(null); setBriefBusy(false); }}><option value="compact">精简写前包</option><option value="full">完整写前包</option></select></label><button type="button" className="button primary small" disabled={!selectedPlan || briefBusy} onClick={() => void buildBrief()}>{briefBusy ? <LoaderCircle className="spin" size={15} /> : <ClipboardList size={15} />}整理写前包</button></div>
        {!plans.length && <Warning>未查到可选的已应用章计划。请先到创作规划提取章计划，并在审批中心批准、应用。</Warning>}
        {currentDocument && <p className="analysis-caption">当前额外作者来源：{currentDocument.path} · 版本 {currentDocument.hash.slice(0, 12)}{currentDocument.path === unsavedPath ? " · 有未保存修改，请先保存" : ""}。已在包内的同版资料按范围去重；草稿与未来材料只作参考。</p>}
        {brief && <><div className="analysis-result-heading"><span>{brief.chapter ? `第 ${brief.chapter} 章` : "本章"} · {brief.budget ? `${brief.budget.characters.toLocaleString()} / ${brief.budget.limit.toLocaleString()} 字符` : "写前材料"}</span><button type="button" className="button secondary small" onClick={() => download(`写前包-${brief.chapter || "本章"}.md`, brief.text)}><Download size={14} />导出 Markdown</button></div>
          {!!brief.missing.length && <Warning><strong>未查到的材料</strong><ul>{brief.missing.map((item) => <li key={item.id}>{item.label}：{item.reason}</li>)}</ul></Warning>}
          {!!brief.omitted.length && <details className="analysis-limits"><summary>本次省略（{brief.omitted.length}）</summary><ul>{brief.omitted.map((item) => <li key={`${item.id}-${item.paths.join()}`}>{item.label}：{item.reason}</li>)}</ul></details>}
          {briefStale && <Warning>写前包的来源版本已变化。下方为整理时的材料，请重新整理写前包后再用于创作。</Warning>}
          {!!brief.input_manifest?.length && <details className="analysis-limits"><summary>实际材料范围与版本（{brief.input_manifest.length}）</summary><ul>{brief.input_manifest.map((item, index) => <li key={index}>{item.path} · 版本 {item.hash.slice(0, 12)} · {item.characters.toLocaleString()} 字符 · {({ past_fact: "既有记录", plan: "未来计划", writing_rules: "作者写作规则", style_reference: "文风参照", author_reference: "作者额外参考" } as Record<string, string>)[item.role] || "参考材料"}{item.ranges.length > 0 ? ` · 摘录范围 ${item.ranges.map((range) => `第 ${range.start_line ?? "—"}–${range.end_line ?? "—"} 行`).join("、")}` : ""}</li>)}</ul></details>}
          <div className="analysis-brief-sections">{brief.sections.map((section) => <details key={section.id} open={section.status === "included"}><summary><strong>{section.label}</strong><span className="tag">{sectionLabels[section.status] || section.status}{section.truncated ? " · 已截取" : ""}</span></summary><div>{section.paths.map((path) => <SourceButton key={path} path={path} expectedHash={brief.sources[path]} stale={!brief.sources[path] || sourceStale(path, brief.sources[path])} onOpen={onOpen} />)}<pre>{section.content || "本次未纳入正文内容"}</pre></div></details>)}</div><Limits items={brief.limitations} /></>}
        {!brief && !briefBusy && <Empty compact icon={<ClipboardList size={26} />} title="先读清楚，再动笔" description="选择已应用章计划，生成本章材料清单。整理写前包不会批准新情节或写入正式记忆。" />}
      </>}
      {tab === "review" && <>
        <div className="analysis-section-heading"><div><h2>检查已保存的文字</h2><p>本地规则筛查句式与明显字面问题。处理标记只更新报告。</p></div><ShieldCheck size={21} /></div>
        <div className="analysis-controls"><label className="analysis-field grow">文稿<select value={documentPath} onChange={(event) => { setDocumentPath(event.target.value); requests.current.report += 1; setReport(null); setReportBusy(false); setMarkBusy(""); setHistoryPath(""); }}><option value="">选择已保存草稿或正式章节</option>{sourceDocuments.map((item) => <option key={item.path} value={item.path}>{item.path}{item.path === unsavedPath ? " · 有未保存修改" : ""}</option>)}</select></label><button type="button" className="button primary small" disabled={!selectedDocument || reportBusy || selectedDocument.path === unsavedPath} onClick={() => void analyze()}>{reportBusy ? <LoaderCircle size={15} className="spin" /> : <ShieldCheck size={15} />}分析已保存版本</button></div>
        {unsavedPath === documentPath && <Warning>当前文稿有未保存修改。请先回到编辑器保存，再进行分析。<SourceButton path={documentPath} onOpen={onOpen} /></Warning>}
        <label className="analysis-field analysis-history"><span><History size={14} />重开历史报告</span><select value={historyPath} onChange={(event) => void loadReport(event.target.value)}><option value="">选择已有报告</option>{reportPaths.map((path) => <option key={path} value={path}>{leaf(path)}</option>)}</select></label>
        {report && <><div className="analysis-report-source"><SourceButton path={report.source.path} expectedHash={report.source.hash} stale={sourceStale(report.source.path, report.source.hash)} onOpen={onOpen} /><code title={report.source.hash}>分析版本 {report.source.hash.slice(0, 12)}</code><span className="tag">{reportStale ? "报告已过期" : "已保存版本"}</span><button type="button" className="button secondary small" disabled={reportBusy || !!markBusy || refreshBusy || unsavedPath === report.source.path} onClick={() => void verifyReport()}><RefreshCw size={14} className={reportBusy ? "spin" : ""} />按当前版本复查</button></div>
          {reportStale && <Warning>正文、固定样章或依赖版本已变化。下方原句属于报告生成时的版本，请重新分析后判断当前文字。{report.stale_reasons?.length > 0 && <ul>{report.stale_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}</Warning>}
          <Metrics value={report.metrics} /><p className="analysis-caption">句段依标点切分；引号与比拟词来自规则统计，数字不代表文学质量评分。{report.baseline?.comparison_available ? "本次按作者固定样章进行句长、段落、引号、比拟词及句长变化对照；待审文稿会从参照中排除。" : "本次未进行文风对照：尚无有效的其他固定样章。请查看覆盖记录中的具体原因。"}</p>
          <BaselineComparison value={report.baseline} documents={documents} onOpen={onOpen} />
          <div className="analysis-issue-toolbar"><h3>原句与建议 <span>{report.issues.length}</span></h3><select aria-label="筛选问题状态" value={issueFilter} onChange={(event) => setIssueFilter(event.target.value as typeof issueFilter)}><option value="all">全部状态</option>{Object.entries(statusLabels).map(([id, label]) => <option key={id} value={id}>{label}（{report.issues.filter((item) => item.status === id).length}）</option>)}</select></div>
          {verification && <Verification value={verification} />}
          {shownIssues.length ? <div className="analysis-issues">{shownIssues.map((issue) => <article className={`analysis-issue ${issue.status}`} key={issue.id}>
            <div className="analysis-issue-heading"><span className="tag">{ruleLabels[issue.rule] || "文字检查"}</span><span>{issue.severity === "warning" ? "优先核对" : "审阅提示"}</span><SourceButton path={report.source.path} line={issue.start_line} expectedHash={report.source.hash} stale={sourceStale(report.source.path, report.source.hash)} onOpen={onOpen} /></div>
            <p className="analysis-line-label">待审原文 · 第 {issue.start_line}{issue.end_line !== issue.start_line ? `–${issue.end_line}` : ""} 行 · 版本 {report.source.hash.slice(0, 12)}</p><blockquote>{issue.quote}</blockquote>
            {issue.related && <div className="analysis-related"><p className="analysis-line-label">对照历史原文 · 第 {issue.related.start_line}–{issue.related.end_line} 行 · 版本 {issue.related.hash.slice(0, 12)}</p><SourceButton path={issue.related.path} line={issue.related.start_line} expectedHash={issue.related.hash} stale={sourceStale(issue.related.path, issue.related.hash)} onOpen={onOpen} /><blockquote>{issue.related.quote}</blockquote>{sourceStale(issue.related.path, issue.related.hash) && <p>历史来源已变化；此处保留报告当时的引文，请重新分析后核对。</p>}</div>}
            <h4>{issue.message}</h4><p>{issue.suggestion}</p><div className="analysis-issue-actions"><label>作者处理标记<select value={issue.status} disabled={!!markBusy || refreshBusy || reportBusy} onChange={(event) => void mark(issue, event.target.value as AnalysisIssue["status"])}>{Object.entries(statusLabels).map(([id, label]) => <option key={id} value={id} disabled={id === "resolved" && reportStale}>{label}{id === "resolved" && reportStale ? "（需重新分析）" : ""}</option>)}</select></label>{markBusy === issue.id && <LoaderCircle size={14} className="spin" />}<small>作者标记不等于自动验证，不修改正文</small></div>
          </article>)}</div> : <Empty compact icon={<CheckCheck size={24} />} title={report.issues.length ? "当前筛选下没有问题" : "本次规则没有命中"} description="这不代表全文不存在连续性、情节重复或文风问题；请结合原文审阅。" />}
          <Coverage value={report.coverage} documents={documents} onOpen={onOpen} /><Limits items={report.limitations} />
        </>}
        {!report && !reportBusy && <Empty compact icon={<ShieldCheck size={26} />} title="保留原句的本地检查" description="选择一份已保存文稿。报告将单独保存，可标记问题并在以后重新打开。" />}
      </>}
      {tab === "baseline" && <>
        <div className="analysis-section-heading"><div><h2>由你选定文风参照</h2><p>明确选择章节或草稿作为样章；系统不会自动替换，文件变化后需再次保存。</p></div><SlidersHorizontal size={21} /></div>
        {baseline?.configured ? <><div className="analysis-result-heading"><span className="tag">{baselineStale ? "样章版本已变化" : "固定样章已配置"}</span><span>{baseline.samples.length} 份样章 · {baseline.hash?.slice(0, 12)}</span></div>{baselineStale && <Warning>至少一份样章已变更或缺失。原基线保留；下方勾选后点击保存，才会按当前版本更新。</Warning>}<Metrics value={baseline.metrics} /></> : <Warning>尚未选定固定样章。本地审稿可以继续，文风对照暂不执行。</Warning>}
        <div className="analysis-samples">{samples.length ? samples.map((item) => { const fixed = baseline?.samples.find((entry) => entry.path === item.path); const changed = !!fixed && fixed.hash !== documents.find((document) => document.path === item.path)?.hash; return <label key={item.path} className="analysis-sample"><input type="checkbox" checked={samplePaths.includes(item.path)} disabled={baselineBusy || (!samplePaths.includes(item.path) && samplePaths.length >= 10)} onChange={(event) => setSamplePaths((items) => event.target.checked ? [...items, item.path] : items.filter((path) => path !== item.path))} /><span><strong>{item.path}</strong><small>{item.kind === "missing" ? "文件已缺失，请取消选择" : fixed ? changed ? "当前内容已变化，保存后才会更新基线" : "与保存的样章版本一致" : "尚未设为样章"}{item.path === unsavedPath ? " · 有未保存修改" : ""}</small></span>{fixed && <code>{fixed.hash.slice(0, 10)}</code>}</label>; }) : <Empty compact title="还没有可选样章" description="先保存一份草稿或正式章节，再由你明确选定。" />}</div>
        <div className="analysis-save-row"><span>{baselineDirty ? "样章选择有未保存修改 · " : ""}{samplePaths.length ? `已选择 ${samplePaths.length} / 10 份；保存时绑定当前文件版本` : "请明确选择 1–10 份样章"}</span><button type="button" className="button primary small" disabled={!baseline || baselineBusy || refreshBusy || !samplePaths.length || samplePaths.length > 10 || samplePaths.includes(unsavedPath || "")} onClick={() => void saveBaseline()}>{baselineBusy ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}保存样章选择</button></div><Limits items={baseline?.limitations} />
      </>}
      {tab === "search" && <>
        <div className="analysis-section-heading"><div><h2>检索正式章节</h2><p>查找历史原句，保留文件版本和行号。本地语义检索需先准备模型与运行依赖。</p></div><FileSearch size={21} /></div>
        <form className="analysis-search-form" onSubmit={(event) => void searchHistory(event)}><label className="analysis-field">检索方式<select value={searchMode} disabled={searchBusy} onChange={(event) => { const value = event.target.value as "literal" | "semantic"; setSearchMode(value); setSearch(null); requests.current.search += 1; if (value === "semantic") void checkSemantic(); }}><option value="literal">字面词项</option><option value="semantic">本地语义</option></select></label><label className="analysis-field grow">检索词<input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={200} placeholder="输入人物、地点、物品或需要核对的描述" required /></label><button className="button primary small" type="submit" disabled={searchBusy || !query.trim() || (searchMode === "semantic" && (!!semanticStatus && !semanticStatus.available || semanticBusy))}>{searchBusy ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}查找历史</button></form>
        {searchMode === "literal" ? <p className="analysis-caption">字面词项按用词相关程度查找；相似分数不能判断情节是否重复或事实是否矛盾。</p> : <div className="analysis-semantic-status"><div><span className="tag">{semanticBusy ? "正在检查本地能力" : semanticStatus?.available ? "本地语义检索可用" : "本地语义检索尚未就绪"}</span><button type="button" className="button secondary small" disabled={semanticBusy} onClick={() => void checkSemantic()}><RefreshCw size={13} className={semanticBusy ? "spin" : ""} />检查状态</button></div>{semanticStatus && <><p>{semanticStatus.reason}</p><small>模型 {semanticStatus.model} · 版本 {semanticStatus.revision.slice(0, 12)} · {semanticStatus.loaded ? "已验证可运行" : "首次检索时加载"}</small>{!semanticStatus.available && semanticStatus.setup_command && <p>本机准备命令：<code>{semanticStatus.setup_command}</code></p>}</>}<p>语义检索在本机运行，不会自动改用字面检索。向量相似只提供回看线索，不能证明情节重复或伏笔回收。</p></div>}
        {search && <><div className="analysis-result-heading"><span>“{searchedQuery}” · 扫描 {search.scanned} 份正式章节 · {search.mode === "local_embedding" ? "候选" : "命中"} {search.results.length} 处</span><span className="tag">{searchStale ? "检索结果已失效" : "来源版本已核对"}</span></div>{search.model && <p className="analysis-caption">本地模型 {search.model} · 索引片段 {search.indexed_chunks ?? "—"} · 复用缓存 {search.reused_chunks ?? "—"}</p>}{searchStale && <Warning>正式章节或版本已变化。以下是检索时保留的历史引文，已停用旧行号跳转；请点击“查找历史”重新检索。</Warning>}<div className="analysis-search-results">{search.results.map((item, index) => <article key={`${item.path}-${item.start_line}-${index}`}><div><SourceButton path={item.path} line={item.start_line} expectedHash={item.hash} stale={searchStale || sourceStale(item.path, item.hash)} onOpen={onOpen} /><span className="tag">{search.mode === "local_embedding" ? "向量相似" : "字面相关"} {Number(item.score.toFixed(2))}</span></div><blockquote>{item.quote}</blockquote><small>第 {item.start_line}–{item.end_line} 行 · 版本 {item.hash.slice(0, 12)}</small></article>)}</div>{!search.results.length && <Empty compact icon={<FileSearch size={24} />} title="本次未查到相关原句" description="只检查了本次扫描范围；你可以尝试更短的描述、人物名或物品名。" />}{search.omitted.length > 0 && <details className="analysis-limits"><summary>未纳入本次检索（{search.omitted.length}）</summary><Omitted items={search.omitted} /></details>}<Limits items={search.limitations} /></>}
        {!search && !searchBusy && <Empty compact icon={<Search size={26} />} title="从历史原句核对" description="仅搜索正式章节，不把候选草稿当作已发生事实。" />}
      </>}
    </section><p className="analysis-footer"><ShieldCheck size={13} />写前材料与检查报告不会改写正式正文、章计划或故事记忆。</p>
  </div>;
}
