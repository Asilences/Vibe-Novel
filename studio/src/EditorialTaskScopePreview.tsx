export const editorialGoalOptions = [{ id: "preserve_voice", label: "保留原有文风" }, { id: "remove_cliches", label: "减少套话" }, { id: "reduce_repetition", label: "减少重复表达" }, { id: "show_actions", label: "增加动作与感知" }, { id: "natural_dialogue", label: "自然对话" }];
const intensityLabels: Record<string, string> = { conservative: "保守", standard: "标准", deep: "深入" };

/** Describe the frozen request, never the still-editable form behind its modal. */
export default function EditorialTaskScopePreview({ request, quote, characters }: { request: Record<string, unknown>; quote?: string; characters?: number }) {
  const selection = request.selection as { start_char: number; end_char: number } | undefined;
  const editorial = request.editorial as { goals: string[]; intensity: string } | undefined;
  if (!selection && !editorial) return null;
  return <section className="analysis-limits" aria-label="冻结的审稿与改写范围">
    <h3>本次处理范围</h3>
    <p>{selection ? `仅选区 [${selection.start_char}, ${selection.end_char}) · ${selection.end_char - selection.start_char} 个 Unicode 字符` : `整章：当前已保存文稿全文${characters === undefined ? "" : `，${characters} 字符`}`}</p>
    <p>编辑目标：{editorial?.goals.map((goal) => editorialGoalOptions.find((option) => option.id === goal)?.label || goal).join("、") || "按作者说明"} · 强度：{intensityLabels[editorial?.intensity || ""] || "未指定"}</p>
    <p>来源：{String(request.document_path || "未指定")} · 版本 {String(request.document_hash || "未指定").slice(0, 12)}</p>
    {selection && quote !== undefined && <details open><summary>核对本次选区原句</summary><pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere",maxHeight:200,overflow:"auto"}}>{quote}</pre></details>}
    <p>区间从 0 开始，起点包含、终点不包含。{request.kind === "rewrite" && selection ? "生成后由后端保留区间之外的原文，再保存完整修订候选。" : ""}</p>
  </section>;
}
