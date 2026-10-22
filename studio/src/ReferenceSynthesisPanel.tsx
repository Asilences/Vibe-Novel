import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

type Data = Record<string, unknown>;
type Node = {id: string; kind: string; state: string; first_read_index: number; last_read_index: number; depends_on: string[]; has_response?: boolean; error?: string};
type Result = {node_id: string; kind: string; result: Data};
type Synthesis = {id: string; state: string; completed_nodes: number; total_nodes: number; estimated_cost_usd: number; budget_max_usd: number; error?: string; nodes?: Node[]; results?: Result[]; source_coverage?: Data; warnings?: string[]};
type Preview = {hash: string; nodes: Node[]; estimated_cost_usd: number; model_identity?: string; warnings: string[]};
type Frozen = {value: Preview; request: Data; identity: string; key: string};
const base = "/reference-syntheses";
const labels: Record<string, string> = {stages: "阶段与转折", main_lines: "主线与辅线", character_arcs: "人物弧光", organization: "内容组织", expression: "表达观察", methods: "可迁移方法", gaps: "证据缺口"};
const fields: Record<string, string> = {title: "主题", name: "人物", goal: "目标", turning_point: "转折", outcome: "结果", development: "推进与交织", trajectory: "变化", mechanism: "安排机制", technique: "表达技巧", effect: "效果推断", limitations: "局限", steps: "步骤"};
const states: Record<string, string> = {prepared: "已准备，等待启动", running: "归并中", pausing: "当前请求完成后暂停", paused: "已暂停", succeeded: "已完成", failed: "待核对／离线修复", interrupted: "已中断，禁止自动重放", cancelled: "已取消", pending: "待处理"};
const money = (n: unknown) => typeof n === "number" ? `$${n.toFixed(4)}` : "报价未知";
const rows = (value: unknown): Data[] => Array.isArray(value) ? value as Data[] : [];
const message = (failure: unknown) => failure instanceof ApiError && failure.status < 500 ? failure.message.slice(0, 500) : "结果尚不明确，请刷新核对；保留原提交键，不要重复创建。";

function Observation({item}: {item: Data}) {
  return <article>{Object.entries(item).filter(([key]) => fields[key]).map(([key, value]) => <p key={key}><strong>{fields[key]}：</strong>{Array.isArray(value) ? value.join("；") : String(value)}</p>)}
    {typeof item.first_read_index === "number" && <p>阅读段 {item.first_read_index + 1}—{Number(item.last_read_index) + 1}</p>}
    {rows(item.source_refs).map((ref, index) => <details key={index}><summary>原文证据 · 字符 [{String(ref.start_char)}, {String(ref.end_char)})</summary><blockquote>{String(ref.quote)}</blockquote><small>书籍 {String(ref.book_id)} · 解析版 {String(ref.edition_id)} · 段 {String(ref.chapter_id)}</small></details>)}
  </article>;
}

export default function ReferenceSynthesisPanel({studyId, state, completedNodes, totalNodes, onDirtyChange}: {studyId: string; state: string; completedNodes: number; totalNodes: number; onDirtyChange?: (dirty: boolean) => void}) {
  const [list, setList] = useState<Synthesis[]>([]), [selected, setSelected] = useState<Synthesis | null>(null);
  const [stageSize, setStageSize] = useState(30), [budget, setBudget] = useState("3"), [output, setOutput] = useState(6144);
  const [preview, setPreview] = useState<Frozen | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [repair, setRepair] = useState<{node: string; hash: string; raw: string; original: string} | null>(null), [reason, setReason] = useState("");
  const [indices, setIndices] = useState<number[]>([]), [methodPreview, setMethodPreview] = useState<{value: Data; indices: number[]; key: string} | null>(null);
  const epoch = useRef(0), gate = useRef(false), selectedIdentity = useRef("");
  selectedIdentity.current = selected?.id || "";
  const request = {study_id: studyId, stage_size: stageSize, budget_max_usd: Number(budget), max_output_tokens: output};
  const identity = JSON.stringify(request), fresh = preview?.identity === identity;
  const ready = state === "succeeded" && completedNodes > 0 && completedNodes === totalNodes;
  const active = !!selected && ["running", "pausing"].includes(selected.state);
  const final = selected?.state === "succeeded" ? selected.results?.[selected.results.length - 1] : undefined;
  const methods = rows(final?.result.methods);
  const callback = useRef(onDirtyChange); callback.current = onDirtyChange;
  const dirty = !!preview || !!repair || indices.length > 0 || !!methodPreview;
  useEffect(() => {callback.current?.(dirty);}, [dirty]);
  useEffect(() => {
    const version = ++epoch.current;
    setList([]); setSelected(null); setPreview(null); setRepair(null); setIndices([]); setMethodPreview(null); setError(""); setNotice("");
    void api<{syntheses: Synthesis[]}>(`${base}?study_id=${studyId}`).then(value => {if (epoch.current === version) setList(value.syntheses);}).catch(failure => {if (epoch.current === version) setError(message(failure));});
    return () => {epoch.current++; callback.current?.(false);};
  }, [studyId]);
  useEffect(() => {
    if (!active || !selected) return;
    const id = selected.id, version = epoch.current;
    const timer = window.setInterval(() => {if (gate.current) return; void api<Synthesis>(`${base}/${id}`).then(value => {if (epoch.current === version && selectedIdentity.current === id) setSelected(value);}).catch(failure => {if (epoch.current === version && selectedIdentity.current === id) setError(message(failure));});}, 3000);
    return () => window.clearInterval(timer);
  }, [active, selected?.id]);
  async function act(work: () => Promise<void>) {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError("");
    const version = epoch.current;
    try {await work();} catch (failure) {if (epoch.current === version) setError(message(failure));} finally {gate.current = false; if (epoch.current === version) setBusy(false);}
  }
  async function refresh(id = selected?.id) {
    const version = epoch.current;
    const result = await api<{syntheses: Synthesis[]}>(`${base}?study_id=${studyId}`);
    const detail = id ? await api<Synthesis>(`${base}/${id}`) : null;
    if (epoch.current === version) {setList(result.syntheses); if (detail) setSelected(detail);}
  }
  async function control(action: string) {
    if (!selected) return;
    const version = epoch.current, id = selected.id;
    const result = await api<Synthesis>(`${base}/${id}/${action}`, "POST");
    if (epoch.current === version) {setSelected(result); await refresh();}
  }
  return <section className="rs-card"><h3>阶段与全书归并</h3>
    <p>将当前已读范围的候选观察归并为阶段、主辅线、人物弧光、内容组织与表达方法。共用私人研究账户累计预算；归并读取摘要和精确引文。</p>
    {!ready && <p>连续精读完成当前选定范围后才能新建归并。当前 {completedNodes}/{totalNodes} 个阅读段；局部范围的归并仅代表该范围。</p>}
    <div className="rs-actions"><label>每阶段阅读段数<input type="number" min={30} max={50} value={stageSize} disabled={busy} onChange={event => setStageSize(Number(event.target.value))}/></label>
      <label>本批预算上限（美元）<input type="number" min="0.01" step="0.1" value={budget} disabled={busy} onChange={event => setBudget(event.target.value)}/></label>
      <label>每次输出 Token 上限<input type="number" min={2048} max={16000} value={output} disabled={busy} onChange={event => setOutput(Number(event.target.value))}/></label>
      <button disabled={busy || !ready} onClick={() => void act(async () => {const version = epoch.current; const value = await api<Preview>(`${base}/preview`, "POST", request); if (epoch.current === version) setPreview({value, request, identity, key: crypto.randomUUID()});})}>预览阶段与最终归并</button>
      <button disabled={busy} onClick={() => void act(refresh)}>刷新归并列表</button></div>
    {error && <p role="alert" className="rs-error">{error}</p>}{notice && <p role="status">{notice}</p>}
    {preview && <div className="rs-card"><strong>保守预览：{money(preview.value.estimated_cost_usd)} · {preview.value.nodes.length} 次归并 · {preview.value.model_identity}</strong>
      <p>每个阶段保留全部阅读摘要及每类最多 3 条观察；原始完整观察仍保留。4 路分层归并保留各阶段结果与依赖树。</p>
      {preview.value.warnings.map(warning => <p key={warning}>{warning}</p>)}
      {!fresh && <p>设置已变化，请重新预览。</p>}
      <button disabled={busy || !fresh || !ready} onClick={() => void act(async () => {const version = epoch.current; const result = await api<Synthesis>(base, "POST", {...preview.request, expected_preview: preview.value.hash, idempotency_key: preview.key}); if (epoch.current === version) {setSelected(result); setPreview(null); await refresh(result.id);}})}>保存为待启动归并</button>
      <button disabled={busy} onClick={() => setPreview(null)}>关闭预览</button></div>}
    <div className="rs-actions">{list.map(item => <button key={item.id} disabled={busy || dirty} onClick={() => void act(async () => {const version = epoch.current; const value = await api<Synthesis>(`${base}/${item.id}`); if (epoch.current === version) {setSelected(value); setIndices([]); setMethodPreview(null); setRepair(null);}})}>{item.id.slice(0, 8)} · {states[item.state] || item.state} · {item.completed_nodes}/{item.total_nodes}</button>)}</div>
    {selected && <><h4>归并 {selected.id.slice(0, 8)} · {states[selected.state] || selected.state}</h4><p>{selected.completed_nodes}/{selected.total_nodes} 个节点 · 保守预览 {money(selected.estimated_cost_usd)} · 本批上限 {money(selected.budget_max_usd)}</p>
      {selected.error && <p role="alert">{selected.error}</p>}
      <div className="rs-actions"><button disabled={busy || !["prepared", "paused"].includes(selected.state)} onClick={() => void act(() => control("start"))}>明确启动付费归并</button>
        <button disabled={busy || !active} onClick={() => void act(() => control("pause"))}>当前请求后暂停</button>
        <button disabled={busy || ["succeeded", "cancelled"].includes(selected.state)} onClick={() => void act(() => control("cancel"))}>取消后续请求</button></div>
      <details><summary>阶段树与执行依赖</summary>{selected.nodes?.map(node => <div key={node.id}><p>{node.id} · 阅读段 {node.first_read_index + 1}—{node.last_read_index + 1} · {states[node.state] || node.state} · 依赖：{node.depends_on.join("、") || "连续精读候选"}</p>{node.error && <p>{node.error}</p>}
        {node.has_response && ["failed", "interrupted"].includes(node.state) && <button disabled={busy || active} onClick={() => void act(async () => {const version = epoch.current; const response = await api<{text: string; hash: string}>(`${base}/${selected.id}/nodes/${node.id}/response`); if (epoch.current === version) {setRepair({node: node.id, hash: response.hash, raw: response.text, original: response.text}); setReason("");}})}>查看原响应／离线修复</button>}</div>)}</details>
      {repair && <div className="rs-card"><h4>离线修复 {repair.node}</h4><p>保留原响应，仅修复候选 JSON 与精确证据，不重新调用模型。</p><textarea rows={12} value={repair.raw} onChange={event => setRepair({...repair, raw: event.target.value})}/><label>修复理由<input value={reason} onChange={event => setReason(event.target.value)}/></label><button disabled={busy || !reason.trim()} onClick={() => void act(async () => {const version = epoch.current; const result = await api<Synthesis>(`${base}/${selected.id}/nodes/${repair.node}/repair`, "POST", {expected_hash: repair.hash, raw: repair.raw, reason}); if (epoch.current === version) {setSelected(result); setRepair(null); setReason("");}})}>保存修复候选</button><button disabled={busy} onClick={() => setRepair(null)}>关闭修复</button></div>}
      {selected.results?.map(result => <details key={result.node_id} open={result.node_id === final?.node_id}><summary>{result.kind === "stage" ? "阶段归并" : "分层归并"} · {result.node_id}</summary><p>{String(result.result.summary)}</p>{Object.entries(labels).map(([section, title]) => <section key={section}><h4>{title}</h4>{section === "gaps" ? (result.result.gaps as string[] || []).map((gap, i) => <p key={i}>{gap}</p>) : rows(result.result[section]).map((item, i) => <Observation key={i} item={item}/>)}</section>)}</details>)}
      {methods.length > 0 && <div className="rs-card"><h4>最终方法候选</h4><p>保存到个人方法库后仍为未采用候选，作者需在方法库明确采用和绑定作品。</p>{methods.map((method, index) => <label key={index}><input type="checkbox" checked={indices.includes(index)} disabled={busy || !!methodPreview} onChange={event => setIndices(event.target.checked ? [...indices, index] : indices.filter(i => i !== index))}/>{String(method.title)}</label>)}
        <button disabled={busy || !indices.length || !!methodPreview} onClick={() => void act(async () => {const version = epoch.current; const value = await api<Data>(`${base}/${selected.id}/methods/preview`, "POST", {method_indices: indices}); if (epoch.current === version) setMethodPreview({value, indices: [...indices], key: crypto.randomUUID()});})}>预览保存方法候选</button>
        {methodPreview && <><pre>{JSON.stringify(methodPreview.value, null, 2)}</pre><button disabled={busy} onClick={() => void act(async () => {const version = epoch.current; await api(`${base}/${selected.id}/methods`, "POST", {method_indices: methodPreview.indices, expected_preview: methodPreview.value.hash, idempotency_key: methodPreview.key}); if (epoch.current === version) {setNotice("已保存到个人方法库，状态为未采用候选。"); setMethodPreview(null); setIndices([]);}})}>确认保存未采用候选</button><button disabled={busy} onClick={() => setMethodPreview(null)}>取消方法预览</button></>}
        {indices.length > 0 && !methodPreview && <button disabled={busy} onClick={() => setIndices([])}>清空方法选择</button>}</div>}
    </>}
  </section>;
}
