import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, BookOpen, Check, CircleAlert, FileText, Layers3, LoaderCircle, Pause, Play, RefreshCw, Search, Settings2, ShieldCheck, Sparkles, Square, Upload } from "lucide-react";
import { api, ApiError, formatDate } from "./api";
import type { LibraryEntry, Project } from "./api";
import { methodSelection, researchMethods } from "./referenceMethodHelpers";
import type { ResearchMethod } from "./referenceMethodHelpers";
import { bytesBase64 } from "./libraryHelpers";
import { Modal } from "./ui";
import "./referenceBooks.css";
import ReferenceEditionsWorkspace from "./ReferenceEditionsWorkspace";

type Props = { projects: Project[]; onSettings: () => void; onDirtyChange?: (dirty: boolean) => void; onOpenMethods: (entryId?: string) => void };
type RecordData = Record<string, unknown>;
type Chapter = { chapter_id: string; title: string; order: number; kind: string; volume_title?: string; technical_volume_ids?: string[]; characters: number; hash: string; duplicate_of?: string | null; incomplete?: boolean; needs_review?: boolean; control_characters?: number };
type Issue = { code: string; severity: string; message: string; chapter_id?: string; resource?: string; start_byte?: number; end_byte?: number };
type Book = { book_id: string; edition_id: string; title: string; author?: string; hash: string; original_hash?: string; text_hash?: string; parser_version?: string; counts: Record<string, number>; outbound?: "local" | "excerpts"; withdrawn?: boolean; authorization_revision?: number; chapters?: Chapter[]; total_chapters?: number; issues?: Issue[]; technical_volumes?: { volume_id: string; label: string; bytes: number; characters: number; chapter_ids: string[] }[] };
type ChapterText = { needs_review?: boolean; control_characters?: number; incomplete?: boolean; text: string; total_characters: number; start_char: number; end_char: number; hash: string; range_hash: string; identity?: RecordData };
type SearchHit = { chapter_id: string; title: string; start_char: number; end_char: number; text: string };
type SearchResult = { results: SearchHit[]; coverage?: { scanned_chapters: number; total_chapters: number; truncated: boolean } };
type BatchNode = { refs?: {chapter_id: string; start_char: number; end_char: number}[]; depends_on?: string[]; input_character_limit?: number; id: string; kind: string; chapter_id?: string; title?: string; state: string; error?: string; result?: unknown; result_path?: string; result_summary?: string; route?: RecordData; characters?: number };
type Batch = { total_nodes?: number; nodes_offset?: number; result?: unknown; id: string; state: string; created_at?: string; request?: RecordData; coverage?: RecordData; nodes?: BatchNode[]; budget?: RecordData; budget_summary?: RecordData; error?: string; [key: string]: unknown };
type BatchPreview = { total_nodes?: number; hash: string; request?: RecordData; nodes?: BatchNode[]; coverage?: RecordData; estimated_cost_usd?: number | null; budget?: RecordData; warnings?: string[]; [key: string]: unknown };
type PreviewState = { data: BatchPreview; body: RecordData; identity: string; key: string };
type EvidenceRange = {chapter_id: string; start_char: number; end_char: number};
type Tab = "read" | "issues" | "research";
const CHAPTER_PAGE = 100, TEXT_PAGE = 24000, NODE_PAGE = 50;
const base = "/reference-books";
const bookUrl = (id: string, suffix = "") => `${base}/${encodeURIComponent(id)}${suffix}`;
const object = (value: unknown): RecordData => value && typeof value === "object" && !Array.isArray(value) ? value as RecordData : {};
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : 0;
const amount = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(4)}` : "报价未知";
const states: Record<string, string> = { candidate: "待核线索", unresolved: "仍开放", recovered: "候选判断：已回收", unread: "尚未模型阅读", not_verified: "尚未核实发行版本完整性", prepared: "已准备，等待启动", queued: "等待开始", pending: "待处理", ready: "可开始", running: "正在分析", paused: "已暂停", pausing: "完成当前请求后暂停", succeeded: "已完成", completed: "已完成", failed: "失败，待核对", cancelled: "已取消", interrupted: "请求结果待核对", stale: "来源或配置已变化", blocked: "已停止，需处理" };
const nodeDone = (state: string) => ["succeeded", "completed"].includes(state);
const active = (state: string) => ["running", "pausing"].includes(state);
const message = (failure: unknown) => failure instanceof ApiError && failure.status === 409 ? "资料、授权或模型版本已变化。输入仍保留，请刷新书籍后重新预览。" : failure instanceof ApiError && failure.status < 500 ? failure.message.slice(0, 350) : "请求未完成或结果尚不明确。请刷新核对本机记录；不要重复创建新批次。";
const purposes = [{ id: "chapter_structure", label: "章节结构与表达基础阅读" }, { id: "content_arrangement", label: "内容安排与阶段推进" }, { id: "character_relationships", label: "人物动机与关系变化" }, { id: "information_threads", label: "信息差、悬念与线索" }, { id: "expression", label: "叙述、对白与表达作用" }];

function ResultContent({ value, onEvidence }: { value: unknown; onEvidence?: (range: EvidenceRange) => void }) {
  if (value == null) return null;
  if (typeof value !== "object") return <p className="rb-report-text">{typeof value === "boolean" ? value ? "是" : "否" : states[String(value)] || String(value)}</p>;
  if (Array.isArray(value)) return <ul>{value.map((item, index) => <li key={index}><ResultContent value={item} onEvidence={onEvidence} /></li>)}</ul>;
  const record = value as RecordData;
  if (typeof record.start_char === "number" && typeof record.end_char === "number") return <div className="rb-evidence"><small>Unicode 字符 [{record.start_char}, {record.end_char})</small>{typeof record.quote === "string" && <blockquote>{record.quote}</blockquote>}{typeof record.chapter_id === "string" && onEvidence && <button className="text-button" onClick={() => onEvidence({chapter_id: String(record.chapter_id), start_char: Number(record.start_char), end_char: Number(record.end_char)})}>回查依据原文<ArrowRight size={12} /></button>}</div>;
  const labels: Record<string, string> = { ok: "归档指纹校验通过", completeness: "版本完整性", analysis_status: "模型阅读", issues: "核验问题", source_refs: "原文依据", setup_refs: "铺设依据", payoff_refs: "揭示依据", gaps: "未读与待核范围", observation: "观察", status: "候选状态", inputs: "所需输入", steps: "步骤", limitations: "适用边界", checks: "检查项", claim: "覆盖说明", input_refs: "实际发送范围", unread_chapters: "尚未完成章节", empty_chapters: "空章段", reserved_cost_usd: "预留或已计费用", uncertain: "是否存在待核对费用", budget: "共享预算", title: "标题", summary: "概览", structure: "内容结构", expression: "表达观察", observations: "观察", scenes: "场景", events: "事件与因果", characters: "人物", relationships: "关系变化", information: "信息分配", threads: "线索", methods: "方法候选", method: "方法结构", purpose: "用途", applies_to: "适用情境", outputs: "预期输出", counterexamples: "边界与反例", method_candidates: "方法候选", uncertainties: "待核实", evidence: "原文依据", quote: "原句", explanation: "解释", rationale: "依据", content: "分析", text: "说明", result: "候选结果", coverage: "实际覆盖", selected_chapters: "所选章节", total_chapters: "全书章节", completed_chapters: "已完成章节", analyzed_characters: "已分析字符", sent_characters: "实际发送字符", state: "状态", total_calls: "本工程调用总数", total_tokens: "本工程Token总上界", estimated_cost_usd: "保守估算费用", price_required: "须配置模型报价", max_cost_usd: "金额上限", budget_max_usd: "本批金额上限", cost_usd: "已计费用", spent_usd: "已计费用", reserved_usd: "已预留", max_tokens: "Token上限", max_calls: "调用上限", used_tokens: "已用Token", calls: "调用次数", warnings: "待处理说明", goal: "目标", action: "行动", outcome: "结果", changes: "变化", open_questions: "未决问题", narrative_function: "叙事作用", start_char: "起始字符", end_char: "结束字符", source_asserted: "原文陈述", inferred: "分析推断", suggested: "候选建议" };
  const hidden = new Set(["id", "hash", "novel_id", "book_id", "edition_id", "parser_version", "configuration", "typed_deps", "snapshot", "request", "route", "prompt", "messages", "idempotency_key", "authorization_revision", "original_hash", "text_hash", "range_hash", "node_id", "batch_id", "chapter_ids", "depends_on"]);
  return <dl className="rb-report">{Object.entries(value).filter(([key]) => !hidden.has(key)).map(([key, item]) => <div key={key}><dt>{labels[key] || key.replace(/_/g, " ")}</dt><dd><ResultContent value={key === "state" && typeof item === "string" ? states[item] || item : item} onEvidence={onEvidence} /></dd></div>)}</dl>;
}

export default function ReferenceBooksWorkspace({ projects, onSettings, onDirtyChange, onOpenMethods }: Props) {
  const [books, setBooks] = useState<Book[]>([]), [book, setBook] = useState<Book | null>(null);
  const [tab, setTab] = useState<Tab>("read"), [directoryOffset, setDirectoryOffset] = useState(0);
  const [chapter, setChapter] = useState<Chapter | null>(null), [chapterText, setChapterText] = useState<ChapterText | null>(null);
  const [chapterEdition, setChapterEdition] = useState("");
  const [outbound, setOutbound] = useState<"local" | "excerpts">("local"), [withdrawn, setWithdrawn] = useState(false);
  const [query, setQuery] = useState(""), [search, setSearch] = useState<SearchResult | null>(null);
  const [importOpen, setImportOpen] = useState(false), [importFile, setImportFile] = useState<File | null>(null);
  const [title, setTitle] = useState(""), [author, setAuthor] = useState(""), [encoding, setEncoding] = useState("auto");
  const [allSelected, setAllSelected] = useState(false);
  const [selected, setSelected] = useState<string[]>([]), [billingId, setBillingId] = useState("");
  const [purpose, setPurpose] = useState(purposes[0].id), [budget, setBudget] = useState("1"), [outputLimit, setOutputLimit] = useState(4000);
  const [preview, setPreview] = useState<PreviewState | null>(null), [uncertain, setUncertain] = useState(false);
  const [batches, setBatches] = useState<Batch[]>([]), [batch, setBatch] = useState<Batch | null>(null);
  const [previewOffset, setPreviewOffset] = useState(0), [batchOffset, setBatchOffset] = useState(0);
  const [nodeResult, setNodeResult] = useState<{batchId: string; nodeId: string; result: unknown} | null>(null);
  const [methodContext, setMethodContext] = useState<{bookId: string; editionId: string; batchId: string; nodeId: string; result: unknown} | null>(null);
  const [methodDirty, setMethodDirty] = useState(false);
  const [editionBook, setEditionBook] = useState<string | null>(null), [editionDirty, setEditionDirty] = useState(false), [editionBusy, setEditionBusy] = useState(false);
  const [verify, setVerify] = useState<RecordData | null>(null), [busy, setBusy] = useState("");
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const mounted = useRef(true), gate = useRef(false), directoryEpoch = useRef(0), bookEpoch = useRef(0), textEpoch = useRef(0), selectedId = useRef(""), batchMutation = useRef(0);
  const dirtyCallback = useRef(onDirtyChange); dirtyCallback.current = onDirtyChange;
  const policyDirty = !!book && (outbound !== (book.outbound || "local") || withdrawn !== !!book.withdrawn);
  const importDirty = importOpen && (!!importFile || !!title || !!author);
  const dirty = policyDirty || importDirty || methodDirty || editionDirty;
  const identity = JSON.stringify([book?.book_id, book?.hash, allSelected, selected, billingId, purpose, budget, outputLimit]);
  const identityRef = useRef(identity); identityRef.current = identity;
  const currentChapters = book?.chapters || [], totalChapters = book?.total_chapters ?? book?.counts.chapters ?? 0;
  const selectedProject = projects.find((item) => item.novel_id === billingId);
  const frozenPreview = !!preview && preview.identity === identity;
  const currentBatchId = batch?.id;
  const currentBatchRef = useRef(currentBatchId); currentBatchRef.current = currentBatchId;

  useEffect(() => { dirtyCallback.current?.(dirty); }, [dirty]);
  useEffect(() => () => dirtyCallback.current?.(false), []);
  useEffect(() => {
    mounted.current = true;
    void action("books", async () => { const result = await api<{ books: Book[] }>(base); if (mounted.current) setBooks(result.books); });
    return () => { mounted.current = false; bookEpoch.current += 1; textEpoch.current += 1; };
  }, []);
  useEffect(() => {
    if (!book || tab !== "research") return;
    const id = book.book_id, epoch = bookEpoch.current; let cancelled = false;
    const read = async () => {
      const mutation = batchMutation.current;
      try {
        const result = await api<{ batches: Batch[] }>(bookUrl(id, "/batches"));
        if (cancelled || !valid(epoch, id) || mutation !== batchMutation.current) return;
        setBatches(result.batches);
        if (currentBatchId) {
          const detail = await api<Batch>(bookUrl(id, `/batches/${encodeURIComponent(currentBatchId)}?offset=${batchOffset}&limit=${NODE_PAGE}`));
          if (!cancelled && valid(epoch, id) && mutation === batchMutation.current) setBatch(detail);
        }
      } catch (failure) { if (!cancelled && valid(epoch, id)) setError(message(failure)); }
    };
    void read(); const timer = window.setInterval(() => void read(), 4000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [book?.book_id, tab, currentBatchId, batchOffset]);

  function valid(epoch: number, id: string) { return mounted.current && epoch === bookEpoch.current && selectedId.current === id; }
  async function action(name: string, operation: () => Promise<void>) {
    if (gate.current) return;
    gate.current = true; setBusy(name); setError(""); setNotice("");
    try { await operation(); } catch (failure) { if (mounted.current) setError(message(failure)); }
    finally { gate.current = false; if (mounted.current) setBusy(""); }
  }
  async function reloadBooks() { const result = await api<{ books: Book[] }>(base); if (mounted.current) setBooks(result.books); }
  async function openBook(id: string, offset = 0, switchBook = false) {
    if (switchBook && dirty && !window.confirm("本页有未保存的设置或方法选择，确认放弃修改并打开另一本书？")) return;
    const switching = switchBook || selectedId.current !== id;
    if (switching) { bookEpoch.current += 1; textEpoch.current += 1; selectedId.current = id; }
    const epoch = bookEpoch.current, directoryRequest = ++directoryEpoch.current;
    const value = await api<Book>(`${bookUrl(id)}?offset=${offset}&limit=${CHAPTER_PAGE}`);
    if (!valid(epoch, id) || directoryRequest !== directoryEpoch.current) return;
    setBook(value); setDirectoryOffset(offset); setVerify(null);
    if (switchBook) { setMethodContext(null); setMethodDirty(false); setTab("read"); setChapter(null); setChapterText(null); setOutbound(value.outbound || "local"); setWithdrawn(!!value.withdrawn); setSelected([]); setAllSelected(false); setSearch(null); setQuery(""); setPreview(null); setUncertain(false); setBatch(null); setBatches([]); setNodeResult(null); setPreviewOffset(0); setBatchOffset(0); }
  }
  async function openChapter(item: Chapter, offset = 0, editionId = book?.edition_id) {
    if (!book) return;
    const id = book.book_id, epoch = bookEpoch.current, request = ++textEpoch.current;
    setChapter(item); setChapterText(null); setChapterEdition(editionId || "");
    const value = await api<ChapterText>(`${bookUrl(id, `/chapters/${encodeURIComponent(item.chapter_id)}`)}?offset=${offset}&limit=${TEXT_PAGE}&edition_id=${encodeURIComponent(editionId || "")}`);
    if (valid(epoch, id) && request === textEpoch.current) { setChapterText(value); setTab("read"); }
  }
  async function importBook() {
    if (!importFile) return;
    if (importFile.size > 128 * 1024 * 1024) { setError("原件超过128 MiB，请保留原件并在本机整理后导入较小的分卷文件。"); return; }
    const value = await api<Book>(`${base}/import`, "POST", { filename: importFile.name, content_base64: bytesBase64(await importFile.arrayBuffer()), ...(title.trim() ? { title: title.trim() } : {}), ...(author.trim() ? { author: author.trim() } : {}), ...(encoding === "auto" ? {} : { encoding }) });
    if (!mounted.current) return;
    setImportOpen(false); setImportFile(null); setTitle(""); setAuthor(""); setEncoding("auto");
    await reloadBooks(); await openBook(value.book_id, 0, true);
    if (mounted.current) setNotice("原件已保存并建立目录与技术分册。未调用模型；请先核对异常和正文范围。");
  }
  async function savePolicy() {
    if (!book) return;
    const id = book.book_id, epoch = bookEpoch.current;
    const value = await api<Book>(bookUrl(id, "/policy"), "PUT", { expected_hash: book.hash, outbound, withdrawn });
    if (!valid(epoch, id)) return;
    setBook((current) => current ? { ...current, ...value, chapters: current.chapters } : value);
    setOutbound(value.outbound || "local"); setWithdrawn(!!value.withdrawn); setPreview(null);
    setNotice("外发设置已保存。批次仍需预览并由你启动；旧预览与授权不会自动恢复。"); await reloadBooks();
  }
  async function searchBook() {
    if (!book || !query.trim()) return;
    const id = book.book_id, epoch = bookEpoch.current, term = query.trim();
    const result = await api<SearchResult>(`${bookUrl(id, "/search")}?q=${encodeURIComponent(term)}`);
    if (valid(epoch, id)) setSearch(result);
  }
  function changeSelection(ids: string[]) { if (uncertain) return; setAllSelected(false); setSelected(ids); setPreview(null); }
  async function previewBatch() {
    if (!book || (!allSelected && !selected.length) || !billingId || policyDirty || book.withdrawn || book.outbound !== "excerpts") return;
    const id = book.book_id, epoch = bookEpoch.current, inputIdentity = identity;
    const body = { novel_id: billingId, chapter_ids: allSelected ? [] : selected, purpose: purposes.find((item) => item.id === purpose)?.label || purpose, budget_max_usd: Number(budget), max_output_tokens: outputLimit };
    const result = await api<BatchPreview>(bookUrl(id, "/batches/preview"), "POST", body);
    if (valid(epoch, id) && inputIdentity === identityRef.current) { setPreview({ data: result, body, identity: inputIdentity, key: crypto.randomUUID() }); setUncertain(false); setPreviewOffset(0); }
  }
  async function createBatch() {
    if (!book || !preview || !frozenPreview || policyDirty) return;
    const id = book.book_id, epoch = bookEpoch.current;
    let result: Batch;
    try { result = await api<Batch>(bookUrl(id, "/batches"), "POST", { ...preview.body, expected_preview: preview.data.hash, idempotency_key: preview.key }); }
    catch (failure) { if (valid(epoch, id)) { if (!(failure instanceof ApiError) || failure.status >= 500) setUncertain(true); else setPreview(null); } throw failure; }
    if (!valid(epoch, id)) return;
    batchMutation.current += 1; setBatch(result); setBatchOffset(0); setNodeResult(null); setBatches((current) => [result, ...current.filter((item) => item.id !== result.id)]); setPreview(null); setUncertain(false);
    setNotice("批次已保存；启动前可再次核对章节、模型与计费作品。");
  }
  async function controlBatch(operation: string) {
    if (!book || !batch) return;
    const id = book.book_id, epoch = bookEpoch.current;
    batchMutation.current += 1;
    const result = await api<Batch>(bookUrl(id, `/batches/${encodeURIComponent(batch.id)}/${operation}`), "POST", {});
    if (valid(epoch, id)) { setBatch(result); setBatchOffset(0); setBatches((current) => current.map((item) => item.id === result.id ? result : item)); setNotice(operation === "pause" ? "已请求暂停。当前进行中的请求完成后停止后续节点并落账。" : operation === "cancel" ? "已取消后续节点；已发生的模型调用仍会保留账目与结果。" : "已请求继续；完成节点复用，结果不明的付费请求不自动重发。"); }
  }
  async function readBatchPage(offset: number) {
    if (!book || !batch) return;
    const id = book.book_id, epoch = bookEpoch.current, batchId = batch.id;
    const result = await api<Batch>(bookUrl(id, `/batches/${encodeURIComponent(batchId)}?offset=${offset}&limit=${NODE_PAGE}`));
    if (valid(epoch, id) && currentBatchRef.current === batchId) { batchMutation.current += 1; setBatch(result); setBatchOffset(offset); }
  }
  function inspectEvidence(range: EvidenceRange) {
    void action("chapter", () => openChapter(currentChapters.find((item) => item.chapter_id === range.chapter_id) || {chapter_id: range.chapter_id, title: "分析依据原文", order: 0, kind: "unknown", characters: 0, hash: ""}, range.start_char, typeof batch?.edition_id === "string" ? batch.edition_id : book?.edition_id));
  }
  async function readNode(node: BatchNode) {
    if (!book || !batch) return;
    const id = book.book_id, epoch = bookEpoch.current, batchId = batch.id;
    const result = await api<RecordData>(bookUrl(id, `/batches/${encodeURIComponent(batchId)}/nodes/${encodeURIComponent(node.id)}`));
    if (valid(epoch, id) && currentBatchRef.current === batchId) setNodeResult({batchId, nodeId: node.id, result: result.result ?? result});
  }
  function renderNodeResult(node: BatchNode) {
    if (!batch || !book) return null;
    const result = nodeResult?.batchId === batch.id && nodeResult.nodeId === node.id ? nodeResult.result : node.result;
    if (result != null) return <><ResultContent value={result} onEvidence={inspectEvidence} />{nodeDone(node.state) && (researchMethods(result).length ? <button className="button secondary small" disabled={!!busy} onClick={() => setMethodContext({bookId: book.book_id, editionId: typeof batch.edition_id === "string" ? batch.edition_id : book.edition_id, batchId: batch.id, nodeId: node.id, result})}><Sparkles size={13} />整理为个人方法候选（{researchMethods(result).length}）</button> : <p className="rb-muted">此节点没有方法候选；可以保留分析观察，不能凭空转为方法卡。</p>)}</>;
    return node.result_path || nodeDone(node.state) ? <button className="button secondary small" disabled={!!busy} onClick={() => void action("node-result", () => readNode(node))}><FileText size={13} />查看完整候选分析</button> : <p className="rb-muted">该节点尚无已保存的分析结果。</p>;
  }
  function closeEditions() { if (editionBusy) return; if (!editionDirty || window.confirm("目录或补丁尚未保存，确认关闭并放弃修改？")) {setEditionBook(null); setEditionDirty(false);} }
  function editionPublished() {
    const id = editionBook;
    if (!id) return;
    void action("edition-refresh", async () => {
      await reloadBooks();
      if (selectedId.current === id) { textEpoch.current += 1; setChapter(null); setChapterText(null); setSelected([]); setAllSelected(false); setSearch(null); setPreview(null); setUncertain(false); await openBook(id, 0); }
    });
  }
  function closeImport() { if (!busy && (!importDirty || window.confirm("本次导入尚未保存，确认关闭并放弃填写？"))) { setImportOpen(false); setImportFile(null); setTitle(""); setAuthor(""); } }

  return <section className="reference-books" aria-label="参考书架">
    <div className="rb-heading"><div><span className="rb-eyebrow"><BookOpen size={15} /> 让整本书成为可回查的参考</span><h2>参考书架 <span>{books.length}</span></h2><p>保留原文，逐章理解内容安排，再连接方法与表达。导入与阅读均在本机完成。</p></div><div className="rb-actions"><button className="button secondary small" disabled={!!busy || policyDirty} onClick={() => void action("refresh", async () => { await reloadBooks(); if (book) await openBook(book.book_id, directoryOffset); })} aria-label="刷新参考书架"><RefreshCw size={14} /></button><button className="button primary small" disabled={!!busy} onClick={() => setImportOpen(true)}><Upload size={14} />导入参考书</button></div></div>
    {error && <div className="rb-alert error" role="alert"><CircleAlert size={17} /><span>{error}</span><button className="text-button" onClick={onSettings}>模型设置</button></div>}
    {notice && <p className="rb-alert success" role="status"><Check size={16} />{notice}</p>}
    <div className={`rb-layout ${book ? "has-book" : ""}`}>
      <aside className="rb-shelf" aria-label="已归档的参考书">{busy === "books" && <p className="rb-muted"><LoaderCircle className="spin" size={16} />正在读取书架…</p>}{books.map((item, index) => <button key={item.book_id} className={`rb-book ${book?.book_id === item.book_id ? "selected" : ""}`} disabled={!!busy} onClick={() => void action("book", () => openBook(item.book_id, 0, true))}><span className={`rb-cover tone-${index % 4}`}><BookOpen size={24} /><span>{item.title.slice(0, 12)}</span></span><span className="rb-book-info"><strong>{item.title}</strong><small>{item.author || "作者待核对"}</small><small>{number(item.counts?.chapters).toLocaleString()} 章段 · {number(item.counts?.characters).toLocaleString()} 字符</small><span className="rb-badge">{item.withdrawn ? "已撤回" : item.outbound === "excerpts" ? "允许所选章节外发" : "仅本地"}</span></span></button>)}{!books.length && !busy && <div className="rb-empty"><BookOpen size={32} /><h3>把第一本参考书放进来</h3><p>支持 EPUB、TXT、MD。原件、目录与派生文本分别保留，大文件按章节建立技术分册。</p><button className="button secondary small" onClick={() => setImportOpen(true)}><Upload size={14} />选择本机文件</button></div>}</aside>
      {book ? <article className="rb-detail"><header className="rb-detail-heading"><div><h2>{book.title}</h2><p>{book.author || "作者待核对"} · {totalChapters.toLocaleString()} 章段 · {number(book.counts.characters).toLocaleString()} 字符</p></div><span className="rb-badge">{book.withdrawn ? "已撤回" : book.outbound === "excerpts" ? "允许所选章节外发" : "仅本地保存"}</span></header>
        <div className="rb-metrics"><span><strong>{number(book.counts.technical_volumes)}</strong>技术分册</span><span><strong>{number(book.counts.duplicate_candidates)}</strong>重复候选</span><span><strong>{number(book.counts.encoding_gaps)}</strong>编码异常</span><span><strong>{number(book.counts.control_characters)}</strong>待核控制字符</span></div>
        <p className="rb-muted rb-coverage-note">解析目录与模型阅读分别记录。实际阅读范围和覆盖在研究批次内查看；解析成功不代表已读整书。</p>
        <nav className="rb-tabs" aria-label="参考书内容"><button aria-current={tab === "read" ? "page" : undefined} className={tab === "read" ? "active" : ""} onClick={() => setTab("read")}><FileText size={14} />原文与目录</button><button aria-current={tab === "issues" ? "page" : undefined} className={tab === "issues" ? "active" : ""} onClick={() => setTab("issues")}><ShieldCheck size={14} />核对与外发</button><button aria-current={tab === "research" ? "page" : undefined} className={tab === "research" ? "active" : ""} onClick={() => setTab("research")}><Sparkles size={14} />逐章研究</button><button disabled={!!busy || policyDirty || uncertain} onClick={() => setEditionBook(book.book_id)}><Layers3 size={14} />目录与修订</button></nav>
        {tab === "read" && <><form className="rb-search" onSubmit={(event) => { event.preventDefault(); void action("search", searchBook); }}><Search size={16} /><input value={query} aria-label="检索这本书的原文" placeholder="查名称、原句或关键词" onChange={(event) => { setQuery(event.target.value); setSearch(null); }} /><button className="button secondary small" disabled={!!busy || !query.trim()}>本地查找</button></form>{search && <section className="rb-search-results"><h3>词项检索 · {search.results.length} 处结果</h3><p className="rb-muted">{search.coverage ? `扫描 ${search.coverage.scanned_chapters}/${search.coverage.total_chapters} 章段${search.coverage.truncated ? "，达到本次扫描上限" : ""}。` : ""}结果提供原文线索，不代表语义判断。</p>{search.results.map((hit) => <button key={`${hit.chapter_id}-${hit.start_char}`} disabled={!!busy} onClick={() => void action("chapter", () => openChapter({ chapter_id: hit.chapter_id, title: hit.title, order: 0, kind: "unknown", characters: 0, hash: "" }, hit.start_char))}><strong>{hit.title}</strong><span>{hit.text}</span><small>字符 [{hit.start_char}, {hit.end_char}) · 打开原文<ArrowRight size={12} /></small></button>)}{!search.results.length && <p className="rb-muted">本次未查到匹配项。</p>}</section>}
          <div className={`rb-reading ${chapter ? "opened" : ""}`}><section className="rb-directory"><div className="rb-section-heading"><h3>章节目录</h3><small>{directoryOffset + 1}–{Math.min(directoryOffset + currentChapters.length, totalChapters)} / {totalChapters}</small></div>{currentChapters.map((item) => <button className={`rb-chapter ${item.chapter_id === chapter?.chapter_id ? "selected" : ""}`} key={item.chapter_id} disabled={!!busy} onClick={() => void action("chapter", () => openChapter(item))}><span><strong>{item.title}</strong><small>{item.volume_title ? `${item.volume_title} · ` : ""}{item.kind === "ancillary" ? "附属文本" : item.kind === "body" ? "正文" : "类型待核"} · {item.characters.toLocaleString()} 字符{item.duplicate_of ? " · 重复候选" : ""}{item.incomplete ? " · 存在缺口" : item.needs_review ? " · 控制字符待核" : ""}</small></span><ArrowRight size={12} /></button>)}<div className="rb-pagination"><button className="text-button" disabled={!!busy || directoryOffset <= 0} onClick={() => void action("directory", () => openBook(book.book_id, Math.max(0, directoryOffset - CHAPTER_PAGE)))}><ArrowLeft size={13} />上一页</button><button className="text-button" disabled={!!busy || directoryOffset + CHAPTER_PAGE >= totalChapters} onClick={() => void action("directory", () => openBook(book.book_id, directoryOffset + CHAPTER_PAGE))}>下一页<ArrowRight size={13} /></button></div></section>{chapter && <section className="rb-reader"><div className="rb-section-heading"><h3>{chapter.title}</h3><button className="text-button" onClick={() => { textEpoch.current += 1; setChapter(null); setChapterText(null); }}>收起原文</button></div>{chapterText ? <><p className="rb-muted">Unicode字符 [{chapterText.start_char}, {chapterText.end_char}) / {chapterText.total_characters.toLocaleString()} · 固定原文</p>{(chapter.needs_review || chapterText.needs_review) && <p className="rb-alert"><CircleAlert size={15} />这章含未解码缺口或控制字符，原字节与定位已保留。核对前不进入模型分析。</p>}<pre>{chapterText.text}</pre><div className="rb-pagination"><button className="text-button" disabled={!!busy || chapterText.start_char <= 0} onClick={() => void action("chapter", () => openChapter(chapter, Math.max(0, chapterText.start_char - TEXT_PAGE), chapterEdition))}><ArrowLeft size={13} />上一段</button><button className="text-button" disabled={!!busy || chapterText.end_char >= chapterText.total_characters} onClick={() => void action("chapter", () => openChapter(chapter, chapterText.end_char, chapterEdition))}>下一段<ArrowRight size={13} /></button></div><details className="rb-identity"><summary>原文版本与指纹</summary><p>原章 SHA256：{chapterText.hash}</p><p>当前片段 SHA256：{chapterText.range_hash}</p></details></> : <p className="rb-muted">正在读取原文…</p>}</section>}</div>
          <details className="rb-volumes"><summary><Layers3 size={14} />技术分册 · 保存边界独立于原书卷和故事阶段</summary>{(book.technical_volumes || []).map((volume) => <p key={volume.volume_id}><strong>{volume.label}</strong> · {(volume.bytes / 1024 / 1024).toFixed(2)} MiB · {volume.chapter_ids.length} 章段</p>)}</details></>}
        {tab === "issues" && <section className="rb-checks"><h3>先核对，再使用</h3><p className="rb-muted">原件不改写。重复、编码缺口、附记和目录问题保留记录；文件解析成功不能证明发行版本没有缺章。</p>{(book.issues || []).map((issue, index) => <article className="rb-issue" key={`${issue.code}-${index}`}><CircleAlert size={16} /><div><strong>{issue.message}</strong>{issue.resource && <small>{issue.resource}</small>}{issue.start_byte !== undefined && <small>原始字节 [{issue.start_byte}, {issue.end_byte})</small>}{issue.chapter_id && <button className="text-button" disabled={!!busy} onClick={() => void action("chapter", () => openChapter(currentChapters.find((item) => item.chapter_id === issue.chapter_id) || { chapter_id: issue.chapter_id!, title: "问题所在章段", order: 0, kind: "unknown", characters: 0, hash: "" }))}>查看相关原文<ArrowRight size={12} /></button>}</div></article>)}{!book.issues?.length && <p className="rb-muted">当前预检未报告异常；尚不能据此认定内容完整。</p>}<button className="button secondary small" disabled={!!busy} onClick={() => void action("verify", async () => { const id = book.book_id, epoch = bookEpoch.current; const report = await api<RecordData>(bookUrl(id, "/verify"), "POST", {}); if (valid(epoch, id)) setVerify(report); })}><ShieldCheck size={14} />核验归档完整性</button>{verify && <div className="rb-verification"><ResultContent value={verify} /></div>}
          <div className="rb-policy"><h3>模型能否读取这本参考书</h3><label>资料外发范围<select disabled={!!busy} value={outbound} onChange={(event) => { setOutbound(event.target.value as "local" | "excerpts"); setPreview(null); }}><option value="local">仅本地保存与检索</option><option value="excerpts">允许发送明确选中的章节片段</option></select></label><label className="rb-checkbox"><input type="checkbox" disabled={!!busy} checked={withdrawn} onChange={(event) => { setWithdrawn(event.target.checked); setPreview(null); }} />撤回这本书的模型使用授权</label><p className="rb-muted">允许外发后仍需选择范围、计费作品并预览模型与预算。保存资料不读取作者稿，也不自动开始付费分析。</p><button className="button primary small" disabled={!!busy || !policyDirty} onClick={() => void action("policy", savePolicy)}><Check size={14} />保存外发设置</button>{policyDirty && <span className="rb-unsaved">有未保存的设置</span>}</div><details className="rb-identity"><summary>归档与来源版本</summary><p>原件 SHA256：{book.original_hash}</p><p>规范文本 SHA256：{book.text_hash}</p><p>解析器：{book.parser_version}</p></details></section>}
        {tab === "research" && <section className="rb-research"><h3>从逐章阅读建立结构与表达资料</h3><p className="rb-muted">先分析所选章段，再归并本批的内容安排与表达观察。结果是候选研究，尚不等于整本书的结构图、有效方法或作者小说事实。</p>{(book.outbound !== "excerpts" || book.withdrawn || policyDirty) && <p className="rb-alert"><ShieldCheck size={16} />请先在“核对与外发”保存所选片段的外发授权。</p>}<div className="rb-research-form"><div className="rb-section-heading"><h3>选择章段 <span>{allSelected ? "全书全部章段" : `${selected.length} 项`}</span></h3><div className="rb-actions"><button className="text-button" disabled={!!busy || uncertain} onClick={() => { setAllSelected(!allSelected); setSelected([]); setPreview(null); }}>{allSelected ? "取消全书选择" : "选择全书（含附记）"}</button><button className="text-button" disabled={!!busy || uncertain} onClick={() => changeSelection([...new Set([...selected, ...currentChapters.filter((item) => item.kind === "body" && !item.duplicate_of && !item.incomplete && !item.needs_review).map((item) => item.chapter_id)])])}>选本页完整正文</button><button className="text-button" disabled={!!busy || uncertain} onClick={() => changeSelection([])}>清空</button></div></div><div className="rb-selection">{currentChapters.map((item) => <label className="rb-checkbox" key={item.chapter_id}><input type="checkbox" checked={allSelected || selected.includes(item.chapter_id)} disabled={!!busy || uncertain || allSelected || !!item.incomplete || !!item.needs_review} onChange={(event) => changeSelection(event.target.checked ? [...selected, item.chapter_id] : selected.filter((id) => id !== item.chapter_id))} /><span>{item.title}<small>{item.characters.toLocaleString()} 字符{item.kind !== "body" ? " · 附属或待核文本" : ""}{item.duplicate_of ? " · 重复候选" : ""}{item.incomplete ? " · 存在缺口，暂不分析" : item.needs_review ? " · 含控制字符，核对前不分析" : ""}</small></span></label>)}</div><div className="rb-pagination"><button className="text-button" disabled={!!busy || directoryOffset <= 0 || uncertain} onClick={() => void action("directory", () => openBook(book.book_id, Math.max(0, directoryOffset - CHAPTER_PAGE)))}><ArrowLeft size={13} />上一页目录</button><button className="text-button" disabled={!!busy || directoryOffset + CHAPTER_PAGE >= totalChapters || uncertain} onClick={() => void action("directory", () => openBook(book.book_id, directoryOffset + CHAPTER_PAGE))}>下一页目录<ArrowRight size={13} /></button></div><p className="rb-muted">所选范围跨页保留。全书选择包含附属文本与重复候选；存在编码缺口或待核控制字符时预览会拒绝，可改选已完整解析的正文。超长章分段处理，实际范围以预览为准。</p><div className="rb-grid"><label>真实计费作品<select value={billingId} disabled={!!busy || uncertain} onChange={(event) => { setBillingId(event.target.value); setPreview(null); }}><option value="">请选择，不自动指定</option>{projects.map((item) => <option key={item.novel_id} value={item.novel_id}>{item.title}</option>)}</select></label><label>研究问题<select value={purpose} disabled={!!busy || uncertain} onChange={(event) => { setPurpose(event.target.value); setPreview(null); }}>{purposes.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label>本批总金额上限（美元）<input type="number" min="0.01" step="0.1" value={budget} disabled={!!busy || uncertain} onChange={(event) => { setBudget(event.target.value); setPreview(null); }} /></label><label>每节点输出 Token 上限<input type="number" min={256} max={16000} step={256} value={outputLimit} disabled={!!busy || uncertain} onChange={(event) => { setOutputLimit(Number(event.target.value)); setPreview(null); }} /></label></div><p className="rb-muted">计费作品只提供模型策略与共享预算，本次研究不读取它的正文与设定。未配置 API 或报价时先到设置页补充。</p><button className="button secondary" disabled={!!busy || uncertain || (!allSelected && !selected.length) || !billingId || !Number.isFinite(Number(budget)) || Number(budget) <= 0 || !Number.isInteger(outputLimit) || outputLimit < 256 || outputLimit > 16000 || policyDirty || book.outbound !== "excerpts" || book.withdrawn} onClick={() => void action("preview", previewBatch)}><Sparkles size={14} />预览范围、模型与总预算</button>{frozenPreview && preview && <div className="rb-preview"><h3>本次预览 · 尚未启动</h3><p>计费作品：<strong>{selectedProject?.title || "未选择"}</strong> · {allSelected ? "全书全部章段（含附记与重复候选）" : `${selected.length} 章段`} · 估算 {amount(object(preview.data.budget).estimated_cost_usd)}</p><ResultContent value={preview.data.coverage} /><ResultContent value={preview.data.budget} />{preview.data.warnings?.map((warning, index) => <p className="rb-muted" key={index}>{warning}</p>)}<details><summary>逐节点模型与实发范围</summary>{(preview.data.nodes || []).slice(previewOffset, previewOffset + NODE_PAGE).map((node) => <div key={node.id}><h4>{node.kind !== "chapter" ? "结构与表达归并" : node.title || "章段分析"}</h4><p>{number(node.characters).toLocaleString()} 字符</p>{(node.refs || []).map((ref) => <p className="rb-muted" key={`${ref.chapter_id}-${ref.start_char}`}>章内 Unicode 字符 [{ref.start_char}, {ref.end_char}) <button type="button" className="text-button" disabled={!!busy} onClick={() => void action("chapter", () => openChapter(currentChapters.find((item) => item.chapter_id === ref.chapter_id) || {chapter_id: ref.chapter_id, title: node.title || "分析片段", order: 0, kind: "unknown", characters: 0, hash: ""}, ref.start_char))}>回查原文</button></p>)}{node.kind !== "chapter" && <p className="rb-muted">基于 {node.depends_on?.length || 0} 个前置节点 · 最多 {node.input_character_limit?.toLocaleString() || "预览未提供"} 字符归并输入</p>}<ModelRoute value={node.route} /></div>)}<p className="rb-muted">本次预览展示前 {preview.data.nodes?.length || 0} / 共 {preview.data.total_nodes ?? preview.data.nodes?.length ?? 0} 个节点。其余节点可保存批次后逐页核对，保存不会启动模型请求。</p><NodePagination offset={previewOffset} total={preview.data.nodes?.length || 0} onChange={setPreviewOffset} /></details><button className="button primary" disabled={!!busy || !preview.data.hash} onClick={() => void action("create-batch", createBatch)}><Check size={14} />{uncertain ? "沿原提交键核对批次" : "保存本次分析批次"}</button>{uncertain && <p className="rb-alert">创建结果尚不明确。输入已锁定，再次提交沿用同一键；请先核对下方批次记录。</p>}</div>}</div>
          <section className="rb-batches"><div className="rb-section-heading"><h3>研究批次与覆盖</h3><button className="text-button" onClick={onSettings}><Settings2 size={13} />模型设置</button></div>{!batches.length && <p className="rb-muted">还没有研究批次。导入、翻页和本地检索不会自动生成任务。</p>}{batches.map((item) => <button className={`rb-batch-card ${batch?.id === item.id ? "selected" : ""}`} key={item.id} disabled={!!busy} onClick={() => void action("batch", async () => { const id = book.book_id, epoch = bookEpoch.current; const value = await api<Batch>(bookUrl(id, `/batches/${encodeURIComponent(item.id)}`)); if (valid(epoch, id)) { batchMutation.current += 1; setBatch(value); setBatchOffset(0); setNodeResult(null); } })}><span><strong>{states[item.state] || item.state}</strong><small>{formatDate(item.created_at)} · {item.total_nodes ?? item.nodes?.length ?? "—"} 个节点</small></span><ArrowRight size={14} /></button>)}{batch && <div className="rb-batch-detail"><div className="rb-section-heading"><h3>{states[batch.state] || batch.state}</h3><div className="rb-actions">{["prepared", "queued", "ready", "paused"].includes(batch.state) && <button className="button primary small" disabled={!!busy || policyDirty || book.withdrawn || book.outbound !== "excerpts"} onClick={() => void action("resume", () => controlBatch("start"))}><Play size={13} />{batch.state === "paused" ? "继续未完成分析" : "开始分析"}</button>}{active(batch.state) && <button className="button secondary small" disabled={!!busy} onClick={() => void action("pause", () => controlBatch("pause"))}><Pause size={13} />暂停后续节点</button>}{!["succeeded", "completed", "cancelled"].includes(batch.state) && <button className="button secondary small" disabled={!!busy} onClick={() => void action("cancel", () => controlBatch("cancel"))}><Square size={12} />取消后续分析</button>}</div></div>{batch.error && <p className="rb-alert error">{batch.error}</p>}<ResultContent value={batch.coverage} /><ResultContent value={batch.budget_summary || batch.budget} /><p className="rb-muted">暂停和关闭前端均保留后端记录。结果不明的付费请求先核对，不会自动重发；研究完成率不能证明文学解释正确。</p>{(batch.nodes || []).map((node) => <details className="rb-node" key={node.id}><summary><span className={`rb-state ${nodeDone(node.state) ? "done" : ""}`}>{states[node.state] || node.state}</span>{node.kind !== "chapter" ? "结构与表达归并" : node.title || "章段分析"}</summary>{node.error && <p className="rb-alert error">{node.error}</p>}{node.result_summary && <p className="rb-muted rb-report-text">{node.result_summary}</p>}{renderNodeResult(node)}</details>)}<NodePagination offset={batchOffset} total={batch.total_nodes ?? batch.nodes?.length ?? 0} disabled={!!busy} onChange={(offset) => void action("nodes", () => readBatchPage(offset))} />{batch.result != null && <details className="rb-node"><summary>阅读本批最终归并候选</summary><ResultContent value={batch.result} onEvidence={inspectEvidence} />{typeof batch.result_node_id === "string" && researchMethods(batch.result).length > 0 && <button className="button secondary small" disabled={!!busy} onClick={() => setMethodContext({bookId: book.book_id, editionId: typeof batch.edition_id === "string" ? batch.edition_id : book.edition_id, batchId: batch.id, nodeId: String(batch.result_node_id), result: batch.result})}><Sparkles size={13} />整理最终归并的方法候选</button>}</details>}</div>}</section>
        </section>}
      </article> : books.length > 0 && <div className="rb-welcome"><BookOpen size={38} /><h3>选一本书，先从原文与目录开始</h3><p>原文、结构、方法与表达相互连接。当前可本地阅读、核对、检索，再预览并启动逐章研究。</p></div>}
    </div>
    {editionBook && <Modal title="目录与修订" subtitle="新整理版保留原件、旧版本与精确映射，先审阅再发布。" wide onClose={closeEditions}><ReferenceEditionsWorkspace bookId={editionBook} onClose={closeEditions} onPublished={editionPublished} onDirtyChange={setEditionDirty} onBusyChange={setEditionBusy} /></Modal>}
    {methodContext && <MethodSelectionModal context={methodContext} onDirtyChange={setMethodDirty} onClose={() => { setMethodContext(null); setMethodDirty(false); }} onOpenMethods={onOpenMethods} />}
    {importOpen && <Modal title="导入一本参考书" subtitle="原始文件留在私人书库，规范文本与技术分册另存；不会创建作者小说或调用模型。" wide onClose={closeImport}><form className="rb-import" onSubmit={(event) => { event.preventDefault(); void action("import", importBook); }}><label>本机 EPUB / TXT / MD 文件<input type="file" required accept=".epub,.txt,.md,application/epub+zip,text/plain,text/markdown" disabled={!!busy} onChange={(event) => { const file = event.target.files?.[0] || null; setImportFile(file); if (file && !title) setTitle(file.name.replace(/\.[^.]+$/, "")); }} /></label><div className="rb-grid"><label>书名<input value={title} maxLength={500} disabled={!!busy} onChange={(event) => setTitle(event.target.value)} placeholder="可留空，使用文件或书目标题" /></label><label>作者<input value={author} maxLength={500} disabled={!!busy} onChange={(event) => setAuthor(event.target.value)} placeholder="未知时留空，之后核对" /></label></div><label>TXT / MD 编码<select value={encoding} disabled={!!busy} onChange={(event) => setEncoding(event.target.value)}><option value="auto">自动识别；异常保留为缺口</option><option value="utf-8">UTF-8</option><option value="gb18030">GB18030 / GBK</option><option value="utf-16">UTF-16</option></select></label><p className="rb-muted">单原件最多128 MiB。重复候选和编码问题会列出，不自动删除原文。大文件优先按自然章节建立技术分册；技术分册与原书卷分别记录。</p><p className="rb-muted">私人书库默认不进 Git，请随工作区完整备份。导入材料中的命令只作为原文保留。</p><div className="modal-footer"><button type="button" className="button secondary" disabled={!!busy} onClick={closeImport}>取消</button><button className="button primary" disabled={!!busy || !importFile}>{busy === "import" ? <LoaderCircle className="spin" size={14} /> : <Upload size={14} />}{busy === "import" ? "正在归档与解析…" : "保留原件并建立目录"}</button></div></form></Modal>}
  </section>;
}

type MethodPreview = { hash: string; methods: ResearchMethod[]; origin: RecordData; warnings?: string[] };
type MethodBundle = { bundle_id: string; methods: LibraryEntry[]; entries: LibraryEntry[] };
function MethodSelectionModal({ context, onClose, onDirtyChange, onOpenMethods }: { context: {bookId: string; editionId: string; batchId: string; nodeId: string; result: unknown}; onClose: () => void; onDirtyChange: (dirty: boolean) => void; onOpenMethods: (entryId?: string) => void }) {
  const methods = researchMethods(context.result);
  const [selected, setSelected] = useState<number[]>([]), [preview, setPreview] = useState<{value: MethodPreview; indices: number[]; key: string} | null>(null);
  const [saved, setSaved] = useState<MethodBundle | null>(null), [uncertain, setUncertain] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const gate = useRef(false), alive = useRef(true);
  const [proof, setProof] = useState<{range: EvidenceRange; text: string} | null>(null);
  const dirty = !saved && (selected.length > 0 || uncertain);
  const url = bookUrl(context.bookId, `/batches/${encodeURIComponent(context.batchId)}/nodes/${encodeURIComponent(context.nodeId)}/methods`);
  useEffect(() => { onDirtyChange(dirty); }, [dirty]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; onDirtyChange(false); }; }, []);
  async function run(save: boolean) {
    if (gate.current || !selected.length || save && !preview) return;
    gate.current = true; setBusy(true); setError("");
    try {
      if (save && preview) {
        const value = await api<MethodBundle>(url, "POST", { method_indices: preview.indices, expected_preview: preview.value.hash, idempotency_key: preview.key });
        if (alive.current) { setSaved(value); setUncertain(false); }
      } else {
        const indices = methodSelection(selected, methods.length);
        const value = await api<MethodPreview>(`${url}/preview`, "POST", { method_indices: indices });
        if (alive.current) setPreview({value, indices, key: crypto.randomUUID()});
      }
    } catch (failure) {
      if (alive.current) {
        if (save && (!(failure instanceof ApiError) || failure.status >= 500)) setUncertain(true);
        else if (failure instanceof ApiError && failure.status === 409) { setPreview(null); setUncertain(false); }
        else if (failure instanceof ApiError && failure.status < 500) setUncertain(false);
        setError(failure instanceof ApiError && failure.status === 409 ? "研究结果、来源授权或方法预览已变化。选择仍保留，请核对后重新预览。" : failure instanceof ApiError && failure.status < 500 ? failure.message.slice(0, 350) : "保存结果尚不明确。保留当前选择和提交键，再次保存会核对同一份候选。");
      }
    } finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  async function inspect(range: EvidenceRange) {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError("");
    try {
      const value = await api<ChapterText>(`${bookUrl(context.bookId, `/chapters/${encodeURIComponent(range.chapter_id)}`)}?edition_id=${encodeURIComponent(context.editionId)}&offset=${range.start_char}&limit=${Math.min(24000, range.end_char - range.start_char)}`);
      if (alive.current) setProof({range, text: value.text});
    } catch (failure) { if (alive.current) setError(message(failure)); }
    finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  function close() { if (busy || uncertain) return; if (!dirty || window.confirm("方法选择尚未保存，确认关闭并放弃本次整理？")) onClose(); }
  return <Modal title="整理为个人方法候选" subtitle="仅复用已完成研究，不调用模型，也不自动采用或选入作品。" wide onClose={close}><section className="rb-method-transfer">
    <p className="rb-muted">选择值得继续核对的方法，先查看保存内容。原书依据与研究节点将固定保留；方法有效性仍需作者判断与试用。</p>
    {error && <p className="rb-alert error" role="alert">{error}</p>}
    {!saved && methods.map((method, index) => <article className="rb-method-choice" key={index}><label className="rb-checkbox"><input type="checkbox" checked={selected.includes(index)} disabled={busy || uncertain} onChange={(event) => { setSelected(methodSelection(event.target.checked ? [...selected, index] : selected.filter((item) => item !== index), methods.length)); setPreview(null); }} /><strong>{method.title}</strong></label><ResultContent value={{steps: method.steps, limitations: method.limitations, source_refs: method.source_refs}} onEvidence={(range) => void inspect(range)} /></article>)}
    {proof && <section className="rb-method-proof"><h3>固定版本原文回查</h3><p className="rb-muted">版本 {context.editionId.slice(0, 8)} · Unicode 字符 [{proof.range.start_char}, {proof.range.end_char})</p><blockquote>{proof.text}</blockquote><button className="text-button" onClick={() => setProof(null)}>收起回查</button></section>}
    {!methods.length && <p className="rb-muted">该节点没有可保存的方法候选。</p>}
    {preview && !saved && <div className="rb-preview"><h3>将保存 {preview.value.methods.length} 张个人方法卡 · 尚未采用</h3><p className="rb-muted">默认待整理、尚未核对、仅本地保存。保存后可编辑步骤与边界，原研究来源保持只读。</p>{preview.value.methods.map((method, index) => <article key={index}><h4>{method.title}</h4><ResultContent value={{steps: method.steps, limitations: method.limitations, source_refs: method.source_refs}} onEvidence={(range) => void inspect(range)} /></article>)}{preview.value.warnings?.map((warning, index) => <p className="rb-alert" key={index}>{warning}</p>)}</div>}
    {uncertain && <p className="rb-alert">提交结果待核对，当前选择已锁定。请使用“沿原提交键核对保存”；不会再建另一组卡片。</p>}
    {saved && <div className="rb-preview" role="status"><h3>已保存 {saved.methods.length} 张个人方法候选</h3><p>接下来到个人库“方法”页编辑、核对，再由作者采用；用于作品还需明确“用于本书”和任务选材政策。</p>{saved.methods.map((method) => <button className="button secondary small" key={method.id} onClick={() => onOpenMethods(method.id)}>{method.title}<ArrowRight size={13} /></button>)}</div>}
    <div className="modal-footer"><button className="button secondary" disabled={busy || uncertain} onClick={close}>{saved ? "完成" : "取消"}</button>{!saved && <><button className="button secondary" disabled={busy || uncertain || !selected.length} onClick={() => void run(false)}>预览方法候选</button>{preview && <button className="button primary" disabled={busy} onClick={() => void run(true)}>{busy ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{uncertain ? "沿原提交键核对保存" : "存为个人方法候选"}</button>}</>}</div>
  </section></Modal>;
}

function ModelRoute({ value }: { value?: RecordData }) {
  const route = object(value), candidates = Array.isArray(route.candidates) ? route.candidates.map(object) : [];
  return <dl className="rb-report">{candidates.map((item, index) => <div key={index}><dt>{index ? "允许备用" : "主模型"}</dt><dd>{String(item.label || item.model || "未配置")} · {String(item.model || "")}</dd></div>)}<div><dt>输入Token估算</dt><dd>{typeof route.estimated_input_tokens === "number" ? route.estimated_input_tokens.toLocaleString() : "预览未提供"}</dd></div><div><dt>预算预留输出Token上限</dt><dd>{typeof route.max_output_tokens === "number" ? route.max_output_tokens.toLocaleString() : "预览未提供"}</dd></div></dl>;
}

function NodePagination({offset, total, onChange, disabled = false}: {offset: number; total: number; disabled?: boolean; onChange: (offset: number) => void}) {
  if (!total) return null;
  return <div className="rb-pagination"><button className="text-button" disabled={disabled || offset <= 0} onClick={() => onChange(Math.max(0, offset - NODE_PAGE))}><ArrowLeft size={13} />上一页节点</button><small className="rb-muted">{offset + 1}–{Math.min(offset + NODE_PAGE, total)} / {total} 节点</small><button className="text-button" disabled={disabled || offset + NODE_PAGE >= total} onClick={() => onChange(offset + NODE_PAGE)}>下一页节点<ArrowRight size={13} /></button></div>;
}
