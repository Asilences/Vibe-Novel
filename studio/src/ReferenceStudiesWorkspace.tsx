import { useEffect, useRef, useState } from "react";
import { BookOpen, GitBranch, Pause, Play, RefreshCw, Search, Square } from "lucide-react";
import { api, ApiError } from "./api";
import { Modal } from "./ui";
import { graphWindow, studyEvidence, studyMethods } from "./referenceStudyHelpers";
import type { StudyEvidence, StudyGraph, StudyGraphNode } from "./referenceStudyHelpers";
import "./referenceStudies.css";

type Data = Record<string, unknown>;
type Book = {book_id: string; edition_id: string; title: string; outbound?: string; withdrawn?: boolean};
type Chapter = {chapter_id: string; title: string; characters: number; needs_review?: boolean; incomplete?: boolean};
type Directory = {chapters: Chapter[]; total_chapters: number; offset: number};
type Source = {book_id: string; edition_id: string; chapter_ids: string[]; ranges?: {chapter_id: string; start_char: number; end_char: number}[]};
type Study = {sources?: {book_id:string;title:string}[]; id: string; hash: string; kind: string; question: string; state: string; completed_nodes: number; total_nodes: number; error?: string; request?: Data; nodes?: {id: string; order: number; state: string; error?: string; characters: number; ranges: Data[]; has_response?: boolean}[]; coverage?: Data; estimated_cost_usd?: number};
type StudyExport = Study & {results: {node_id: string; read_index: number; temporal_mode: string; result: Data}[]; graphs: Record<string, StudyGraph>; map_version?: string; map_version_stale?: boolean};
type Preview = {hash: string; nodes: Data[]; estimated_cost_usd: number; sources: Data[]; warnings: string[]};
type FrozenPreview = {value: Preview; request: Data; identity: string; key: string};
type Props = {onDirtyChange?: (dirty: boolean) => void; onOpenMethods?: (entryId?: string) => void; onOpenBooks?: () => void; onOpenAccount?: () => void};
const base = "/reference-studies", PAGE = 24;
const kindLabels: Record<string, string> = {reading: "连续精读", reread: "主动补读", comparison: "跨书比较"};
const states: Record<string, string> = {prepared: "已准备，等待启动", pending: "待处理", running: "正在精读", pausing: "当前请求完成后暂停", paused: "已暂停", succeeded: "已完成", failed: "待核对／离线修复", interrupted: "中断，禁止自动重放", cancelled: "已取消"};
const groups: Record<string, string> = {causal: "内容与因果", characters: "人物关系与知情", information: "信息差与线索", rhythm: "节奏与情绪"};
const amount = (value: unknown) => typeof value === "number" ? `$${value.toFixed(4)}` : "报价未知";
const label = (value: unknown) => typeof value === "string" ? value : "";
const arr = (value: unknown): Data[] => Array.isArray(value) ? value as Data[] : [];
const message = (failure: unknown) => failure instanceof ApiError && failure.status < 500 ? failure.message.slice(0, 500) : "请求结果尚不明确，请刷新核对；保存操作保留原提交键，不要重复新建研究。";

function Evidence({refs, onOpen, onReread}: {refs: StudyEvidence[]; onOpen: (ref: StudyEvidence) => void; onReread: (ref: StudyEvidence) => void}) {
  return <div className="rs-evidence">{refs.map((ref, index) => <article key={`${ref.book_id}-${ref.chapter_id}-${ref.start_char}-${index}`}><small>固定版本 · 字符 [{ref.start_char}, {ref.end_char})</small><blockquote>{ref.quote}</blockquote><button onClick={() => onOpen(ref)}><Search size={12}/>回查原文</button><button onClick={() => onReread(ref)}>补读上下文</button></article>)}</div>;
}

function Graph({graph, group, checkpoint, onSelect}: {graph: StudyGraph; group: string; checkpoint: number; onSelect: (node: StudyGraphNode) => void}) {
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [group, checkpoint]);
  const window = graphWindow(graph, offset, PAGE, checkpoint), height = Math.max(180, Math.ceil(window.nodes.length / 3) * 108 + 30);
  const positions = new Map(window.nodes.map((node, index) => [node.id, {x: 22 + index % 3 * 285, y: 20 + Math.floor(index / 3) * 108}]));
  return <><div className="rs-graph-meta"><span>{window.total} 个节点 · {window.totalEdges} 条关系；当前视窗 {window.nodes.length} 个节点</span><span>所有节点均为候选解释，可点击回查</span></div>
    {window.total ? <div className="rs-graph-scroll"><svg className="rs-graph" viewBox={`0 0 875 ${height}`} role="img" aria-label={groups[group] + "候选结构图"}>
      <defs><marker id={`arrow-${group}`} markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="#b5a7bc"/></marker></defs>
      {window.edges.map((edge, index) => {const from = positions.get(edge.from)!, to = positions.get(edge.to)!; return <g key={index}><path d={`M${from.x + 125},${from.y + 65} C${from.x + 125},${from.y + 95} ${to.x + 125},${to.y - 12} ${to.x + 125},${to.y}`} markerEnd={`url(#arrow-${group})`} fill="none" stroke="#b5a7bc" strokeWidth="1.5"/><title>{edge.label}</title></g>;})}
      {window.nodes.map((node) => {const pos = positions.get(node.id)!; return <g className={`rs-svg-node ${node.kind === "knowledge" ? "knowledge" : ""}`} key={node.id} role="button" tabIndex={0} aria-label={node.label} onClick={() => onSelect(node)} onKeyDown={(event) => {if (event.key === "Enter" || event.key === " ") {event.preventDefault(); onSelect(node);}}}>
        <rect x={pos.x} y={pos.y} width="255" height="70" rx="9"/><text x={pos.x + 12} y={pos.y + 23}>{Array.from(node.label).slice(0, 17).join("")}{Array.from(node.label).length > 17 ? "…" : ""}</text><text className="rs-svg-caption" x={pos.x + 12} y={pos.y + 48}>阅读段 {node.read_index + 1}{typeof node.tension === "number" ? ` · 张力 ${node.tension}/5 · 情绪 ${node.valence}` : node.status ? ` · ${node.status}` : " · 有原文依据"}</text><title>{node.description}</title>
      </g>;})}
    </svg></div> : <p className="rs-empty">当前阅读时点尚无该组有证据的结构节点。后文信息不会回填到这里。</p>}
    <div className="rs-pager"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一视窗</button><span>{offset + (window.total ? 1 : 0)}—{Math.min(offset + PAGE, window.total)} / {window.total}</span><button disabled={offset + PAGE >= window.total} onClick={() => setOffset(offset + PAGE)}>下一视窗</button></div>
    {window.nodes.length > 0 && <div className="rs-node-table">{window.nodes.map((node) => <button key={node.id} onClick={() => onSelect(node)}><span>段 {node.read_index + 1}</span><strong>{node.label}</strong><small>{node.description}</small></button>)}</div>}
  </>;
}

export default function ReferenceStudiesWorkspace({onDirtyChange, onOpenMethods, onOpenBooks, onOpenAccount}: Props) {
  const [books, setBooks] = useState<Book[]>([]), [studies, setStudies] = useState<Study[]>([]), [study, setStudy] = useState<Study | null>(null), [exported, setExported] = useState<StudyExport | null>(null);
  const [creating, setCreating] = useState(false), [kind, setKind] = useState("reading"), [sources, setSources] = useState<Source[]>([]), [directories, setDirectories] = useState<Record<string, Directory>>({});
  const [question, setQuestion] = useState("分析内容推进、人物选择与代价、读者/角色当时知情、线索铺设及节奏变化"), [budget, setBudget] = useState("1"), [output, setOutput] = useState(6144);
  const [preview, setPreview] = useState<FrozenPreview | null>(null), [uncertain, setUncertain] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [tab, setTab] = useState("graphs"), [group, setGroup] = useState("causal"), [checkpoint, setCheckpoint] = useState(Number.MAX_SAFE_INTEGER), [selectedNode, setSelectedNode] = useState<StudyGraphNode | null>(null);
  const [nodePage, setNodePage] = useState(0), [nodeLabel, setNodeLabel] = useState(""), [nodeDescription, setNodeDescription] = useState(""), [editReason, setEditReason] = useState("");
  const [evidence, setEvidence] = useState<{ref: StudyEvidence; text: string} | null>(null), [repair, setRepair] = useState<{nodeId: string; hash: string; text: string; original: string} | null>(null), [repairReason, setRepairReason] = useState("");
  const [methodIndices, setMethodIndices] = useState<number[]>([]), [methodPreview, setMethodPreview] = useState<{value: Data; indices: number[]; key: string} | null>(null);
  const alive = useRef(true), gate = useRef(false), selection = useRef(0), selectedStudyId = useRef("");
  const dirtyCallback = useRef(onDirtyChange); dirtyCallback.current = onDirtyChange;
  const graphDirty = !!selectedNode && (nodeLabel !== selectedNode.label || nodeDescription !== selectedNode.description);
  const dirty = graphDirty || !!repair && repair.text !== repair.original || creating && sources.length > 0 || methodIndices.length > 0;
  const request = {kind, sources, question, budget_max_usd: Number(budget), max_output_tokens: output, segment_characters: 12000};
  const requestIdentity = JSON.stringify(request), freshPreview = preview?.identity === requestIdentity;
  const active = !!study && ["running", "pausing"].includes(study.state);
  const methods = exported ? studyMethods(exported.results) : [];

  useEffect(() => {dirtyCallback.current?.(dirty);}, [dirty]);
  useEffect(() => {alive.current = true; void act(async () => {const [bookResult, studyResult] = await Promise.all([api<{books: Book[]}>("/reference-books"), api<{studies: Study[]}>(base)]); if (alive.current) {setBooks(bookResult.books); setStudies(studyResult.studies);}}); return () => {alive.current = false; selection.current++; dirtyCallback.current?.(false);};}, []);
  useEffect(() => {
    if (!study || !active) return;
    const id = study.id, epoch = selection.current;
    const timer = window.setInterval(() => {if (gate.current) return; void Promise.all([api<Study>(`${base}/${id}?offset=${nodePage}&limit=50`), api<{studies: Study[]}>(base)]).then(async ([detail, list]) => {
      if (!alive.current || selectedStudyId.current !== id || epoch !== selection.current) return;
      setStudy(detail); setStudies(list.studies);
      if (!["running", "pausing"].includes(detail.state)) {try {const result = await api<StudyExport>(`${base}/${id}/export`); if (alive.current && selectedStudyId.current === id) setExported(result);} catch (failure) {if (alive.current) setError(message(failure));}}
    }).catch((failure) => {if (alive.current && selectedStudyId.current === id) setError(message(failure));});}, 2500);
    return () => window.clearInterval(timer);
  }, [study?.id, active, nodePage]);

  async function act(fn: () => Promise<void>) {
    if (gate.current) return; gate.current = true; setBusy(true); setError("");
    try {await fn();} catch (failure) {if (alive.current) setError(message(failure));} finally {gate.current = false; if (alive.current) setBusy(false);}
  }
  async function refreshList() {const list = await api<{studies: Study[]}>(base); if (alive.current) setStudies(list.studies);}
  async function refreshWorkspace() {
    const id = study?.id, epoch = selection.current;
    const [corpus, list] = await Promise.all([api<{books: Book[]}>("/reference-books"), api<{studies: Study[]}>(base)]);
    if (!alive.current) return;
    setBooks(corpus.books); setStudies(list.studies);
    if (!id || creating) return;
    const [detail, result] = await Promise.all([api<Study>(`${base}/${id}?offset=${nodePage}&limit=50`), api<StudyExport>(`${base}/${id}/export`)]);
    if (alive.current && epoch === selection.current && selectedStudyId.current === id) {setStudy(detail); setExported(result);}
  }
  async function selectStudy(id: string) {
    if (creating && sources.length) {
      if (uncertain) {setError("保存结果尚不明确，请先沿原提交键核对；不要放弃草案后另建研究。"); return;}
      if (!window.confirm("当前研究草案尚未保存，确认放弃并打开历史研究？")) return;
    }
    await openStudy(id);
  }
  async function openStudy(id: string, offset = 0) {
    if (graphDirty || repair || methodIndices.length) {setError("请先保存或放弃当前编辑及方法选择，再切换研究。"); return;}
    const epoch = ++selection.current; selectedStudyId.current = id;
    const detail = await api<Study>(`${base}/${id}?offset=${offset}&limit=50`);
    if (!alive.current || epoch !== selection.current) return;
    setStudy(detail); setNodePage(offset); setCreating(false); setSelectedNode(null); setCheckpoint(Number.MAX_SAFE_INTEGER);
    try {const result = await api<StudyExport>(`${base}/${id}/export`); if (alive.current && epoch === selection.current) setExported(result);} catch (failure) {setExported(null); throw failure;}
  }
  async function directory(book: Book, offset = 0) {
    const result = await api<Directory>(`/reference-books/${book.book_id}?edition_id=${book.edition_id}&offset=${offset}&limit=30`);
    if (alive.current) setDirectories((old) => ({...old, [book.book_id]: {...result, offset}}));
  }
  function selectNode(node: StudyGraphNode) {if (graphDirty) {setError("请先保存或放弃当前图节点编辑。"); return;} setSelectedNode(node); setNodeLabel(node.label); setNodeDescription(node.description); setEditReason("");}
  function updateSource(bookId: string, update: Partial<Source>) {setSources((old) => old.map((source) => source.book_id === bookId ? {...source, ...update} : source)); setPreview(null);}
  async function toggleBook(book: Book) {
    if (uncertain) return;
    if (sources.some((source) => source.book_id === book.book_id)) setSources((old) => old.filter((source) => source.book_id !== book.book_id));
    else {setSources((old) => kind === "comparison" ? [...old, {book_id: book.book_id, edition_id: book.edition_id, chapter_ids: []}] : [{book_id: book.book_id, edition_id: book.edition_id, chapter_ids: []}]); await directory(book);}
    setPreview(null);
  }
  async function showEvidence(ref: StudyEvidence) {
    const start = Math.max(0, ref.start_char - 400), result = await api<{text: string}>(`/reference-books/${ref.book_id}/chapters/${ref.chapter_id}?edition_id=${ref.edition_id}&offset=${start}&limit=${Math.min(6000, ref.end_char - start + 400)}`);
    if (alive.current) setEvidence({ref, text: result.text});
  }
  function reread(ref: StudyEvidence) {
    if (graphDirty || repair || methodIndices.length) {setError("请先保存或放弃当前编辑，再建立补读。"); return;}
    const book = books.find((item) => item.book_id === ref.book_id); if (!book) return;
    setCreating(true); setKind("reread"); setSources([{book_id: ref.book_id, edition_id: ref.edition_id, chapter_ids: [], ranges: [{chapter_id: ref.chapter_id, start_char: Math.max(0, ref.start_char - 500), end_char: ref.end_char}]}]);
    setQuestion("复核以下证据的上下文、角色当时知情和原判断边界；只增加新的回看候选，不改写初读检查点"); setPreview(null); setEvidence(null); setNotice("已建立补读草案。请核对精确范围和预算，补读结果独立存证。"); void act(() => directory(book));
  }
  async function previewStudy() {const value = await api<Preview>(`${base}/preview`, "POST", request); if (alive.current) setPreview({value, request, identity: requestIdentity, key: crypto.randomUUID()});}
  async function createStudy() {
    if (!preview || !freshPreview) return;
    try {const value = await api<Study>(base, "POST", {...preview.request, expected_preview: preview.value.hash, idempotency_key: preview.key}); if (alive.current) {setUncertain(false); setSources([]); setPreview(null); await refreshList(); await openStudy(value.id); setNotice("研究已准备；点击“启动精读”才会发送付费请求。");}}
    catch (failure) {if (!(failure instanceof ApiError) || failure.status >= 500) setUncertain(true); throw failure;}
  }
  async function control(action: string) {if (!study) return; const value = await api<Study>(`${base}/${study.id}/${action}`, "POST"); if (alive.current) {setStudy(value); await refreshList();}}
  async function saveGraph() {
    if (!study || !exported || !selectedNode) return;
    const graphs = {...exported.graphs, [group]: {...exported.graphs[group], nodes: exported.graphs[group].nodes.map((node) => node.id === selectedNode.id ? {...node, label: nodeLabel, description: nodeDescription} : node)}};
    const value = await api<StudyExport>(`${base}/${study.id}/maps`, "PUT", {expected_hash: exported.hash, graphs, reason: editReason});
    if (alive.current) {setExported(value); setSelectedNode(null); setNotice("已保存新的人工候选图版本，旧版本和初读检查点保留。");}
  }
  async function openRepair(nodeId: string) {if (!study) return; const value = await api<{hash: string; text: string}>(`${base}/${study.id}/nodes/${nodeId}/response`); if (alive.current) {setRepair({nodeId, hash: value.hash, text: value.text, original: value.text}); setRepairReason("");}}
  async function saveRepair() {if (!study || !repair) return; const result = await api<Study>(`${base}/${study.id}/nodes/${repair.nodeId}/response`, "PUT", {expected_hash: repair.hash, text: repair.text, reason: repairReason}); if (alive.current) {setStudy(result); setRepair(null); setExported(await api<StudyExport>(`${base}/${study.id}/export`)); setNotice("响应已离线修复，没有新增付费请求。后续节点仍需明确启动。");}}
  async function prepareMethods() {if (!study) return; const value = await api<Data>(`${base}/${study.id}/methods/preview`, "POST", {method_indices: methodIndices}); if (alive.current) setMethodPreview({value, indices: [...methodIndices], key: crypto.randomUUID()});}
  async function saveMethods() {if (!study || !methodPreview) return; const value = await api<Data>(`${base}/${study.id}/methods`, "POST", {method_indices: methodPreview.indices, expected_preview: methodPreview.value.hash, idempotency_key: methodPreview.key}); if (alive.current) {setMethodPreview(null); setMethodIndices([]); setNotice("方法已保存为个人未采用候选，尚未进入本书任务。"); if (onOpenMethods) onOpenMethods(typeof value.id === "string" ? value.id : undefined);}}

  return <section className="reference-studies">
    <header className="rs-heading"><div><span className="rs-eyebrow"><GitBranch size={15}/>原文有据 · 阅读有序 · 候选可回查</span><h2>精读与结构研究</h2><p>连续保留每个阅读时点的理解，将内容安排、人物知情、线索与情绪整理成可核对的四组图。</p></div><div className="rs-actions"><button disabled={busy || dirty} title={dirty ? "先保存或放弃当前编辑后刷新完整研究状态" : "重新读取书架、研究状态和当前结构图"} onClick={() => void act(refreshWorkspace)}><RefreshCw size={14}/>刷新研究状态</button><button disabled={busy || dirty} onClick={() => {setCreating(true); setSources([]); setPreview(null); setUncertain(false);}}>新建研究</button></div></header>
    {error && <div className="rs-error" role="alert">{error}</div>}{notice && <div className="rs-notice" role="status">{notice}</div>}
    <div className="rs-layout"><aside className="rs-list"><h3>研究工程 <small>{studies.length}</small></h3>{!studies.length && <p className="rs-empty">尚无研究。先选择本地书籍与精确范围，再核对模型和预算。</p>}{studies.map((item) => <button key={item.id} className={study?.id === item.id && !creating ? "selected" : ""} disabled={busy} onClick={() => void act(() => selectStudy(item.id))}><span>{kindLabels[item.kind]} · {states[item.state] || item.state}</span><strong>{item.sources?.map(source => source.title).join(" · ") || item.question}</strong><small className="rs-card-question" title={item.question}>{item.question}</small><small>{item.completed_nodes} / {item.total_nodes} 个阅读段</small></button>)}</aside>
    <main className="rs-main">{creating ? <div className="rs-create"><h3>建立有边界的阅读任务</h3><div className="rs-form-row"><label>研究方式<select value={kind} disabled={uncertain} onChange={(event) => {setKind(event.target.value); setSources([]); setPreview(null);}}><option value="reading">按原书顺序连续精读</option><option value="reread">针对问题主动补读</option><option value="comparison">围绕同一问题跨书比较</option></select></label><label>本批预算上限（美元）<input type="number" min="0.01" step="0.1" value={budget} disabled={uncertain} onChange={(event) => setBudget(event.target.value)}/></label><label>单节点输出上限<select value={output} disabled={uncertain} onChange={(event) => setOutput(Number(event.target.value))}>{[2048, 4096, 6144, 8192, 12000, 16000].map((value) => <option key={value} value={value}>{value} Token</option>)}</select></label></div><label>统一研究问题<textarea value={question} disabled={uncertain} rows={3} maxLength={2000} onChange={(event) => setQuestion(event.target.value)}/></label>
      <p className="rs-muted">默认只使用已明确允许“外发片段”的书籍。独立资料研究账户承担费用，共享总预算还有另一道硬限制。</p><div className="rs-actions">{onOpenBooks && <button disabled={busy || uncertain} onClick={onOpenBooks}>参考书架：导入与外发授权</button>}{onOpenAccount && <button disabled={busy || uncertain} onClick={onOpenAccount}>设置研究模型、价格与总预算</button>}</div>{!books.length && <p className="rs-empty">尚未导入参考书。先到参考书架保留原件，核对目录并明确外发授权，再回来新建研究。</p>}<div className="rs-book-choices">{books.map((book) => <button key={book.book_id} disabled={busy || uncertain || book.outbound !== "excerpts" || book.withdrawn} className={sources.some((source) => source.book_id === book.book_id) ? "selected" : ""} onClick={() => void act(() => toggleBook(book))}><BookOpen size={14}/>{book.title}<small>{book.withdrawn ? "已撤回" : book.outbound !== "excerpts" ? "仅本地，先在书架授权" : "可选范围"}</small></button>)}</div>
      {sources.map((source) => {const book = books.find((item) => item.book_id === source.book_id)!, page = directories[source.book_id]; return <article className="rs-source" key={source.book_id}><h4>{book.title}</h4>{kind === "reading" && <label className="rs-checkbox"><input type="checkbox" checked={!source.chapter_ids.length && !source.ranges} disabled={uncertain} onChange={(event) => updateSource(source.book_id, {chapter_ids: event.target.checked ? [] : page?.chapters.slice(0, 1).map((chapter) => chapter.chapter_id) || []})}/>精读当前完整文件（包括附属资源与重复候选；不等于出版版本完整）</label>}
      {source.ranges ? <>{source.ranges.map((range, index) => <div className="rs-form-row" key={index}><label>固定章节<select value={range.chapter_id} disabled={uncertain} onChange={(event) => updateSource(source.book_id, {ranges: source.ranges!.map((part, i) => i === index ? {...part, chapter_id: event.target.value, start_char: 0, end_char: page?.chapters.find((chapter) => chapter.chapter_id === event.target.value)?.characters || part.end_char} : part)})}>{!page?.chapters.some((chapter) => chapter.chapter_id === range.chapter_id) && <option value={range.chapter_id}>来自证据的固定章节</option>}{page?.chapters.map((chapter) => <option key={chapter.chapter_id} value={chapter.chapter_id}>{chapter.title}（{chapter.characters}字符）</option>)}</select></label><label>起始字符<input type="number" min="0" value={range.start_char} disabled={uncertain} onChange={(event) => updateSource(source.book_id, {ranges: source.ranges!.map((part, i) => i === index ? {...part, start_char: Number(event.target.value)} : part)})}/></label><label>结束字符<input type="number" min="1" value={range.end_char} disabled={uncertain} onChange={(event) => updateSource(source.book_id, {ranges: source.ranges!.map((part, i) => i === index ? {...part, end_char: Number(event.target.value)} : part)})}/></label></div>)}<button onClick={() => updateSource(source.book_id, {ranges: undefined})}>改选完整章节</button></> : <><p className="rs-muted">{kind === "comparison" ? "每本书请选择用于同一问题的代表原文，单书最多 24,000 字符。" : "也可勾选指定章节；勾选后按原书顺序阅读。"} 当前选中 {source.chapter_ids.length || (kind === "reading" ? "完整文件" : "尚未明确")}。</p><div className="rs-chapters">{page?.chapters.map((chapter) => <label key={chapter.chapter_id}><input type="checkbox" disabled={uncertain || chapter.incomplete || chapter.needs_review} checked={source.chapter_ids.includes(chapter.chapter_id)} onChange={(event) => updateSource(source.book_id, {chapter_ids: event.target.checked ? [...source.chapter_ids, chapter.chapter_id] : source.chapter_ids.filter((id) => id !== chapter.chapter_id)})}/><span>{chapter.title}<small>{chapter.characters}字符{chapter.incomplete || chapter.needs_review ? " · 有异常待核" : ""}</small></span></label>)}</div></>}
      {page && <div className="rs-pager"><button disabled={busy || page.offset <= 0} onClick={() => void act(() => directory(book, Math.max(0, page.offset - 30)))}>上一目录页</button><span>{page.offset + 1}—{Math.min(page.offset + 30, page.total_chapters)} / {page.total_chapters}</span><button disabled={busy || page.offset + 30 >= page.total_chapters} onClick={() => void act(() => directory(book, page.offset + 30))}>下一目录页</button></div>}
      </article>;})}
      {preview && <div className="rs-preview"><h4>付费范围与模型预览</h4><p>{preview.value.nodes.length} 个顺序节点 · 保守报价 {amount(preview.value.estimated_cost_usd)}</p><p>保存后保持“已准备”；启动才会调用 API。完整原文区间在已准备工程的节点清单中保留。</p><details><summary>展开模型、范围及覆盖清单</summary><pre>{JSON.stringify({sources: preview.value.sources, first_node: preview.value.nodes[0], warnings: preview.value.warnings}, null, 2)}</pre></details>{!freshPreview && <strong>输入已变化，须重新预览。</strong>}</div>}
      <div className="rs-actions"><button disabled={busy || uncertain || !sources.length || kind === "comparison" && (sources.length < 2 || sources.some((source) => !source.chapter_ids.length && !source.ranges))} onClick={() => void act(previewStudy)}>预览精确范围与报价</button><button disabled={busy || !freshPreview} onClick={() => void act(createStudy)}>{uncertain ? "核对同一份保存结果" : "保存为待启动研究"}</button><button disabled={busy || uncertain} onClick={() => {setCreating(false); setSources([]); setPreview(null);}}>放弃草案</button></div>
    </div> : study ? <><header className="rs-study-header"><div><span className="rs-eyebrow">{kindLabels[study.kind]} · {states[study.state] || study.state}</span><h3>{study.question}</h3><p>{study.completed_nodes} / {study.total_nodes} 个节点完成 · 预览 {amount(study.estimated_cost_usd)}</p></div><div className="rs-actions"><button disabled={busy || !["prepared", "paused"].includes(study.state)} onClick={() => void act(() => control("start"))}><Play size={14}/>启动精读</button><button disabled={busy || !active} onClick={() => void act(() => control("pause"))}><Pause size={14}/>暂停</button><button disabled={busy || ["succeeded", "cancelled"].includes(study.state)} onClick={() => void act(() => control("cancel"))}><Square size={14}/>取消</button></div></header>{study.error && <div className="rs-error">{study.error}</div>}
      <nav className="rs-tabs">{[["graphs", "四组结构图"], ["reading", "阅读与知情检查点"], ["comparison", "跨书解法比较"], ["methods", "方法候选"]].map(([value, name]) => <button key={value} className={tab === value ? "active" : ""} disabled={graphDirty} onClick={() => {setTab(value); setSelectedNode(null);}}>{name}</button>)}</nav>
      {tab === "graphs" && exported && <><p className="rs-muted">完整保留所有已成功节点的结构与证据。每个视窗只展示部分节点，分页不会删除其余图谱；关系与评分是候选，仍需作者核对。</p><div className="rs-form-row"><label>图谱类型<select value={group} disabled={graphDirty} onChange={(event) => {setGroup(event.target.value); setSelectedNode(null);}}>{Object.entries(groups).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label><label>阅读时点<select value={checkpoint} disabled={graphDirty} onChange={(event) => {setCheckpoint(Number(event.target.value)); setSelectedNode(null);}}><option value={Number.MAX_SAFE_INTEGER}>全部已读范围</option>{exported.results.map((row) => <option key={row.node_id} value={row.read_index}>只看读完第 {row.read_index + 1} 段时</option>)}</select></label></div>{exported.map_version_stale && <p className="rs-notice">继续阅读后，旧人工图版本已归档；当前显示包含新证据的自动候选图，请重新核对。</p>}<Graph graph={exported.graphs[group]} group={group} checkpoint={checkpoint} onSelect={selectNode}/>
        {selectedNode && <article className="rs-node-detail"><h4>候选节点 · 阅读段 {selectedNode.read_index + 1}</h4>{active && <p className="rs-muted">研究运行中可回查证据；先暂停并等待当前请求完成，再编辑候选图。</p>}<label>标题<input disabled={busy || active} maxLength={500} value={nodeLabel} onChange={(event) => setNodeLabel(event.target.value)}/></label><label>解释<textarea disabled={busy || active} rows={3} maxLength={3000} value={nodeDescription} onChange={(event) => setNodeDescription(event.target.value)}/></label><Evidence refs={selectedNode.source_refs} onOpen={(ref) => void act(() => showEvidence(ref))} onReread={reread}/>{graphDirty && <><label>修改依据<input maxLength={2000} value={editReason} onChange={(event) => setEditReason(event.target.value)}/></label><div className="rs-actions"><button disabled={busy || !editReason.trim() || active} onClick={() => void act(saveGraph)}>保存新的候选图版本</button><button onClick={() => {setNodeLabel(selectedNode.label); setNodeDescription(selectedNode.description);}}>放弃修改</button></div></>}</article>}
      </>}
      {tab === "reading" && <><p className="rs-muted">初读检查点按顺序独立保存。后来的揭晓和补读不能修改较早时点；连续笔记有界，未发送历史会明确列为上下文缺口。</p>{study.nodes?.map((node) => <article className="rs-checkpoint" key={node.id}><h4>段 {node.order + 1} · {states[node.state] || node.state} <small>{node.characters}字符</small></h4>{node.error && <p className="rs-error">{node.error}</p>}<details><summary>本段精确输入区间</summary><pre>{JSON.stringify(node.ranges, null, 2)}</pre></details>{exported?.results.filter((row) => row.node_id === node.id).map((row) => <div key={row.node_id}><p>{label(row.result.summary)}</p><h5>本时点新增知情</h5>{arr(row.result.knowledge).map((item, index) => <div className="rs-knowledge" key={index}><strong>{label(item.subject) === "reader" ? "读者" : label(item.subject)} · {label(item.status)}</strong><p>{label(item.fact)}</p><Evidence refs={studyEvidence(item.source_refs)} onOpen={(ref) => void act(() => showEvidence(ref))} onReread={reread}/></div>)}<details><summary>连续阅读笔记及缺口</summary><p>{label(row.result.carry_forward)}</p><ul>{Array.isArray(row.result.gaps) && row.result.gaps.map((gap, index) => <li key={index}>{String(gap)}</li>)}</ul></details></div>)}{node.state === "failed" && node.has_response && <button disabled={busy} onClick={() => void act(() => openRepair(node.id))}>查看原响应并离线修复（不收费）</button>}</article>)}<div className="rs-pager"><button disabled={busy || !nodePage} onClick={() => void act(() => openStudy(study.id, Math.max(0, nodePage - 50)))}>前50段</button><span>{nodePage + 1}—{Math.min(nodePage + 50, study.total_nodes)} / {study.total_nodes}</span><button disabled={busy || nodePage + 50 >= study.total_nodes} onClick={() => void act(() => openStudy(study.id, nodePage + 50))}>后50段</button></div></>}
      {tab === "comparison" && (study.kind !== "comparison" ? <p className="rs-empty">本工程是单书阅读。新建“跨书比较”，选择至少两本书实际原文，围绕同一个问题比较安排、前提、差异和反例。</p> : exported?.results.map((row) => <div key={row.node_id}><h4>{label(row.result.summary)}</h4><div className="rs-comparison-grid">{arr(row.result.per_book).map((item) => <article key={label(item.book_id)}><h4>{books.find((book) => book.book_id === item.book_id)?.title || "固定版本书籍"}</h4><p>{label(item.solution)}</p><strong>成立前提</strong><ul>{Array.isArray(item.premises) && item.premises.map((value, index) => <li key={index}>{String(value)}</li>)}</ul><strong>边界与反例</strong><ul>{Array.isArray(item.limitations) && item.limitations.map((value, index) => <li key={index}>{String(value)}</li>)}</ul><Evidence refs={studyEvidence(item.source_refs)} onOpen={(ref) => void act(() => showEvidence(ref))} onReread={reread}/></article>)}</div>{arr(row.result.comparisons).map((item, index) => <article className="rs-checkpoint" key={index}><h4>{label(item.title)}</h4><p>共同机制：{label(item.similarities)}</p><p>不同安排：{label(item.differences)}</p><p>反例与迁移限制：{label(item.counterexamples)}</p><Evidence refs={studyEvidence(item.source_refs)} onOpen={(ref) => void act(() => showEvidence(ref))} onReread={reread}/></article>)}</div>))}
      {tab === "methods" && <><p className="rs-muted">研究方法需先保存为个人未采用候选，之后由作者编辑采用并选入作品；证据通过不等于方法有效。</p>{methods.map(({index, value}) => <article className="rs-checkpoint" key={index}><label className="rs-checkbox"><input type="checkbox" disabled={!!methodPreview || methodIndices.length >= 6 && !methodIndices.includes(index)} checked={methodIndices.includes(index)} onChange={(event) => setMethodIndices((old) => event.target.checked ? [...old, index].sort((a, b) => a - b) : old.filter((item) => item !== index))}/><strong>{value.title}</strong></label><ol>{value.steps.map((step, i) => <li key={i}>{step}</li>)}</ol><p>边界：{value.limitations.join("；")}</p><Evidence refs={value.source_refs} onOpen={(ref) => void act(() => showEvidence(ref))} onReread={reread}/></article>)}{!methods.length && <p className="rs-empty">当前成功节点尚未产出有证据的方法。</p>}<div className="rs-actions"><button disabled={busy || !methodIndices.length} onClick={() => void act(prepareMethods)}>预览所选方法候选</button>{methodIndices.length > 0 && <button disabled={busy || !!methodPreview} onClick={() => setMethodIndices([])}>清空选择</button>}</div></>}
    </> : <div className="rs-empty"><GitBranch size={38}/><h3>从当前文件，读到可核对的结构</h3><p>连续精读保留当时理解，主动补读审查推断，跨书比较寻找可迁移的安排方法。</p></div>}</main></div>
    {evidence && <Modal title="原文证据回查" subtitle={`固定版本 · 精确引文 [${evidence.ref.start_char}, ${evidence.ref.end_char})`} onClose={() => setEvidence(null)} wide><blockquote>{evidence.ref.quote}</blockquote><pre className="rs-original">{evidence.text}</pre><button onClick={() => reread(evidence.ref)}>建立上下文补读</button></Modal>}
    {repair && <Modal title="离线修复已保存响应" subtitle="人工或代理均可修复候选并重新验证证据；不重发API，原响应保留，不代表作者采用。" onClose={() => {if (!busy && (!repair || repair.text === repair.original || window.confirm("响应修复尚未保存，确认放弃？"))) {setRepair(null); setRepairReason("");}}} wide><label>候选JSON<textarea className="rs-json" rows={18} value={repair.text} onChange={(event) => setRepair({...repair, text: event.target.value})}/></label><label>修复说明<input value={repairReason} onChange={(event) => setRepairReason(event.target.value)}/></label><button disabled={busy || !repairReason.trim()} onClick={() => void act(saveRepair)}>校验证据并保存修复候选</button></Modal>}
    {methodPreview && <Modal title="保存研究方法候选" subtitle="不会调用模型，不会自动采用或选入作品。" onClose={() => setMethodPreview(null)} wide><pre className="rs-json">{JSON.stringify(methodPreview.value, null, 2)}</pre><button disabled={busy} onClick={() => void act(saveMethods)}>保存到个人方法候选</button></Modal>}
  </section>;
}
