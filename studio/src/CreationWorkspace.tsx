import { useTaskReference } from "./TaskReferenceContext";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ArrowRight, BookOpen, Check, Eye, Lightbulb, LoaderCircle, Plus, RefreshCw, Save, Settings2, Sparkles, Trash2, X } from "lucide-react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { api, ApiError, activeStates, formatDate, leaf, projectUrl, stateLabels } from "./api";
import type { Document as NovelDocument, DocumentItem, Job, Project, Version } from "./api";
import { addAuthorTag, briefFormIssues, briefWarnings, generationIdentity, listText, previewConfiguration, selectedTags, setTagDecision, textList } from "./creationHelpers";
import type { BriefEnvelope, CreationCatalog, CreationPreview, CreationSelection, CreativeBrief } from "./creationHelpers";
import "./creation.css";
import { libraryPreviewHash } from "./libraryHelpers";
import TaskMaterialPreview from "./TaskMaterialPreview";

export type CreationWorkspaceProps = {
  project: Project; documents: DocumentItem[]; jobs: Job[];
  onChanged: () => Promise<void>; onJob: (job: Job) => void;
  onOpenDocument: (path: string) => void; onPlanning: (path: string) => void;
  onSettings: () => void; onDirtyChange?: (dirty: boolean) => void;
};
type Candidate = { document: NovelDocument; version_id: string | null; job: Job | undefined };
const failureMessage = (failure: unknown) => failure instanceof ApiError && failure.status === 409
  ? "来源、简报或模型策略已变化。输入已保留，请重新读取或核对版本后再操作。"
  : failure instanceof ApiError && failure.status === 403
    ? "本机会话已失效，请保存好输入后刷新页面。"
    : failure instanceof ApiError && failure.status === 400
      ? "操作未完成。请核对输入长度、规划模型和密钥、角色绑定与预算；输入仍保留在这里。"
      : "请求未完成或提交结果尚不明确。输入仍保留；请先核对任务记录，重试提交会沿用同一幂等键。";

function ListField({ label, value, onChange, disabled, placeholder }: { label: string; value: string[]; onChange: (value: string[]) => void; disabled: boolean; placeholder?: string }) {
  const [text, setText] = useState(listText(value));
  const serialized = JSON.stringify(value);
  useEffect(() => { setText((current) => JSON.stringify(textList(current)) === serialized ? current : listText(value)); }, [serialized]); // Retain the author's trailing newline while entering a list.
  return <label>{label}<textarea rows={3} maxLength={40040} value={text} disabled={disabled} placeholder={placeholder || "每行一项；可以留空"} onChange={(event) => { setText(event.target.value); onChange(textList(event.target.value)); }} /><small>每项最多 1000 字，最多 40 项。</small></label>;
}

function FormSection({ number, title, description, children, open = false }: { number: string; title: string; description: string; children: ReactNode; open?: boolean }) {
  return <details className="creation-section" open={open || undefined}><summary><span className="creation-step-number">{number}</span><span><strong>{title}</strong><small>{description}</small></span><Plus size={16} className="creation-expand" /></summary><div className="creation-section-body">{children}</div></details>;
}

function RoutePreview({ preview }: { preview: CreationPreview }) {
  const route = (preview.route || preview) as Record<string, unknown>;
  const candidates = Array.isArray(route.candidates) ? route.candidates.filter((item): item is Record<string, unknown> => !!item && typeof item === "object") : [];
  const budget = (preview.budget || route.budget || {}) as Record<string, unknown>;
  const maxAttempts = typeof route.max_attempts === "number" ? route.max_attempts : 1;
  const first = candidates[0];
  const estimatedCost = first && typeof first.price_input_usd === "number" && typeof first.price_output_usd === "number" && typeof route.estimated_input_tokens === "number" && typeof route.max_output_tokens === "number"
    ? (first.price_input_usd * route.estimated_input_tokens + first.price_output_usd * route.max_output_tokens) / 1000000 : null;
  return <div className="creation-route"><h3>本次执行预览 · 尚未调用模型</h3><dl><dt>执行职责</dt><dd>规划角色 · 一个初版总纲节点</dd>{candidates.map((item, index) => <div key={String(item.id || index)}><dt>{index === 0 ? "主模型" : `允许的备用 ${index}`}</dt><dd>{String(item.label || item.id || "未配置")} · {String(item.model || "未填写")}</dd></div>)}<dt>技术尝试</dt><dd>首次 1 次，最多 {maxAttempts} 次；允许的备用尝试也会占用预算</dd>{typeof route.estimated_input_tokens === "number" && <><dt>输入估算</dt><dd>{route.estimated_input_tokens.toLocaleString()} Token（含余量）</dd></>}{typeof route.max_output_tokens === "number" && <><dt>最大输出</dt><dd>{route.max_output_tokens.toLocaleString()} Token</dd></>}{typeof budget.max_calls === "number" && <><dt>共享调用预算</dt><dd>最多 {budget.max_calls} 次</dd></>}{typeof budget.max_tokens === "number" && <><dt>共享 Token 预算</dt><dd>{budget.max_tokens.toLocaleString()} Token</dd></>}{"max_cost_usd" in budget && <><dt>费用预算</dt><dd>{typeof budget.max_cost_usd === "number" ? `$${budget.max_cost_usd}` : "未设金额上限"}</dd></>}<dt>首尝试费用估算</dt><dd>{estimatedCost === null ? "未知：尚无完整报价" : `按输入估计与最大输出预留约 $${estimatedCost.toFixed(6)}`}</dd><dt>费用说明</dt><dd>按作者所填报价和实际用量估算；缺少报价或用量时为未知，以服务商账单为准。</dd><dt>材料范围</dt><dd>当前保存的创作简报、本次规则与下方列出的补充资料。未检索整部小说。</dd></dl>{preview.warnings?.map((warning, index) => <p className="analysis-warning" key={index}>{warning}</p>)}</div>;
}

export default function CreationWorkspace({ project, documents, jobs, onChanged, onJob, onOpenDocument, onPlanning, onSettings, onDirtyChange }: CreationWorkspaceProps) {
  const taskReference = useTaskReference(project.novel_id);
  const base = projectUrl(project.novel_id);
  const [catalog, setCatalog] = useState<CreationCatalog | null>(null);
  const [saved, setSaved] = useState<BriefEnvelope | null>(null);
  const [brief, setBrief] = useState<CreativeBrief | null>(null);
  const [selection, setSelection] = useState<CreationSelection | null>(null);
  const [preview, setPreview] = useState<CreationPreview | null>(null);
  const [previewIdentity, setPreviewIdentity] = useState("");
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [candidatePath, setCandidatePath] = useState("");
  const [query, setQuery] = useState("");
  const [customTag, setCustomTag] = useState("");
  const [customDimension, setCustomDimension] = useState("plot");
  const [customDecision, setCustomDecision] = useState<"selected" | "excluded">("selected");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [submissionUncertain, setSubmissionUncertain] = useState(false);
  const alive = useRef(true);
  const currentBase = useRef(base); currentBase.current = base;
  const dirtyCallback = useRef(onDirtyChange); dirtyCallback.current = onDirtyChange;
  const actionBusy = useRef(false);
  const generation = useRef(0);
  const candidateGeneration = useRef(0);
  const submitKey = useRef<{ identity: string; key: string } | null>(null);
  const dirty = !!saved && !!brief && JSON.stringify(brief) !== JSON.stringify(saved.brief);
  const pendingTag = !!customTag.trim();
  const currentIdentity = saved ? JSON.stringify([generationIdentity(saved), taskReference.ticket]) : "";
  const validPreview = !!preview && !!previewConfiguration(preview) && previewIdentity === currentIdentity && !dirty && !pendingTag;
  const warnings = brief ? briefWarnings(brief) : [];
  const formIssues = brief && catalog ? briefFormIssues(brief, catalog) : [];
  const outlineJobs = jobs.filter((job) => job.kind === "generate_story_outline");
  const activeJob = outlineJobs.find((job) => activeStates.has(job.state));
  const paths = [...new Set([...outlineJobs.flatMap((job) => job.result_path ? [job.result_path] : []), ...documents.filter((item) => /^outlines\/proposals\/story-outline-[^/]+\.md$/.test(item.path)).map((item) => item.path), ...(selection?.selection ? [selection.selection.document_path] : [])])];
  const candidateStale = !!candidate && documents.some((item) => item.path === candidate.document.path && item.hash !== candidate.document.hash);
  const html = useMemo(() => candidate ? DOMPurify.sanitize(marked.parse(candidate.document.content, { async: false }) as string) : "", [candidate]);

  useEffect(() => { dirtyCallback.current?.(dirty || pendingTag); }, [dirty, pendingTag]);
  useEffect(() => () => { dirtyCallback.current?.(false); }, []);
  useEffect(() => {
    if (!submissionUncertain || !submitKey.current) return;
    const key = submitKey.current.key;
    const accepted = outlineJobs.find((job) => (job.request as Record<string, unknown> | undefined)?.idempotency_key === key);
    if (accepted) {
      setSubmissionUncertain(false); setPreview(null); submitKey.current = null;
      setNotice("已在本机任务记录中找到刚才的提交，未重复调用。请查看任务状态与候选。");
    }
  }, [jobs, submissionUncertain]);
  useEffect(() => {
    alive.current = true;
    const request = ++generation.current;
    setSaved(null); setBrief(null); setSelection(null); setCandidate(null); setCandidatePath(""); setPreview(null); setCustomTag(""); setSubmissionUncertain(false); submitKey.current = null;
    setBusy("load"); setError("");
    void Promise.all([api<CreationCatalog>("/creation/catalog"), api<BriefEnvelope>(`${base}/creative-brief`), api<CreationSelection>(`${base}/creation-selection`)]).then(([terms, value, chosen]) => {
      if (!alive.current || request !== generation.current) return;
      setCatalog(terms); setSaved(value); setBrief(value.brief); setSelection(chosen);
    }).catch((failure) => { if (alive.current && request === generation.current) setError(failureMessage(failure)); }).finally(() => { if (alive.current && request === generation.current) setBusy(""); });
    return () => { alive.current = false; generation.current += 1; candidateGeneration.current += 1; };
  }, [base]);

  function update(next: CreativeBrief) { setBrief(next); setPreview(null); setNotice(""); }
  async function act(name: string, operation: () => Promise<unknown>) {
    if (actionBusy.current) return;
    actionBusy.current = true; setBusy(name); setError(""); setNotice("");
    const request = generation.current;
    try { await operation(); }
    catch (failure) { if (alive.current && request === generation.current) setError(failureMessage(failure)); }
    finally { actionBusy.current = false; if (alive.current && request === generation.current) setBusy(""); }
  }
  async function saveBrief() {
    if (!brief || !saved || formIssues.length) return false;
    const value = await api<BriefEnvelope>(`${base}/creative-brief`, "PUT", { brief, expected_hash: saved.hash, expected_version_id: saved.version_id });
    if (!alive.current || currentBase.current !== base) return false;
    setSaved(value); setBrief(value.brief); setPreview(null); setNotice("构思已保存到本机。保存不会调用模型。");
    await onChanged();
    const chosen = await api<CreationSelection>(`${base}/creation-selection`);
    if (alive.current && currentBase.current === base) setSelection(chosen);
    return alive.current && currentBase.current === base;
  }
  async function reload() {
    const [value, chosen, terms] = await Promise.all([api<BriefEnvelope>(`${base}/creative-brief`), api<CreationSelection>(`${base}/creation-selection`), api<CreationCatalog>("/creation/catalog")]);
    if (!alive.current || currentBase.current !== base) return;
    setSaved(value); setBrief(value.brief); setSelection(chosen); setCatalog(terms); setPreview(null); setNotice("已读取本机最新版本。");
  }
  async function previewGeneration() {
    if (!saved || dirty || pendingTag || submissionUncertain || !brief?.story_seed.trim()) return;
    const identity = currentIdentity;
    const value = await api<CreationPreview>(`${base}/jobs/preview`, "POST", { ...(taskReference.selection ? {library_selection:taskReference.selection}:{}), kind: "generate_story_outline", provider: "api", document_path: saved.path, document_hash: saved.hash, document_version_id: saved.version_id });
    if (!alive.current || currentBase.current !== base) return;
    setPreview(value); setPreviewIdentity(identity);
    if (!submitKey.current || submitKey.current.identity !== identity) submitKey.current = { identity, key: crypto.randomUUID() };
  }
  async function generate() {
    if (!saved || !validPreview || !submitKey.current || activeJob) return;
    let job: Job;
    try {
      job = await api<Job>(`${base}/jobs`, "POST", { ...(taskReference.selection ? {library_selection:taskReference.selection}:{}), kind: "generate_story_outline", provider: "api", document_path: saved.path, document_hash: saved.hash, document_version_id: saved.version_id, idempotency_key: submitKey.current.key, expected_configuration: previewConfiguration(preview!), expected_library_hash: libraryPreviewHash(preview!) });
    } catch (failure) {
      if (alive.current && currentBase.current === base) {
        if (failure instanceof ApiError && failure.status < 500) { setPreview(null); submitKey.current = null; setSubmissionUncertain(false); }
        else { setSubmissionUncertain(true); void onChanged().catch(() => undefined); }
      }
      throw failure;
    }
    if (!alive.current || currentBase.current !== base) return;
    taskReference.consume(); onJob(job); setPreview(null); submitKey.current = null; setSubmissionUncertain(false);
    setNotice("总纲任务已提交。完成后在下方查看候选；不会自动展开章纲或写入正式正文。");
    await onChanged();
  }
  async function readCandidate(path: string) {
    const request = ++candidateGeneration.current;
    setCandidatePath(path); setCandidate(null);
    if (!path) return;
    const [doc, history] = await Promise.all([api<NovelDocument>(`${base}/document?path=${encodeURIComponent(path)}`), api<{ versions: Version[] }>(`${base}/history?path=${encodeURIComponent(path)}`)]);
    if (!alive.current || request !== candidateGeneration.current) return;
    const latest = history.versions[0];
    setCandidate({ document: doc, version_id: latest?.hash === doc.hash ? latest.id : null, job: outlineJobs.find((job) => job.result_path === path) });
  }
  async function chooseCandidate() {
    if (!candidate || !candidate.version_id || !selection || !saved || dirty) return;
    const chosen = await api<CreationSelection>(`${base}/creation-selection`, "PUT", {
      document_path: candidate.document.path, document_hash: candidate.document.hash, document_version_id: candidate.version_id,
      brief_hash: candidate.job ? saved.hash : null, brief_version_id: candidate.job ? saved.version_id : null, job_id: candidate.job?.id || null,
      expected_hash: selection.hash, expected_version_id: selection.version_id,
    });
    if (!alive.current || currentBase.current !== base) return;
    setSelection(chosen); setNotice("已选用这版总纲作为后续规划依据。它仍是规划材料，不代表正文或记忆已定稿。");
    await onChanged();
  }

  return <div className="creation-workspace">
    <header className="creation-heading"><div className="creation-kicker"><Lightbulb size={14} /> 从一个想法开始</div><h1>创意与定位</h1><p>先说说你想写的故事。其他信息可以留白，之后再慢慢补齐。</p><div className="creation-flow"><span className="current">1 故事想法</span><ArrowRight size={13} /><span>2 初版总纲</span><ArrowRight size={13} /><span>3 卷章与故事资料</span></div></header>
    {error && <div className="creation-error" role="alert"><p>{error}</p><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("settings", async () => { if (dirty && !(await saveBrief())) return; if (alive.current && currentBase.current === base) onSettings(); })}><Settings2 size={14} />{dirty ? "保存构思后打开模型设置" : "打开模型设置"}</button></div>}
    {notice && <p className="analysis-success" role="status">{notice}</p>}
    {!brief || !saved || !catalog || !selection ? <div className="creation-loading">{busy ? <><LoaderCircle size={20} className="spin" />读取创作简报…</> : <button className="button secondary" type="button" onClick={() => void act("reload", reload)}><RefreshCw size={16} />重新读取</button>}</div> : <>
      <div className="creation-savebar"><span className={dirty ? "creation-unsaved" : "creation-saved"}>{dirty ? "有未保存的构思" : saved.hash ? "构思已保存" : "尚未保存 · 可先留空保存"}</span><div><button type="button" className="button secondary small" disabled={!!busy} title={dirty ? "重新读取前会确认是否放弃本页修改" : "读取本机最新版本"} onClick={() => { if (!dirty || window.confirm("重新读取会放弃本页未保存的构思。确认读取本机最新版本？")) void act("reload", reload); }}><RefreshCw size={14} />读取最新</button><button type="button" className="button secondary small" disabled={!!busy || !!formIssues.length || (!dirty && !!saved.hash)} onClick={() => void act("save", saveBrief)}><Save size={14} />保存构思</button></div></div>
      <section className="creation-seed-card"><label htmlFor="creation-seed"><span>你的故事想法 <small>生成时唯一必填</small></span></label><textarea id="creation-seed" rows={5} maxLength={catalog.limits.story_seed} value={brief.story_seed} disabled={!!busy} onChange={(event) => update({ ...brief, story_seed: event.target.value })} placeholder="什么样的人，遇到了什么事？他想得到什么，又会付出什么代价？\n一句灵感、几段情节都可以，暂时没想好的部分交给后续讨论。" /><div className="creation-field-note"><span>不用先写大纲，也不必一次想清楚所有设定。</span><span>{Array.from(brief.story_seed).length} / {catalog.limits.story_seed}</span></div><div className="creation-category-row"><label>阅读主分类<select disabled={!!busy} value={brief.reading_category} onChange={(event) => update({ ...brief, reading_category: event.target.value })}><option value="undecided">暂未确定</option>{catalog.categories.filter((item) => item.id !== "undecided").map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}{brief.reading_category.startsWith("custom:") && <option value={brief.reading_category}>{brief.reading_category.slice(7)}</option>}</select></label><p>{catalog.genres.find((item) => item.id === project.genre)?.capability === "general" ? "本作品使用通用创作规则；尚无该题材专用检查。" : `本作品使用${catalog.genres.find((item) => item.id === project.genre)?.label || project.genre}规则。`}阅读分类与标签不会更换规则模块。</p></div></section>
      <FormSection number="01" title="阅读与内容标签" description="帮助确定方向；不选也可以。相同标签在不同分组保持同步。">
        <label className="creation-tag-search">搜索标签<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索情节、情感、人设或世界观" /></label>
        {([{ title: "阅读标签", dimensions: ["theme", "role", "plot"] }, { title: "内容标签", dimensions: ["plot", "emotion", "persona", "world"] }]).map((group) => <div className="creation-tag-group" key={group.title}><h3>{group.title}</h3>{group.dimensions.map((dimension) => { const terms = catalog.tags.filter((item) => item.dimensions.includes(dimension) && item.label.toLocaleLowerCase().includes(query.toLocaleLowerCase())); return <fieldset className="creation-tag-dimension" key={dimension}><legend>{catalog.dimensions.find((item) => item.id === dimension)?.label || dimension}</legend><div className="creation-tag-options">{terms.map((term) => { const current = brief.tags.find((item) => item.id === term.id); return <button type="button" key={term.id} className={`creation-tag ${current?.decision || ""}`} aria-pressed={current?.decision === "selected"} disabled={!!busy} onClick={() => update({ ...brief, tags: setTagDecision(brief.tags, term, "selected") })}>{current?.decision === "selected" && <Check size={12} />}{term.label}{current?.decision === "excluded" && <span> · 避免</span>}</button>; })}{!terms.length && <small>没有匹配项，可添加自定义标签。</small>}</div></fieldset>; })}</div>)}
        <div className="creation-custom-tag"><h3>添加自己的标签</h3><div className="creation-grid"><label>标签名称<input maxLength={catalog.limits.tag_label} disabled={!!busy} value={customTag} onChange={(event) => setCustomTag(event.target.value)} placeholder="例如：俱乐部经营" /></label><label>所属分组<select disabled={!!busy} value={customDimension} onChange={(event) => setCustomDimension(event.target.value)}>{catalog.dimensions.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label><label>使用方式<select disabled={!!busy} value={customDecision} onChange={(event) => setCustomDecision(event.target.value as "selected" | "excluded")}><option value="selected">希望包含</option><option value="excluded">不希望出现</option></select></label><button type="button" className="button secondary small" disabled={!!busy || !customTag.trim()} onClick={() => { update({ ...brief, tags: addAuthorTag(brief.tags, catalog, customTag, customDimension, customDecision, `custom:${crypto.randomUUID()}`) }); setCustomTag(""); }}><Plus size={14} />添加标签</button></div></div>
        {!!brief.tags.length && <div className="creation-selected-tags"><h3>我的标签与取舍</h3>{brief.tags.map((tag) => <div className="creation-tag-choice" key={tag.id}><span>{tag.label}<small>{tag.source === "ai" ? "AI 来源" : tag.source === "external" ? "外部来源" : ""}{tag.decision === "pending" ? " · 尚未接受" : tag.decision === "undecided" ? " · 未定" : ""}</small></span><select aria-label={`${tag.label}的使用方式`} disabled={!!busy} value={tag.decision} onChange={(event) => update({ ...brief, tags: brief.tags.map((item) => item.id === tag.id ? { ...item, decision: event.target.value as typeof item.decision } : item) })}><option value="selected">希望包含</option><option value="excluded">避免出现</option><option value="undecided">暂未决定</option><option value="pending">尚未接受</option></select><button type="button" className="creation-remove" disabled={!!busy} aria-label={`移除${tag.label}`} onClick={() => update({ ...brief, tags: brief.tags.filter((item) => item.id !== tag.id) })}><X size={14} /></button></div>)}</div>}
      </FormSection>
      <FormSection number="02" title="主角与叙事" description="名字、人物目标、单主角或群像、叙述人称与焦点角色。">
        <div className="creation-grid"><label>人物结构<select disabled={!!busy} value={brief.narration.ensemble} onChange={(event) => update({ ...brief, narration: { ...brief.narration, ensemble: event.target.value } })}><option value="undecided">暂未确定</option><option value="single">单主角</option><option value="ensemble">群像 / 多主角</option></select></label><label>叙述人称<select disabled={!!busy} value={brief.narration.person} onChange={(event) => update({ ...brief, narration: { ...brief.narration, person: event.target.value } })}><option value="undecided">暂未确定</option><option value="first">第一人称</option><option value="third">第三人称</option></select></label><label className="creation-full">焦点角色与视角范围<input maxLength={2000} disabled={!!busy} value={brief.narration.focus} onChange={(event) => update({ ...brief, narration: { ...brief.narration, focus: event.target.value } })} placeholder="例如：围绕主角限知叙事，不提前揭露其他角色的秘密" /></label></div>
        {brief.protagonists.map((person, index) => <article className="creation-character" key={person.id}><div><h3>主角 {index + 1}</h3><button type="button" className="creation-remove" aria-label={`删除主角${index + 1}`} disabled={!!busy} onClick={() => update({ ...brief, protagonists: brief.protagonists.filter((item) => item.id !== person.id) })}><Trash2 size={14} /></button></div><div className="creation-grid">{([{ key: "name", label: "名字 / 暂名", hint: "未定也可以" }, { key: "role", label: "身份与定位", hint: "他在这个故事里是谁" }, { key: "goal", label: "最想得到什么", hint: "外在目标或深层需要" }, { key: "flaw", label: "短板或内在矛盾", hint: "阻碍他的习惯、信念或处境" }] as const).map((field) => <label key={field.key}>{field.label}<input maxLength={field.key === "name" ? 200 : 2000} disabled={!!busy} value={person[field.key]} onChange={(event) => update({ ...brief, protagonists: brief.protagonists.map((item) => item.id === person.id ? { ...item, [field.key]: event.target.value } : item) })} placeholder={field.hint} /></label>)}</div></article>)}
        <button type="button" className="button secondary small" disabled={!!busy || brief.protagonists.length >= catalog.limits.protagonists} onClick={() => update({ ...brief, protagonists: [...brief.protagonists, { id: `protagonist-${crypto.randomUUID()}`, name: "", role: "", goal: "", flaw: "" }] })}><Plus size={14} />添加主角</button>
      </FormSection>
      <FormSection number="03" title="特殊优势与开局" description="明确有、无或未定；信息、技能、人脉也可以成为优势。">
        <label>金手指 / 特殊优势<select disabled={!!busy} value={brief.advantage.status} onChange={(event) => update({ ...brief, advantage: { ...brief.advantage, status: event.target.value as CreativeBrief["advantage"]["status"] } })}><option value="undecided">尚未决定</option><option value="no">明确没有</option><option value="yes">有特殊优势</option></select></label>
        {brief.advantage.status === "yes" && <div className="creation-grid creation-subgrid">{([{ key: "kind", label: "优势类型", hint: "信息、技能、人脉、系统、超自然能力等" }, { key: "ability", label: "能力与作用", hint: "它具体能做什么" }, { key: "limits", label: "限制条件", hint: "它做不到什么" }, { key: "cost", label: "代价", hint: "使用它会失去什么" }, { key: "reveal", label: "揭示时机", hint: "开局即公开，还是逐步揭示" }] as const).map((field) => <label key={field.key}>{field.label}<input maxLength={2000} disabled={!!busy} value={brief.advantage[field.key]} onChange={(event) => update({ ...brief, advantage: { ...brief.advantage, [field.key]: event.target.value } })} placeholder={field.hint} /></label>)}</div>}
        {brief.advantage.status === "no" && <p className="creation-field-note">“明确没有”会作为有效条件发送；之前填写的优势细节仍保留在构思中，不作为有效优势要求。</p>}
        <h3>开局时间、地点与因果</h3><div className="creation-grid">{([{ key: "era", label: "时代与背景", hint: "例如：架空王朝 / 近未来 / 当代" }, { key: "time", label: "起始时间或范围", hint: "可以写某年，也可以写春季开学前" }, { key: "place", label: "开局地点", hint: "真实或虚构地点均可" }, { key: "situation", label: "开局身份与处境", hint: "故事开始时，主角身处什么局面" }, { key: "inciting_incident", label: "引爆事件", hint: "什么打破了原来的生活" }, { key: "obstacle", label: "主要阻力", hint: "他必须面对什么人、规则或困境" }] as const).map((field) => <label key={field.key}>{field.label}<input maxLength={2000} disabled={!!busy} value={brief.opening[field.key]} onChange={(event) => update({ ...brief, opening: { ...brief.opening, [field.key]: event.target.value } })} placeholder={field.hint} /></label>)}</div>
      </FormSection>
      <FormSection number="04" title="篇幅、文风与作者条件" description="规模只是规划参考。不能改变的条件与可协商偏好分开保存。">
        <div className="creation-grid">{([{ key: "target_words_min", label: "目标总字数下限" }, { key: "target_words_max", label: "目标总字数上限" }, { key: "volume_count", label: "预计卷数" }, { key: "chapter_words", label: "单章目标字数" }] as const).map((field) => <label key={field.key}>{field.label}<input type="number" min={1} max={field.key === "volume_count" ? 1000 : field.key === "chapter_words" ? 100000 : 100000000} step={1} disabled={!!busy} value={brief.scale[field.key] ?? ""} onChange={(event) => update({ ...brief, scale: { ...brief.scale, [field.key]: event.target.value === "" ? null : Number(event.target.value) } })} placeholder="未定" /></label>)}<label className="creation-full">结局方向<input maxLength={2000} disabled={!!busy} value={brief.scale.ending} onChange={(event) => update({ ...brief, scale: { ...brief.scale, ending: event.target.value } })} placeholder="可以保留开放结局，不必现在定下细节" /></label>{([{ key: "tone", label: "叙述基调", hint: "轻松、冷峻、热血、克制……" }, { key: "pace", label: "节奏偏好", hint: "紧凑推进、慢热、生活细节……" }, { key: "dialogue", label: "对话偏好", hint: "口语程度、潜台词、角色差异……" }] as const).map((field) => <label key={field.key}>{field.label}<input maxLength={2000} disabled={!!busy} value={brief.style[field.key]} onChange={(event) => update({ ...brief, style: { ...brief.style, [field.key]: event.target.value } })} placeholder={field.hint} /></label>)}</div>
        <div className="creation-grid creation-subgrid"><ListField label="不能改变的条件" value={brief.constraints.hard} disabled={!!busy} onChange={(value) => update({ ...brief, constraints: { ...brief.constraints, hard: value } })} placeholder="例如：主角不重生；不改变故事发生年代" /><ListField label="可协商的偏好" value={brief.constraints.preferences} disabled={!!busy} onChange={(value) => update({ ...brief, constraints: { ...brief.constraints, preferences: value } })} /><ListField label="避免的套路或内容" value={brief.constraints.avoid} disabled={!!busy} onChange={(value) => update({ ...brief, constraints: { ...brief.constraints, avoid: value } })} /><ListField label="留待讨论的问题" value={brief.unresolved} disabled={!!busy} onChange={(value) => update({ ...brief, unresolved: value })} /></div>
      </FormSection>
      {formIssues.map((issue) => <p className="inline-error" role="alert" key={issue}>{issue}</p>)}
      {warnings.map((warning) => <p className="analysis-warning" key={warning}>{warning}</p>)}
      <section className="creation-generation"><div className="creation-generation-heading"><div><h2><Sparkles size={18} />生成第一版总纲</h2><p>先保存构思，再预览模型与预算。这里只生成一个候选，旧版会继续保留。</p></div><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("settings", async () => { if (dirty && !(await saveBrief())) return; if (alive.current && currentBase.current === base) onSettings(); })}><Settings2 size={14} />{dirty ? "保存后设置模型" : "模型与预算"}</button></div>
        {pendingTag && <p className="creation-unsaved">自定义标签尚未添加。请先点击“添加标签”，或清空标签名称后再预览。</p>}{dirty && <p className="creation-unsaved">请先点击“保存构思”，确保预览和模型读取的是同一版。</p>}{!saved.hash && !dirty && <p className="creation-field-note">构思可以为空保存；生成前至少填入故事想法。</p>}
        <div className="creation-actions"><button type="button" className="button secondary" disabled={!!busy || !!formIssues.length || (!dirty && !!saved.hash)} onClick={() => void act("save", saveBrief)}><Save size={16} />保存构思</button><button type="button" className="button secondary" disabled={!!busy || dirty || !!formIssues.length || pendingTag || submissionUncertain || !saved.hash || !brief.story_seed.trim() || !!activeJob} onClick={() => void act("preview", previewGeneration)}><Eye size={16} />预览生成</button><button type="button" className="button primary" disabled={!!busy || !validPreview || !!activeJob} onClick={() => void act("generate", generate)}>{busy === "generate" ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}生成初版总纲</button></div>
        {validPreview && <><div className="creation-effective"><h3>这次发送的创作要求</h3><p className="creation-seed-summary">{brief.story_seed}</p><dl><dt>已选标签</dt><dd>{selectedTags(brief.tags).map((item) => item.label).join("、") || "未指定"}</dd><dt>避免标签</dt><dd>{brief.tags.filter((item) => item.decision === "excluded").map((item) => item.label).join("、") || "未指定"}</dd><dt>特殊优势</dt><dd>{brief.advantage.status === "no" ? "明确没有" : brief.advantage.status === "yes" ? brief.advantage.kind || "有，类型待定" : "尚未决定"}</dd><dt>主角</dt><dd>{brief.protagonists.map((person) => [person.name || "暂未命名", person.role, person.goal && `目标：${person.goal}`, person.flaw && `短板：${person.flaw}`].filter(Boolean).join(" / ")).join("；") || "未指定"}</dd><dt>叙述视角</dt><dd>{({ undecided: "人称未定", first: "第一人称", third: "第三人称" } as Record<string, string>)[brief.narration.person]} · {({ undecided: "人物结构未定", single: "单主角", ensemble: "群像" } as Record<string, string>)[brief.narration.ensemble]}{brief.narration.focus && `；${brief.narration.focus}`}</dd><dt>开局</dt><dd>{Object.values(brief.opening).filter(Boolean).join("；") || "未指定"}</dd><dt>篇幅与结局</dt><dd>{brief.scale.target_words_min !== null || brief.scale.target_words_max !== null ? `${brief.scale.target_words_min ?? "未定"}–${brief.scale.target_words_max ?? "未定"} 字` : "总字数未定"}{brief.scale.volume_count !== null && `；${brief.scale.volume_count} 卷`}{brief.scale.chapter_words !== null && `；每章 ${brief.scale.chapter_words} 字`}{brief.scale.ending && `；${brief.scale.ending}`}</dd><dt>文风</dt><dd>{Object.values(brief.style).filter(Boolean).join("；") || "未指定"}</dd><dt>硬条件</dt><dd>{brief.constraints.hard.join("；") || "未指定"}</dd><dt>偏好与避用</dt><dd>{[...brief.constraints.preferences, ...brief.constraints.avoid.map((item) => `避免：${item}`)].join("；") || "未指定"}</dd><dt>待决定</dt><dd>{brief.unresolved.join("；") || "未填写的可选项继续保留未定"}</dd></dl><p className="creation-field-note">“尚未接受”和“暂未决定”的标签不会当作已选条件。生成结果是否符合语义要求，仍需作者阅读核对。</p></div><RoutePreview preview={preview!} /><TaskMaterialPreview route={preview!} /></>}
        {submissionUncertain && <div className="analysis-warning"><p>上一笔提交结果尚未确定。先核对任务记录；保持原简报时，可以用“生成初版总纲”重试同一幂等键。</p><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("check", onChanged)}><RefreshCw size={14} />核对任务记录</button><button type="button" className="button secondary small" disabled={!!busy} onClick={() => { if (window.confirm("请先核对已有任务。继续会开始一笔新的生成预览；若上一笔已送达，可能产生另一笔费用。确认继续？")) { setSubmissionUncertain(false); setPreview(null); submitKey.current = null; } }}>已核对，准备新的生成</button></div>}
        {activeJob && <p className="creation-job-status" role="status"><LoaderCircle size={16} className="spin" />总纲任务{stateLabels[activeJob.state] || activeJob.state}。关闭页面不会取消后台记录，后端重启不会自动重放请求。</p>}
        {!!outlineJobs.length && <details className="creation-job-history"><summary>总纲任务记录（{outlineJobs.length}）</summary>{outlineJobs.map((job) => <p key={job.id}><span>{formatDate(job.created_at)}</span><strong>{stateLabels[job.state] || job.state}</strong>{job.result_path && <button className="text-button" type="button" disabled={!!busy} onClick={() => void act("candidate", () => readCandidate(job.result_path!))}>查看结果</button>}</p>)}</details>}
      </section>
      <section className="creation-candidates"><div className="creation-generation-heading"><div><h2><BookOpen size={18} />总纲候选与下一步</h2><p>可以修改候选或保留多个版本。选用后，再按需展开卷纲、人物与章计划。</p></div></div>
        {selection.selection && <div className={selection.stale ? "analysis-warning" : "creation-selected-outline"}>{selection.stale ? "原选用总纲已失效，请核对后重新选用。" : "当前规划依据："}<button type="button" className="text-button" disabled={!!busy} onClick={() => void act("candidate", () => readCandidate(selection.selection!.document_path))}>{leaf(selection.selection.document_path)}</button>{selection.stale && selection.drift.length > 0 && <p>{selection.drift.join("；")}</p>}{!selection.stale && <button type="button" className="button primary small" disabled={!!busy || dirty} onClick={() => onPlanning(selection.selection!.document_path)}>展开卷纲与故事资料<ArrowRight size={14} /></button>}</div>}
        {!paths.length ? <div className="creation-empty"><BookOpen size={30} /><h3>你的第一份总纲，会出现在这里</h3><p>只需一句故事想法，保存后就能开始。模型尚未配置也可以先保存构思。</p></div> : <><label>选择要查看的总纲<select disabled={!!busy} value={candidatePath} onChange={(event) => void act("candidate", () => readCandidate(event.target.value))}><option value="">选择一个版本</option>{paths.map((path, index) => <option key={path} value={path}>候选 {index + 1} · {leaf(path)}</option>)}</select></label>{candidate && <><div className="creation-candidate-toolbar"><span>{candidateStale ? "候选已变化，请重新读取" : "当前候选 · 仅为规划材料"}</span><div><button type="button" className="button secondary small" disabled={!!busy || dirty} onClick={() => onOpenDocument(candidate.document.path)}>打开编辑器修改</button><button type="button" className="button secondary small" disabled={!!busy} onClick={() => void act("candidate", () => readCandidate(candidate.document.path))}><RefreshCw size={14} />核对版本</button><button type="button" className="button primary small" disabled={!!busy || dirty || candidateStale || !candidate.version_id} onClick={() => void act("choose", chooseCandidate)}><Check size={14} />选用这版继续规划</button></div></div><article className="creation-outline-prose" dangerouslySetInnerHTML={{ __html: html }} /><p className="creation-field-note">选用总纲不批准新章，不更新正式人物状态或记忆。再次编辑本稿或更换构思后，需要核对选用状态。</p></>}</>}
      </section>
    </>}
  </div>;
}
