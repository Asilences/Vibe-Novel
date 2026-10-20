export type StudyEvidence = { book_id: string; edition_id: string; chapter_id: string; start_char: number; end_char: number; quote: string };
export type StudyGraphNode = { id: string; label: string; description: string; source_refs: StudyEvidence[]; read_index: number; kind?: string; status?: string; tension?: number; valence?: number };
export type StudyGraphEdge = { from: string; to: string; label: string; source_refs: StudyEvidence[]; read_index: number };
export type StudyGraph = { nodes: StudyGraphNode[]; edges: StudyGraphEdge[] };

export function graphWindow(graph: StudyGraph, offset: number, limit: number, checkpoint = Number.MAX_SAFE_INTEGER) {
  const eligible = graph.nodes.filter((node) => node.read_index <= checkpoint);
  const nodes = eligible.slice(Math.max(0, offset), Math.max(0, offset) + Math.max(1, limit));
  const ids = new Set(nodes.map((node) => node.id));
  return { nodes, edges: graph.edges.filter((edge) => edge.read_index <= checkpoint && ids.has(edge.from) && ids.has(edge.to)),
    total: eligible.length, totalEdges: graph.edges.filter((edge) => edge.read_index <= checkpoint).length };
}

export function studyEvidence(value: unknown): StudyEvidence[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is StudyEvidence => !!item && typeof item === "object" &&
    ["book_id", "edition_id", "chapter_id", "quote"].every((key) => typeof item[key] === "string") &&
    Number.isSafeInteger(item.start_char) && Number.isSafeInteger(item.end_char) && item.start_char >= 0 && item.end_char > item.start_char)
    .map((item) => ({book_id: item.book_id, edition_id: item.edition_id, chapter_id: item.chapter_id, start_char: item.start_char, end_char: item.end_char, quote: item.quote}));
}

export function studyMethods(results: {result: Record<string, unknown>}[]) {
  return results.flatMap((record) => Array.isArray(record.result.methods) ? record.result.methods : [])
    .map((value, index) => ({index, value: value as {title: string; steps: string[]; limitations: string[]; source_refs: StudyEvidence[]}}));
}
