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
  capabilities: { providers: Provider[]; tasks?: unknown[] };
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
  handoff_prompt?: string;
  context_sources?: Record<string, string>;
  recovery_available?: boolean;
  recovery_candidate?: string;
};
export type Imported = {
  import_id: string;
  original_name: string;
  source_hash?: string;
  extracted_hash?: string;
  created_at?: string;
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
  awaiting_input: "等待接力结果",
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
