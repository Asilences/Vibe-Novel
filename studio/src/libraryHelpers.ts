import type { LibraryEntry, LibraryMethod, LibrarySourceRef } from "./api";

export const emptyMethod = (): LibraryMethod => ({ purpose: "", steps: [], inputs: [], outputs: [], limitations: [], applies_to: [], counterexamples: [], checks: [] });
export const emptyLibraryEntry = (type: LibraryEntry["type"] = "material"): LibraryEntry => ({ id: "", type, title: "", content: "", tags: [], summary: "", outbound: "local", status: "inbox", verification: "unverified", provenance: "AUTHOR", source_refs: [], ...(type === "method" ? { method: emptyMethod() } : {}), hash: "", version_id: "", revision: 0 });

export function entryPayload(entry: LibraryEntry): Record<string, unknown> {
  return { ...(entry.id ? { id: entry.id } : {}), type: entry.type, title: entry.title, content: methodContent(entry) ? JSON.stringify({ ...methodContent(entry), method: entry.method || emptyMethod() }) : entry.content, tags: entry.tags, summary: entry.summary, outbound: entry.outbound, status: entry.status, verification: entry.verification, provenance: entry.provenance, withdrawn: !!entry.withdrawn, withdrawal_reason: entry.withdrawal_reason || "", source_refs: entry.source_refs, ...(entry.type === "method" ? { method: entry.method || emptyMethod() } : {}), ...(entry.analysis ? { analysis: entry.analysis } : {}) };
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
