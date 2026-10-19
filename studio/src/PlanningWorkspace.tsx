import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import type { FormEvent, ReactNode } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  CircleAlert,
  FileText,
  GitBranch,
  Layers3,
  ListTree,
  LoaderCircle,
  Network,
  Plus,
  PenLine,
  Save,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { api, activeStates, leaf, projectUrl, stateLabels } from "./api";
import type {
  Approval,
  Blueprint,
  DocumentItem,
  Job,
  PlanningChapter,
  PlanningEvidence,
  PlanningPackage,
  PlanningPackageSummary,
  PlanningRecord,
  Project,
  Provider,
} from "./api";
import { Empty, IconButton, Modal } from "./ui";

type Props = {
  project: Project;
  documents: DocumentItem[];
  providers: Provider[];
  onSettings: () => void;
  jobs: Job[];
  approvals: Approval[];
  onJob: (job: Job) => void;
  onProposal: (proposal: Approval) => void;
  onTasks: (id?: string) => void;
  onSource: (path: string, line?: number) => void;
  initialPath?: string;
  initialSourcePath?: string;
  initialTab?: Tab;
  onDirtyChange?: (dirty: boolean) => void;
  onImport?: () => void;
  onPaste?: () => void;
};
type Tab =
  | "overview"
  | "characters"
  | "relationships"
  | "threads"
  | "timeline"
  | "settings"
  | "style"
  | "unknowns";
const tabs: { id: Tab; label: string }[] = [
  { id: "overview", label: "总览与卷章" },
  { id: "characters", label: "人物表" },
  { id: "relationships", label: "规划关系" },
  { id: "threads", label: "伏笔台账" },
  { id: "timeline", label: "计划时间线" },
  { id: "settings", label: "故事设定" },
  { id: "style", label: "写作风格" },
  { id: "unknowns", label: "待确认点" },
];
const basisLabels = {
  source_asserted: "来源明确陈述",
  inferred: "根据来源推断",
  suggested: "新增规划建议",
};
const categoryLabels: Record<string, string> = {
  world: "世界",
  location: "地点",
  organization: "组织",
  item: "物品",
  rule: "规则",
  domain: "题材知识",
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const packageLabel = (item: PlanningPackageSummary) =>
  `${item.approved ? "已应用" : "待审候选"}${item.stale ? " · 已失效" : !item.valid ? " · 校验失败" : ""} · ${leaf(item.path)}`;

function Evidence({
  items,
  onOpen,
}: {
  items: PlanningEvidence[];
  onOpen: Props["onSource"];
}) {
  if (!items.length)
    return <p className="planning-no-evidence">未提供来源证据，待作者确认。</p>;
  return (
    <details className="planning-evidence">
      <summary>查看 {items.length} 处来源引用</summary>
      {items.map((item, index) => (
        <div key={`${item.path}-${item.start_line}-${index}`}>
          <button
            type="button"
            onClick={() => onOpen(item.path, item.start_line)}
            title="打开当前来源文件，引用正文保留在规划包中"
          >
            <FileText size={12} />
            <span>
              {item.path}:{item.start_line}
              {item.end_line !== item.start_line ? `–${item.end_line}` : ""}
            </span>
            <ArrowUpRight size={12} />
          </button>
          <blockquote>{item.quote}</blockquote>
        </div>
      ))}
    </details>
  );
}
function RecordCard({
  record,
  children,
  onOpen,
  aside,
}: {
  record: PlanningRecord;
  children?: ReactNode;
  onOpen: Props["onSource"];
  aside?: ReactNode;
}) {
  return (
    <article className="planning-record">
      <div className="planning-record-top">
        <h3>{record.title}</h3>
        {aside}
        <span className={`basis-tag ${record.basis}`}>
          {basisLabels[record.basis]}
        </span>
      </div>
      <p>{record.summary}</p>
      {children}
      <Evidence items={record.evidence} onOpen={onOpen} />
    </article>
  );
}
function ReferenceNames({
  ids,
  records,
}: {
  ids: string[];
  records: PlanningRecord[];
}) {
  if (!ids.length) return <span className="muted">未指定</span>;
  return (
    <>
      {ids
        .map((id) => records.find((item) => item.id === id)?.title || id)
        .join("、")}
    </>
  );
}
function RelationshipGraph({
  data,
  onOpen,
}: {
  data: Blueprint;
  onOpen: Props["onSource"];
}) {
  const marker = useId().replace(/:/g, "");
  const shown = data.characters.slice(0, 60);
  const nodeMap = new Map(
    shown.map((person, index) => {
      const angle =
        (index * 2 * Math.PI) / Math.max(shown.length, 1) - Math.PI / 2;
      const radius =
        shown.length === 1 ? 0 : Math.min(185, 100 + shown.length * 9);
      return [
        person.id,
        {
          person,
          x: 380 + Math.cos(angle) * radius * 1.45,
          y: 255 + Math.sin(angle) * radius,
        },
      ];
    }),
  );
  const edges = data.relationships.filter(
    (item) => nodeMap.has(item.from_id) && nodeMap.has(item.to_id),
  );
  return (
    <>
      <div className="planning-section-note">
        <Network size={15} />
        <span>
          规划关系图：只展示包中提供的人物与关系，不代表角色当前已知或事件已经发生。下方列表提供同等信息。
        </span>
      </div>
      {shown.length > 0 ? (
        <figure className="relationship-figure">
          <svg
            viewBox="0 0 760 510"
            role="img"
            aria-labelledby={`${marker}-title ${marker}-desc`}
          >
            <title id={`${marker}-title`}>规划人物关系图</title>
            <desc id={`${marker}-desc`}>
              包含{shown.length}名来源于规划包的人物与{edges.length}
              条规划关系。完整关系和证据见下方文字列表。
            </desc>
            <defs>
              <marker
                id={`${marker}-arrow`}
                viewBox="0 0 10 10"
                refX="8"
                refY="5"
                markerWidth="6"
                markerHeight="6"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#b5bdd5" />
              </marker>
            </defs>
            {edges.map((edge, index) => {
              const from = nodeMap.get(edge.from_id)!;
              const to = nodeMap.get(edge.to_id)!;
              const length = Math.hypot(to.x - from.x, to.y - from.y) || 1;
              const dx = (to.x - from.x) / length;
              const dy = (to.y - from.y) / length;
              return (
                <g key={edge.id}>
                  <title>
                    {from.person.title} → {to.person.title}：{edge.title}
                  </title>
                  <line
                    x1={from.x + dx * 29}
                    y1={from.y + dy * 29}
                    x2={to.x - dx * 34}
                    y2={to.y - dy * 34}
                    markerEnd={`url(#${marker}-arrow)`}
                    stroke="#c3cadf"
                    strokeWidth="1.3"
                  />
                  <text
                    x={(from.x + to.x) / 2}
                    y={(from.y + to.y) / 2 + ((index % 3) - 1) * 14}
                    textAnchor="middle"
                    className="relationship-edge-label"
                  >
                    {edge.title.length > 14
                      ? edge.title.slice(0, 14) + "…"
                      : edge.title}
                  </text>
                </g>
              );
            })}
            {[...nodeMap.values()].map(({ person, x, y }) => (
              <g key={person.id}>
                <title>
                  {person.title} · {person.role}
                </title>
                <circle cx={x} cy={y} r="27" fill="#f0f1f8" stroke="#d5d9e8" />
                <text
                  x={x}
                  y={y + 4}
                  textAnchor="middle"
                  className="relationship-node-label"
                >
                  {person.title.length > 5
                    ? person.title.slice(0, 5) + "…"
                    : person.title}
                </text>
              </g>
            ))}
          </svg>
          <figcaption>
            仅展示真实规划记录
            {data.characters.length > 60
              ? "中的前 60 个人物；完整关系见下方列表"
              : "；箭头方向对应关系记录的起点与终点"}
            。
          </figcaption>
        </figure>
      ) : (
        <Empty
          compact
          icon={<Users size={24} />}
          title="还没有规划人物"
          description="此规划包没有人物记录，图中不会补入虚构节点。"
        />
      )}
      <div className="planning-records" aria-label="规划关系文字列表">
        {data.relationships.length ? (
          data.relationships.map((item) => (
            <RecordCard key={item.id} record={item} onOpen={onOpen}>
              <div className="relationship-pair">
                <span>
                  {data.characters.find((person) => person.id === item.from_id)
                    ?.title || item.from_id}
                </span>
                <ArrowRight size={13} />
                <span>
                  {data.characters.find((person) => person.id === item.to_id)
                    ?.title || item.to_id}
                </span>
              </div>
            </RecordCard>
          ))
        ) : (
          <Empty
            compact
            title="未规划人物关系"
            description="人物关系可以留待后续规划，不会根据姓名自动推断。"
          />
        )}
      </div>
    </>
  );
}

export default function PlanningWorkspace({
  project,
  documents,
  providers,
  onSettings,
  jobs,
  approvals,
  onJob,
  onProposal,
  onTasks,
  onSource,
  initialPath,
  initialSourcePath,
  initialTab,
  onDirtyChange,
  onImport,
  onPaste,
}: Props) {
  const [sourcePath, setSourcePath] = useState(initialSourcePath || "");
  const [start, setStart] = useState(1);
  const [count, setCount] = useState(3);
  const [instruction, setInstruction] = useState("");
  const [packages, setPackages] = useState<PlanningPackageSummary[]>([]);
  const [selectedPath, setSelectedPath] = useState(initialPath || "");
  const [selected, setSelected] = useState<PlanningPackage | null>(null);
  const [tab, setTab] = useState<Tab>(initialTab || "overview");
  const [loading, setLoading] = useState(false);
  const [loadingPackage, setLoadingPackage] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<{
    path: string;
    hash: string;
    content: string;
  } | null>(null);
  const [editText, setEditText] = useState("");
  const alive = useRef(true);
  const listingRequest = useRef(0);
  const packageRequest = useRef(0);
  const selectedPathRef = useRef(selectedPath);
  selectedPathRef.current = selectedPath;
  const lastExtraction = useRef("");
  const base = projectUrl(project.novel_id);
  const sources = useMemo(
    () =>
      documents
        .filter(
          (item) =>
            /\.(md|txt)$/.test(item.path) &&
            (item.path.startsWith("outlines/source/") ||
              item.path.startsWith("outlines/proposals/") ||
              /^imports\/original\/[^/]+\/extracted\.md$/.test(item.path)),
        )
        .sort((a, b) => a.path.localeCompare(b.path, "zh-CN")),
    [documents],
  );
  const source = sources.find((item) => item.path === sourcePath);
  const selectedProvider = providers.find((item) => item.id === "api");
  const blueprintJobs = jobs.filter((job) => job.kind === "build_blueprint");
  const latestJob = blueprintJobs[0];
  const jobSignature = blueprintJobs
    .map((job) => `${job.id}:${job.state}:${job.result_path || ""}`)
    .join("|");
  const data = selected?.valid && selected.data ? selected.data : null;
  const actionable =
    !!selected &&
    selected.valid &&
    !selected.stale &&
    !!selected.hash &&
    !loading &&
    !loadingPackage;
  const pendingExtraction = approvals.find(
    (item) =>
      /^outlines\/approved\/ch\d+\.md$/.test(item.target) &&
      ["pending", "approved"].includes(item.state),
  );
  const blockedExtraction =
    !!pendingExtraction ||
    (!!lastExtraction.current &&
      !approvals.some(
        (item) =>
          item.id === lastExtraction.current &&
          ["applied", "rejected"].includes(item.state),
      ));

  useEffect(() => { if (initialTab) setTab(initialTab); }, [initialTab]);
  useEffect(() => { if (initialSourcePath) setSourcePath(initialSourcePath); }, [initialSourcePath]);
  const onDirtyRef = useRef(onDirtyChange); onDirtyRef.current = onDirtyChange;
  const formDirty = (!!editing && editText !== editing.content) || !!instruction.trim() || start !== 1 || count !== 3;
  useEffect(() => { onDirtyRef.current?.(formDirty); }, [formDirty]);
  useEffect(() => () => { onDirtyRef.current?.(false); }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      listingRequest.current += 1;
      packageRequest.current += 1;
    };
  }, []);
  useEffect(() => {
    if (!editing || editText === editing.content) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [editing, editText]);
  const loadPackage = useCallback(
    async (path: string) => {
      const request = ++packageRequest.current;
      setLoadingPackage(true);
      setSelected(null);
      try {
        const item = await api<PlanningPackage>(
          `${base}/planning/package?path=${encodeURIComponent(path)}`,
        );
        if (!alive.current || request !== packageRequest.current) return;
        setSelected(item);
      } catch (failure) {
        if (alive.current && request === packageRequest.current)
          setError(message(failure));
      } finally {
        if (alive.current && request === packageRequest.current)
          setLoadingPackage(false);
      }
    },
    [base],
  );
  const refresh = useCallback(
    async (preserveError = false) => {
      const request = ++listingRequest.current;
      setLoading(true);
      if (!preserveError) setError("");
      try {
        const result = await api<{ packages: PlanningPackageSummary[] }>(
          `${base}/planning`,
        );
        if (!alive.current || request !== listingRequest.current) return;
        setPackages(result.packages);
        const path = result.packages.some(
          (item) => item.path === selectedPathRef.current,
        )
          ? selectedPathRef.current
          : result.packages[0]?.path || "";
        setSelectedPath(path);
        selectedPathRef.current = path;
        if (path) await loadPackage(path);
        else {
          packageRequest.current += 1;
          setSelected(null);
          setLoadingPackage(false);
        }
      } catch (failure) {
        if (alive.current && request === listingRequest.current)
          setError(message(failure));
      } finally {
        if (alive.current && request === listingRequest.current)
          setLoading(false);
      }
    },
    [base, loadPackage],
  );
  useEffect(() => {
    void refresh();
  }, [refresh, jobSignature]);
  async function generate(event: FormEvent) {
    event.preventDefault();
    if (
      !source ||
      !selectedProvider?.available ||
      busy ||
      !Number.isInteger(start) ||
      start < 1 ||
      !Number.isInteger(count) ||
      count < 1 ||
      count > 10
    )
      return;
    setBusy("generate");
    setError("");
    try {
      const job = await api<Job>(`${base}/jobs`, "POST", {
        kind: "build_blueprint",
        provider: "api",
        document_path: source.path,
        document_hash: source.hash,
        planning_start: start,
        planning_count: count,
        instruction: instruction.trim(),
      });
      if (!alive.current) return;
      onJob(job);
    } catch (failure) {
      if (alive.current) {
        setError(message(failure));
        void refresh(true);
      }
    } finally {
      if (alive.current) setBusy("");
    }
  }
  function closeEditing() {
    if (busy === "save-package") return;
    if (
      editing &&
      editText !== editing.content &&
      !window.confirm(
        "候选 JSON 有未保存的修改。关闭会放弃这些修改，仍要继续吗？",
      )
    )
      return;
    setEditing(null);
  }
  async function saveEditing(event: FormEvent) {
    event.preventDefault();
    if (!editing || busy) return;
    const snapshot = editing;
    const content = editText;
    setBusy("save-package");
    setError("");
    try {
      await api(`${base}/document`, "PUT", {
        path: snapshot.path,
        content,
        expected_hash: snapshot.hash,
      });
      if (!alive.current) return;
      setEditing(null);
      await refresh();
    } catch (failure) {
      if (alive.current) setError(message(failure));
    } finally {
      if (alive.current) setBusy("");
    }
  }
  async function propose(chapter?: PlanningChapter) {
    if (
      !selected ||
      !actionable ||
      busy ||
      (chapter && (!selected.approved || blockedExtraction))
    )
      return;
    const snapshot = selected;
    setBusy(chapter?.id || "proposal");
    setError("");
    try {
      const result = await api<Approval>(`${base}/planning/proposal`, "POST", {
        path: snapshot.path,
        expected_hash: snapshot.hash,
        ...(chapter ? { chapter_id: chapter.id } : {}),
      });
      if (!alive.current) return;
      if (chapter) lastExtraction.current = result.id;
      onProposal(result);
    } catch (failure) {
      if (alive.current) {
        setError(message(failure));
        void refresh(true);
      }
    } finally {
      if (alive.current) setBusy("");
    }
  }
  const stateText = selected?.approved
    ? "已应用的规划包"
    : "候选规划 · 待作者审阅";
  return (
    <div className="planning-workspace scroll-area">
      <div className="page-heading compact-heading">
        <div>
          <div className="eyebrow">BLUEPRINT BEFORE THE FIRST WORD</div>
          <h1>{initialTab === "characters" ? "人物与世界" : initialTab === "timeline" ? "时间线与伏笔" : initialTab === "style" ? "写作风格" : "大纲与卷章"}</h1>
          <p>{initialTab && initialTab !== "overview" ? "查看同一规划包中的创作资料，可切换下方页签。所有记录描述未来规划。" : "把大纲展开为相互关联的创作蓝图。所有记录描述未来规划。"}</p>
        </div>
        <div className="planning-title-icon">
          <GitBranch size={26} strokeWidth={1.4} />
        </div>
      </div>
      <details className="planning-generate-toggle" open={!initialTab || initialTab === "overview"}>
      <summary>导入大纲与生成规划</summary>
      <section className="planning-generator">
        <div className="planning-import-actions"><button className="button secondary small" onClick={onImport} disabled={!!busy}><Plus size={14} />导入已有大纲</button><button className="button secondary small" onClick={onPaste} disabled={!!busy}><PenLine size={14} />粘贴或手写大纲</button><span>总纲选用后，在这里分批展开卷纲、章纲与创作资料。</span></div>
        <div className="planning-generator-heading">
          <span className="card-eyebrow">
            <Sparkles size={15} />
            从一份有据可查的大纲开始
          </span>
          <span className="tag">候选先审，逐章推进</span>
        </div>
        <form onSubmit={(event) => void generate(event)}>
          <div className="planning-form-grid">
            <label className="planning-field source-field">
              来源文稿
              <select
                value={sourcePath}
                onChange={(event) => setSourcePath(event.target.value)}
                required
                disabled={!!busy}
              >
                <option value="">选择大纲或提取后的来源材料</option>
                {sources.map((item) => (
                  <option key={item.path} value={item.path}>
                    {item.path}
                  </option>
                ))}
              </select>
            </label>
            <label className="planning-field">
              执行方式
              <div className="api-fixed-provider"><span>API 生成</span><button type="button" onClick={onSettings}>模型设置</button></div>
            </label>
            <label className="planning-field">
              起始章节
              <input
                aria-label="规划起始章节"
                type="number"
                min="1"
                max="999999"
                step="1"
                required
                value={start}
                onChange={(event) => setStart(Number(event.target.value))}
                disabled={!!busy}
              />
            </label>
            <label className="planning-field">
              本批章数
              <input
                aria-label="规划章数"
                type="number"
                min="1"
                max="10"
                step="1"
                required
                value={count}
                onChange={(event) => setCount(Number(event.target.value))}
                disabled={!!busy}
              />
            </label>
          </div>
          <p className="planning-source-policy">
            主来源仅限导入提取文本、源大纲与大纲草案。已有正式正文与记忆只作有限上下文校验，已批准规划不作为新包输入。
          </p>
          <label className="planning-field instruction-field">
            作者要求<span>可留空，待确认之处交由作者决定</span>
            <textarea
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="本批希望推进哪条主线？有哪些人物、时间、文风或不可改动的边界？"
              rows={2}
              maxLength={12000}
              disabled={!!busy}
            />
          </label>
          {!selectedProvider?.available && (
            <p className="planning-api-note">
              API 尚未配置。请在“模型设置”中选择模型并填写密钥，保存后再开始生成。
            </p>
          )}
          <div className="planning-generation-actions">
            <span>
              <Layers3 size={12} />
              {source ? (
                <>
                  绑定来源版本 <code>{source.hash.slice(0, 10)}</code>
                  ，结果保存为候选规划包
                </>
              ) : sources.length ? (
                "先选择来源，再生成规划"
              ) : (
                "先导入大纲或创建一份大纲文稿"
              )}
            </span>
            <button
              type="submit"
              className="button primary small"
              disabled={
                !source ||
                !selectedProvider?.available ||
                !!busy ||
                !Number.isInteger(count) ||
                count < 1 ||
                count > 10 ||
                !Number.isInteger(start) ||
                start < 1
              }
            >
              {busy === "generate" ? (
                <LoaderCircle className="spin" size={14} />
              ) : (
                <Sparkles size={14} />
              )}
              生成创作规划
              <ArrowRight size={13} />
            </button>
          </div>
        </form>
        {latestJob && (
          <button
            className="planning-latest-job"
            onClick={() => onTasks(latestJob.id)}
          >
            <span
              className={
                activeStates.has(latestJob.state)
                  ? "connection-dot orange-dot"
                  : "connection-dot"
              }
            />
            <strong>{stateLabels[latestJob.state] || latestJob.state}</strong>
            <span>
              {latestJob.error ||
                (latestJob.result_path
                  ? "规划候选已生成，可在下方选择查看"
                  : "打开任务记录，查看执行日志与候选结果")}
            </span>
            <ArrowUpRight size={14} />
          </button>
        )}
      </section>
      </details>
      {error && (
        <div className="planning-error" role="alert">
          <CircleAlert size={16} />
          <span>{error}</span>
          <button onClick={() => setError("")} aria-label="关闭规划错误提示">
            ×
          </button>
        </div>
      )}
      <section className="planning-packages">
        <div className="planning-package-toolbar">
          <div>
            <label htmlFor="planning-package">规划包</label>
            <select
              id="planning-package"
              value={selectedPath}
              disabled={loading || !!busy}
              onChange={(event) => {
                const path = event.target.value;
                selectedPathRef.current = path;
                setSelectedPath(path);
                setError("");
                void loadPackage(path);
              }}
            >
              {!packages.length && <option value="">尚无规划包</option>}
              {packages.map((item) => (
                <option key={item.path} value={item.path}>
                  {packageLabel(item)}
                </option>
              ))}
            </select>
          </div>
          <IconButton
            label="刷新规划包"
            disabled={loading || loadingPackage || !!busy}
            onClick={() => void refresh()}
          >
            <RefreshCw className={loading ? "spin" : ""} size={16} />
          </IconButton>
        </div>
        {loadingPackage ? (
          <div className="planning-loading">
            <LoaderCircle className="spin" size={20} />
            正在读取规划包与依赖状态…
          </div>
        ) : !selected ? (
          <Empty
            compact
            icon={<ListTree size={28} />}
            title="故事蓝图，等你来展开"
            description="选择一份来源，规划本批章节。任务完成后，这里将呈现卷章结构、人物、关系、伏笔、时间线、设定与文风。"
          />
        ) : (
          <>
            <div className="planning-package-summary">
              <div>
                <span
                  className={`tag ${selected.approved ? "green" : "orange"}`}
                >
                  {selected.approved ? (
                    <Check size={11} />
                  ) : (
                    <ShieldCheck size={11} />
                  )}
                  {stateText}
                </span>
                <code>{selected.path}</code>
              </div>
              <div className="planning-package-buttons">
                {selected.path.startsWith("outlines/proposals/") &&
                  selected.hash && (
                    <button
                      className="button secondary small"
                      disabled={!!busy || loading || loadingPackage}
                      onClick={() => {
                        setEditing({
                          path: selected.path,
                          hash: selected.hash!,
                          content: selected.content,
                        });
                        setEditText(selected.content);
                      }}
                    >
                      <PenLine size={13} />
                      修订候选 JSON
                    </button>
                  )}
                <button
                  className="button secondary small"
                  disabled={!actionable || selected.approved || !!busy}
                  onClick={() => void propose()}
                >
                  {busy === "proposal" ? (
                    <LoaderCircle className="spin" size={13} />
                  ) : (
                    <ShieldCheck size={13} />
                  )}
                  提交整包审批
                  <ArrowRight size={12} />
                </button>
              </div>
            </div>
            {selected.stale && (
              <div className="planning-warning">
                <CircleAlert size={15} />
                <span>
                  来源或依赖已变化，此包已经失效。重新生成或修订候选后再提交；当前不能审批或提取章计划。
                </span>
              </div>
            )}
            {!selected.valid && (
              <div className="planning-warning">
                <CircleAlert size={15} />
                <div>
                  <strong>规划包未通过校验</strong>
                  {selected.issues?.length > 0 && (
                    <ul>
                      {selected.issues.map((issue, index) => (
                        <li key={index}>{issue}</li>
                      ))}
                    </ul>
                  )}
                  <span>
                    保留原始文件供检查，不会将无效数据当作已确认设定。
                  </span>
                </div>
              </div>
            )}
            {data ? (
              <>
                <div
                  className="planning-tabs"
                  role="tablist"
                  aria-label="规划内容"
                  onKeyDown={(event) => {
                    if (
                      !["ArrowLeft", "ArrowRight", "Home", "End"].includes(
                        event.key,
                      )
                    )
                      return;
                    event.preventDefault();
                    const current = tabs.findIndex((item) => item.id === tab);
                    const next =
                      event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? tabs.length - 1
                          : (current +
                              (event.key === "ArrowRight" ? 1 : -1) +
                              tabs.length) %
                            tabs.length;
                    setTab(tabs[next].id);
                    document
                      .getElementById(`planning-tab-${tabs[next].id}`)
                      ?.focus();
                  }}
                >
                  {tabs.map((item) => (
                    <button
                      role="tab"
                      tabIndex={tab === item.id ? 0 : -1}
                      aria-selected={tab === item.id}
                      aria-controls={`planning-${item.id}`}
                      id={`planning-tab-${item.id}`}
                      key={item.id}
                      className={tab === item.id ? "selected" : ""}
                      onClick={() => setTab(item.id)}
                    >
                      {item.label}
                      {item.id === "unknowns" && data.unknowns.length > 0 && (
                        <span>{data.unknowns.length}</span>
                      )}
                    </button>
                  ))}
                </div>
                <div
                  className="planning-content"
                  role="tabpanel"
                  tabIndex={0}
                  id={`planning-${tab}`}
                  aria-labelledby={`planning-tab-${tab}`}
                >
                  {tab === "overview" && (
                    <>
                      <RecordCard
                        record={data.book}
                        onOpen={onSource}
                        aside={<BookOpen size={16} />}
                      />
                      <div className="planning-subheading">
                        <h2>卷章结构</h2>
                        <span>仅展示本包规划的章节范围</span>
                      </div>
                      {!selected.approved && (
                        <p className="planning-section-note">
                          <ShieldCheck size={14} />
                          先提交整包审批并应用，再逐章提取章计划提案。提取不会自动批准或写正文。
                        </p>
                      )}
                      {selected.approved && blockedExtraction && (
                        <p className="planning-section-note">
                          <CircleAlert size={14} />
                          已有章计划提案待处理。先在审批中心完成应用或驳回，再提取下一章。
                        </p>
                      )}
                      {data.volumes.length ? (
                        data.volumes
                          .slice()
                          .sort((a, b) => a.number - b.number)
                          .map((volume) => (
                            <details
                              open
                              className="planning-volume"
                              key={volume.id}
                            >
                              <summary>
                                <span>
                                  卷 {volume.number.toString().padStart(2, "0")}
                                </span>
                                <strong>{volume.title}</strong>
                                <small>
                                  第 {volume.chapter_start}–
                                  {volume.chapter_end ?? "开放"} 章
                                </small>
                              </summary>
                              <div className="planning-volume-body">
                                <p>{volume.summary}</p>
                                <Evidence
                                  items={volume.evidence}
                                  onOpen={onSource}
                                />
                                {data.chapters
                                  .filter(
                                    (chapter) =>
                                      chapter.volume_id === volume.id,
                                  )
                                  .sort((a, b) => a.number - b.number)
                                  .map((chapter) => (
                                    <div
                                      className="planning-chapter"
                                      key={chapter.id}
                                    >
                                      <div className="planning-chapter-heading">
                                        <span>
                                          {chapter.number
                                            .toString()
                                            .padStart(3, "0")}
                                        </span>
                                        <h3>{chapter.title}</h3>
                                        <span className="basis-tag">
                                          {basisLabels[chapter.basis]}
                                        </span>
                                        <button
                                          className="button secondary small"
                                          disabled={
                                            !actionable ||
                                            !selected.approved ||
                                            blockedExtraction ||
                                            !!busy
                                          }
                                          onClick={() => void propose(chapter)}
                                          title="只创建当前章的审批提案，批准应用后再提取下一章"
                                        >
                                          {busy === chapter.id ? (
                                            <LoaderCircle
                                              className="spin"
                                              size={12}
                                            />
                                          ) : (
                                            <Plus size={12} />
                                          )}
                                          提取章计划
                                        </button>
                                      </div>
                                      <p>{chapter.summary}</p>
                                      <dl className="planning-chapter-fields">
                                        <dt>目标</dt>
                                        <dd>{chapter.goal}</dd>
                                        <dt>冲突</dt>
                                        <dd>{chapter.conflict}</dd>
                                        <dt>转折</dt>
                                        <dd>{chapter.turn}</dd>
                                        <dt>预期结果</dt>
                                        <dd>{chapter.outcome}</dd>
                                      </dl>
                                      {chapter.beats.length > 0 && (
                                        <ol className="planning-beats">
                                          {chapter.beats.map((beat, index) => (
                                            <li key={index}>{beat}</li>
                                          ))}
                                        </ol>
                                      )}
                                      <dl className="planning-chapter-fields references">
                                        <dt>人物</dt>
                                        <dd>
                                          <ReferenceNames
                                            ids={chapter.character_ids}
                                            records={data.characters}
                                          />
                                        </dd>
                                        <dt>伏笔</dt>
                                        <dd>
                                          <ReferenceNames
                                            ids={chapter.thread_ids}
                                            records={data.threads}
                                          />
                                        </dd>
                                        <dt>前置章节</dt>
                                        <dd>
                                          <ReferenceNames
                                            ids={chapter.depends_on}
                                            records={data.chapters}
                                          />
                                        </dd>
                                      </dl>
                                      <Evidence
                                        items={chapter.evidence}
                                        onOpen={onSource}
                                      />
                                    </div>
                                  ))}
                              </div>
                            </details>
                          ))
                      ) : (
                        <Empty
                          compact
                          title="卷章尚待规划"
                          description="此包没有卷纲记录。空白部分保持待确认。"
                        />
                      )}
                      <details className="planning-source-hashes">
                        <summary>
                          本包绑定的来源版本（
                          {Object.keys(data.source_hashes).length}）
                        </summary>
                        {Object.entries(data.source_hashes).map(
                          ([path, hash]) => (
                            <button key={path} onClick={() => onSource(path)}>
                              <FileText size={12} />
                              <span>{path}</span>
                              <code>{hash.slice(0, 12)}</code>
                              <ArrowUpRight size={12} />
                            </button>
                          ),
                        )}
                      </details>
                    </>
                  )}
                  {tab === "characters" &&
                    (data.characters.length ? (
                      <div className="planning-table-wrap">
                        <table className="planning-table">
                          <thead>
                            <tr>
                              <th>人物 / 规划角色</th>
                              <th>人物摘要</th>
                              <th>规划的知情边界</th>
                              <th>依据</th>
                            </tr>
                          </thead>
                          <tbody>
                            {data.characters.map((person) => (
                              <tr key={person.id}>
                                <th scope="row">
                                  {person.title}
                                  <small>{person.role}</small>
                                </th>
                                <td>{person.summary}</td>
                                <td>
                                  {person.knowledge || "待确认"}
                                  <small>
                                    此处描述规划边界，不代表角色已经知道。
                                  </small>
                                </td>
                                <td>
                                  <span className="basis-tag">
                                    {basisLabels[person.basis]}
                                  </span>
                                  <Evidence
                                    items={person.evidence}
                                    onOpen={onSource}
                                  />
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <Empty
                        compact
                        icon={<Users size={26} />}
                        title="人物尚待规划"
                        description="此包没有人物记录，留待后续补充或生成。"
                      />
                    ))}
                  {tab === "relationships" && (
                    <RelationshipGraph data={data} onOpen={onSource} />
                  )}
                  {tab === "threads" &&
                    (data.threads.length ? (
                      <div className="planning-records">
                        {data.threads.map((thread) => (
                          <RecordCard
                            key={thread.id}
                            record={thread}
                            onOpen={onSource}
                          >
                            <div className="planning-thread-range">
                              <span>
                                计划铺设{" "}
                                <strong>
                                  {thread.setup_chapter == null
                                    ? "待确认"
                                    : `第 ${thread.setup_chapter} 章`}
                                </strong>
                              </span>
                              <ArrowRight size={13} />
                              <span>
                                计划回收{" "}
                                <strong>
                                  {thread.payoff_chapter == null
                                    ? "待确认"
                                    : `第 ${thread.payoff_chapter} 章`}
                                </strong>
                              </span>
                            </div>
                          </RecordCard>
                        ))}
                      </div>
                    ) : (
                      <Empty
                        compact
                        title="伏笔台账还是空白"
                        description="此包没有规划伏笔；尚未铺设或回收的事件不会标为已完成。"
                      />
                    ))}
                  {tab === "timeline" && (
                    <>
                      <p className="planning-section-note">
                        计划时间线仅描述未来事件，保留来源的时间标签，不擅自推算公历日期。
                      </p>
                      {data.timeline.length ? (
                        <ol className="planning-timeline">
                          {data.timeline.map((event) => (
                            <li key={event.id}>
                              <div className="planning-time-label">
                                <span>{event.time || "时间待确认"}</span>
                                <small>
                                  {event.chapter == null
                                    ? "章节待确认"
                                    : `计划第 ${event.chapter} 章`}
                                </small>
                              </div>
                              <RecordCard
                                record={event}
                                onOpen={onSource}
                                aside={<span className="tag">计划事件</span>}
                              >
                                <p className="planning-event-characters">
                                  涉及人物：
                                  <ReferenceNames
                                    ids={event.character_ids}
                                    records={data.characters}
                                  />
                                </p>
                              </RecordCard>
                            </li>
                          ))}
                        </ol>
                      ) : (
                        <Empty
                          compact
                          title="时间线尚待规划"
                          description="尚未提供计划事件，不会根据章号补出故事时间。"
                        />
                      )}
                    </>
                  )}
                  {tab === "settings" &&
                    (data.settings.length ? (
                      <div className="planning-records">
                        {data.settings.map((setting) => (
                          <RecordCard
                            key={setting.id}
                            record={setting}
                            onOpen={onSource}
                            aside={
                              <span className="tag">
                                {categoryLabels[setting.category] ||
                                  setting.category}
                              </span>
                            }
                          />
                        ))}
                      </div>
                    ) : (
                      <Empty
                        compact
                        title="故事设定尚待整理"
                        description="世界、地点、组织、物品、规则与题材知识将在有据可依后记录。"
                      />
                    ))}
                  {tab === "style" && (
                    <RecordCard record={data.style} onOpen={onSource}>
                      <div className="planning-style-columns">
                        <section>
                          <h4>建议遵循的写法</h4>
                          {data.style.rules.length ? (
                            <ul>
                              {data.style.rules.map((rule, index) => (
                                <li key={index}>{rule}</li>
                              ))}
                            </ul>
                          ) : (
                            <p>待作者确认</p>
                          )}
                        </section>
                        <section>
                          <h4>避免的表达与模式</h4>
                          {data.style.avoid.length ? (
                            <ul>
                              {data.style.avoid.map((rule, index) => (
                                <li key={index}>{rule}</li>
                              ))}
                            </ul>
                          ) : (
                            <p>待作者确认</p>
                          )}
                        </section>
                      </div>
                    </RecordCard>
                  )}
                  {tab === "unknowns" &&
                    (data.unknowns.length ? (
                      <ol className="planning-unknowns">
                        {data.unknowns.map((unknown, index) => (
                          <li key={index}>
                            <span>{String(index + 1).padStart(2, "0")}</span>
                            <p>{unknown}</p>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <Empty
                        compact
                        title="本包未列出待确认点"
                        description="这仅表示当前包没有列出问题，不代表整部作品不存在不确定之处。"
                      />
                    ))}
                </div>
              </>
            ) : (
              <details open className="planning-raw">
                <summary>查看保留的原始规划文本</summary>
                <pre>{selected.content || "未读取到文件正文"}</pre>
              </details>
            )}
          </>
        )}
      </section>
      <div className="planning-footnote">
        <ShieldCheck size={12} />
        规划包与章计划分别审批；正式记忆仍需独立提案，不随规划自动更新。
      </div>
      {editing && (
        <Modal
          title="修订候选规划 JSON"
          subtitle={editing.path}
          onClose={closeEditing}
          wide
        >
          <form
            className="modal-form"
            onSubmit={(event) => void saveEditing(event)}
          >
            <div className="form-note">
              <ShieldCheck size={14} />
              保存只更新候选，随后重新校验结构与来源。无效或失效的包仍不能提交审批。
            </div>
            {error && (
              <div className="inline-error" role="alert">
                <CircleAlert size={15} />
                <div>
                  {error}
                  <p>
                    输入仍保留。文件冲突时可先复制文字，再关闭、刷新并比较目标版本。
                  </p>
                </div>
              </div>
            )}
            <label>
              完整规划 JSON
              <textarea
                className="proposal-source"
                rows={20}
                value={editText}
                onChange={(event) => setEditText(event.target.value)}
                readOnly={busy === "save-package"}
                spellCheck={false}
                required
              />
            </label>
            <div className="modal-footer">
              <button
                type="button"
                className="button secondary"
                disabled={busy === "save-package"}
                onClick={closeEditing}
              >
                取消
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={!!busy || editText === editing.content}
              >
                {busy === "save-package" ? (
                  <LoaderCircle className="spin" size={14} />
                ) : (
                  <Save size={14} />
                )}
                保存并重新校验
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
