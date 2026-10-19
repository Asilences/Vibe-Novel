export type Project = {
  novel_id: string;
  title: string;
  genre: string;
  counts?: Record<string, number>;
  canonical_revision?: number;
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
  request?: { instruction?: string; document_path?: string; document_hash?: string; document_version_id?: string; selection?: { start_char: number; end_char: number }; editorial?: { goals: string[]; intensity: "conservative" | "standard" | "deep" } };
  logs?: Array<string | Record<string, unknown>>;
  usage?: Record<string, unknown> | null;
  cost_usd?: number | null;
  route?: Record<string, unknown> | null;
  attempts?: Array<Record<string, unknown>>;
  budget_summary?: Record<string, unknown> | null;
  context_sources?: Record<string, string>;
  recovery_available?: boolean;
  recovery_candidate?: string;
  repair_candidate?: string;
  result_hash?: string;
  result_version_id?: string;
  editorial_context?: { selection?: { start_char: number; end_char: number }; goals: string[]; intensity: string; sources: Record<string, string>; omitted: unknown[]; runtime_sources?: Record<string, string>; prompt_budget?: { characters: number; limit: number } };
};
export type ModelRole = "planner" | "writer" | "reviewer" | "memory";
export type ModelProfile = {
  id: string; label: string; base_url: string; model: string; allow_loopback: boolean;
  roles: ModelRole[]; context_tokens: number; max_output_tokens: number;
  price_input_usd: number | null; price_output_usd: number | null; has_api_key: boolean;
};
export type ModelRegistry = {
  hash: string | null; revision: number; migration_required: boolean;
  profiles: ModelProfile[]; presets: APISettingsData["presets"];
};
export type RoutingPolicy = {
  bindings: Record<ModelRole, { primary: string; fallbacks: string[] }>;
  max_calls: number; max_tokens: number; max_cost_usd: number | null; max_attempts: number;
};
export type RoutingSettings = { hash: string | null; revision: number; policy: RoutingPolicy };
export type ModelWorkflow = {
  id: string; state: string; created_at?: string; error?: string;
  nodes: { id?: string; job_id?: string; kind: string; state: string; result_path?: string; result_hash?: string; result_version_id?: string; route?: Record<string, unknown> }[];
  budget_summary?: Record<string, unknown>; request?: Record<string, unknown>;
  revision_rounds?: number; independent_review?: boolean;
  stale?: boolean; drift?: string | null;
};
export type EvidenceReview = { target_path: string; target_hash: string; target_version_id: string | null; unchecked: string[]; issues: { id: string; quote: string; start_char: number; end_char: number; severity: string; category: string; explanation: string; suggestion: string }[] };
export type Benchmark = { id: string; hash: string; created_at: string; stale: boolean; status: "awaiting_vote" | "voted"; candidates?: { label: "A" | "B"; content: string; hash: string }[]; vote?: { decision: "prefer_a" | "prefer_b" | "tie"; note: string; at: string } | null; limitations: string[] };
export type Imported = {
  import_id: string;
  original_name: string;
  source_hash?: string;
  extracted_hash?: string;
  created_at?: string;
};
export type ManuscriptSection = { id: string; order: number; title: string; start_line: number; end_line: number; start_char: number; end_char: number; characters: number };
export type ManuscriptPreview = { novel_id: string; source: { path: string; hash: string; characters: number; lines: number }; sections: ManuscriptSection[]; warnings: string[]; limitations: string[] };
export type ManuscriptSession = { schema_version: number; id: string; novel_id: string; created_at: string; source: ManuscriptPreview["source"]; sections: (ManuscriptSection & { path: string; original_hash: string; current_hash: string | null; changed: boolean; accepted: boolean })[]; hash: string; path: string; stale: boolean; stale_reasons: string[]; limitations: string[] };
export type ManuscriptSummary = { id: string; created_at: string; source: ManuscriptPreview["source"]; hash: string; path: string; sections?: number | ManuscriptSession["sections"] };

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
  related?: { path: string; hash: string; start_line: number; end_line: number; quote: string };
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
export type AnalysisVerification = {
  report: AnalysisReport | null;
  previous: { path: string; hash: string; source: { path: string; hash: string } };
  results: { issue_id: string; rule: string; previous_status: AnalysisIssue["status"]; status: "remaining" | "not_found" | "unchecked"; matched_issue_ids: string[]; reason: string }[];
  limitations: string[];
};
export type AnalysisSearch = {
  results: { path: string; hash: string; start_line: number; end_line: number; quote: string; score: number }[];
  scanned: number;
  omitted: unknown[];
  limitations: string[];
  canonical_revision?: number;
  mode?: "literal" | "semantic" | "local_embedding";
  indexed_chunks?: number;
  reused_chunks?: number;
  model?: string;
  revision?: string;
};
export type SemanticStatus = { available: boolean; model_present: boolean; dependencies_present: boolean; model: string; revision: string; reason: string; setup_command: string; loaded: boolean };
export type ChapterBrief = {
  sections: { id: string; label: string; status: string; paths: string[]; content: string; truncated?: boolean }[];
  sources: Record<string, string>;
  missing: { id: string; label: string; paths: string[]; reason: string }[];
  omitted: { id: string; label: string; paths: string[]; reason: string }[];
  limitations: string[];
  text: string;
  chapter?: number;
  budget?: { characters: number; limit: number };
  canonical_revision?: number;
  input_manifest?: { path: string; hash: string; role: string; ranges: Record<string, unknown>[]; characters: number; section_id: string }[];
};
export type AnalysisEvidence = { path: string; hash: string; start_line: number; end_line: number; quote: string };
export type AnalysisToolkit = {
  schema_version: number;
  novel_id: string;
  configured: boolean;
  hash: string | null;
  lexicon: { id: string; term: string; aliases: string[]; forbidden: string[]; required: boolean; note: string }[];
  characters: { id: string; name: string; aliases: string[]; max_absence: number }[];
  threads: { id: string; label: string; terms: string[]; start_chapter: number; remind_after: number; end_chapter: number | null }[];
  style: { instructions: string; avoid: string[]; anchors: (AnalysisEvidence & { current_hash: string | null; stale: boolean })[] };
  stale: boolean;
};
export type AnalysisStructure = {
  novel_id: string;
  canonical_revision: number;
  toolkit_hash: string | null;
  scanned: number;
  omitted: { path: string; reason: string }[];
  characters: { id: string; name: string; mentioned_chapters: number[]; last_mentioned: number | null; absence: number | null; status: string; evidence: AnalysisEvidence[] }[];
  threads: { id: string; label: string; start_chapter: number; end_chapter: number | null; last_mentioned: number | null; span: number | null; status: string; evidence: AnalysisEvidence[] }[];
  outline: { path: string; hash: string; chapter: number | null; has_chapter: boolean | null }[];
  keywords: { term: string; count: number }[];
  limitations: string[];
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
  generate_story_outline: "生成初版总纲",
  build_blueprint: "生成创作规划",
  expand_outline: "扩写大纲",
  draft: "章节起草",
  review: "独立审稿",
  rewrite: "局部改写",
  review_evidence: "证据审稿",
  memory_delta: "候选记忆抽取",
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
  awaiting_advance: "等待启动下一节点",
  ready: "候选流程已完成",
};
export const activeStates = new Set([
  "queued",
  "running",
  "awaiting_input",
  "cancelling",
  "paused",
]);
