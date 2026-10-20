import { useEffect, useRef, useState } from "react";
import { CircleAlert, FileText, LoaderCircle, RefreshCw, Save } from "lucide-react";
import { api, projectUrl } from "./api";
import type { AnalysisEvidence, AnalysisStructure, AnalysisToolkit, DocumentItem, Project } from "./api";
import { Empty } from "./ui";

type Props = { project: Project; documents: DocumentItem[]; onOpen: (path: string, line?: number, expectedHash?: string) => void; onRefresh: () => Promise<void>; onDirtyChange?: (dirty: boolean) => void; view: "toolkit" | "structure" };
function configuration(value: AnalysisToolkit) {
  return { lexicon: value.lexicon, characters: value.characters, threads: value.threads, style: { instructions: value.style.instructions, avoid: value.style.avoid, anchors: value.style.anchors.map(({ path, hash, start_line, end_line }) => ({ path, hash, start_line, end_line })) } };
}
function errorText(error: unknown) { return error instanceof Error ? error.message : "操作未完成，请重试。"; }

export default function AnalysisToolkitWorkspace({ project, documents, onOpen, onRefresh, onDirtyChange, view }: Props) {
  const base = `${projectUrl(project.novel_id)}/analysis`;
  const [toolkit, setToolkit] = useState<AnalysisToolkit | null>(null);
  const [structure, setStructure] = useState<AnalysisStructure | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [signature, setSignature] = useState("");
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
  const alive = useRef(true);
  const requestId = useRef(0);
  const historySignature = documents.filter((item) => item.path.startsWith("chapters/") || item.path.startsWith("outlines/approved/")).map((item) => `${item.path}:${item.hash}`).sort().join("|");
  const stale = !!structure && (signature !== historySignature || structure.toolkit_hash !== toolkit?.hash || (project.canonical_revision !== undefined && structure.canonical_revision !== project.canonical_revision));
  const anchorStale = !!toolkit && (toolkit.stale || toolkit.style.anchors.some((item) => item.stale || documents.find((document) => document.path === item.path)?.hash !== item.hash));
  const dirty = !!toolkit && text !== JSON.stringify(configuration(toolkit), null, 2);
  const dirtyCallback = useRef(onDirtyChange); dirtyCallback.current = onDirtyChange;
  useEffect(() => { dirtyCallback.current?.(dirty); }, [dirty]);
  useEffect(() => () => dirtyCallback.current?.(false), []);

  async function load() {
    const request = ++requestId.current;
    const snapshot = historySignature;
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await api<AnalysisToolkit>(`${base}/toolkit`);
      const report = view === "structure" ? await api<AnalysisStructure>(`${base}/structure`) : null;
      if (!alive.current || request !== requestId.current) return;
      setToolkit(value); setText(JSON.stringify(configuration(value), null, 2));
      setStructure(report); setSignature(snapshot);
      setDiagnostics(null);
    } catch (failure) { if (alive.current && request === requestId.current) setError(errorText(failure)); }
    finally { if (alive.current && request === requestId.current) setBusy(false); }
  }
  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; requestId.current += 1; }; }, [base, view]);

  async function save() {
    if (!toolkit || busy) return;
    const snapshot = text;
    setBusy(true); setError(""); setNotice("");
    const request = ++requestId.current;
    try {
      let data: unknown;
      try { data = JSON.parse(snapshot); } catch { throw new Error("JSON 格式有误，请检查逗号、引号和括号。尚未保存任何设置。"); }
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("配置须是包含 lexicon、characters、threads、style 的 JSON 对象。");
      const value = await api<AnalysisToolkit>(`${base}/toolkit`, "PUT", { ...(data as Record<string, unknown>), expected_hash: toolkit.hash });
      if (!alive.current || request !== requestId.current) return;
      setToolkit(value); setText(JSON.stringify(configuration(value), null, 2));
      setNotice("已保存本作品的检查配置。人物与伏笔仅用于检索提示；保存配置不会更新正式设定或故事记忆。");
      await onRefresh();
    } catch (failure) { if (alive.current && request === requestId.current) setError(errorText(failure)); }
    finally { if (alive.current && request === requestId.current) setBusy(false); }
  }
  async function diagnose() {
    const request = ++requestId.current;
    setBusy(true); setError("");
    try {
      const value = await api<Record<string, unknown>>(`${base}/diagnostics`);
      if (alive.current && request === requestId.current) setDiagnostics(value);
    } catch (failure) { if (alive.current && request === requestId.current) setError(errorText(failure)); }
    finally { if (alive.current && request === requestId.current) setBusy(false); }
  }
  function evidence(items: AnalysisEvidence[]) {
    return items.length ? <details className="analysis-limits"><summary>查看原句证据（{items.length}）</summary>{items.map((item, index) => {
      const changed = stale || documents.find((document) => document.path === item.path)?.hash !== item.hash;
      return <div className="analysis-evidence" key={`${item.path}:${item.start_line}:${index}`}><button type="button" className="analysis-source" disabled={changed} onClick={() => onOpen(item.path, item.start_line, item.hash)}><FileText size={13} />{item.path}:{item.start_line}–{item.end_line}{changed ? " · 来源已失效" : ""}</button><small>版本 {item.hash.slice(0, 12)}</small><blockquote>{item.quote}</blockquote></div>;
    })}</details> : <p className="analysis-caption">本次扫描未查到原句证据；不能据此认定人物未出场或伏笔未推进。</p>;
  }
  return <>
    <div className="analysis-section-heading"><div><h2>{view === "toolkit" ? "本作品的用词与检查档案" : "跨章线索看板"}</h2><p>{view === "toolkit" ? "配置统一用词、人物检索名、伏笔提醒和作者文风锚段；每份作品分别保存。" : "从正式章节提取姓名和关键词提及，供作者回看原文；提及不等于出场、知情或伏笔回收。"}</p></div><button type="button" className="button secondary small" disabled={busy || dirty} onClick={() => void load()}><RefreshCw size={14} className={busy ? "spin" : ""} />{view === "toolkit" ? "重新读取" : "重新检查"}</button></div>
    {error && <div className="analysis-error" role="alert"><CircleAlert size={16} />{error}</div>}
    {notice && <p className="analysis-success" role="status">{notice}</p>}
    {busy && !toolkit && <div className="analysis-loading"><LoaderCircle className="spin" size={18} />正在读取本作品的检查资料</div>}
    {view === "toolkit" && toolkit && <>
      <details className="analysis-limits" open><summary>配置字段与示例</summary><p><code>lexicon</code>：统一用词。每项为 <code>{'{"id":"club","term":"标准名称","aliases":["登记别名"],"forbidden":["不推荐写法"],"required":false,"note":"使用说明"}'}</code>。别名用于提示是否统一用词，不推荐写法命中时保留原句。required 为 true 时，每次检查会提醒该文稿未出现的必用词；是否适用仍由作者判断。</p><p><code>characters</code>：<code>{'{"id":"hero","name":"人物姓名","aliases":[],"max_absence":5}'}</code>。按名称提及检查长期空档。</p><p><code>threads</code>：<code>{'{"id":"clue","label":"伏笔名称","terms":["线索关键词"],"start_chapter":1,"remind_after":5,"end_chapter":null}'}</code>。章号和间隔由作者明确填写，end_chapter 只是规划范围。</p><p><code>style.instructions</code> 为作者风格要求；<code>avoid</code> 为需核对的表达。<code>anchors</code> 显式填写已保存草稿或正式章节的 path、完整 hash、start_line、end_line，后端提取对应原句；原文变化后失效，不会自动换样。</p></details>
      {anchorStale && <div className="analysis-warning"><CircleAlert size={16} /><span>至少一处文风锚段已变化或缺失。请核对来源，显式更新 hash 与行号后再保存；旧锚段不会自动替换。</span></div>}
      <label className="analysis-field analysis-json-label">作者检查配置 · JSON<textarea className="analysis-config-editor" aria-label="作者检查配置 JSON" value={text} spellCheck={false} disabled={busy} onChange={(event) => setText(event.target.value)} /></label>
      {toolkit.style.anchors.length > 0 && <details className="analysis-limits"><summary>当前固定的文风锚段（{toolkit.style.anchors.length}）</summary>{evidence(toolkit.style.anchors)}</details>}
      <details className="analysis-limits"><summary>查找锚段所需的文件版本</summary><p>先在正文编辑器中确认原句行号，再复制下列对应文件的完整版本。</p>{documents.filter((item) => /^(chapters|drafts)\/.+\.md$/.test(item.path)).map((item) => <p className="analysis-file-version" key={item.path}><span>{item.path}</span><code>{item.hash}</code></p>)}</details>
      <div className="analysis-save-row"><span>{dirty ? "有尚未保存的检查配置" : toolkit.configured ? `检查档案已保存 · 版本 ${toolkit.hash?.slice(0, 12)}` : "尚未保存自定义检查档案"}</span><button type="button" className="button primary small" disabled={busy || !dirty} onClick={() => void save()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}保存检查配置</button></div>
    </>}
    {view === "structure" && structure && <>
      <div className="analysis-result-heading"><span>扫描 {structure.scanned} 份正式章节 · 正式版本 {structure.canonical_revision}</span><span className="tag">{stale ? "看板已过期" : "本次检查快照"}</span></div>
      {stale && <div className="analysis-warning"><CircleAlert size={16} /><span>正式材料已变化。下方保留本次检查的引文，旧位置跳转已停用，请重新检查。</span></div>}
      {!structure.characters.length && !structure.threads.length && <Empty compact title="先配置要跟踪的人物与伏笔" description="在“用词与档案”中明确填写姓名、别名与线索词；系统不会把规划自动当作已发生事实。" />}
      <div className="analysis-structure-grid"><section><h3>人物提及与空档</h3>{structure.characters.map((item) => <article className="analysis-structure-card" key={item.id}><div><strong>{item.name}</strong><span className="tag">{item.status === "attention" ? "建议回看" : item.status === "not_found" ? "未查到提及" : "本次无空档提醒"}</span></div><p>最近提及：{item.last_mentioned === null ? "未查到" : `第 ${item.last_mentioned} 章`} · 间隔 {item.absence ?? "—"} 章</p><p>提及章节：{item.mentioned_chapters.join("、") || "本次未查到"}</p>{evidence(item.evidence)}</article>)}</section><section><h3>伏笔关键词跨度</h3>{structure.threads.map((item) => <article className="analysis-structure-card" key={item.id}><div><strong>{item.label}</strong><span className="tag">{item.status === "attention" ? "建议回看" : item.status === "planned" ? "尚未进入计划起始章" : item.status === "not_found" ? "未查到提及" : "关键词检查记录"}</span></div><p>作者跟踪范围：第 {item.start_chapter} 章起{item.end_chapter === null ? "" : `，至第 ${item.end_chapter} 章`}</p><p>最近关键词提及：{item.last_mentioned === null ? "未查到" : `第 ${item.last_mentioned} 章`} · 跨度 {item.span ?? "—"} 章</p>{evidence(item.evidence)}</article>)}</section></div>
      {structure.outline.length > 0 && <details className="analysis-limits"><summary>已批准章计划与正式正文覆盖（{structure.outline.length}）</summary><ul>{structure.outline.map((item) => <li key={item.path}><button type="button" className="analysis-source" disabled={stale || documents.find((document) => document.path === item.path)?.hash !== item.hash} onClick={() => onOpen(item.path, undefined, item.hash)}>{item.path}</button> · {item.has_chapter === null ? "章号待确认，未判断正文覆盖" : item.has_chapter ? "已存在对应正文（不代表已符合计划）" : "尚未查到对应正式正文"}</li>)}</ul></details>}
      {structure.keywords.length > 0 && <details className="analysis-limits"><summary>高频字词（双字组、三字组与英文词）</summary><p>{structure.keywords.map((item) => `${item.term}：${item.count}`).join(" · ")}</p></details>}
      {!!structure.omitted.length && <details className="analysis-limits"><summary>未纳入本次检查（{structure.omitted.length}）</summary><ul>{structure.omitted.map((item, index) => <li key={index}>{item.path}：{item.reason}</li>)}</ul></details>}
      <div className="analysis-section-heading"><div><h3>章节与运行诊断</h3><p>生成只含计数、版本和状态的诊断报告，包含逐章字数与行数、任务失败汇总和检查覆盖；不包含正文、提示词、密钥或错误原文。</p></div><button className="button secondary small" type="button" disabled={busy} onClick={() => void diagnose()}>生成诊断报告</button></div>
      {diagnostics && <details className="analysis-limits" open><summary>元数据诊断 JSON · 可复制反馈</summary><pre className="analysis-raw">{JSON.stringify(diagnostics, null, 2)}</pre></details>}
      <details className="analysis-limits" open><summary>检查范围与限制</summary><ul>{structure.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>
    </>}
  </>;
}
