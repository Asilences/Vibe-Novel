export type ImportEncoding = "auto" | "gb18030" | "utf-16";
export type ImportIntent = "planning" | "manuscript" | "general";
export type ImportSourceSummary = { import_id: string; original_name: string; source_hash?: string; extracted_hash?: string };

const isImportId = (value: unknown): value is string => typeof value === "string" && value.length === 64 && /^[0-9a-f]+$/.test(value);

export function importedSourcePath(item: { import_id: string }): string {
  if (!isImportId(item?.import_id)) throw new Error("导入来源 ID 必须是完整的小写 64 位十六进制字符串。");
  return `imports/original/${item.import_id}/extracted.md`;
}

export function planningSourceLabel(path: string, imports: ImportSourceSummary[]): string {
  const item = imports.find((candidate) => isImportId(candidate.import_id) && importedSourcePath(candidate) === path);
  return item?.original_name?.trim() ? `${item.original_name} · ${item.import_id.slice(0, 8)}` : path;
}

export function initialImportEncoding(): ImportEncoding {
  return "auto";
}

export function encodingRequest(encoding: ImportEncoding): { encoding?: "gb18030" | "utf-16" } {
  if (encoding === "auto") return {};
  if (encoding === "gb18030" || encoding === "utf-16") return { encoding };
  throw new Error("不支持的导入编码。");
}
