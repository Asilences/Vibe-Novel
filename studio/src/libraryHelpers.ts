import type { LibraryEntry, LibraryMethod, LibraryOverview, LibraryScope, LibrarySourceRef } from "./api";

export function libraryViewForScope<T extends string>(view: T, scope: LibraryScope, hasProject: boolean): T | "all" {
  return (scope !== "book" || !hasProject) && (view === "bindings" || view === "policy") ? "all" : view;
}

const readableFields: Record<string, string> = {
  title: "", summary: "摘要", content: "整理", purpose: "用途", steps: "步骤", inputs: "输入", outputs: "输出",
  limitations: "边界", applies_to: "适用情境", counterexamples: "反例", checks: "检查", description: "说明",
  observation: "观察", rationale: "依据", prompt: "合成练习", expected: "预期观察", gaps: "待补资料",
  method: "方法", reading_map: "阅读地图", observations: "结构观察", directions: "可提炼方向", references: "普通参考",
  source_cases: "原书案例", synthetic_exercises: "合成练习", checks_status: "核对状态",
};

// Search quotes also contain serialized storage metadata. Render only reading
// content from the matching fixed version; provenance remains in its detail view.
function readableContent(value: unknown, label = ""): string[] {
  if (typeof value === "string") return value.trim() ? [label ? `${label}：${value}` : value] : [];
  if (Array.isArray(value)) return value.flatMap((item) => readableContent(item, label));
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).flatMap(([key, item]) => key in readableFields ? readableContent(item, readableFields[key]) : []);
}

function contentLines(entry: LibraryEntry): string[] {
  let content: unknown = entry.content;
  try { content = JSON.parse(entry.content); } catch { /* Author prose is already readable. */ }
  return [...new Set([
    ...readableContent(entry.summary), ...readableContent(content), ...readableContent(entry.method),
    ...entry.source_refs.flatMap((ref) => ref.quote ? [`原文依据：${ref.quote}`] : []),
  ])];
}

function matchingExcerpt(lines: string[], query: string): string {
  const wanted = query.trim().toLocaleLowerCase();
  const terms = (wanted.match(/[a-z0-9_]+|[\u3400-\u9fff]+/g) || []).flatMap((word) => /^[\u3400-\u9fff]/.test(word) && word.length > 1 ? Array.from({ length: word.length - 1 }, (_, index) => word.slice(index, index + 2)) : [word]);
  let best = lines[0] || "", bestScore = -1, offset = 0;
  for (const line of lines) {
    const lower = line.toLocaleLowerCase(), exact = wanted ? lower.indexOf(wanted) : -1;
    const matches = terms.filter((term) => lower.includes(term));
    const score = (exact >= 0 ? 1000 : 0) + matches.length;
    if (score > bestScore) { best = line; bestScore = score; offset = exact >= 0 ? exact : matches.length ? Math.min(...matches.map((term) => lower.indexOf(term))) : 0; }
  }
  const start = Math.max(0, Array.from(best.slice(0, offset)).length - 45), chars = Array.from(best);
  return `${start ? "…" : ""}${chars.slice(start, start + 200).join("")}${chars.length > start + 200 ? "…" : ""}`;
}

export function librarySearchExcerpt(hit: Record<string, unknown>, overview: LibraryOverview, query: string): string {
  if (hit.kind === "source") return Array.from(typeof hit.quote === "string" ? hit.quote : "").slice(0, 200).join("");
  const binding = hit.kind === "binding" ? overview.bindings.find((item) => item.id === hit.id && item.version_id === hit.version_id) : undefined;
  const entry = hit.kind === "entry" ? overview.entries.find((item) => item.id === hit.id && item.version_id === hit.version_id) : binding?.snapshot || binding?.entry;
  if (!entry) return "命中此资料；打开固定版本查看正文与出处。";
  return matchingExcerpt(contentLines(entry), query) || "打开固定版本查看整理内容与出处。";
}

export const emptyMethod = (): LibraryMethod => ({ purpose: "", steps: [], inputs: [], outputs: [], limitations: [], applies_to: [], counterexamples: [], checks: [] });
export const emptyLibraryEntry = (type: LibraryEntry["type"] = "material"): LibraryEntry => ({ id: "", type, title: "", content: "", tags: [], summary: "", outbound: "local", status: "inbox", verification: "unverified", provenance: "AUTHOR", source_refs: [], ...(type === "method" ? { method: emptyMethod() } : {}), hash: "", version_id: "", revision: 0 });

export function entryPayload(entry: LibraryEntry): Record<string, unknown> {
  return { ...(entry.id ? { id: entry.id } : {}), type: entry.type, title: entry.title, content: methodContent(entry) ? JSON.stringify({ ...methodContent(entry), method: entry.method || emptyMethod() }) : entry.content, tags: entry.tags, summary: entry.summary, outbound: entry.outbound, status: entry.status, verification: entry.verification, provenance: entry.provenance, withdrawn: !!entry.withdrawn, withdrawal_reason: entry.withdrawal_reason || "", source_refs: entry.source_refs, ...(entry.type === "method" ? { method: entry.method || emptyMethod() } : {}), ...(entry.analysis ? { analysis: entry.analysis } : {}), ...(entry.research_origin ? { research_origin: entry.research_origin } : {}) };
}

export function sourceExcerpt(text: string, start: number, end: number): string {
  return Array.from(text).slice(start, end).join("");
}

export function sourceLineAt(text: string, character: number): number {
  const prefix = sourceExcerpt(text, 0, character);
  return prefix.split(/\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]/).length;
}

export function sourceReference(source: { id: string; version_id: string }, text: string, start: number, end: number): LibrarySourceRef {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > Array.from(text).length) throw new Error("请选择有效的原文范围。");
  return { source_id: source.id, source_version_id: source.version_id, start_char: start, end_char: end, quote: sourceExcerpt(text, start, end) };
}

export function bytesBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let output = "";
  for (let offset = 0; offset < bytes.length; offset += 16384) output += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
  return btoa(output);
}

export function libraryTaskConfiguration(route: Record<string, unknown>): Record<string, unknown> | null {
  const candidates = route.candidates;
  if (!Array.isArray(candidates) || !candidates[0] || typeof candidates[0] !== "object") return null;
  const first = candidates[0] as Record<string, unknown>;
  if (!("policy_revision" in route) || !("policy_hash" in route) || !("registry_revision" in route) || !("registry_hash" in first) || !("legacy_marker" in first)) return null;
  return { policy_revision: route.policy_revision, policy_hash: route.policy_hash, registry_revision: route.registry_revision, registry_hash: first.registry_hash, registry_legacy_marker: first.legacy_marker };
}

export function libraryPreviewHash(route: Record<string, unknown>): string | undefined {
  const preview = route.method_preview || route.library_preview;
  if (!preview || typeof preview !== "object") return undefined;
  const snapshot = (preview as Record<string, unknown>).snapshot;
  if (!snapshot || typeof snapshot !== "object") return undefined;
  const hash = (snapshot as Record<string, unknown>).hash;
  return typeof hash === "string" ? hash : undefined;
}

export function methodContent(entry: LibraryEntry): Record<string, unknown> | null {
  if (entry.type !== "method") return null;
  try {
    const value: unknown = JSON.parse(entry.content);
    return value && typeof value === "object" && !Array.isArray(value) && "method" in value ? value as Record<string, unknown> : null;
  } catch { return null; }
}
