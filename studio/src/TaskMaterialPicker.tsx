import { requestConfirmation } from "./confirm";
import { useEffect, useRef, useState } from "react";
import { BookOpen, Database, LoaderCircle, RefreshCw, Search } from "lucide-react";
import { api, libraryUrl } from "./api";
import type { LibraryOverview } from "./api";
import { Modal } from "./ui";
import ReferenceRetrievalWorkspace from "./ReferenceRetrievalWorkspace";
import { materialIds, mergeTaskMaterials, retrievalMaterialCount, taskMaterialOptions } from "./taskMaterialHelpers";
import type { MaterialPurpose } from "./taskMaterialHelpers";
import "./taskMaterialPicker.css";

type Props = { novelId: string; value: Record<string, unknown> | null; onChange: (selection: Record<string, unknown> | null) => void; purpose?: MaterialPurpose; disabled?: boolean; onOpenLibrary?: () => void };
const typeLabels: Record<string, string> = { literature: "文献", material: "素材", technique: "技法", style: "文风", term: "术语", method: "方法" };

export default function TaskMaterialPicker({ novelId, value, onChange, purpose = "planning", disabled = false, onOpenLibrary }: Props) {
  const [overview, setOverview] = useState<LibraryOverview | null>(null), [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [filter, setFilter] = useState(""), [retrievalOpen, setRetrievalOpen] = useState(false), [retrievalDirty, setRetrievalDirty] = useState(false);
  const epoch = useRef(0), alive = useRef(true);
  async function reload() {
    const generation = ++epoch.current;
    setLoading(true); setError("");
    try { const next = await api<LibraryOverview>(libraryUrl("book", novelId)); if (alive.current && generation === epoch.current) setOverview(next); }
    catch (failure) { if (alive.current && generation === epoch.current) setError(failure instanceof Error ? failure.message : "本书资料读取失败，请重试。"); }
    finally { if (alive.current && generation === epoch.current) setLoading(false); }
  }
  useEffect(() => { alive.current = true; setOverview(null); setFilter(""); setRetrievalOpen(false); void reload(); return () => { alive.current = false; epoch.current++; }; }, [novelId]);
  const entryIds = materialIds(value, "entry_ids"), bindingIds = materialIds(value, "binding_ids"), retrievalCount = retrievalMaterialCount(value);
  const options = overview ? taskMaterialOptions(overview, purpose) : [];
  const missing = [...entryIds.map((id) => ({ id, kind: "entry" as const })), ...bindingIds.map((id) => ({ id, kind: "binding" as const }))].filter((item) => overview && !options.some((option) => option.id === item.id && option.kind === item.kind));
  const displayed = options.filter((item) => !filter.trim() || `${item.title} ${item.summary} ${typeLabels[item.type] || ""}`.toLocaleLowerCase().includes(filter.trim().toLocaleLowerCase()));
  function patch(selection: Record<string, unknown>) { try { onChange(mergeTaskMaterials(value, selection)); setError(""); return true; } catch (failure) { setError(failure instanceof Error ? failure.message : "资料选择未更新。"); return false; } }
  function toggle(kind: "entry" | "binding", id: string, checked: boolean) {
    const key = kind === "entry" ? "entry_ids" : "binding_ids", ids = materialIds(value, key);
    patch({ [key]: checked ? [...new Set([...ids, id])] : ids.filter((current) => current !== id) });
  }
  async function closeRetrieval() { if (!retrievalDirty || (await requestConfirmation("检索片段尚未加入任务，确认关闭并放弃本次勾选？"))) { setRetrievalOpen(false); setRetrievalDirty(false); } }
  return <section className="task-material-picker" aria-label="为本次任务选择资料与方法">
    <div className="tm-heading"><div><h3><BookOpen size={16} />为本次任务选择资料与方法</h3><p>可选本书资料卡、已固定用于本书的个人资料，以及参考书的精确检索片段。改选后请重新预览任务。</p></div><button type="button" className="button secondary small" disabled={disabled || loading} onClick={() => void reload()}><RefreshCw size={13} className={loading ? "spin" : ""} />刷新可选资料</button></div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="tm-actions"><button type="button" className="button secondary small" disabled={disabled} onClick={() => setRetrievalOpen(true)}><Database size={14} />从参考书检索选段</button>{onOpenLibrary && <button type="button" className="button secondary small" disabled={disabled} onClick={onOpenLibrary}>资料中心：整理与授权</button>}<span>已选 {entryIds.length + bindingIds.length} 份卡片 / 固定资料，{retrievalCount} 个检索片段</span></div>
    {retrievalCount > 0 && <p className="tm-selection-note">已有检索片段会和本次勾选的卡片一并预览。<button type="button" className="text-button" disabled={disabled} onClick={() => patch({retrieval_selection: null})}>移除检索选择</button></p>}
    {loading && <p className="tm-muted" role="status"><LoaderCircle size={14} className="spin" />正在读取本书资料…</p>}
    {overview && !options.length && <p className="tm-muted">本书还没有资料卡或固定个人资料。可先在资料中心创建卡片、采用方法并“用于本书”；参考小说原文请用上方“从参考书检索选段”，不会按书名发送整本书。</p>}
    {options.length > 0 && <><label className="tm-filter"><Search size={13} /><input aria-label="筛选本书资料卡" value={filter} disabled={disabled} maxLength={200} placeholder="按卡片标题、摘要或类型筛选" onChange={(event) => setFilter(event.target.value)} /></label><div className="tm-options">{displayed.map((option) => { const selected = (option.kind === "entry" ? entryIds : bindingIds).includes(option.id), full = !selected && (option.kind === "entry" ? entryIds : bindingIds).length >= 30; return <label className={`tm-option ${selected ? "selected" : ""}`} key={option.key}><input type="checkbox" checked={selected} disabled={disabled || full || !!option.disabledReason && !selected} onChange={(event) => toggle(option.kind, option.id, event.target.checked)} /><span><strong>{option.title}</strong><small>{option.kind === "binding" ? "个人资料的本书固定版本" : "本书资料卡"} · {typeLabels[option.type] || option.type} · {option.outbound === "summary" ? "仅发送作者摘要" : option.outbound === "local" ? "仅本地" : "允许所选内容"} · {option.version.slice(0, 8)}</small>{option.summary && <p>{option.summary}</p>}{option.disabledReason && <em>{option.disabledReason}；请先到资料中心处理{selected ? "，或取消本次选择" : ""}。</em>}{full && <em>本组最多30项，请先减少选择。</em>}</span></label>; })}</div>{!displayed.length && <p className="tm-muted">没有匹配的本书资料卡。这里筛选卡片，不检索参考书全文。</p>}</>}
    {missing.map((item) => <p className="tm-selection-note" key={`${item.kind}:${item.id}`}>已选资料 {item.id.slice(0, 8)} 当前不可读取，请核对后移除。<button type="button" className="text-button" disabled={disabled} onClick={() => toggle(item.kind, item.id, false)}>移除此项</button></p>)}
    <p className="tm-muted">勾选仅用于当前任务，不采用方法、不修改外发政策。最终任务预览会再校验来源、用途、版本和24,000字符总上限；确认提交后才可能调用模型。</p>
    {retrievalOpen && <Modal title="从参考书选择本次任务资料" subtitle="仅本地检索。选中精确片段并预览后，再加入当前任务。" wide onClose={closeRetrieval}><div className="tm-retrieval"><ReferenceRetrievalWorkspace novelId={novelId} pickerMode onDirtyChange={setRetrievalDirty} onUseSelection={(selected) => { if (patch(selected)) { setRetrievalOpen(false); setRetrievalDirty(false); } }} onOpenBooks={onOpenLibrary} /></div></Modal>}
  </section>;
}
