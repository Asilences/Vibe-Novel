import type { Job } from "./api";

type RetryJob = Job & { workflow_id?: string | null };
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const nonnegative = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const maximum = (values: unknown[]): number => Math.max(0, ...values.map(nonnegative).filter((value): value is number => value !== null));

/** Explain known reasons a generic retry cannot proceed. The server remains authoritative. */
export function jobRetryGate(job: RetryJob, jobs: readonly Job[]): string | null {
  if (job.recovery_available) return "模型已经返回候选，请先恢复已保存结果；重复调用不能覆盖已有候选。";
  if (job.provider !== "api") return "旧协作任务仅保留历史，请返回原文重新预览 API 任务。";
  if (job.workflow_id) return "工作流节点不能从任务页重复调用，请返回原多模型流程核对状态与费用。";
  if (job.kind === "generate_story_outline") return "请返回构思页重新预览，并明确生成新的初版总纲。";
  if (["analyze_reference", "extract_methods"].includes(job.kind)) return "请返回资料中心重新核对来源、模型和共享预算。";
  if (!["failed", "cancelled", "interrupted"].includes(job.state)) return "只有失败、取消或中断的任务可以重试。";
  const route = record(job.route), group = route.group_id;
  if (typeof group !== "string" || !group) return "旧任务未冻结完整模型与预算，请返回原文重新预览。";
  const siblings = [job, ...jobs.filter((item) => item.id !== job.id && record(item.route).group_id === group)];
  // Failed reservations may leave the newest job without a summary. Earlier
  // snapshots still retain the group's consumed or uncertain allowance.
  const summaries = siblings.map((item) => record(item.budget_summary));
  const budget = record(route.budget);
  const calls = maximum(summaries.map((item) => item.calls));
  const maxCalls = nonnegative(budget.max_calls);
  if (maxCalls !== null && calls + 1 > maxCalls) return "原流程调用次数已用尽，不确定费用仍占用额度。请保留旧记录，核对后重新预览独立新任务。";
  const input = nonnegative(route.estimated_input_tokens);
  const candidate = record(Array.isArray(route.candidates) ? route.candidates[0] : null);
  const output = nonnegative(candidate.max_output_tokens) ?? nonnegative(route.max_output_tokens);
  const tokens = maximum(summaries.map((item) => item.tokens)), maxTokens = nonnegative(budget.max_tokens);
  if (input !== null && output !== null && maxTokens !== null && tokens + input + output > maxTokens) return "原流程剩余 Token 预算不足，重试沿用原额度。请核对后返回原文重新预览。";
  const maxCost = nonnegative(budget.max_cost_usd), inputPrice = nonnegative(candidate.price_input_usd), outputPrice = nonnegative(candidate.price_output_usd);
  const reserved = maximum(summaries.map((item) => item.reserved_cost_usd ?? item.cost_usd));
  if (maxCost !== null && input !== null && output !== null && inputPrice !== null && outputPrice !== null && reserved + (input * inputPrice + output * outputPrice) / 1_000_000 > maxCost) return "原流程剩余金额预算不足，未知费用不会自动释放。请核对费用后重新预览独立新任务。";
  const attempts = new Set(siblings.flatMap((item) => (item.attempts || []).map((attempt) => attempt.id).filter((id): id is string => typeof id === "string")));
  if (attempts.size >= 32) return "原流程已保留32次尝试，不能继续追加。请保留记录并重新预览独立新任务。";
  return null;
}
