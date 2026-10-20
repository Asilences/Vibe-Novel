import { useEffect, useRef, useState } from "react";
import { Database, Search, Layers, Check, RefreshCw, Square, ArrowRight } from "lucide-react";
import { api } from "./api";
import { Modal } from "./ui";
import { retrievalIdentity, retrievalLayers, retrievalLayerLabels, retrievalSelection, selectedRetrievalIds } from "./referenceRetrievalHelpers";
import type { RetrievalLayer } from "./referenceRetrievalHelpers";
import "./referenceRetrieval.css";

type Props = { novelId?: string; onDirtyChange?: (value: boolean) => void; onError?: (value: string) => void; onUseSelection?: (selection: Record<string, unknown>) => void; onOpenBooks?: () => void; pickerMode?: boolean };
type Book = { book_id: string; title: string; edition_id: string; withdrawn: boolean; outbound: string; counts: { characters?: number; chapters?: number } };
type Engine = { available: boolean; model: string; reason?: string };
type Index = { id: string; state: string; book_ids: string[]; layers: RetrievalLayer[]; sources: {title: string; book_id: string; edition_id: string}[]; progress: {indexed_chunks: number; indexed_characters: number; layers: Record<string, number>}; coverage: {source_characters?: number; source_sections?: number; claim?: string; studies?: {id: string; state: string; kind: string; question: string; completed_segments: number; total_segments: number}[]}; omitted: {reason: string; chapter_id?: string}[]; error?: string; cancel_requested?: boolean };
type Preview = { hash: string; coverage: {source_characters: number; source_sections: number; estimated_original_chunks_upper: number; adopted_methods: number; verified_studies: number}; warnings: string[]; engine: Engine };
type Ref = { book_id?: string; edition_id?: string; chapter_id?: string; start_char?: number; end_char?: number; quote?: string };
type Hit = { id: string; layer: RetrievalLayer; title: string; text: string; score: number; locator: Record<string, unknown>; refs: Ref[]; channels: Record<string, {rank?: number; score?: number; evidence_edges?: {label: string}[]}> };
type Results = { query: string; index_id: string; results: Hit[]; indexed_layers: Record<string, number>; relationship_candidates: number; limitations: string[]; omitted: {reason: string}[] };
type Selection = { hash: string; text: string; ids: string[]; characters: number; library_selection: Record<string, unknown> };
const base = "/reference-retrieval";
const stateLabels: Record<string, string> = {ready: "可检索", building: "正在建立", failed: "建立失败", cancelled: "已取消", interrupted: "已中断", stale: "来源变化，需重建"};
const number = (value?: number) => (value || 0).toLocaleString("zh-CN");

export default function ReferenceRetrievalWorkspace({ novelId, onDirtyChange, onError, onUseSelection, onOpenBooks, pickerMode = false }: Props) {
  const [books, setBooks] = useState<Book[]>([]), [indexes, setIndexes] = useState<Index[]>([]), [engine, setEngine] = useState<Engine | null>(null);
  const [bookIds, setBookIds] = useState<string[]>([]), [buildLayers, setBuildLayers] = useState<RetrievalLayer[]>([...retrievalLayers]);
  const [indexId, setIndexId] = useState(""), [query, setQuery] = useState(""), [semantic, setSemantic] = useState(true), [relationships, setRelationships] = useState(true);
  const [searchLayers, setSearchLayers] = useState<RetrievalLayer[]>([...retrievalLayers]), [preview, setPreview] = useState<{data: Preview; identity: string} | null>(null);
  const [result, setResult] = useState<{data: Results; identity: string} | null>(null), [ids, setIds] = useState<string[]>([]), [selection, setSelection] = useState<{data: Selection; identity: string} | null>(null);
  const [evidence, setEvidence] = useState<{ref: Ref; text: string} | null>(null), [busy, setBusy] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const alive = useRef(true), gate = useRef(false), callbacks = useRef({onDirtyChange, onError}); callbacks.current = {onDirtyChange, onError};
  const currentIndex = indexes.find((item) => item.id === indexId);
  const availableLayers = searchLayers.filter((layer) => currentIndex?.layers.includes(layer));
  const searchIdentity = retrievalIdentity(indexId, query, availableLayers, semantic, relationships), searchRef = useRef(searchIdentity); searchRef.current = searchIdentity;
  const buildIdentity = JSON.stringify([bookIds.slice().sort(), buildLayers.slice().sort()]), buildRef = useRef(buildIdentity); buildRef.current = buildIdentity;
  const selectionIdentity = JSON.stringify([indexId, ids, result?.identity]), selectionRef = useRef(selectionIdentity); selectionRef.current = selectionIdentity;
  const dirty = ids.length > 0;
  const displayed = result?.identity === searchIdentity ? result.data : null;
  const validSelection = selection?.identity === selectionIdentity && !!displayed;
  const canSearch = !busy && !!query.trim() && currentIndex?.state === "ready" && availableLayers.length > 0;

  useEffect(() => { callbacks.current.onDirtyChange?.(dirty); }, [dirty]);
  useEffect(() => { alive.current = true; void run("load", reload); return () => { alive.current = false; callbacks.current.onDirtyChange?.(false); }; }, []);
  useEffect(() => { setIds([]); setSelection(null); }, [searchIdentity]);
  useEffect(() => {
    if (!indexes.some((item) => item.state === "building")) return;
    let cancelled = false;
    const timer = window.setInterval(() => { void api<{indexes: Index[]; engine: Engine}>(base).then((value) => { if (!cancelled && alive.current) { setIndexes(value.indexes); setEngine(value.engine); } }).catch(() => {}); }, 3000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [indexes.some((item) => item.state === "building")]);

  async function reload() {
    const [corpus, value] = await Promise.all([api<{books: Book[]}>("/reference-books"), api<{indexes: Index[]; engine: Engine}>(base)]);
    if (!alive.current) return;
    setBooks(corpus.books); setIndexes(value.indexes); setEngine(value.engine);
    setIndexId((previous) => previous || value.indexes.find((item) => item.state === "ready")?.id || value.indexes[0]?.id || "");
  }
  async function run(label: string, work: () => Promise<void>) {
    if (gate.current) return;
    gate.current = true; setBusy(label); setError("");
    try { await work(); } catch (failure) { if (alive.current) { const value = failure instanceof Error ? failure.message : "操作未完成，请刷新核对本地索引。"; setError(value); callbacks.current.onError?.(value); } }
    finally { gate.current = false; if (alive.current) setBusy(""); }
  }
  function toggleBook(id: string) { setBookIds((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]); }
  async function previewBuild() {
    const identity = buildIdentity;
    const value = await api<Preview>(`${base}/preview`, "POST", {book_ids: bookIds, layers: buildLayers});
    if (alive.current && buildRef.current === identity) setPreview({data: value, identity});
  }
  async function createIndex() {
    if (!preview || preview.identity !== buildIdentity) return;
    const identity = buildIdentity;
    const value = await api<Index>(`${base}/indexes`, "POST", {book_ids: bookIds, layers: buildLayers, expected_preview: preview.data.hash});
    if (!alive.current) return;
    if (buildRef.current === identity) { setIndexId(value.id); setPreview(null); }
    setIndexes((previous) => [value, ...previous.filter((item) => item.id !== value.id)]);
    setNotice("本地索引已开始。没有调用付费 API；只有成功发布的完整索引可检索。");
  }
  async function search() {
    const identity = searchIdentity;
    const value = await api<Results>(`${base}/indexes/${indexId}/search`, "POST", {query, layers: availableLayers, semantic, relationships, limit: 12});
    if (alive.current && searchRef.current === identity) { setResult({data: value, identity}); setIds([]); setSelection(null); }
  }
  async function previewSelection() {
    if (!displayed) return;
    const identity = selectionIdentity;
    const selected = selectedRetrievalIds(ids, displayed.results);
    const value = await api<Selection>(`${base}/indexes/${indexId}/selection/preview`, "POST", {ids: selected});
    if (alive.current && selectionRef.current === identity) setSelection({data: value, identity});
  }
  async function readEvidence(ref: Ref) {
    if (!ref.book_id || !ref.edition_id || !ref.chapter_id || ref.start_char == null || ref.end_char == null) return;
    const identity = searchIdentity;
    const value = await api<{text: string}>(`/reference-books/${encodeURIComponent(ref.book_id)}/chapters/${encodeURIComponent(ref.chapter_id)}?edition_id=${encodeURIComponent(ref.edition_id)}&offset=${ref.start_char}&limit=${Math.max(1, Math.min(60000, ref.end_char - ref.start_char))}`);
    if (alive.current && searchRef.current === identity) setEvidence({ref, text: value.text});
  }

  return <section className="reference-retrieval"><header className="rr-heading"><div><span className="rr-eyebrow"><Database size={16} />本地资料检索</span><h2>让参考资料进入具体问题</h2><p>原文、结构、表达与已采用方法共同检索。选中精确片段，核对后再加入创作任务。</p></div><button type="button" className="button" disabled={!!busy} onClick={() => void run("refresh", reload)}><RefreshCw size={14} />刷新</button></header>
    {error && <p className="inline-error" role="alert">{error}</p>}{notice && <p className="rr-notice" role="status">{notice}</p>}
    <div className="rr-layout">{!pickerMode && <aside className="rr-panel"><h3><Layers size={16} />建立私有索引</h3><p className="rr-muted">{engine?.available ? "本地 BGE 中文语义模型就绪" : engine?.reason || "正在检查本地语义模型"}。不会自动联网下载。</p>{!books.length && <div className="rr-muted"><p>尚无参考书。先导入 EPUB / TXT / MD 原件并核对目录，再建立本地索引。</p>{onOpenBooks && <button type="button" className="button" disabled={!!busy} onClick={onOpenBooks}>前往参考书架导入</button>}</div>}<div className="rr-book-picker">{books.map((book) => <label key={book.book_id}><input type="checkbox" checked={bookIds.includes(book.book_id)} disabled={book.withdrawn || !!busy} onChange={() => toggleBook(book.book_id)} /><span>{book.title}<small>{number(book.counts.characters)} 字符 · {book.withdrawn ? "已撤回" : book.outbound === "local" ? "仅本地" : "允许摘录外发"}</small></span></label>)}</div>
      <fieldset className="rr-layer-picker"><legend>需要建立的资料层</legend>{retrievalLayers.map((layer) => <label key={layer}><input type="checkbox" checked={buildLayers.includes(layer)} disabled={!!busy} onChange={() => setBuildLayers((current) => current.includes(layer) ? current.filter((value) => value !== layer) : [...current, layer])} />{retrievalLayerLabels[layer]}</label>)}</fieldset>
      <p className="rr-muted">先勾选书籍和资料层，再预览范围；预览后点击“确认建立本地索引”。完成后在右侧选择索引并输入问题。</p><button type="button" className="button" disabled={!!busy || !bookIds.length || !buildLayers.length} onClick={() => void run("preview", previewBuild)}>预览索引范围</button>
      {preview?.identity === buildIdentity && <div className="rr-build-preview"><strong>本次仅本地计算 · $0 API 费用</strong><p>{number(preview.data.coverage.source_characters)} 原文字符 · {number(preview.data.coverage.source_sections)} 解析章段</p><p>{preview.data.coverage.adopted_methods} 个已采用方法 · {preview.data.coverage.verified_studies} 个有效图谱研究</p>{preview.data.warnings.map((value) => <p className="rr-muted" key={value}>{value}</p>)}<button type="button" className="button primary" disabled={!!busy || !preview.data.engine.available} onClick={() => void run("build", createIndex)}>确认建立本地索引</button></div>}
    </aside>}<main className="rr-main"><section className="rr-panel"><label className="rr-index-label">当前检索范围<select value={indexId} onChange={(event) => { setIndexId(event.target.value); setEvidence(null); }} disabled={!!busy}><option value="">请选择已建立的索引</option>{indexes.map((item) => <option key={item.id} value={item.id}>{item.sources.map((source) => source.title).join("、")} · {stateLabels[item.state] || item.state} · {item.id.slice(0, 6)}</option>)}</select></label>
      {currentIndex && <><div className="rr-status"><strong>{stateLabels[currentIndex.state] || currentIndex.state}</strong><span>{number(currentIndex.progress.indexed_chunks)} 个片段 · {number(currentIndex.progress.indexed_characters)} 索引字符（含重叠）</span>{currentIndex.state === "building" && !pickerMode && <button type="button" className="text-button" disabled={!!busy || currentIndex.cancel_requested} onClick={() => void run("cancel", async () => { const value = await api<Index>(`${base}/indexes/${indexId}/cancel`, "POST", {}); if (alive.current) setIndexes((previous) => previous.map((item) => item.id === value.id ? value : item)); })}><Square size={12} />{currentIndex.cancel_requested ? "等待当前计算结束后取消" : "取消建立"}</button>}</div><div className="rr-layer-counts">{retrievalLayers.map((layer) => <span key={layer}>{retrievalLayerLabels[layer]} <b>{number(currentIndex.progress.layers[layer])}</b></span>)}</div><p className="rr-muted">本索引 {currentIndex.book_ids.length} 本；其余 {Math.max(0, books.length - currentIndex.book_ids.length)} 本未在此范围。0 个片段的层尚无可检索内容。</p>{currentIndex.error && <p className="inline-error">{currentIndex.error}</p>}{currentIndex.omitted.length > 0 && <details><summary>{currentIndex.omitted.length} 项未索引范围</summary><ul>{currentIndex.omitted.slice(0, 100).map((item, i) => <li key={i}>{item.chapter_id ? `${item.chapter_id.slice(0, 8)}：` : ""}{item.reason}</li>)}</ul></details>}</>}
      {currentIndex?.coverage.studies?.map((study) => <p className="rr-muted" key={study.id}>研究“{study.question.slice(0, 80)}”：已验证 {study.completed_segments} / {study.total_segments} 个精读片段。{study.completed_segments < study.total_segments ? "部分覆盖；失败或暂停前的有效阅读已保留，未读范围不推断。" : "完成本次所选范围，仍是候选文学分析。"}</p>)}
      {!currentIndex && <p className="rr-muted">{pickerMode ? "尚无可用检索范围。请先在资料中心的本地资料检索页，为所需书籍建立索引，再返回这里选择精确片段。" : "当前还没有检索范围。请先在左侧建立索引，或从上方下拉框选择已有的可检索索引。"}</p>}<div role="search"><label className="rr-query-label">你想研究的问题<input value={query} maxLength={200} placeholder="例如：人物经历失去之后，怎样通过日常细节表达悲伤？" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key !== "Enter" || event.nativeEvent.isComposing) return; event.preventDefault(); event.stopPropagation(); if (canSearch) void run("search", search); }} /></label><div className="rr-search-modes"><label><input type="checkbox" checked={semantic} onChange={(event) => setSemantic(event.target.checked)} />中文语义召回</label><label><input type="checkbox" checked={relationships} onChange={(event) => setRelationships(event.target.checked)} />扩展有证据的图谱关系</label></div><div className="rr-search-layers">{retrievalLayers.filter((layer) => currentIndex?.layers.includes(layer)).map((layer) => <label key={layer}><input type="checkbox" checked={searchLayers.includes(layer)} onChange={() => setSearchLayers((current) => current.includes(layer) ? current.filter((value) => value !== layer) : [...current, layer])} />{retrievalLayerLabels[layer]}</label>)}</div><button type="button" className="button primary" disabled={!canSearch} onClick={() => { if (canSearch) void run("search", search); }}><Search size={14} />{busy === "search" ? "正在本地检索…" : "检索资料"}</button></div>
    </section>
    {displayed && <section className="rr-panel"><div className="rr-results-heading"><h3>召回证据 <span>{displayed.results.length}</span></h3><p className="rr-muted">词项与真实 BGE 语义按 RRF 合并；{displayed.relationship_candidates} 个有证据的关系扩展候选。</p></div>{displayed.results.map((hit) => <article className={`rr-hit ${ids.includes(hit.id) ? "selected" : ""}`} key={hit.id}><div className="rr-hit-heading"><label><input type="checkbox" checked={ids.includes(hit.id)} onChange={() => setIds((current) => current.includes(hit.id) ? current.filter((id) => id !== hit.id) : [...current, hit.id])} /><strong>{hit.title}</strong></label><span>{retrievalLayerLabels[hit.layer]}</span></div><p className="rr-hit-text">{hit.text}</p><small className="rr-muted">融合分数 {hit.score.toFixed(5)} · {Object.keys(hit.channels).map((key) => key === "semantic" ? `语义 #${hit.channels[key].rank}` : key === "lexical" ? `词项 #${hit.channels[key].rank}` : "证据关系扩展").join(" / ")}</small>{hit.refs.length > 0 && <details><summary>依据与精确范围（{hit.refs.length}）</summary>{hit.refs.map((ref, i) => <div className="rr-evidence" key={i}><small>章段 {ref.chapter_id?.slice(0, 8) || "资料来源"} · 字符 [{ref.start_char}, {ref.end_char})</small>{ref.quote && <blockquote>{ref.quote}</blockquote>}{ref.book_id && ref.edition_id && ref.chapter_id && <button type="button" className="text-button" disabled={!!busy} onClick={() => void run("evidence", () => readEvidence(ref))}>回查原文<ArrowRight size={12} /></button>}</div>)}</details>}</article>)}{!displayed.results.length && <p className="rr-muted">当前范围未召回资料。可修改问题或建立其他书籍的索引。</p>}<ul className="rr-limitations">{displayed.limitations.map((value) => <li key={value}>{value}</li>)}</ul><div className="rr-selection-bar"><span>已选择 {ids.length} 个片段</span><button type="button" className="button" disabled={!!busy || !ids.length} onClick={() => void run("selection", previewSelection)}>预览用于任务的精确资料</button></div></section>}
    {validSelection && selection && <section className="rr-panel rr-send-preview"><h3>任务资料预览 · {selection.data.characters} / 24000 字符</h3><p>来源与当前外发授权已核对。仅下方文本进入任务，相关证据和其他章节不会自动发送。</p><pre>{selection.data.text}</pre><button type="button" className="button primary" disabled={!novelId || !onUseSelection || !!busy} onClick={() => { onUseSelection?.(retrievalSelection(indexId, selection.data.ids, selection.data.hash)); setNotice(pickerMode ? "已选作当前任务的资料；仍需重新核对任务模型与预算预览。" : "已选作本书下一次任务的资料；仍需核对任务的模型与预算预览。"); }}><Check size={14} />{pickerMode ? "将所选片段加入当前任务" : "用于本书下一次任务"}</button>{!novelId && <p className="rr-muted">先在 Studio 选择一部创作作品，才可将资料加入该作品任务。</p>}</section>}
    </main></div>{pickerMode && !indexes.some((item) => item.state === "ready") && onOpenBooks && <button type="button" className="button secondary small" disabled={!!busy} onClick={onOpenBooks}>前往资料中心建立索引</button>}{evidence && (pickerMode ? <section className="rr-inline-evidence" aria-label="回查固定版本原文"><h3>回查固定版本原文</h3><p className="rr-muted">Unicode 字符 [{evidence.ref.start_char}, {evidence.ref.end_char})</p><blockquote className="rr-hit-text">{evidence.text}</blockquote><button type="button" className="text-button" onClick={() => setEvidence(null)}>收起原文回查</button></section> : <Modal title="回查固定版本原文" subtitle={`Unicode 字符 [${evidence.ref.start_char}, ${evidence.ref.end_char})`} onClose={() => setEvidence(null)}><blockquote className="rr-hit-text">{evidence.text}</blockquote></Modal>)}
  </section>;
}
