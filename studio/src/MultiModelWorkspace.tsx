import { useEffect, useRef, useState } from "react";
import { ArrowRight, Eye, GitBranch, KeyRound, LoaderCircle, Pause, Play, Plus, RefreshCw, Save, ShieldCheck, Square, Trash2 } from "lucide-react";
import { api, ApiError, formatDate, kindLabels, leaf, projectUrl, stateLabels } from "./api";
import type { Document as NovelDocument, DocumentItem, EvidenceReview, ModelProfile, ModelRegistry, ModelRole, ModelWorkflow, Project, RoutingPolicy, RoutingSettings } from "./api";
import NovelBenchWorkspace from "./NovelBenchWorkspace";
import { reviewMatchesCandidate } from "./reviewIdentity";

const roles: { id: ModelRole; label: string }[] = [{ id: "planner", label: "规划" }, { id: "writer", label: "正文与编辑" }, { id: "reviewer", label: "证据审稿" }, { id: "memory", label: "候选记忆抽取" }];
const emptyProfile = (): ModelProfile => ({ id: "", label: "", base_url: "", model: "", allow_loopback: false, roles: roles.map((item) => item.id), context_tokens: 65536, max_output_tokens: 4096, price_input_usd: null, price_output_usd: null, has_api_key: false });
const emptyPolicy = (): RoutingPolicy => ({ bindings: { planner: { primary: "", fallbacks: [] }, writer: { primary: "", fallbacks: [] }, reviewer: { primary: "", fallbacks: [] }, memory: { primary: "", fallbacks: [] } }, max_calls: 3, max_tokens: 120000, max_cost_usd: null, max_attempts: 1 });
const safeFailure = (error: unknown) => error instanceof ApiError && error.status === 409 ? "配置或来源版本已经变化，本次操作未完成。请重新读取并核对后再试。" : error instanceof ApiError && error.status === 403 ? "本机会话校验失败，请刷新页面后重试。" : "操作未完成。请检查模型配置、角色绑定、文稿版本和预算；详细原因可在本机任务记录中核对。";
const workflowDriftReason = (reason?: string | null) => {
  if (/模型|预算策略|注册表|接口/.test(reason || "")) return "模型配置或作品预算策略已经变化。";
  if (/章计划|批准/.test(reason || "")) return "本流程依赖的章节计划或批准已经失效。";
  if (/固定样章/.test(reason || "")) return "固定样章或其配置已经变化。";
  if (/版本|文档|输入|候选|来源|正式内容|运行依赖/.test(reason || "")) return "来源、候选或关联材料的版本已经变化。";
  return "当前流程的来源或配置无法继续通过版本核对。";
};

function RecordSummary({ value }: { value: Record<string, unknown> }) {
  const candidates = Array.isArray(value.candidates) ? value.candidates.filter((item): item is Record<string, unknown> => !!item && typeof item === "object") : [];
  const budget = value.budget && typeof value.budget === "object" ? value.budget as Record<string, unknown> : null;
  return <><dl className="model-route-summary">{typeof value.role === "string" && <><dt>执行职责</dt><dd>{roles.find((item) => item.id === value.role)?.label || value.role}</dd></>}{candidates.map((item, index) => <div key={index}><dt>{index === 0 ? "主模型" : `允许备用 ${index}`}</dt><dd><strong>{String(item.label || item.id || "未命名")}</strong> · {String(item.model || "未填写")}<small>{String(item.base_url || "")} · 配置版本 {String(item.registry_revision ?? "未知")}</small></dd></div>)}{typeof value.estimated_input_tokens === "number" && <><dt>输入保守估算</dt><dd>{value.estimated_input_tokens.toLocaleString()} Token（含估算余量）</dd></>}{typeof value.max_output_tokens === "number" && <><dt>最大输出</dt><dd>{value.max_output_tokens.toLocaleString()} Token</dd></>}{typeof value.max_attempts === "number" && <><dt>最多尝试</dt><dd>{value.max_attempts} 次；仅限允许的技术故障</dd></>}{typeof value.calls === "number" && <><dt>已占用调用</dt><dd>{value.calls} 次</dd></>}{typeof value.tokens === "number" && <><dt>已占用 Token</dt><dd>{value.tokens.toLocaleString()}</dd></>}{"cost_usd" in value && <><dt>按所填报价估算</dt><dd>{typeof value.cost_usd === "number" ? `$${value.cost_usd.toFixed(6)}` : "未知"}{value.uncertain === true ? " · 存在未结算或计费不确定调用" : ""}</dd></>}{budget && <><dt>流程上限</dt><dd>{String(budget.max_calls)} 次 · {Number(budget.max_tokens).toLocaleString()} Token · {typeof budget.max_cost_usd === "number" ? `$${budget.max_cost_usd}` : "未设金额上限"}</dd></>}</dl>{typeof value.reason === "string" && <p>{value.reason}</p>}<details><summary>查看完整记录</summary><pre>{JSON.stringify(value, null, 2)}</pre></details></>;
}

export function JobRouteDetails({ route, attempts, budget }: { route?: Record<string, unknown> | null; attempts?: Record<string, unknown>[]; budget?: Record<string, unknown> | null }) {
  return <>{route && <details className="analysis-limits model-snapshot" open><summary>本次模型与路由快照</summary><RecordSummary value={route} /><p>这里显示任务创建时保存的配置；后续设置修改不会改写本次快照。</p></details>}{attempts && attempts.length > 0 && <details className="analysis-limits model-snapshot"><summary>调用尝试（{attempts.length}）</summary><pre>{JSON.stringify(attempts, null, 2)}</pre></details>}{budget && <details className="analysis-limits model-snapshot" open><summary>预算与费用记录</summary><RecordSummary value={budget} /><p>以上费用按你填写的报价及服务端用量估算，并非服务商结算账单。缺少报价或用量时保留未知；本机预算不能保证最终账单上限。</p></details>}</>;
}

export default function MultiModelWorkspace({ project, documents, onSaved, onOpen, onTask }: { project: Project; documents: DocumentItem[]; onSaved: () => Promise<void>; onOpen: (path: string) => void; onTask: (id: string) => void }) {
  const base = projectUrl(project.novel_id);
  const [registry, setRegistry] = useState<ModelRegistry | null>(null);
  const [routing, setRouting] = useState<RoutingSettings | null>(null);
  const [policy, setPolicy] = useState<RoutingPolicy>(emptyPolicy);
  const [profile, setProfile] = useState<ModelProfile>(emptyProfile);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [secret, setSecret] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [workflows, setWorkflows] = useState<ModelWorkflow[]>([]);
  const [workflow, setWorkflow] = useState<ModelWorkflow | null>(null);
  const [sourcePath, setSourcePath] = useState("");
  const [instruction, setInstruction] = useState("");
  const [withReview, setWithReview] = useState(true);
  const [withMemory, setWithMemory] = useState(true);
  const [independentReview, setIndependentReview] = useState(false);
  const [evidence, setEvidence] = useState<EvidenceReview | null>(null);
  const [evidenceReadStale, setEvidenceStale] = useState(false);
  const [issueIds, setIssueIds] = useState<string[]>([]);
  const [revisionInstruction, setRevisionInstruction] = useState("");
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [previewIdentity, setPreviewIdentity] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  const loadGeneration = useRef(0);
  const evidenceGeneration = useRef(0);
  const actionGeneration = useRef(0);
  const actionBusy = useRef(false);
  const source = documents.find((item) => item.path === sourcePath);
  const sources = documents.filter((item) => item.path.endsWith(".md") && /^(drafts|chapters|outlines\/source)\//.test(item.path));
  const currentIdentity = JSON.stringify([source?.path, source?.hash, instruction, withReview, withMemory, independentReview, registry?.hash, routing?.hash]);
  const policyDirty = !!routing && JSON.stringify(policy) !== JSON.stringify(routing.policy);
  const identityRef = useRef(currentIdentity); identityRef.current = currentIdentity;
  const selectedSaved = registry?.profiles.find((item) => item.id === selectedProfile);
  const changedEndpoint = !!selectedSaved?.has_api_key && selectedSaved.base_url !== profile.base_url.trim() && !secret.trim() && !clearKey;
  const lastReview = workflow ? [...workflow.nodes].reverse().find((node) => node.kind === "review_evidence" && node.state === "succeeded") : undefined;
  const lastWriter = workflow ? [...workflow.nodes].reverse().find((node) => ["draft", "rewrite"].includes(node.kind)) : undefined;
  const evidenceStale = !!workflow?.stale || evidenceReadStale || !!evidence && (!reviewMatchesCandidate(evidence, lastWriter) || documents.find((item) => item.path === evidence.target_path)?.hash !== evidence.target_hash || documents.find((item) => item.path === lastReview?.result_path)?.hash !== lastReview?.result_hash);
  function chooseProfile(value?: ModelProfile) { setSelectedProfile(value?.id || ""); setProfile(value ? { ...value, roles: [...value.roles] } : emptyProfile()); setSecret(""); setClearKey(false); setNotice(""); setError(""); }
  async function reload() {
    const generation = ++loadGeneration.current;
    const values = await Promise.all([api<ModelRegistry>("/model-registry"), api<RoutingSettings>(`${base}/routing`), api<{ workflows: ModelWorkflow[] }>(`${base}/workflows`)]);
    if (!alive.current || generation !== loadGeneration.current) return;
    setRegistry(values[0]); setRouting(values[1]); setPolicy(values[1].policy); setWorkflows(values[2].workflows);
    setSelectedProfile(""); setProfile(emptyProfile()); setSecret(""); setClearKey(false); setPreview(null); setPreviewIdentity("");
  }
  useEffect(() => {
    alive.current = true;
    void reload().catch((failure) => { if (alive.current) setError(safeFailure(failure)); });
    return () => { alive.current = false; loadGeneration.current += 1; };
    // This component is remounted when the selected project changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base]);
  useEffect(() => {
    if (!workflow || !["queued", "running"].includes(workflow.state)) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (actionBusy.current) return;
      const generation = actionGeneration.current;
      void api<ModelWorkflow>(`${base}/workflows/${encodeURIComponent(workflow.id)}`).then((value) => { if (!cancelled && alive.current && !actionBusy.current && generation === actionGeneration.current) { setWorkflow(value); setWorkflows((items) => [value, ...items.filter((item) => item.id !== value.id)]); } }).catch(() => { if (!cancelled && alive.current && !actionBusy.current && generation === actionGeneration.current) setError("暂时无法刷新流程状态，请手动刷新；不会自动重发模型请求。"); });
    }, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [base, workflow?.id, workflow?.state]);
  useEffect(() => {
    const generation = ++evidenceGeneration.current;
    setEvidence(null); setEvidenceStale(false); setIssueIds([]); setRevisionInstruction("");
    if (!lastReview?.result_path || !lastReview.result_hash) return;
    void api<NovelDocument>(`${base}/document?path=${encodeURIComponent(lastReview.result_path)}`).then((value) => {
      if (!alive.current || generation !== evidenceGeneration.current) return;
      const parsed = JSON.parse(value.content) as EvidenceReview;
      if (!Array.isArray(parsed.issues) || !Array.isArray(parsed.unchecked)) throw new Error("invalid");
      setEvidence(parsed); setEvidenceStale(value.hash !== lastReview.result_hash || !reviewMatchesCandidate(parsed, lastWriter));
    }).catch(() => { if (alive.current && generation === evidenceGeneration.current) setError("证据审稿结果无法读取或格式无效，请查看对应任务记录。"); });
  }, [base, lastReview?.result_path, lastReview?.result_hash, lastReview?.result_version_id, lastWriter?.result_path, lastWriter?.result_hash, lastWriter?.result_version_id]);
  async function act(id: string, operation: () => Promise<void>) {
    if (actionBusy.current) return;
    actionBusy.current = true; actionGeneration.current += 1;
    setBusy(id); setError(""); setNotice("");
    try { await operation(); } catch (failure) { if (alive.current) setError(safeFailure(failure)); } finally { actionBusy.current = false; if (alive.current) setBusy(""); }
  }
  async function saveProfile(remove = false) {
    if (!registry || registry.migration_required || changedEndpoint) return;
    const { has_api_key: _hasKey, ...publicProfile } = profile;
    const entry = { ...publicProfile, id: profile.id.trim(), label: profile.label.trim(), base_url: profile.base_url.trim(), model: profile.model.trim(), ...(clearKey ? { clear_api_key: true } : secret ? { api_key: secret } : {}) };
    const profiles = registry.profiles.filter((item) => item.id !== selectedProfile).map(({ has_api_key: _hasKey, ...item }) => item);
    if (!remove) profiles.push(entry);
    const value = await api<ModelRegistry>("/model-registry", "PUT", { expected_hash: registry.hash, profiles });
    if (!alive.current) return;
    setRegistry(value); setSecret(""); setClearKey(false); setPreview(null);
    if (remove) chooseProfile(); else { const saved = value.profiles.find((item) => item.id === entry.id); if (saved) chooseProfile(saved); }
    setNotice(remove ? "模型配置已移除。若仍被角色引用，请重新绑定后再开始任务。" : "本机模型配置已保存；尚未调用服务商测试连接。");
    try { await onSaved(); } catch { if (alive.current) setNotice("模型配置已保存，工作台状态尚未刷新；无需再次提交密钥。"); }
  }
  const taskRequest = () => ({ kind: "rewrite", provider: "api", document_path: source?.path, document_hash: source?.hash, instruction: instruction.trim() });
  async function routePreview() {
    const identity = currentIdentity;
    const value = await api<Record<string, unknown>>(`${base}/routes/preview`, "POST", taskRequest());
    if (alive.current && identityRef.current === identity) { setPreview(value); setPreviewIdentity(identity); }
  }
  async function createWorkflow() {
    if (!preview || currentIdentity !== previewIdentity) return;
    const value = await api<ModelWorkflow>(`${base}/workflows`, "POST", { request: taskRequest(), review: withReview, memory: withMemory, independent_review: withReview && independentReview });
    if (!alive.current) return;
    setWorkflow(value); setWorkflows((items) => [value, ...items.filter((item) => item.id !== value.id)]); setNotice("首个编辑节点已启动。后续节点须查看结果后点击启动，不会自动连续调用。");
  }
  async function workflowAction(action: "advance" | "pause" | "cancel") {
    if (!workflow || action === "advance" && workflow.stale) return;
    const value = await api<ModelWorkflow>(`${base}/workflows/${encodeURIComponent(workflow.id)}/${action}`, "POST", {});
    if (alive.current) { setWorkflow(value); setWorkflows((items) => [value, ...items.filter((item) => item.id !== value.id)]); }
  }
  async function reviseWorkflow() {
    if (!workflow || workflow.stale || !evidence || evidenceStale || issueIds.length === 0) return;
    const value = await api<ModelWorkflow>(`${base}/workflows/${workflow.id}/revise`, "POST", { issue_ids: issueIds, instruction: revisionInstruction.trim() });
    if (alive.current) { setWorkflow(value); setWorkflows((items) => [value, ...items.filter((item) => item.id !== value.id)]); setNotice("已按所选证据启动局部修订。生成新的正文版本后，旧审稿将失效，须重新审稿。"); }
  }
  return <div className="multi-model-workspace">
    <header className="model-workspace-heading"><div><span className="eyebrow">由作者选择职责与模型</span><h1>多模型协作</h1><p>为规划、正文、审稿和候选记忆分别绑定 API；每个生成节点由一个模型返回候选。</p></div><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("reload", reload)}><RefreshCw size={14} />重新读取</button></header>
    {error && <p className="inline-error" role="alert">{error}</p>}{notice && <p className="analysis-success" role="status">{notice}</p>}
    {!registry || !routing ? <div className="analysis-loading"><LoaderCircle size={20} className="spin" />读取模型与作品策略…</div> : <>
      <section className="model-panel"><div className="section-heading"><h2><KeyRound size={18} />本机模型配置</h2><span className="tag">配置版本 {registry.revision}</span></div>
        {registry.migration_required && <div className="analysis-warning model-migration"><p>检测到原有单 API 配置。你可以显式复制为 <code>default_api</code>，之后为作品选择角色；原配置保留。</p><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("migrate", async () => { const value = await api<ModelRegistry>("/model-registry/migrate", "POST", { expected_hash: registry.hash }); if (alive.current) { setRegistry(value); chooseProfile(value.profiles.find((item) => item.id === "default_api")); setNotice("原 API 已复制为 default_api，请为作品保存角色绑定。"); } await onSaved(); })}>复制原 API 配置</button></div>}
        <div className="model-profiles"><div className="model-profile-list"><button className={`model-profile-item ${!selectedProfile ? "selected" : ""}`} disabled={!!busy} type="button" onClick={() => chooseProfile()}><Plus size={14} /><span>添加模型配置</span></button>{registry.profiles.map((item) => <button type="button" key={item.id} className={`model-profile-item ${selectedProfile === item.id ? "selected" : ""}`} disabled={!!busy} onClick={() => chooseProfile(item)}><span><strong>{item.label}</strong><small>{item.model}</small><small>{item.has_api_key ? "密钥已配置" : "未配置密钥"}</small></span></button>)}</div>
          <form className="model-profile-form" onSubmit={(event) => { event.preventDefault(); void act("profile", () => saveProfile()); }}>
            <div className="model-form-grid"><label>逻辑 ID<input required pattern="[a-z][a-z0-9_-]*" maxLength={64} disabled={!!busy || !!selectedProfile} value={profile.id} onChange={(event) => setProfile({ ...profile, id: event.target.value })} placeholder="例如 deepseek_writer" /></label><label>显示名称<input required maxLength={120} disabled={!!busy} value={profile.label} onChange={(event) => setProfile({ ...profile, label: event.target.value })} placeholder="例如 主写模型" /></label>
              <label>服务商预设<select disabled={!!busy} value="" onChange={(event) => { const preset = registry.presets.find((item) => item.id === event.target.value); if (preset) { setProfile({ ...profile, base_url: preset.base_url, model: preset.models[0]?.id || "" }); setSecret(""); setClearKey(false); } }}><option value="">填入常用接口（模型 ID 可编辑）</option>{registry.presets.filter((item) => item.base_url).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
              <label>模型 ID<input required maxLength={500} disabled={!!busy} value={profile.model} onChange={(event) => setProfile({ ...profile, model: event.target.value })} placeholder="模型名称或豆包 ep-…" /></label><label className="model-wide-field">Base URL<input type="url" required disabled={!!busy} value={profile.base_url} onChange={(event) => setProfile({ ...profile, base_url: event.target.value })} placeholder="https://api.example.com/v1" /></label>
              <label className="model-wide-field">API Key<input type="password" autoComplete="new-password" spellCheck={false} disabled={!!busy || clearKey} value={secret} onChange={(event) => setSecret(event.target.value)} placeholder={selectedSaved?.has_api_key ? "留空保留本配置密钥，已保存密钥不会回显" : "填写此配置对应的密钥"} /></label>
              <label>声明上下文上限（Token）<input required type="number" min={256} max={2000000} disabled={!!busy} value={profile.context_tokens} onChange={(event) => setProfile({ ...profile, context_tokens: Number(event.target.value) })} /></label><label>单次最大输出（Token）<input required type="number" min={1} max={Math.min(131072, profile.context_tokens - 1)} disabled={!!busy} value={profile.max_output_tokens} onChange={(event) => setProfile({ ...profile, max_output_tokens: Number(event.target.value) })} /></label>
              <label>输入报价（美元／百万 Token）<input type="number" min={0} step="any" disabled={!!busy} value={profile.price_input_usd ?? ""} onChange={(event) => setProfile({ ...profile, price_input_usd: event.target.value === "" ? null : Number(event.target.value) })} placeholder="留空表示未知" /></label><label>输出报价（美元／百万 Token）<input type="number" min={0} step="any" disabled={!!busy} value={profile.price_output_usd ?? ""} onChange={(event) => setProfile({ ...profile, price_output_usd: event.target.value === "" ? null : Number(event.target.value) })} placeholder="留空表示未知" /></label></div>
            <fieldset className="model-role-checks"><legend>允许用于</legend>{roles.map((role) => <label key={role.id}><input type="checkbox" disabled={!!busy} checked={profile.roles.includes(role.id)} onChange={(event) => setProfile({ ...profile, roles: event.target.checked ? [...profile.roles, role.id] : profile.roles.filter((item) => item !== role.id) })} />{role.label}</label>)}</fieldset>
            <label className="model-checkbox"><input type="checkbox" checked={clearKey} disabled={!!busy} onChange={(event) => { setClearKey(event.target.checked); if (event.target.checked) setSecret(""); }} />清除此配置的本机密钥</label><label className="model-checkbox"><input type="checkbox" checked={profile.allow_loopback} disabled={!!busy} onChange={(event) => setProfile({ ...profile, allow_loopback: event.target.checked })} />允许本机回环接口</label>
            {changedEndpoint && <p className="inline-error">地址已变化，请填写新服务的密钥或显式清除旧密钥。</p>}
            <p className="model-caption">API 使用 OpenAI 兼容协议。上限与报价由你声明，不表示已验证服务能力；保存不会调用模型。密钥只保存在本机。</p>
            <div className="model-actions">{selectedProfile && <button type="button" className="button secondary small" disabled={!!busy || registry.migration_required || registry.profiles.length < 2} onClick={() => void act("remove", () => saveProfile(true))}><Trash2 size={14} />移除配置</button>}<button className="button primary small" disabled={!!busy || registry.migration_required || changedEndpoint || profile.roles.length === 0}><Save size={14} />保存模型配置</button></div>
          </form></div>
      </section>
      <section className="model-panel"><div className="section-heading"><h2><GitBranch size={18} />本作品角色与预算</h2><span className="tag">{project.title} · 策略版本 {routing.revision}</span></div>
        <form onSubmit={(event) => { event.preventDefault(); void act("policy", async () => { const value = await api<RoutingSettings>(`${base}/routing`, "PUT", { expected_hash: routing.hash, policy }); if (alive.current) { setRouting(value); setPolicy(value.policy); setPreview(null); setNotice("作品角色与预算已保存。新任务使用新策略；现有任务保留原快照。"); } await onSaved(); }); }}>
          <div className="model-form-grid">{roles.map((role) => <div className="model-role-binding" key={role.id}><label>{role.label}主模型<select required disabled={!!busy} value={policy.bindings[role.id]?.primary || ""} onChange={(event) => setPolicy({ ...policy, bindings: { ...policy.bindings, [role.id]: { primary: event.target.value, fallbacks: (policy.bindings[role.id]?.fallbacks || []).filter((item) => item !== event.target.value) } } })}><option value="">请选择主模型</option>{registry.profiles.filter((item) => item.roles.includes(role.id)).map((item) => <option key={item.id} value={item.id}>{item.label} · {item.model}{item.has_api_key ? "" : "（未配置密钥）"}</option>)}</select></label><details className="model-fallbacks"><summary>允许的技术备用（{policy.bindings[role.id]?.fallbacks.length || 0}）</summary>{[0, 1].map((index) => <label key={index}>备用 {index + 1}<select value={policy.bindings[role.id]?.fallbacks[index] || ""} disabled={!!busy || (index === 1 && !policy.bindings[role.id]?.fallbacks[0])} onChange={(event) => { const next = [...(policy.bindings[role.id]?.fallbacks || [])]; if (event.target.value) next[index] = event.target.value; else next.splice(index); setPolicy({ ...policy, bindings: { ...policy.bindings, [role.id]: { ...policy.bindings[role.id], fallbacks: next } } }); }}><option value="">不使用备用</option>{registry.profiles.filter((item) => item.roles.includes(role.id) && item.id !== policy.bindings[role.id]?.primary && !(policy.bindings[role.id]?.fallbacks || []).some((identity, at) => at !== index && identity === item.id)).map((item) => <option key={item.id} value={item.id}>{item.label} · {item.model}</option>)}</select></label>)}</details></div>)}</div>
          <div className="model-form-grid model-budget-grid"><label>流程最大调用次数<input type="number" required min={1} max={32} disabled={!!busy} value={policy.max_calls} onChange={(event) => setPolicy({ ...policy, max_calls: Number(event.target.value) })} /></label><label>流程 Token 预算<input type="number" required min={1} max={32000000} disabled={!!busy} value={policy.max_tokens} onChange={(event) => setPolicy({ ...policy, max_tokens: Number(event.target.value) })} /></label><label>流程费用预算（美元）<input type="number" min={0} step="any" disabled={!!busy} value={policy.max_cost_usd ?? ""} onChange={(event) => setPolicy({ ...policy, max_cost_usd: event.target.value === "" ? null : Number(event.target.value) })} placeholder="留空不设金额上限" /></label><label>单节点最多尝试次数<input type="number" required min={1} max={3} disabled={!!busy} value={policy.max_attempts} onChange={(event) => setPolicy({ ...policy, max_attempts: Number(event.target.value) })} /></label></div>
          <p className="model-caption">新任务按职责使用这里明确绑定的模型。未知报价不会按零计算；设置金额上限时，缺少报价的路线可能被拒绝。备用仅在这里允许的顺序、次数与预算内处理技术故障；认证和版本错误会停止。</p><div className="model-actions"><button className="button primary small" disabled={!!busy}><Save size={14} />保存作品策略</button></div>
        </form>
      </section>
      <section className="model-panel"><div className="section-heading"><h2><ShieldCheck size={18} />候选协作流程</h2><span className="tag">编辑 → 审稿 → 候选记忆</span></div><p className="model-caption">选择已保存文稿发起编辑。每一步均生成独立候选，后续调用由你明确启动；候选记忆不代表事件已定稿，不更新正式正文或记忆。章节起草仍须从已批准章计划入口开始。</p>
        <div className="model-form-grid"><label>来源文稿<select value={sourcePath} disabled={!!busy} onChange={(event) => { setSourcePath(event.target.value); setPreview(null); }}><option value="">选择文稿</option>{sources.map((item) => <option key={item.path} value={item.path}>{item.path}</option>)}</select></label><div className="model-workflow-options"><label className="model-checkbox"><input type="checkbox" checked={withReview} disabled={!!busy} onChange={(event) => { setWithReview(event.target.checked); setPreview(null); }} />后续进行证据审稿</label><label className="model-checkbox"><input type="checkbox" checked={withMemory} disabled={!!busy} onChange={(event) => { setWithMemory(event.target.checked); setPreview(null); }} />后续抽取候选记忆</label><label className="model-checkbox"><input type="checkbox" checked={independentReview} disabled={!!busy || !withReview} onChange={(event) => { setIndependentReview(event.target.checked); setPreview(null); }} />审稿使用不同接口或模型</label></div><label className="model-wide-field">本次编辑目标<textarea rows={3} required maxLength={20000} value={instruction} disabled={!!busy} onChange={(event) => { setInstruction(event.target.value); setPreview(null); }} placeholder="例如：保留情节与人物知识，减少套话，让对话更自然。" /></label></div>
        {policy.max_calls < 1 + Number(withReview) + Number(withMemory) && <p className="analysis-warning">当前调用次数上限不足以完成所选节点。请先保存至少 {1 + Number(withReview) + Number(withMemory)} 次的流程预算；技术备用尝试也占用调用次数。</p>}
        {policyDirty && <p className="analysis-warning">作品策略有未保存的修改，请先保存后再预览和启动新流程。</p>}
        <div className="model-actions"><button type="button" className="button secondary small" disabled={!!busy || !source || !instruction.trim() || policyDirty} onClick={() => void act("preview", routePreview)}><Eye size={14} />预览首节点路线与预算</button><button type="button" className="button primary small" disabled={!!busy || !preview || currentIdentity !== previewIdentity || policyDirty || routing.policy.max_calls < 1 + Number(withReview) + Number(withMemory)} onClick={() => void act("create", createWorkflow)}><Play size={14} />启动候选流程</button></div>
        {preview && <details className="analysis-limits model-snapshot" open><summary>实际路由预览{previewIdentity !== currentIdentity ? " · 已失效，请重新预览" : " · 尚未调用模型"}</summary><RecordSummary value={preview.route && typeof preview.route === "object" ? preview.route as Record<string, unknown> : preview} /><p>首节点预览不包含后续审稿与抽取的实际输入；后续节点启动前仍由后端复核来源与剩余预算。</p></details>}
        <div className="model-workflows"><label>已有流程<select value={workflow?.id || ""} disabled={!!busy} onChange={(event) => { const id = event.target.value; if (!id) { setWorkflow(null); return; } void act("select-workflow", async () => { const value = await api<ModelWorkflow>(`${base}/workflows/${encodeURIComponent(id)}`); if (alive.current) setWorkflow(value); }); }}><option value="">选择流程查看节点</option>{workflows.map((item) => <option key={item.id} value={item.id}>{formatDate(item.created_at)} · {stateLabels[item.state] || item.state} · {item.id.slice(0, 8)}</option>)}</select></label>{workflow && <div className="model-workflow-detail"><div className="model-workflow-status"><strong>{stateLabels[workflow.state] || workflow.state}</strong><code>{workflow.id}</code><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("refresh-workflow", async () => { const value = await api<ModelWorkflow>(`${base}/workflows/${encodeURIComponent(workflow.id)}`); if (alive.current) setWorkflow(value); })}><RefreshCw size={13} />刷新</button></div>
          {workflow.stale && <div className="analysis-warning" role="status"><span>{workflowDriftReason(workflow.drift)}此流程已停用后继与局部修订，请核对当前资料并重新预览创建；原记录仍可回顾。</span></div>}
          <ol className="model-node-list">{workflow.nodes.map((node, index) => <li key={node.id || index}><div><strong>{index + 1}. {kindLabels[node.kind] || node.kind}</strong><span className="tag">{stateLabels[node.state] || node.state}</span></div>{node.job_id && <button type="button" className="text-button" onClick={() => onTask(node.job_id!)}>查看调用快照与记录<ArrowRight size={12} /></button>}{node.result_path && <button type="button" className="text-button" onClick={() => onOpen(node.result_path!)}>{leaf(node.result_path)}<ArrowRight size={12} /></button>}{node.result_hash && <small>结果版本 {node.result_hash.slice(0, 12)}</small>}</li>)}</ol>
          <JobRouteDetails budget={workflow.budget_summary} />
          {evidence && <div className="model-evidence"><h3>最新证据审稿{evidenceStale ? " · 不适用于当前候选" : ""}</h3><p className="model-caption">审稿判断供你核对。选择 1–20 项精确证据后可启动局部修订；所选原句范围须重叠或相邻，分离问题请分别处理。每个流程最多两轮，每轮修订、复审与候选记忆均占用同一预算。</p>{evidence.issues.map((issue) => <article key={issue.id}><label className="model-checkbox"><input type="checkbox" checked={issueIds.includes(issue.id)} disabled={!!busy || evidenceStale || !["awaiting_advance", "ready"].includes(workflow.state) || (workflow.revision_rounds || 0) >= 2 || (issueIds.length >= 20 && !issueIds.includes(issue.id))} onChange={(event) => setIssueIds(event.target.checked ? [...issueIds, issue.id] : issueIds.filter((id) => id !== issue.id))} /><strong>{issue.category}</strong><span className="tag">{issue.severity === "warning" ? "需关注" : "提示"}</span></label><blockquote>{issue.quote}</blockquote><small>字符范围 [{issue.start_char}, {issue.end_char})</small><p>{issue.explanation}</p><p>建议：{issue.suggestion}</p></article>)}{!evidence.issues.length && <p className="model-caption">本次未列出有定位证据的问题，不代表已证明全文没有问题。</p>}{evidence.unchecked.length > 0 && <details><summary>未检查或证据不足（{evidence.unchecked.length}）</summary><ul>{evidence.unchecked.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}<div className="model-form-grid"><label className="model-wide-field">本轮修订补充（可选）<textarea rows={2} value={revisionInstruction} maxLength={20000} disabled={!!busy || evidenceStale} onChange={(event) => setRevisionInstruction(event.target.value)} /></label></div><div className="model-actions"><span className="model-caption">已修订 {workflow.revision_rounds || 0} / 2 轮</span><button type="button" className="button primary small" disabled={!!busy || evidenceStale || issueIds.length === 0 || !["awaiting_advance", "ready"].includes(workflow.state) || (workflow.revision_rounds || 0) >= 2} onClick={() => void act("revise", reviseWorkflow)}><Play size={14} />按所选证据启动局部修订</button></div></div>}
          <div className="model-actions">{["awaiting_advance", "paused"].includes(workflow.state) && <button type="button" className="button primary small" disabled={!!busy || !!workflow.stale} onClick={() => void act("advance", () => workflowAction("advance"))}><Play size={14} />明确启动下一节点</button>}{["queued", "running", "awaiting_advance"].includes(workflow.state) && <button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("pause", () => workflowAction("pause"))}><Pause size={14} />暂停流程</button>}{!["ready", "failed", "cancelled"].includes(workflow.state) && <button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("cancel", () => workflowAction("cancel"))}><Square size={14} />取消流程</button>}</div>
        </div>}</div>
      </section>
      <NovelBenchWorkspace project={project} documents={documents} />
    </>}
  </div>;
}
