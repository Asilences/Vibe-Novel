import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { researchModelDraft } from "./researchAccountHelpers";
import { requestConfirmation } from "./confirm";

type Config = { model_id: string; max_cost_usd: number; max_calls: number; price_input_usd: number; price_output_usd: number; price_source: string };
const editable = (config: Config): Config => ({model_id: config.model_id, max_cost_usd: config.max_cost_usd, max_calls: config.max_calls, price_input_usd: config.price_input_usd, price_output_usd: config.price_output_usd, price_source: config.price_source});
type PendingAttempt = {id: string; state: "reserved" | "unknown"; model: string; created_at: string; reserved_cost_usd: number; attempt_hash: string; cached_response_hash: string | null};
type Status = { config: Config | null; config_hash: string; calls: number; reserved_cost_usd: number; reported_cost_usd: number; uncertain_calls: number; pending_reconciliations?: PendingAttempt[] };
export default function ResearchAccountPanel({onDirtyChange, onSettings}: {onDirtyChange?: (dirty: boolean) => void; onSettings?: () => void}) {
  const [status, setStatus] = useState<Status | null>(null), [models, setModels] = useState<{id: string; label: string; model: string; available: boolean}[]>([]);
  const [draft, setDraft] = useState<Config>({model_id: "default_api", max_cost_usd: 5, max_calls: 256, price_input_usd: 0, price_output_usd: 0, price_source: ""});
  const [changed, setChanged] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({}), [notice, setNotice] = useState("");
  async function refresh() { const [account, registry] = await Promise.all([api<Status>("/research-account"), api<{profiles: typeof models}>("/model-registry")]); setStatus(account); setModels(registry.profiles); if (!changed && account.config) setDraft(editable(account.config)); }
  useEffect(() => { void refresh().catch(() => setError("研究账户读取失败，请刷新核对。")); }, []);
  useEffect(() => { onDirtyChange?.(changed || Object.values(reasons).some(reason => reason.trim().length > 0)); }, [changed, reasons, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  const edit = (patch: Partial<Config>) => { setDraft({...draft, ...patch}); setChanged(true); };
  async function reconcile(attempt: PendingAttempt) {
    const reason = (reasons[attempt.id] || "").trim();
    if (busy || !reason) return;
    const restoring = !!attempt.cached_response_hash;
    setBusy(true); setError(""); setNotice("");
    try {
      if (!await requestConfirmation(restoring
        ? `核对请求 ${attempt.id} 的原响应缓存，并按当时冻结价格恢复到账？不会重发 API，保守占用不会减少。`
        : `核对请求 ${attempt.id} 的原执行器是否已经停止，并将结果标为费用未知？仍保留 $${attempt.reserved_cost_usd.toFixed(6)} 占用，不重发 API。`,
        {title: "核对历史研究请求", confirmLabel: restoring ? "核验缓存并恢复到账" : "核对停止并保留未知费用", cancelLabel: "保留原状态"})) return;
      const value = await api<Status>(`/research-account/attempts/${attempt.id}/reconcile`, "POST", {
        action: restoring ? "restore_response" : "mark_unknown", expected_attempt_hash: attempt.attempt_hash,
        expected_response_hash: attempt.cached_response_hash, reason,
      });
      setStatus(value); setReasons(current => {const next = {...current}; delete next[attempt.id]; return next;});
      setNotice(restoring ? "已核验原响应并恢复到账，没有新 API 请求；原保守占用保留。" : "已核对原执行器无活动，标为费用未知；原费用仍占用额度，不会自动重发。");
    } catch (failure) { setError(failure instanceof ApiError && failure.status < 500 ? failure.message : "核对未完成，原记录与费用保留，请刷新后检查。"); }
    finally { setBusy(false); }
  }
  const pendingSection = !!status?.pending_reconciliations?.length && <section aria-label="历史研究请求核对">
    <h3>历史研究请求核对</h3>
    <p>核对会检查原研究执行器是否停止。有缓存时核验原请求与用量后恢复到账；没有缓存时保留未知费用。核对不会调用模型，也不会清除已占用额度。</p>
    {status.pending_reconciliations.map(attempt => <article className="library-binding" key={attempt.id}>
      <strong>请求 {attempt.id}</strong>
      <p>{attempt.model} · {attempt.created_at} · {attempt.state === "reserved" ? "原执行状态待核对" : "费用未知"} · 保留 ${attempt.reserved_cost_usd.toFixed(6)}</p>
      <p>{attempt.cached_response_hash ? "发现原响应缓存，可核验并恢复到账。" : "未发现原响应缓存；保留原费用，不重新发送。"}</p>
      {(attempt.state === "reserved" || attempt.cached_response_hash) && <>
        <label>核对说明（{attempt.id}）<textarea disabled={busy} maxLength={2000} value={reasons[attempt.id] || ""} onChange={event => setReasons({...reasons, [attempt.id]: event.target.value})}/></label>
        <button type="button" className="button secondary" disabled={busy || !(reasons[attempt.id] || "").trim()} onClick={() => void reconcile(attempt)}>
          {attempt.cached_response_hash ? "核验缓存并恢复到账" : "核对执行器并保留未知费用"}
        </button>
      </>}
    </article>)}
  </section>;
  return <section className="library-policy"><h2>独立研究预算</h2><p>精读、跨书比较与方法效果试验共享此账户。不会计入创作作品的任务预算。预览和本地语义检索不收费。</p>{error && <p role="alert" className="inline-error">{error}</p>}{notice && <p role="status">{notice}</p>}{status && <dl><dt>累计请求</dt><dd>{status.calls}</dd><dt>保守已占用</dt><dd>${status.reserved_cost_usd.toFixed(6)} / ${status.config?.max_cost_usd ?? "未设置"}</dd><dt>按已报告 Token 与所填单价估算</dt><dd>${status.reported_cost_usd.toFixed(6)}（不是服务商账单）</dd><dt>结果或费用待核对</dt><dd>{status.uncertain_calls} 次；不自动重放</dd></dl>}{pendingSection}<form onSubmit={(event) => {event.preventDefault(); if (!status) return; setBusy(true); setError(""); void api<Status>("/research-account", "PUT", {...draft, expected_hash: status.config_hash}).then(value => {setStatus(value); setChanged(false); if (value.config) setDraft(editable(value.config));}).catch(() => setError("保存未完成；请核对模型、正数价格、进行中的研究或配置版本。输入仍保留。")).finally(() => setBusy(false));}}><div className="library-grid"><label>研究模型<select value={draft.model_id} onChange={e => {setDraft(researchModelDraft(draft, e.target.value)); setChanged(true);}} disabled={busy}>{!models.some(model => model.id === draft.model_id) && <option value={draft.model_id}>{draft.model_id} · 当前模型未在注册表中</option>}{models.map(model => <option value={model.id} key={model.id}>{model.label} · {model.model}{!model.available ? " · 未配置完整" : ""}</option>)}</select></label><label>共享总上限（美元）<input disabled={busy} required type="number" min="0.001" max="10000" step="any" value={draft.max_cost_usd} onChange={e=>edit({max_cost_usd:Number(e.target.value)})}/></label><label>累计最多请求<input disabled={busy} required type="number" min="1" max="4096" value={draft.max_calls} onChange={e=>edit({max_calls:Number(e.target.value)})}/></label><label>每百万输入 Token 美元价<input disabled={busy} required type="number" min="0.000001" step="any" value={draft.price_input_usd} onChange={e=>edit({price_input_usd:Number(e.target.value)})}/></label><label>每百万输出 Token 美元价<input disabled={busy} required type="number" min="0.000001" step="any" value={draft.price_output_usd} onChange={e=>edit({price_output_usd:Number(e.target.value)})}/></label><label>报价来源与核对日期<input disabled={busy} maxLength={2000} value={draft.price_source} onChange={e=>edit({price_source:e.target.value})}/></label></div><p className="library-muted">切换研究模型后须重新填写该模型的报价。使用峰时未命中缓存的价格可保守预留；费用未知仍占用额度。修改预算不会清除此前费用。API 密钥仍在模型设置页本地保存。</p><button className="button primary" disabled={busy || !changed || !status}>保存研究预算</button>{onSettings && <button type="button" className="button secondary" disabled={busy} onClick={onSettings}>配置模型与 API 密钥</button>}<button type="button" className="button secondary" disabled={busy} onClick={() => void refresh().catch(()=>setError("刷新失败"))}>刷新用量</button></form></section>;
}
