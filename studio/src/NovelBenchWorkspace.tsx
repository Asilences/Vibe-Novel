import { useEffect, useRef, useState } from "react";
import { Download, Eye, GitCompareArrows, RefreshCw } from "lucide-react";
import { api, ApiError, download, formatDate, projectUrl } from "./api";
import type { Benchmark, DocumentItem, Project } from "./api";

const decisions = [{ id: "prefer_a", label: "更偏好 A" }, { id: "prefer_b", label: "更偏好 B" }, { id: "tie", label: "相近／各有优点" }] as const;
export default function NovelBenchWorkspace({ project, documents }: { project: Project; documents: DocumentItem[] }) {
  const base = `${projectUrl(project.novel_id)}/benchmarks`;
  const [records, setRecords] = useState<Benchmark[]>([]);
  const [record, setRecord] = useState<Benchmark | null>(null);
  const [pathOne, setPathOne] = useState("");
  const [pathTwo, setPathTwo] = useState("");
  const [note, setNote] = useState("");
  const [revealed, setRevealed] = useState<{ label: string; path: string; hash: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  const generation = useRef(0);
  const candidates = documents.filter((item) => item.path.startsWith("drafts/") && item.path.endsWith(".md"));
  const one = candidates.find((item) => item.path === pathOne), two = candidates.find((item) => item.path === pathTwo);
  useEffect(() => {
    alive.current = true;
    void api<{ benchmarks: Benchmark[] }>(base).then((value) => { if (alive.current) setRecords(value.benchmarks); }).catch(() => { if (alive.current) setError("无法读取盲评记录，请稍后重试。"); });
    return () => { alive.current = false; generation.current += 1; };
  }, [base]);
  function adopt(value: Benchmark) { setRecord(value); setRecords((items) => [value, ...items.filter((item) => item.id !== value.id)]); setRevealed(null); setNote(value.vote?.note || ""); }
  async function act(operation: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError("");
    const request = ++generation.current;
    try { await operation(); } catch (failure) { if (alive.current && request === generation.current) setError(failure instanceof ApiError && failure.status === 409 ? "候选或比较记录已经变化，不能使用旧版本投票。请重新读取；需要时创建新的比较。" : "盲评操作未完成，请检查两份草稿是否有效并重试。"); } finally { if (alive.current && request === generation.current) setBusy(false); }
  }
  async function read(id: string) { const value = await api<Benchmark>(`${base}/${encodeURIComponent(id)}`); if (alive.current) adopt(value); }
  return <section className="model-panel model-bench"><div className="section-heading"><h2><GitCompareArrows size={18} />作者盲评 · NovelBench</h2><span className="tag">不调用模型</span></div><p className="model-caption">比较你明确选择的两份草稿。A/B 顺序随机后固定，先隐藏来源与模型标签；个人偏好不等于质量认证，不自动训练路由或批准定稿。</p>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <details className="model-bench-create" open={!record}><summary>创建新的匿名对照</summary><div className="model-form-grid"><label>候选来源一<select value={pathOne} disabled={busy} onChange={(event) => setPathOne(event.target.value)}><option value="">选择草稿</option>{candidates.map((item) => <option key={item.path} value={item.path}>{item.path}</option>)}</select></label><label>候选来源二<select value={pathTwo} disabled={busy} onChange={(event) => setPathTwo(event.target.value)}><option value="">选择另一份草稿</option>{candidates.filter((item) => item.path !== pathOne).map((item) => <option key={item.path} value={item.path}>{item.path}</option>)}</select></label></div><div className="model-actions"><button type="button" className="button secondary small" disabled={busy || !one || !two || one.path === two.path} onClick={() => void act(async () => { const value = await api<Benchmark>(base, "POST", { candidate_a_path: one!.path, candidate_a_hash: one!.hash, candidate_b_path: two!.path, candidate_b_hash: two!.hash }); if (alive.current) { adopt(value); setPathOne(""); setPathTwo(""); } })}><GitCompareArrows size={14} />锁定版本并随机对照</button></div></details>
    <div className="model-form-grid"><label className="model-wide-field">历史比较<select disabled={busy} value={record?.id || ""} onChange={(event) => { const id = event.target.value; if (!id) { setRecord(null); setRevealed(null); return; } void act(() => read(id)); }}><option value="">选择匿名比较</option>{records.map((item) => <option key={item.id} value={item.id}>{formatDate(item.created_at)} · {item.stale ? "已过期" : item.status === "voted" ? "已投票" : "待投票"} · {item.id.slice(0, 8)}</option>)}</select></label></div>
    {record && <><div className="model-workflow-status"><strong>{record.stale ? "来源已变化 · 仅供回顾" : record.status === "voted" ? "作者偏好已记录" : "请先阅读两份匿名候选"}</strong><button className="button secondary small" disabled={busy} type="button" onClick={() => void act(() => read(record.id))}><RefreshCw size={13} />核对当前版本</button></div><div className="model-bench-columns">{record.candidates?.map((candidate) => <article key={candidate.label}><h3>候选 {candidate.label}</h3><pre>{candidate.content}</pre></article>)}</div>
      {record.status === "awaiting_vote" ? <><label className="model-bench-note">偏好依据（可选）<textarea rows={3} maxLength={4000} disabled={busy || record.stale} value={note} onChange={(event) => setNote(event.target.value)} placeholder="例如：A 的对白更贴合人物，B 的场景节奏更清楚。" /></label><div className="model-actions">{decisions.map((item) => <button key={item.id} type="button" className="button secondary small" disabled={busy || record.stale} onClick={() => void act(async () => { const value = await api<Benchmark>(`${base}/${record.id}/vote`, "POST", { decision: item.id, note, expected_hash: record.hash }); if (alive.current) adopt(value); })}>{item.label}</button>)}</div></> : <p className="analysis-success">{decisions.find((item) => item.id === record.vote?.decision)?.label || "已投票"}{record.vote?.note ? ` · ${record.vote.note}` : ""}</p>}
      <div className="model-actions"><button type="button" className="button secondary small" disabled={busy} onClick={() => void act(async () => { const value = await api<{ filename: string; content: string }>(`${base}/${record.id}/export`); download(value.filename, value.content); })}><Download size={14} />导出匿名对照</button>{record.status === "voted" && <button type="button" className="button secondary small" disabled={busy} onClick={() => void act(async () => { const value = await api<{ sources: { label: string; path: string; hash: string }[] }>(`${base}/${record.id}/reveal`, "POST", {}); if (alive.current) setRevealed(value.sources); })}><Eye size={14} />显式揭示来源</button>}</div>
      {revealed && <div className="model-revealed">{revealed.map((item) => <p key={item.label}>候选 {item.label}：{item.path} · 版本 {item.hash.slice(0, 12)}</p>)}</div>}
    </>}
  </section>;
}
