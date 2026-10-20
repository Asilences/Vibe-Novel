import type { LibraryEntry, LibraryOverview } from "./api";

export type MaterialPurpose = "planning" | "writing" | "review" | "rewrite" | "reference";
export type TaskMaterialOption = { id: string; key: string; kind: "entry" | "binding"; title: string; summary: string; type: string; outbound: string; version: string; disabledReason: string | null };
const keys = ["entry_ids", "binding_ids", "source_ranges", "query", "retrieval_selection"] as const;
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

export function materialIds(value: Record<string, unknown> | null, key: "entry_ids" | "binding_ids"): string[] {
  const ids = value?.[key];
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
}

/** Patch one selection group without losing the other explicit groups. */
export function mergeTaskMaterials(current: Record<string, unknown> | null, patch: Record<string, unknown>): Record<string, unknown> | null {
  const merged = { ...current, ...patch }, value: Record<string, unknown> = {};
  for (const key of keys) {
    if (merged[key] === undefined || merged[key] === null) continue;
    if (["entry_ids", "binding_ids", "source_ranges"].includes(key)) {
      if (!Array.isArray(merged[key])) throw new Error("任务资料选择格式无效，请重新勾选。");
      if ((merged[key] as unknown[]).length) value[key] = [...merged[key] as unknown[]];
    } else if (key === "query") {
      if (typeof merged[key] !== "string" || merged[key].length > 200) throw new Error("资料查询最多200字符。");
      if (merged[key].trim()) value[key] = merged[key];
    } else value[key] = merged[key];
  }
  return Object.keys(value).length ? value : null;
}

function entryReason(entry: LibraryEntry | undefined): string | null {
  if (!entry) return "固定资料快照缺失，请到资料中心重新选用";
  if (entry.withdrawn) return "资料已撤回";
  if (entry.status === "archived") return "资料已归档";
  if (entry.outbound === "local") return "仅本地保存，尚未允许模型使用";
  if (entry.outbound === "summary" && !entry.summary?.trim()) return "仅允许摘要外发，但尚未填写摘要";
  if (entry.type === "method" && !entry.adopted && !entry.adoption) return "方法尚未由作者采用";
  return null;
}

export function taskMaterialOptions(overview: LibraryOverview, purpose: MaterialPurpose): TaskMaterialOption[] {
  const entries: TaskMaterialOption[] = overview.entries.map((entry) => ({ id: entry.id, key: `entry:${entry.id}`, kind: "entry", title: entry.title, summary: entry.summary, type: entry.type, outbound: entry.outbound, version: entry.version_id, disabledReason: entryReason(entry) }));
  const bindings: TaskMaterialOption[] = overview.bindings.map((binding) => {
    const snapshot = binding.snapshot || binding.entry;
    const disabledReason = binding.withdrawn ? "本书选用已撤回" : binding.stale ? "本书固定来源已失效" : binding.purpose !== purpose && binding.purpose !== "reference" ? "本书选用未授权当前任务用途" : entryReason(snapshot);
    return { id: binding.id, key: `binding:${binding.id}`, kind: "binding", title: snapshot?.title || "固定个人资料", summary: snapshot?.summary || "", type: snapshot?.type || "material", outbound: snapshot?.outbound || "local", version: binding.version_id, disabledReason };
  });
  // Only this book's cards and explicitly bound personal versions are offered.
  return [...entries, ...bindings];
}

export function retrievalMaterialCount(value: Record<string, unknown> | null): number {
  const selection = record(value?.retrieval_selection);
  return Array.isArray(selection.ids) ? selection.ids.length : 0;
}
