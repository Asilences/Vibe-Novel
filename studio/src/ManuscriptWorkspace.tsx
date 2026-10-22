import { useTaskReference } from "./TaskReferenceContext";
import type { TaskReference } from "./TaskReferenceContext";
import { useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, Check, CircleAlert, Download, FileSearch, GitCompareArrows, LoaderCircle, RefreshCw, Scissors, ShieldCheck, Sparkles, Upload } from "lucide-react";
import { api, activeStates, download, formatDate, leaf, projectUrl, stateLabels } from "./api";
import type { AnalysisReport, Document as NovelDocument, DocumentItem, Imported, Job, ManuscriptPreview, ManuscriptSession, ManuscriptSummary, Project, Provider } from "./api";
import { Diff, Empty, Modal } from "./ui";
import { originalSelectionRange, splitSourceLines } from "./textSelection";
import { libraryPreviewHash, libraryTaskConfiguration } from "./libraryHelpers";
import { JobRouteDetails } from "./MultiModelWorkspace";
import TaskMaterialPreview from "./TaskMaterialPreview";
import { planningSourceLabel } from "./importSourceHelpers";
import EditorialTaskScopePreview from "./EditorialTaskScopePreview";
import "./manuscriptGuide.css";

type Props = { project: Project; documents: DocumentItem[]; imports?: Imported[]; jobs: Job[]; providers: Provider[]; onSettings: () => void; onImport: () => void; onRefresh: () => Promise<void>; onOpen: (path: string, line?: number, expectedHash?: string) => void; onJob: (job: Job) => void };
type BatchAudit = { reports: AnalysisReport[]; coverage: { requested: number; analyzed: number; corpus_scanned: number; omitted: { section_id: string; path: string; reason: string }[]; source_hashes: Record<string, string>; scope: string }; limitations: string[] };
const goalOptions = [{ id: "remove_cliches", label: "减少套话" }, { id: "reduce_repetition", label: "减少重复表达" }, { id: "show_actions", label: "增加动作与感知" }, { id: "natural_dialogue", label: "自然对话" }, { id: "preserve_voice", label: "保留原有文风" }];
const failureText = (error: unknown) => error instanceof Error ? error.message : "操作未完成，请重试。";
const length = (text: string) => Array.from(text).length;
const selectionText = (text: string, start: number, end: number) => Array.from(text).slice(start, end).join("");

export default function ManuscriptWorkspace({ project, documents, imports = [], jobs, providers, onSettings, onImport, onRefresh, onOpen, onJob }: Props) {
  const taskReference = useTaskReference(project.novel_id);
  const base = `${projectUrl(project.novel_id)}/manuscripts`;
  const [sessions, setSessions] = useState<ManuscriptSummary[]>([]);
  const [sessionOmitted, setSessionOmitted] = useState<unknown[]>([]);
  const [sourcePath, setSourcePath] = useState("");
  const [boundaries, setBoundaries] = useState("");
  const [preview, setPreview] = useState<ManuscriptPreview | null>(null);
  const [sourceText, setSourceText] = useState("");
  const [sourceTextHash, setSourceTextHash] = useState("");
  const [sourceLineStart, setSourceLineStart] = useState(1);
  const [session, setSession] = useState<ManuscriptSession | null>(null);
  const [sectionId, setSectionId] = useState("");
  const [document, setDocument] = useState<NovelDocument | null>(null);
  const [rangeEnabled, setRangeEnabled] = useState(false);
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(0);
  const [goals, setGoals] = useState(["remove_cliches", "reduce_repetition", "preserve_voice"]);
  const [intensity, setIntensity] = useState<"conservative" | "standard" | "deep">("conservative");
  const [instruction, setInstruction] = useState("");
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [batchAudit, setBatchAudit] = useState<BatchAudit | null>(null);
  const [checkedSections, setCheckedSections] = useState<string[]>([]);
  const [batchOffset, setBatchOffset] = useState(0);
  const [selectedJobId, setSelectedJobId] = useState("");
  const [candidate, setCandidate] = useState<NovelDocument | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [taskPreview, setTaskPreview] = useState<{ request: Record<string, unknown>; route: Record<string, unknown>; reference: TaskReference } | null>(null);
  const alive = useRef(true);
  const requests = useRef({ session: 0, document: 0, result: 0 });
  const documentRef = useRef(document); documentRef.current = document;
  const sourceLines = useMemo(() => splitSourceLines(sourceText), [sourceText]);
  const sources = useMemo(() => documents.filter((item) => item.path.endsWith(".md") && !/^drafts\/manuscript-[0-9a-f]{32}\//.test(item.path) && (/^(drafts|chapters|bible|memory)\//.test(item.path) || item.path.startsWith("outlines/source/") || /^imports\/original\/[0-9a-f]{64}\/extracted\.md$/.test(item.path))).sort((a, b) => a.path.localeCompare(b.path)), [documents]);
  const source = sources.find((item) => item.path === sourcePath);
  const section = session?.sections.find((item) => item.id === sectionId);
  const docStale = !!document && documents.find((item) => item.path === document.path)?.hash !== document.hash;
  const previewStale = !!preview && documents.find((item) => item.path === preview.source.path)?.hash !== preview.source.hash;
  const selectedJob = jobs.find((item) => item.id === selectedJobId);
  const relevantJobs = section ? jobs.filter((item) => item.request?.document_path === section.path) : [];
  const apiAvailable = providers.some((item) => item.id === "api" && item.available);
  const rangeValid = !!document && Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= length(document.content);
  const reportStale = !!report && (report.stale || documents.find((item) => item.path === report.source.path)?.hash !== report.source.hash || Object.entries(report.dependencies || {}).some(([path, hash]) => documents.find((item) => item.path === path)?.hash !== hash));
  const batchStale = !!batchAudit && Object.entries(batchAudit.coverage.source_hashes).some(([path, hash]) => documents.find((item) => item.path === path)?.hash !== hash);
  const candidateStale = !!candidate && (!selectedJob || !document || session?.stale || selectedJob.request?.document_hash !== document.hash || docStale || selectedJob.context_sources?.[document.path] !== document.hash || Object.entries(selectedJob.context_sources || {}).some(([path, hash]) => documents.find((item) => item.path === path)?.hash !== hash) || documents.find((item) => item.path === candidate.path)?.hash !== candidate.hash || (!!selectedJob.result_hash && selectedJob.result_hash !== candidate.hash));

  useEffect(() => {
    alive.current = true;
    void api<{ manuscripts: ManuscriptSummary[]; omitted?: unknown[] }>(base).then((value) => { if (alive.current) { setSessions(value.manuscripts); setSessionOmitted(value.omitted || []); } }).catch((failure) => { if (alive.current) setError(failureText(failure)); });
    return () => { alive.current = false; requests.current.session += 1; requests.current.document += 1; requests.current.result += 1; };
  }, [base]);

  useEffect(() => {
    const request = ++requests.current.document;
    setDocument(null); setCandidate(null); setSelectedJobId(""); setReport(null); setRangeEnabled(false); setStart(0); setEnd(0);
    if (!section) return;
    void api<NovelDocument>(`${projectUrl(project.novel_id)}/document?path=${encodeURIComponent(section.path)}`).then((value) => {
      if (!alive.current || request !== requests.current.document) return;
      setDocument(value); setEnd(length(value.content));
    }).catch((failure) => { if (alive.current && request === requests.current.document) setError(failureText(failure)); });
  }, [project.novel_id, section?.path, section?.current_hash]);

  useEffect(() => {
    const request = ++requests.current.result;
    setCandidate(null);
    if (!selectedJob?.result_path || selectedJob.state !== "succeeded") return;
    void api<NovelDocument>(`${projectUrl(project.novel_id)}/document?path=${encodeURIComponent(selectedJob.result_path)}`).then((value) => { if (alive.current && request === requests.current.result) setCandidate(value); }).catch((failure) => { if (alive.current && request === requests.current.result) setError(failureText(failure)); });
  }, [project.novel_id, selectedJob?.result_path, selectedJob?.state]);

  async function perform(key: string, action: () => Promise<void>) {
    if (busy) return;
    setBusy(key); setError(""); setNotice("");
    try { await action(); } catch (failure) { if (alive.current) setError(failureText(failure)); }
    finally { if (alive.current) setBusy(""); }
  }
  function sourceRequest() {
    if (!source) throw new Error("请先选择已保存的 Markdown 来源，或导入 .md / .txt 文件。");
    const startLines = boundaries.trim() ? boundaries.split(/[\s,，;；]+/).filter(Boolean).map(Number) : undefined;
    if (startLines?.some((line) => !Number.isInteger(line) || line < 1)) throw new Error("分章起始行须为正整数，用逗号或空格分隔。");
    return { source_path: source.path, source_hash: source.hash, ...(startLines ? { start_lines: startLines } : {}) };
  }
  async function previewSource() { await perform("preview", async () => {
    const value = await api<ManuscriptPreview>(`${base}/preview`, "POST", sourceRequest());
    const original = await api<NovelDocument>(`${projectUrl(project.novel_id)}/document?path=${encodeURIComponent(value.source.path)}`);
    if (original.hash !== value.source.hash) throw new Error("预览期间原稿已变化，请重新预览。");
    if (alive.current) { setPreview(value); setSourceText(original.content); setSourceTextHash(original.hash); }
  }); }
  async function prepare() {
    await perform("prepare", async () => {
      if (!preview || previewStale) throw new Error("请先按当前来源与分章起始行预览，再创建副本。");
      const value = await api<ManuscriptSession>(base, "POST", sourceRequest());
      if (!alive.current) return;
      setSession(value); setSectionId(value.sections[0]?.id || ""); setCheckedSections([]); setBatchAudit(null); setBatchOffset(0);
      const list = await api<{ manuscripts: ManuscriptSummary[]; omitted?: unknown[] }>(base);
      if (alive.current) { setSessions(list.manuscripts); setSessionOmitted(list.omitted || []); setNotice("已创建独立修订副本。原始稿和正式章节保留；后续仅接受到本副本。"); }
      await onRefresh();
    });
  }
  async function loadSession(id: string) {
    if (!id) { setSession(null); setSectionId(""); return; }
    const request = ++requests.current.session;
    await perform("session", async () => {
      const value = await api<ManuscriptSession>(`${base}/${encodeURIComponent(id)}`);
      if (!alive.current || request !== requests.current.session) return;
      setSession(value); setSectionId((current) => value.sections.some((item) => item.id === current) ? current : value.sections[0]?.id || ""); setBatchAudit(null); setCheckedSections([]); setBatchOffset(0);
    });
  }
  async function localAnalysis() {
    await perform("analysis", async () => {
      if (!session || !section || !document || docStale) throw new Error("副本文稿已变化，请先重新读取。");
      const value = await api<BatchAudit>(`${base}/${session.id}/audit`, "POST", { section_ids: [section.id] });
      if (alive.current && documentRef.current?.path === document.path) { setReport(value.reports[0] || null); setBatchAudit(value); }
      await onRefresh();
    });
  }
  async function batchAnalysis(ids: string[], nextOffset?: number) {
    await perform("batch", async () => {
      if (!session || !ids.length) throw new Error("请先勾选要检查的章节。");
      if (ids.length > 20) throw new Error("每次最多检查 20 章，请分批选择。");
      const value = await api<BatchAudit>(`${base}/${session.id}/audit`, "POST", { section_ids: ids });
      if (!alive.current) return;
      setBatchAudit(value); setReport(value.reports[0] || null); if (nextOffset !== undefined) setBatchOffset(nextOffset);
      await onRefresh(); setNotice(`本批本地检查完成：${value.coverage.analyzed} / ${value.coverage.requested} 章；对照 ${value.coverage.corpus_scanned} 份本副本章节。未自动运行其他批次。`);
    });
  }
  async function startJob(kind: "review" | "rewrite") {
    await perform("job", async () => {
      if (!document || docStale) throw new Error("请先重新读取当前副本。");
      if (rangeEnabled && !rangeValid) throw new Error("请明确选择有效文本范围；起点包含、终点不包含。");
      if (!goals.length) throw new Error("请至少选择一项编辑目标。");
      const request = { ...(taskReference.selection ? {library_selection:taskReference.selection}:{}), kind, provider: "api", document_path: document.path, document_hash: document.hash, instruction: instruction.trim(), editorial: { goals, intensity }, ...(rangeEnabled ? { selection: { start_char: start, end_char: end } } : {}), idempotency_key: crypto.randomUUID() };
      const route = await api<Record<string, unknown>>(`${projectUrl(project.novel_id)}/jobs/preview`, "POST", request);
      if (!alive.current) return;
      setTaskPreview({ request, route, reference:taskReference.ticket });
    });
  }
  async function submitPreview() {
    if (!taskPreview || busy) return;
    await perform("submit", async () => {
      const value = await api<Job>(`${projectUrl(project.novel_id)}/jobs`, "POST", { ...taskPreview.request,
        expected_configuration: libraryTaskConfiguration(taskPreview.route),
        expected_library_hash: libraryPreviewHash(taskPreview.route) });
      if (!alive.current) return;
      taskReference.consume(taskPreview.reference); setTaskPreview(null); setSelectedJobId(value.id); setCandidate(null); onJob(value);
      setNotice("已提交 API 任务；结果需审阅后决定是否接受到修订副本。");
    });
  }
  async function accept() {
    await perform("accept", async () => {
      if (!session || !section || !document || !candidate || candidateStale || selectedJob?.kind !== "rewrite") throw new Error("候选来源已变化，请重新编辑当前版本。");
      const value = await api<ManuscriptSession>(`${base}/${session.id}/accept`, "POST", { section_id: section.id, base_hash: document.hash, candidate_path: candidate.path, candidate_hash: candidate.hash });
      if (!alive.current) return;
      setSession(value); setCandidate(null); setReport(null); setSelectedJobId("");
      await onRefresh(); setNotice("已显式接受到独立修订副本。请重新检查当前章节，随后可单独导出。");
    });
  }
  function captureSelection(target: HTMLTextAreaElement) {
    if (target.selectionEnd <= target.selectionStart) return;
    if (!document) return;
    const range = originalSelectionRange(document.content, target.selectionStart, target.selectionEnd);
    setStart(range.start_char); setEnd(range.end_char); setRangeEnabled(true);
  }
  return <div className="manuscript-workspace scroll-area">
    {taskPreview && <Modal title="审稿与编辑任务预览" subtitle="确认后才调用模型。使用下方固定的文稿、资料与配置版本。" wide onClose={() => { if (!busy) setTaskPreview(null); }}><EditorialTaskScopePreview request={taskPreview.request} /><JobRouteDetails route={taskPreview.route} /><TaskMaterialPreview route={taskPreview.route} />{error && <p className="inline-error">{error}；提交不确定时请先核对任务记录，重试沿用同一幂等键。</p>}<div className="modal-footer"><button className="button primary" disabled={!!busy || !libraryTaskConfiguration(taskPreview.route)} onClick={() => void submitPreview()}>确认并提交任务</button></div></Modal>}
    <div className="page-heading compact-heading"><div><div className="eyebrow">REVISION, WITH YOUR VOICE INTACT</div><h1>已有小说修订</h1><p>从原稿建立独立副本，逐章审稿、比较改写，再由你接受。</p></div><button type="button" className="button secondary small" onClick={onImport}><Upload size={15} />导入原稿</button></div>
    <div className="analysis-local-note"><ShieldCheck size={15} />编辑目标描述可观察的文字问题。此工作流不提供 AI 概率或检测分数。</div>
    {error && <div className="analysis-error" role="alert"><CircleAlert size={16} />{error}</div>}{notice && <p className="analysis-success" role="status">{notice}</p>}
    <section className="manuscript-source-panel"><div className="analysis-section-heading"><div><h2>1 · 校对分章，建立修订副本</h2><p>可选导入提取稿或已保存 Markdown。手工起始行只决定本次分章；原始文件保留。</p></div><Scissors size={20} /></div>
      <div className="analysis-controls"><label className="analysis-field grow">原稿来源<select value={sourcePath} disabled={!!busy} onChange={(event) => { setSourcePath(event.target.value); setPreview(null); setBoundaries(""); setSourceText(""); setSourceLineStart(1); }}><option value="">选择导入稿或已保存文稿</option>{sources.map((item) => <option key={item.path} value={item.path}>{planningSourceLabel(item.path, imports)}</option>)}</select></label><button type="button" className="button secondary small" disabled={!!busy || !source} onClick={() => void previewSource()}>{busy === "preview" ? <LoaderCircle className="spin" size={15} /> : <FileSearch size={15} />}预览分章</button></div>
      <label className="analysis-field">手工校正每章起始行（留空使用自动识别；如 1, 125, 287）<input value={boundaries} disabled={!!busy} onChange={(event) => { setBoundaries(event.target.value); setPreview(null); }} placeholder="填原始稿行号，用逗号分隔；修改后须重新预览" /></label>
      {source && <p className="analysis-caption">来源版本 {source.hash} <button type="button" className="analysis-source" onClick={() => onOpen(source.path, 1, source.hash)}>打开原稿核对行号</button></p>}
      {!!sourceText && <details className="analysis-limits"><summary>留在本页核对原稿行号（每次显示 100 行）</summary><p>预览快照版本 {sourceTextHash.slice(0, 12)}{source?.hash !== sourceTextHash ? " · 原稿已变化，请重新预览" : ""}。</p><label className="analysis-field">从第几行开始<input type="number" min={1} max={sourceLines.length} value={sourceLineStart} onChange={(event) => setSourceLineStart(Math.max(1, Number(event.target.value) || 1))} /></label><pre className="manuscript-source-lines">{sourceLines.slice(sourceLineStart - 1, sourceLineStart + 99).map((line, index) => `${sourceLineStart + index} │ ${line}`).join("\n")}</pre><p>行号前缀仅用于核对，不属于原稿。填好分章起始行后，请重新预览。</p></details>}
      {preview && <><div className="analysis-result-heading"><span>{preview.sections.length} 章 · {preview.source.characters.toLocaleString()} 字符 · {preview.source.lines.toLocaleString()} 行</span><button type="button" className="button primary small" disabled={!!busy || previewStale} onClick={() => void prepare()}>{busy === "prepare" ? <LoaderCircle className="spin" size={15} /> : <BookOpen size={15} />}创建独立修订副本</button></div>{previewStale && <div className="analysis-warning">来源版本已变化，请重新预览。</div>}<div className="manuscript-sections-preview">{preview.sections.map((item) => <div key={item.id}><strong>{item.order} · {item.title}</strong><span>第 {item.start_line}–{item.end_line} 行 · {item.characters.toLocaleString()} 字符</span></div>)}</div>{preview.warnings.map((item, index) => <p className="analysis-caption" key={index}>{item}</p>)}<details className="analysis-limits"><summary>分章检查范围</summary><ul>{preview.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details></>}
    </section>
    <section className="manuscript-session-panel"><div className="analysis-controls"><label className="analysis-field grow">2 · 打开独立修订副本<select value={session?.id || ""} disabled={!!busy} onChange={(event) => void loadSession(event.target.value)}><option value="">选择已有副本</option>{sessions.map((item) => <option key={item.id} value={item.id}>{formatDate(item.created_at)} · {leaf(item.source.path)} · {item.id.slice(0, 8)}</option>)}</select></label>{session && <button type="button" className="button secondary small" disabled={!!busy} onClick={() => void loadSession(session.id)}><RefreshCw size={14} />重新读取</button>}{session && <button type="button" className="button secondary small" disabled={!!busy || session.stale} onClick={() => void perform("export", async () => { const value = await api<{ filename: string; content: string }>(`${base}/${session.id}/export`); download(value.filename, value.content); })}><Download size={14} />独立导出</button>}</div>
      {session?.stale && <div className="analysis-warning"><CircleAlert size={16} /><div>副本或来源状态需要核对。{session.stale_reasons.map((item, index) => <p key={index}>{item}</p>)}</div></div>}
      {sessionOmitted.length > 0 && <p className="analysis-caption">会话列表有 {sessionOmitted.length} 项未纳入：可能达到目录／会话上限，或清单已损坏。未展示不表示不存在。</p>}
      {!session && <Empty compact icon={<GitCompareArrows size={26} />} title="先保留原稿，再开始修订" description="预览分章后创建副本，或打开以前的修订副本。正式正文的变更仍须作者审批。" />}
      {session && <><section className="manuscript-purpose-guide" aria-label="修订与续写指南">
        <div className="manuscript-purpose-intro"><h3><GitCompareArrows size={15} />独立修订</h3><p>在本副本中检查、审稿、比较并接受编辑，完成后点击上方“独立导出”。正文仍属于修订副本。</p><p>切分序号仅用于副本排序，不等于原稿章号；续写前请核对正式章号。</p></div>
        <details className="manuscript-continuation-guide"><summary>继续写作：查看正式承接步骤</summary><p>逐章确认已有正文，再批准下一章计划、准备承接材料并起草。</p><p className="manuscript-order-note">{section ? `当前是第 ${section.order} 个切分区段：${section.title}。` : "先在下方选择一个切分区段。"}切分序号不等于原稿章号；序言、番外、跳号和重号须由你核对对应的正式章号。</p><ol>
          <li><strong>打开本章副本。</strong>在下方选择要纳入正式的章节，再打开编辑器核对内容。<div className="manuscript-guide-action"><button type="button" className="button secondary small" disabled={!!busy || !document || document.path !== section?.path || docStale} onClick={() => { if (document && document.path === section?.path) onOpen(document.path, undefined, document.hash); }}>打开当前章副本编辑器</button></div></li>
          <li><strong>核对正式章号。</strong>在“提交定稿提案”中确认目标，例如 <code>chapters/ch001.md</code>；若改目标，先“核对目标”。按原稿结构决定序言和番外位置，已有同名正式章节时审阅替换差异。</li>
          <li><strong>逐章批准并应用。</strong>填写理由和影响，提交审批提案；到“待审变更”先“作者批准”，再“应用到目标文件”，逐章重复。</li>
          <li><strong>补核正式记忆。</strong>到“正式设定与记忆”，按定稿证据更新人物状态、摘要、事件与伏笔，逐份提案审批。记忆须明确截止章号；修订副本本身不会更新这些记录。</li>
          <li><strong>批准下一章计划。</strong>在“大纲与卷章”审阅后续规划，提取下一章计划，再批准并应用；也可提交手写章计划审批。</li>
          <li><strong>准备写前包并起草。</strong>到“审稿与检查 → 写前包”选下一章已应用计划，核对上一章定稿与省略项；再到“章节写作”的助手选择“章节起草”和该章计划，预览确认后生成草稿。</li>
        </ol></details>
      </section><div className="manuscript-batch-controls"><span>批次本地检查 · 已选 {checkedSections.length} / 20 章</span><button type="button" className="button secondary small" disabled={!!busy || !checkedSections.length} onClick={() => void batchAnalysis(checkedSections)}><ShieldCheck size={14} />检查勾选章节</button><button type="button" className="button secondary small" disabled={!!busy || batchOffset >= session.sections.length} onClick={() => { const next = Math.min(batchOffset + 20, session.sections.length); void batchAnalysis(session.sections.slice(batchOffset, next).map((item) => item.id), next); }}><ShieldCheck size={14} />{batchOffset >= session.sections.length ? "本轮批次已完成" : `${batchOffset ? "检查下一批" : "分批检查全稿"}（${batchOffset + 1}–${Math.min(batchOffset + 20, session.sections.length)}）`}</button>{batchOffset > 0 && <button type="button" className="analysis-source" disabled={!!busy} onClick={() => setBatchOffset(0)}>重置批次</button>}<small>每批最多 20 章／50 万字符，仅本地规则，不产生模型费用；超出范围会明确列为未检查。</small></div>
      {batchAudit && <details className="analysis-limits" open><summary>批次覆盖：已检查 {batchAudit.coverage.analyzed} / {batchAudit.coverage.requested} 章 · 对照 {batchAudit.coverage.corpus_scanned} 份本副本章节{batchStale ? " · 材料已变化" : ""}</summary><label className="analysis-field">查看本批报告<select value={report?.path || ""} onChange={(event) => setReport(batchAudit.reports.find((item) => item.path === event.target.value) || null)}><option value="">选择章节报告</option>{batchAudit.reports.map((item) => <option value={item.path} key={item.path}>{leaf(item.source.path)} · {item.issues.length} 项线索</option>)}</select></label>{batchAudit.coverage.omitted.length > 0 && <ul>{batchAudit.coverage.omitted.map((item, index) => <li key={index}>{item.path}：{item.reason}</li>)}</ul>}<ul>{batchAudit.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
      <div className="manuscript-grid"><aside className="manuscript-chapters">{session.sections.map((item) => <div className="manuscript-chapter-row" key={item.id}><input type="checkbox" aria-label={`批次选择${item.title}`} checked={checkedSections.includes(item.id)} disabled={!!busy || (!checkedSections.includes(item.id) && checkedSections.length >= 20)} onChange={(event) => setCheckedSections((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /><button type="button" disabled={!!busy} className={sectionId === item.id ? "selected" : ""} onClick={() => setSectionId(item.id)}><strong>{item.order} · {item.title}</strong><small>{item.accepted ? "已接受编辑" : item.changed ? "副本已变化" : "原稿副本"}</small></button></div>)}</aside><div className="manuscript-editor-panel">
        {!document ? <div className="analysis-loading"><LoaderCircle className="spin" size={17} />读取当前章节</div> : <>
          <div className="analysis-result-heading"><span>{section?.title} · {length(document.content).toLocaleString()} 字符 · 版本 {document.hash.slice(0, 12)}</span><button type="button" className="analysis-source" onClick={() => onOpen(document.path, undefined, document.hash)}>打开副本编辑器</button></div>
          {docStale && <div className="analysis-warning">副本文稿已变化。请重新读取当前副本，旧选区和候选暂不可用。</div>}
          <label className="analysis-field">已保存副本 · 拖选文字可指定编辑范围<textarea className="manuscript-text" aria-label="修订副本文本与选区" readOnly value={document.content} onMouseUp={(event) => captureSelection(event.currentTarget)} onKeyUp={(event) => captureSelection(event.currentTarget)} /></label>
          <div className="manuscript-range"><label><input type="checkbox" checked={rangeEnabled} onChange={(event) => setRangeEnabled(event.target.checked)} />仅处理选区</label>{rangeEnabled && <><label>起始字符<input type="number" min={0} max={length(document.content)} value={start} onChange={(event) => setStart(Number(event.target.value))} /></label><label>结束字符<input type="number" min={1} max={length(document.content)} value={end} onChange={(event) => setEnd(Number(event.target.value))} /></label><span>从 0 开始，起点包含、终点不包含；按 Unicode 字符计数。</span></>}</div>
          {rangeEnabled && (rangeValid ? <details className="analysis-limits" open><summary>实际选区 [{start}, {end}) · {end - start} 字符</summary><pre>{selectionText(document.content, start, end)}</pre></details> : <div className="analysis-warning">请选择有效范围；结束字符须大于起始字符，且不得超过章节长度。</div>)}
          <div className="manuscript-goals"><h3>编辑目标</h3><div>{goalOptions.map((item) => <label key={item.id}><input type="checkbox" checked={goals.includes(item.id)} onChange={(event) => setGoals((current) => event.target.checked ? [...current, item.id] : current.filter((goal) => goal !== item.id))} />{item.label}</label>)}</div></div>
          <div className="analysis-controls"><label className="analysis-field">编辑强度<select value={intensity} onChange={(event) => setIntensity(event.target.value as typeof intensity)}><option value="conservative">保守 · 尽量保留原句</option><option value="standard">标准 · 调整表达与节奏</option><option value="deep">深入 · 较大范围重构表达</option></select></label><label className="analysis-field grow">作者补充要求<input value={instruction} onChange={(event) => setInstruction(event.target.value)} maxLength={10000} placeholder="例如保留人物口吻，不增加情节或改变事实" /></label></div>
          {!apiAvailable && <div className="analysis-warning">API 尚未配置。<button type="button" className="analysis-source" onClick={onSettings}>选择模型并配置 API key</button></div>}
          <div className="manuscript-actions"><button type="button" className="button secondary small" disabled={!!busy || docStale} onClick={() => void localAnalysis()}><ShieldCheck size={14} />本章本地检查／复查</button><button type="button" className="button secondary small" disabled={!!busy || docStale || !apiAvailable || (rangeEnabled && !rangeValid)} onClick={() => void startJob("review")}><FileSearch size={14} />API 证据审稿</button><button type="button" className="button primary small" disabled={!!busy || docStale || !apiAvailable || (rangeEnabled && !rangeValid)} onClick={() => void startJob("rewrite")}><Sparkles size={14} />生成编辑候选</button></div>
          {report && <details className="analysis-limits" open><summary>{leaf(report.source.path)} 本地检查 · {report.issues.length} 项线索{reportStale ? " · 已过期" : ""}</summary><p>规则按该章完整已保存副本执行，对照范围见本批覆盖记录；未命中不代表没有情节或语义问题。</p><p>报告来源 {report.source.path} · 版本 {report.source.hash.slice(0, 12)}。{reportStale ? "材料已变化，原句属于旧报告，请重新检查。" : ""}</p>{report.issues.map((item) => <article className="manuscript-local-issue" key={item.id}><strong>第 {item.start_line}–{item.end_line} 行 · {item.message}</strong><blockquote>{item.quote}</blockquote><button type="button" className="analysis-source" disabled={reportStale} onClick={() => onOpen(report.source.path, item.start_line, report.source.hash)}>核对待审原句</button><p>{item.suggestion}</p>{item.related && <><small>对照 {item.related.path}:{item.related.start_line}–{item.related.end_line} · 版本 {item.related.hash.slice(0, 12)}</small><blockquote>{item.related.quote}</blockquote><button type="button" className="analysis-source" disabled={documents.find((document) => document.path === item.related!.path)?.hash !== item.related.hash} onClick={() => onOpen(item.related!.path, item.related!.start_line, item.related!.hash)}>核对历史原句</button></>}</article>)}<ul>{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
          {relevantJobs.length > 0 && <label className="analysis-field manuscript-job-select">本章 API 任务<select value={selectedJobId} onChange={(event) => setSelectedJobId(event.target.value)}><option value="">选择任务查看结果</option>{relevantJobs.map((item) => <option key={item.id} value={item.id}>{item.kind === "rewrite" ? "编辑候选" : "证据审稿"} · {stateLabels[item.state] || item.state} · {formatDate(item.created_at)}</option>)}</select></label>}
          {selectedJob && <><div className="manuscript-job-status">{activeStates.has(selectedJob.state) && <LoaderCircle className="spin" size={15} />}<span>{selectedJob.kind === "rewrite" ? "编辑任务" : "证据审稿"} · {stateLabels[selectedJob.state] || selectedJob.state} · 来源版本 {selectedJob.request?.document_hash?.slice(0, 12) || "未记录"}</span>{selectedJob.error && <p>{selectedJob.error}</p>}</div><p className="analysis-caption">任务实际范围：{selectedJob.request?.selection ? `[${selectedJob.request.selection.start_char}, ${selectedJob.request.selection.end_char}) 字符` : "整章"} · 编辑目标：{selectedJob.request?.editorial?.goals.map((goal) => goalOptions.find((item) => item.id === goal)?.label || goal).join("、") || "未记录"} · 强度：{({ conservative: "保守", standard: "标准", deep: "深入" } as Record<string, string>)[selectedJob.request?.editorial?.intensity || ""] || "未记录"}</p></>}
          {candidate && selectedJob?.kind === "review" && <details className="analysis-limits" open><summary>API 证据审稿结果{candidateStale ? " · 来源版本已变化" : ""}</summary>{candidateStale && <p>下方是旧版本审稿，请重新检查当前副本后判断。</p>}<pre>{candidate.content}</pre><p>审稿意见是候选判断，请核对原句；不会自动修改副本。</p></details>}
          {selectedJob?.editorial_context && <details className="analysis-limits"><summary>本次 API 编辑材料覆盖</summary><p>{selectedJob.editorial_context.prompt_budget ? `实际提示词 ${selectedJob.editorial_context.prompt_budget.characters.toLocaleString()} / ${selectedJob.editorial_context.prompt_budget.limit.toLocaleString()} 字符。` : ""}仅使用下列明确绑定的来源；未提供材料不代表不存在。</p><ul>{Object.entries(selectedJob.editorial_context.sources).map(([path, hash]) => <li key={path}>{path} · 版本 {hash.slice(0, 12)}</li>)}</ul>{selectedJob.editorial_context.omitted.length > 0 && <p>另有 {selectedJob.editorial_context.omitted.length} 项未纳入，请在任务记录中核对覆盖详情。</p>}</details>}
          {candidate && selectedJob?.kind === "rewrite" && <div className="manuscript-candidate"><div className="analysis-result-heading"><h3>3 · 审阅差异后接受到副本</h3><span className="tag">{candidateStale ? "候选来源已变化" : "等待作者决定"}</span></div>{candidateStale ? <><div className="analysis-warning">当前副本、候选或来源版本与任务不一致，已禁止接受与旧版本差异定位；请重新读取后生成候选。</div><details className="analysis-limits"><summary>仅查看旧候选文本</summary><pre>{candidate.content}</pre></details></> : <Diff before={document.content} after={candidate.content} />}<button type="button" className="button primary small" disabled={!!busy || candidateStale} onClick={() => void accept()}><Check size={15} />接受到本章修订副本</button><p className="analysis-caption">接受只更新本次独立副本。请复查后独立导出；不会自动发布到正式章节或记忆。</p></div>}
        </>}
      </div></div></>}
    </section><p className="analysis-footer"><ShieldCheck size={13} />原稿、修订副本与正式作品分开管理；来源文本中的命令不构成作者批准。</p>
  </div>;
}
