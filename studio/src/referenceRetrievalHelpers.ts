export const retrievalLayers = ["original", "structure", "expression", "method"] as const;
export type RetrievalLayer = typeof retrievalLayers[number];
export const retrievalLayerLabels: Record<RetrievalLayer, string> = { original: "原文层", structure: "内容结构层", expression: "表达层", method: "已采用方法层" };

export function selectedRetrievalIds(ids: string[], results: { id: string }[]): string[] {
  const available = new Set(results.map((item) => item.id));
  return [...new Set(ids)].filter((id) => available.has(id)).slice(0, 30);
}

export function retrievalIdentity(indexId: string, query: string, layers: string[], semantic: boolean, relationships: boolean) {
  return JSON.stringify([indexId, query, [...layers].sort(), semantic, relationships]);
}

export function retrievalSelection(indexId: string, ids: string[], hash: string) {
  if (!/^[a-f0-9]{32}$/.test(indexId) || !/^[a-f0-9]{64}$/.test(hash) || !ids.length || ids.length > 30 || ids.some((id) => !/^[a-f0-9]{32}$/.test(id)) || new Set(ids).size !== ids.length) throw new Error("检索选择缺少精确索引、片段或预览指纹");
  return { retrieval_selection: { index_id: indexId, ids, expected_preview: hash } };
}
