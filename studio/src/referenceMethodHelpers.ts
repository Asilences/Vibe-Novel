export type ResearchEvidence = { book_id?: string; edition_id?: string; chapter_id: string; start_char: number; end_char: number; quote?: string };
export type ResearchOrigin = {
  schema?: number; schema_version?: number; book_id: string; book_title?: string; edition_id: string;
  batch_id: string; node_id: string; result_hash: string; source_type?: string; study_id?: string; study_hash?: string; books?: {book_id:string;edition_id:string}[]; authorization_revision?: string | number;
  plan_hash?: string; method_indices?: number[]; evidence?: ResearchEvidence[]; raw_dependencies?: unknown;
  [key: string]: unknown;
};
export type ResearchMethod = { title: string; steps: string[]; limitations: string[]; source_refs: ResearchEvidence[] };
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

export function researchMethods(result: unknown): ResearchMethod[] {
  const value = record(result);
  if (!Array.isArray(value.methods)) return [];
  // Keep original positions: indices are bound to the saved node result, never a filtered/reordered list.
  return value.methods.slice(0, 6).map((item) => {
    const method = record(item);
    return { title: typeof method.title === "string" ? method.title : "未命名方法候选", steps: Array.isArray(method.steps) ? method.steps.filter((line): line is string => typeof line === "string") : [], limitations: Array.isArray(method.limitations) ? method.limitations.filter((line): line is string => typeof line === "string") : [], source_refs: researchEvidence(method.source_refs) };
  });
}

export function researchEvidence(value: unknown): ResearchEvidence[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const ref = record(item);
    return typeof ref.chapter_id === "string" && Number.isInteger(ref.start_char) && Number.isInteger(ref.end_char) && Number(ref.start_char) >= 0 && Number(ref.end_char) > Number(ref.start_char)
      ? [{ chapter_id: ref.chapter_id, start_char: Number(ref.start_char), end_char: Number(ref.end_char), ...(typeof ref.book_id === "string" ? {book_id:ref.book_id}:{}), ...(typeof ref.edition_id === "string" ? {edition_id:ref.edition_id}:{}), ...(typeof ref.quote === "string" ? { quote: ref.quote } : {}) }] : [];
  });
}

export function researchOrigin(value: unknown): ResearchOrigin | null {
  const origin = record(value);
  if (origin.source_type === "reference_study" && typeof origin.study_id === "string" && typeof origin.study_hash === "string" && Array.isArray(origin.books) && origin.books.length) return origin as ResearchOrigin;
  return ["book_id", "edition_id", "batch_id", "node_id", "result_hash"].every((key) => typeof origin[key] === "string" && origin[key]) ? origin as ResearchOrigin : null;
}

export function methodSelection(indices: number[], count: number): number[] {
  return [...new Set(indices)].filter((index) => Number.isInteger(index) && index >= 0 && index < count && index < 6).sort((left, right) => left - right);
}
