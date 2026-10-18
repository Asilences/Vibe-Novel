export type Project = {
  novel_id: string;
  title: string;
  genre: string;
  counts?: Record<string, number>;
};
export type Provider = {
  id: string;
  label: string;
  available: boolean;
  reason?: string;
};
export type Bootstrap = {
  version: string;
  csrf_token: string;
  capabilities: {
    providers: Provider[];
    tasks?: unknown[];
    default_provider?: "api";
  };
};
export type DocumentItem = {
  path: string;
  kind: string;
  hash: string;
  bytes: number;
  readonly?: boolean;
};
export type Document = DocumentItem & { content: string; revision?: number };
export type Version = {
  id: string;
  version?: number;
  hash: string;
  created_at: string;
  content?: string;
  provenance?: unknown;
};
export type Approval = {
  id: string;
  target: string;
  before: string | null;
  after: string;
  after_hash?: string;
  reason: string;
  impact: string[];
  state: string;
  base_revision?: number;
  created_at?: string;
};
export type Job = {
  id: string;
  kind: string;
  provider: string;
  state: string;
  created_at: string;
  result_path?: string;
  error?: string;
  request?: { instruction?: string; document_path?: string };
  logs?: Array<string | Record<string, unknown>>;
  usage?: Record<string, unknown> | null;
  cost_usd?: number | null;
  context_sources?: Record<string, string>;
  recovery_available?: boolean;
  recovery_candidate?: string;
  repair_candidate?: string;
};
export type Imported = {
  import_id: string;
  original_name: string;
  source_hash?: string;
  extracted_hash?: string;
  created_at?: string;
};

export type PlanningEvidence = {
  path: string;
  start_line: number;
  end_line: number;
  quote: string;
};
export type PlanningRecord = {
  id: string;
  title: string;
  summary: string;
  basis: "source_asserted" | "inferred" | "suggested";
  evidence: PlanningEvidence[];
};
export type PlanningVolume = PlanningRecord & {
  number: number;
  chapter_start: number;
  chapter_end: number | null;
};
export type PlanningChapter = PlanningRecord & {
  number: number;
  volume_id: string;
  goal: string;
  conflict: string;
  turn: string;
  outcome: string;
  beats: string[];
  character_ids: string[];
  depends_on: string[];
  thread_ids: string[];
};
export type PlanningCharacter = PlanningRecord & {
  role: string;
  knowledge: string;
};
export type PlanningRelationship = PlanningRecord & {
  from_id: string;
  to_id: string;
};
export type PlanningThread = PlanningRecord & {
  setup_chapter: number | null;
  payoff_chapter: number | null;
};
export type PlanningEvent = PlanningRecord & {
  time: string;
  chapter: number | null;
  character_ids: string[];
  status: "planned";
};
export type Blueprint = {
  schema_version: 1;
  novel_id: string;
  source_hashes: Record<string, string>;
  book: PlanningRecord;
  volumes: PlanningVolume[];
  chapters: PlanningChapter[];
  characters: PlanningCharacter[];
  relationships: PlanningRelationship[];
  threads: PlanningThread[];
  timeline: PlanningEvent[];
  settings: (PlanningRecord & {
    category:
      | "world"
      | "location"
      | "organization"
      | "item"
      | "rule"
      | "domain";
  })[];
  style: PlanningRecord & { rules: string[]; avoid: string[] };
  unknowns: string[];
};
export type PlanningPackage = {
  path: string;
  hash: string | null;
  content: string;
  data: Blueprint | null;
  valid: boolean;
  issues: string[];
  approved: boolean;
  stale: boolean;
};
export type PlanningPackageSummary = Omit<PlanningPackage, "content" | "data">;

export type AnalysisMetrics = Record<string, number>;
export type StyleBaseline = {
  novel_id: string;
  configured: boolean;
  path: string;
  hash: string | null;
  config_hash: string | null;
  samples: { path: string; hash: string; current_hash: string | null; stale: boolean }[];
  metrics: AnalysisMetrics | null;
  stale: boolean;
  limitations: string[];
};
export type AnalysisIssue = {
  id: string;
  rule: string;
  severity: string;
  start_line: number;
  end_line: number;
  quote: string;
  message: string;
  suggestion: string;
  status: "pending" | "resolved" | "ignored";
};
export type AnalysisReport = {
  id: string;
  novel_id: string;
  path: string;
  hash: string;
  source: { path: string; hash: string; current_hash: string | null };
  dependencies?: Record<string, string>;
  stale: boolean;
  stale_reasons: string[];
  metrics: AnalysisMetrics;
  baseline: Record<string, unknown> | null;
  issues: AnalysisIssue[];
  coverage: Record<string, unknown>;
  limitations: string[];
};
export type AnalysisSearch = {
  results: { path: string; hash: string; start_line: number; end_line: number; quote: string; score: number }[];
  scanned: number;
  omitted: unknown[];
  limitations: string[];
};
export type ChapterBrief = {
  sections: { id: string; label: string; status: string; paths: string[]; content: string; truncated?: boolean }[];
  sources: Record<string, string>;
  missing: { id: string; label: string; paths: string[]; reason: string }[];
  omitted: { id: string; label: string; paths: string[]; reason: string }[];
  limitations: string[];
  text: string;
  chapter?: number;
  budget?: { characters: number; limit: number };
};
export type APISettingsData = {
  hash: string | null;
  default_provider: "api";
  api: { base_url: string; model: string; has_api_key: boolean; allow_loopback: boolean };
  environment_overrides: string[];
  presets: { id: string; label: string; base_url: string; models: { id: string; label: string }[] }[];
};

let token = "";
export const setToken = (value: string) => {
  token = value;
};
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(method !== "GET"
        ? { "Content-Type": "application/json", "X-VibeNovel-Token": token }
        : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : `请求失败（${response.status}）`,
      response.status,
    );
  }
  return payload as T;
}
export const projectUrl = (id: string) => `/projects/${encodeURIComponent(id)}`;
export function download(filename: string, content: string) {
  const url = URL.createObjectURL(
    new Blob([content], { type: "text/markdown;charset=utf-8" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const leaf = (path: string) => path.split("/").at(-1) || path;
export const formatDate = (date?: string) =>
  date
    ? new Date(date).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
export const kindLabels: Record<string, string> = {
  build_blueprint: "生成创作规划",
  expand_outline: "扩写大纲",
  draft: "章节起草",
  review: "独立审稿",
  rewrite: "局部改写",
};
export const stateLabels: Record<string, string> = {
  pending: "待作者审批",
  approved: "已批准 · 待应用",
  rejected: "已驳回",
  applied: "已应用",
  queued: "排队中",
  running: "执行中",
  awaiting_input: "等待修复结果",
  succeeded: "已完成",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  cancelling: "正在取消",
  interrupted: "已中断",
  paused: "已暂停",
};
export const activeStates = new Set([
  "queued",
  "running",
  "awaiting_input",
  "cancelling",
  "paused",
]);
