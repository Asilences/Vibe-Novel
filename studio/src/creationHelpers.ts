export type BriefTag = {
  id: string; label: string; source: "catalog" | "author" | "ai" | "external";
  decision: "selected" | "pending" | "excluded" | "undecided"; dimensions: string[];
};
export type CreativeBrief = {
  schema_version: 1; novel_id: string; catalog_version: string; reading_category: string; story_seed: string;
  tags: BriefTag[];
  protagonists: { id: string; name: string; role: string; goal: string; flaw: string }[];
  narration: { ensemble: string; person: string; focus: string };
  advantage: { status: "undecided" | "no" | "yes"; kind: string; ability: string; limits: string; cost: string; reveal: string };
  opening: { era: string; time: string; place: string; situation: string; inciting_incident: string; obstacle: string };
  scale: { target_words_min: number | null; target_words_max: number | null; volume_count: number | null; chapter_words: number | null; ending: string };
  style: { tone: string; pace: string; dialogue: string };
  constraints: { hard: string[]; preferences: string[]; avoid: string[] }; unresolved: string[];
};
export type CreationCatalog = {
  catalog_version: string;
  categories: { id: string; label: string }[];
  dimensions: { id: string; label: string }[];
  tags: { id: string; label: string; dimensions: string[] }[];
  limits: { story_seed: number; text: number; short_text: number; tag_label: number; tags_per_dimension: number; protagonists: number; list_items: number; max_bytes: number };
  genres: { id: string; label: string; capability: "specialized" | "general" }[];
};
export type BriefEnvelope = { brief: CreativeBrief; path: string; hash: string | null; version_id: string | null };
export type CreationSelectionRecord = {
  schema_version: 1; novel_id: string; document_path: string; document_hash: string; document_version_id: string;
  brief_hash: string | null; brief_version_id: string | null; job_id: string | null; selected_at: string; invalidated_at: string | null;
};
export type CreationSelection = { selection: CreationSelectionRecord | null; path: string; hash: string | null; version_id: string | null; stale: boolean; drift: string[] };
export type CreationPreview = {
  route?: Record<string, unknown>; budget?: Record<string, unknown>; warnings?: string[];
  [key: string]: unknown;
};

export function setTagDecision(tags: BriefTag[], term: { id: string; label: string; dimensions: string[] }, decision: "selected" | "excluded"): BriefTag[] {
  const existing = tags.find((item) => item.id === term.id);
  if (existing?.decision === decision) return tags.filter((item) => item.id !== term.id);
  const next: BriefTag = { ...term, source: existing?.source || "catalog", decision, dimensions: [...term.dimensions] };
  return [...tags.filter((item) => item.id !== term.id), next];
}

export function selectedTags(tags: BriefTag[], dimension?: string): BriefTag[] {
  return tags.filter((tag) => tag.decision === "selected" && (!dimension || tag.dimensions.includes(dimension)));
}

export function addAuthorTag(tags: BriefTag[], catalog: CreationCatalog, label: string, dimension: string, decision: "selected" | "excluded", newId: string): BriefTag[] {
  const term = catalog.tags.find((item) => item.label === label.trim());
  const existing = tags.find((item) => term ? item.id === term.id : item.label === label.trim());
  const next: BriefTag = term
    ? { ...term, source: existing?.source || "catalog", decision }
    : existing
      ? { ...existing, decision, dimensions: [...new Set([...existing.dimensions, dimension])] }
      : { id: newId, label: label.trim(), source: "author", decision, dimensions: [dimension] };
  return [...tags.filter((item) => item.id !== next.id), next];
}

export function generationIdentity(envelope: Pick<BriefEnvelope, "path" | "hash" | "version_id">): string {
  return JSON.stringify([envelope.path, envelope.hash, envelope.version_id]);
}

export function outlineSourceChanged(source: { document_path: string; document_hash: string } | null, documents: { path: string; hash: string }[]): boolean {
  return !!source && !documents.some((item) => item.path === source.document_path && item.hash === source.document_hash);
}

export function previewConfiguration(preview: CreationPreview): Record<string, unknown> | null {
  const route = preview.route || preview;
  const candidates = route.candidates;
  if (!Array.isArray(candidates) || !candidates.length || !candidates[0] || typeof candidates[0] !== "object") return null;
  const first = candidates[0] as Record<string, unknown>;
  if (!("policy_revision" in route) || !("policy_hash" in route) || !("registry_revision" in route) || !("registry_hash" in first) || !("legacy_marker" in first)) return null;
  return { policy_revision: route.policy_revision, policy_hash: route.policy_hash, registry_revision: route.registry_revision, registry_hash: first.registry_hash, registry_legacy_marker: first.legacy_marker };
}

export function briefWarnings(brief: CreativeBrief): string[] {
  const warnings: string[] = [];
  if (brief.scale.target_words_min !== null && brief.scale.target_words_max !== null && brief.scale.target_words_min > brief.scale.target_words_max)
    warnings.push("目标字数下限高于上限，请核对；系统不会替你改值。");
  if (brief.advantage.status === "no" && selectedTags(brief.tags).some((item) => /系统|外挂|金手指/.test(item.label)))
    warnings.push("你选择了无特殊优势，同时包含系统或外挂相关标签，请说明两者如何兼容，或调整选择。");
  if (selectedTags(brief.tags).some((tag) => tag.id === "world:realistic") && selectedTags(brief.tags).some((tag) => tag.id === "world:supernatural"))
    warnings.push("写实与超自然同时选中，可以是混合题材；请补充世界规则。");
  return warnings;
}

export function briefFormIssues(brief: CreativeBrief, catalog: CreationCatalog): string[] {
  const issues: string[] = [];
  for (const dimension of catalog.dimensions) {
    if (brief.tags.filter((tag) => tag.dimensions.includes(dimension.id)).length > catalog.limits.tags_per_dimension)
      issues.push(`${dimension.label}最多保留 ${catalog.limits.tags_per_dimension} 个标签，请移除部分标签后再保存。`);
  }
  const lists = [["不能改变的条件", brief.constraints.hard], ["可协商的偏好", brief.constraints.preferences], ["避免的套路或内容", brief.constraints.avoid], ["留待讨论的问题", brief.unresolved]] as const;
  for (const [label, items] of lists) {
    if (items.length > catalog.limits.list_items) issues.push(`${label}最多 ${catalog.limits.list_items} 项，请分批整理。`);
    if (items.some((item) => Array.from(item).length > 1000)) issues.push(`${label}每项最多 1000 字，请拆分长段。`);
  }
  const numbers = [["目标总字数下限", brief.scale.target_words_min, 100000000], ["目标总字数上限", brief.scale.target_words_max, 100000000], ["预计卷数", brief.scale.volume_count, 1000], ["单章目标字数", brief.scale.chapter_words, 100000]] as const;
  for (const [label, value, max] of numbers) if (value !== null && (!Number.isInteger(value) || value < 1 || value > max)) issues.push(`${label}应为 1–${max.toLocaleString()} 的整数，或留空表示未定。`);
  return issues;
}

export function listText(items: string[]): string { return items.join("\n"); }
export function textList(value: string): string[] { return value.split("\n").filter((line) => line.trim().length > 0); }
