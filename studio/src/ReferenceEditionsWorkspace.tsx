import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Check, GitCompareArrows, LoaderCircle, Scissors, ShieldCheck } from "lucide-react";
import { api } from "./api";
import "./referenceEditions.css";

type Segment = {chapter_id: string; start_char: number; end_char: number};
type Chapter = {title: string; kind: "body" | "ancillary"; volume_title: string; segments: Segment[]};
type Patch = Segment & {expected_text: string; replacement: string; reason: string};
type Request = {base_edition_id: string; expected_hash: string; reason: string; chapters: Chapter[]; patches: Patch[]};
type Candidate = {id: string; hash: string; reason: string; base_edition_id: string; request: Request; summary: Record<string, unknown>};
type Audit = {title: string; findings: Record<string, unknown>[]; issues: Record<string, unknown>[]; heading_candidates: {title: string; chapter_id: string; start_char: number}[]; proposal: Request; alternatives: {headings: Request; retain_later: Request}; note: string};
type Book = {hash: string; edition_id: string; editions: {edition_id: string}[]};
type Props = {bookId: string; onClose: () => void; onPublished?: () => void; onDirtyChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void};
const PAGE = 40;
const visible = (value: unknown) => JSON.stringify(value, null, 2);

export default function ReferenceEditionsWorkspace({bookId, onClose, onPublished, onDirtyChange, onBusyChange}: Props) {
  const [audit, setAudit] = useState<Audit | null>(null), [request, setRequest] = useState<Request | null>(null);
  const [book, setBook] = useState<Book | null>(null), [candidates, setCandidates] = useState<Candidate[]>([]), [selected, setSelected] = useState<Candidate | null>(null);
  const [preview, setPreview] = useState<{hash: string; summary: Record<string, unknown>} | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false), [dirty, setDirty] = useState(false), [page, setPage] = useState(0);
  const [patchText, setPatchText] = useState("[]"), [splitAt, setSplitAt] = useState(""), [splitIndex, setSplitIndex] = useState<number | null>(null);
  const [confirm, setConfirm] = useState(false), [mapEdition, setMapEdition] = useState(""), [mapPage, setMapPage] = useState(0), [mapping, setMapping] = useState<{ranges: unknown[]; total: number; note: string} | null>(null);
  useEffect(() => {onBusyChange?.(busy); return () => onBusyChange?.(false);}, [busy, onBusyChange]);
  const generation = useRef(0), key = useRef(crypto.randomUUID());
  const url = `/reference-books/${encodeURIComponent(bookId)}`;
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    const current = ++generation.current;
    setBusy(true); setError(""); setAudit(null); setRequest(null); setSelected(null); setPreview(null); setDirty(false); setMapping(null); setMapEdition(""); setPage(0);
    Promise.all([api<Audit>(`${url}/edition-audit`), api<Book>(url), api<{candidates: Candidate[]}>(`${url}/edition-candidates`)])
      .then(([a, b, c]) => { if (current === generation.current) {setAudit(a); setRequest(a.proposal); setPatchText(visible(a.proposal.patches)); setBook(b); setCandidates(c.candidates);} })
      .catch(e => {if (current === generation.current) setError(String(e.message || e));})
      .finally(() => {if (current === generation.current) setBusy(false);});
    return () => {generation.current++;};
  }, [bookId, url]);
  const update = (next: Request) => {setRequest(next); setPreview(null); setSelected(null); setConfirm(false); setDirty(true); key.current = crypto.randomUUID();};
  const chapterUpdate = (index: number, change: Partial<Chapter>) => {if (request) update({...request, chapters: request.chapters.map((c, i) => i === index ? {...c, ...change} : c)});};
  const move = (index: number, step: number) => {if (!request || index + step < 0 || index + step >= request.chapters.length) return; const next = [...request.chapters]; [next[index], next[index + step]] = [next[index + step], next[index]]; update({...request, chapters: next});};
  const execute = async (action: () => Promise<void>) => {const current = generation.current; setBusy(true); setError(""); setNotice(""); try {await action();} catch (e) {if(current === generation.current) setError(String((e as Error).message || e));} finally {if(current === generation.current) setBusy(false);}};
  const refresh = async () => {const [b, c] = await Promise.all([api<Book>(url), api<{candidates: Candidate[]}>(`${url}/edition-candidates`)]); setBook(b); setCandidates(c.candidates); return b;};
  const prepared = () => {if (!request) throw Error("请先读取审计方案"); const patches = JSON.parse(patchText) as Patch[]; if (!Array.isArray(patches)) throw Error("补丁需要JSON数组"); return {...request, patches};};
  const doPreview = () => execute(async () => {const body = prepared(); const value = await api<{hash: string; summary: Record<string, unknown>}>(`${url}/edition-candidates/preview`, "POST", body); setRequest(body); setPreview(value);});
  const save = () => execute(async () => {if (!preview) return; const candidate = await api<Candidate>(`${url}/edition-candidates`, "POST", {...prepared(), expected_preview:preview.hash, idempotency_key:key.current}); setSelected(candidate); setDirty(false); await refresh(); setNotice("已保存整理候选，当前版本与原件尚未改变。");});
  const publish = () => execute(async () => {if (!selected || !book || !confirm) return; const value = await api<Book>(`${url}/edition-candidates/${selected.id}/publish`, "POST", {expected_candidate_hash:selected.hash, expected_hash:book.hash, make_current:true}); const latest = await refresh(); setDirty(false); setConfirm(false); setMapEdition(value.edition_id); setNotice(latest.edition_id === value.edition_id ? "新整理版已建立并设为当前版，原件、旧版本和旧引文保持固定版本。" : "该整理版已存在；当前版保留最近的选择。可在版本区另行切换。"); onPublished?.();});
  const split = () => {if (!request || splitIndex === null) return; const c = request.chapters[splitIndex], at = Number(splitAt); if(c.segments.length !== 1 || !Number.isInteger(at) || !(c.segments[0].start_char < at && at < c.segments[0].end_char)) {setError("请输入该来源段内的Unicode码点位置；多段合并章请在高级方案中拆分。"); return;} const segment = c.segments[0]; const next = [...request.chapters]; next.splice(splitIndex, 1, {...c, segments:[{...segment,end_char:at}]}, {...c,title:c.title + " · 后段",segments:[{...segment,start_char:at}]}); update({...request,chapters:next}); setSplitIndex(null);};
  const merge = (index: number) => {if(!request || index + 1 >= request.chapters.length) return; const next = [...request.chapters], a = next[index], b = next[index+1]; next.splice(index,2,{...a,segments:[...a.segments,...b.segments]}); update({...request,chapters:next});};
  const loadMap = (offset = mapPage) => execute(async () => {if(!mapEdition) return; setMapping(await api(`${url}/editions/${mapEdition}/map?offset=${offset}&limit=40`)); setMapPage(offset);});
  return <section className="reference-editions" aria-label="目录与版本整理">
    <header className="editions-header"><button className="secondary" onClick={onClose} disabled={busy}><ArrowLeft size={15}/>返回参考书架</button><div><h2>目录与版本整理</h2><p>{audit?.title || "正在读取参考书"} · 所有整理先预览，原件与旧版本保留</p></div>{busy && <LoaderCircle className="spin" size={20}/>}</header>
    {error && <p role="alert" className="error-message">{error}</p>}{notice && <p role="status" className="editions-notice">{notice}</p>}
    {audit && request && <>
      <div className="editions-explainer"><ShieldCheck size={19}/><p>技术分册用于文件存储；这里编辑的是正文结构区段。拆分、合并、重排均须完整保留来源内容；重复和附记可隔离，坏字节缺口保持未知。</p></div>
      <details><summary>本地审计：{audit.issues.length} 项原始提示、{audit.findings.length} 项整理线索</summary><pre>{visible(audit.findings)}</pre><p>{audit.note}</p></details>
      <div className="editions-toolbar"><button className="secondary" disabled={busy} onClick={() => {update(audit.alternatives.headings); setPatchText(visible(audit.alternatives.headings.patches));}}>载入正文标题拆分建议（{audit.heading_candidates.length} 个）</button><button className="secondary" disabled={busy} onClick={() => {update(audit.alternatives.retain_later); setPatchText(visible(audit.alternatives.retain_later.patches));}}>重复区段保留后出现的位置</button></div>
      <label className="editions-field">整理理由<input value={request.reason} maxLength={2000} disabled={busy} onChange={e => update({...request, reason:e.target.value})}/></label>
      <p className="muted">共 {request.chapters.length} 个结构区段。附记隔离仍保留全部文本；从头或尾保留重复内容均是待核建议。</p>
      <div className="editions-table-wrap"><table><thead><tr><th>位置</th><th>标题与原书卷</th><th>用途</th><th>精确来源</th><th>调整</th></tr></thead><tbody>{request.chapters.slice(page,page+PAGE).map((c,n) => {const i=page+n; return <tr key={i}><td>{i+1}</td><td><input aria-label={`第${i+1}段标题`} value={c.title} maxLength={500} disabled={busy} onChange={e=>chapterUpdate(i,{title:e.target.value})}/><input aria-label={`第${i+1}段卷名`} placeholder="原书卷名（可空）" value={c.volume_title} disabled={busy} onChange={e=>chapterUpdate(i,{volume_title:e.target.value})}/></td><td><select aria-label={`第${i+1}段用途`} value={c.kind} disabled={busy} onChange={e=>chapterUpdate(i,{kind:e.target.value as Chapter["kind"]})}><option value="body">正文</option><option value="ancillary">隔离 / 附记</option></select></td><td><details><summary>{c.segments.length} 个来源段</summary><pre>{visible(c.segments)}</pre></details></td><td><div className="editions-row-actions"><button aria-label={`上移第${i+1}段`} disabled={busy || i===0} onClick={()=>move(i,-1)}><ArrowUp size={14}/></button><button aria-label={`下移第${i+1}段`} disabled={busy || i===request.chapters.length-1} onClick={()=>move(i,1)}><ArrowDown size={14}/></button><button disabled={busy || c.segments.length!==1} onClick={()=>{setSplitIndex(i);setSplitAt("");}}><Scissors size={14}/>拆分</button><button disabled={busy || i===request.chapters.length-1} onClick={()=>merge(i)}>合并下一段</button></div></td></tr>;})}</tbody></table></div>
      <div className="editions-toolbar"><button disabled={busy || !page} onClick={()=>setPage(Math.max(0,page-PAGE))}>上一页</button><span>{page+1}–{Math.min(page+PAGE,request.chapters.length)}</span><button disabled={busy || page+PAGE>=request.chapters.length} onClick={()=>setPage(page+PAGE)}>下一页</button></div>
      {splitIndex!==null && <div className="editions-inline"><label>第 {splitIndex+1} 段拆分位置（来源Unicode码点）<input inputMode="numeric" value={splitAt} onChange={e=>setSplitAt(e.target.value)}/></label><button onClick={split}>确认拆分草案</button><button onClick={()=>setSplitIndex(null)}>取消</button></div>}
      <details><summary>精确文本补丁（{request.patches.length} 条）与高级方案</summary><p>补丁必须提供章段ID、准确位置、旧原文、新文本与理由。NUL以JSON的 \u0000 显示；缺口不自动补字。</p><textarea aria-label="精确文本补丁JSON" rows={10} value={patchText} disabled={busy} onChange={e=>{setPatchText(e.target.value);setPreview(null);setSelected(null);setDirty(true);key.current=crypto.randomUUID();}}/><details><summary>完整组成方案JSON（可复制核对）</summary><pre>{visible(request)}</pre></details></details>
      <div className="editions-toolbar"><button className="primary" disabled={busy} onClick={doPreview}><GitCompareArrows size={16}/>预览整理差异</button><button className="secondary" disabled={busy || !preview} onClick={save}>保存候选</button></div>
      {preview && <div className="editions-preview"><h3>整理预览</h3><pre>{visible(preview.summary)}</pre><p>来源全覆盖核对通过。保存候选不会改当前版；发布需再次确认。</p></div>}
      <section><h3>已保存候选</h3>{candidates.length===0 && <p className="muted">暂无候选。</p>}{candidates.map(c=><button key={c.id} className="editions-candidate" disabled={busy || dirty} onClick={()=>execute(async()=>{const value=await api<Candidate>(`${url}/edition-candidates/${c.id}`);setSelected(value);setRequest(value.request);setPatchText(visible(value.request.patches));setPreview(null);setConfirm(false);setPage(0);})}>{c.reason}<small>{c.id.slice(0,12)} · 来源 {c.base_edition_id.slice(0,12)}</small></button>)}</section>
      {selected && <div className="editions-preview"><h3>待发布候选</h3><pre>{visible(selected.summary)}</pre><label><input type="checkbox" checked={confirm} disabled={busy} onChange={e=>setConfirm(e.target.checked)}/>我已核对目录顺序、精确补丁与隔离区段，建立新整理版并设为当前版</label><button className="primary" disabled={busy || !confirm || dirty} onClick={publish}><Check size={16}/>发布整理版本</button></div>}
      <section><h3>版本与映射回查</h3><div className="editions-toolbar"><select aria-label="选择回查版本" value={mapEdition} onChange={e=>{setMapEdition(e.target.value);setMapping(null);setMapPage(0);}}><option value="">选择版本</option>{book?.editions.map(e=><option key={e.edition_id} value={e.edition_id}>{e.edition_id}{e.edition_id===book.edition_id ? " · 当前版" : ""}</option>)}</select><button disabled={busy || !mapEdition} onClick={()=>loadMap(0)}>查看映射</button><button disabled={busy || !mapEdition || !book || mapEdition===book.edition_id || dirty} onClick={()=>execute(async()=>{if(!book)return;await api(`${url}/editions/${mapEdition}/select`,"POST", {expected_hash:book.hash});await refresh();setNotice("已切换当前版；旧引用不会改绑或复活。");onPublished?.();})}>设为当前版</button></div>{mapping && <><p>{mapping.note || "该原始解析版没有父版本映射。"}</p><pre>{visible(mapping.ranges)}</pre><div className="editions-toolbar"><button disabled={busy || !mapPage} onClick={()=>loadMap(Math.max(0,mapPage-PAGE))}>上一页映射</button><span>{mapping.total} 条</span><button disabled={busy || mapPage+PAGE>=mapping.total} onClick={()=>loadMap(mapPage+PAGE)}>下一页映射</button></div></>}</section>
    </>}
  </section>;
}
