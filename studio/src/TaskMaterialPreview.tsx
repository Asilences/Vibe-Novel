/** Show the exact supplementary library input frozen by the task preview. */
export default function TaskMaterialPreview({ route }: { route: Record<string, unknown> }) {
  const context = route.library_preview as { mode?: string; snapshot?: { hash?: string; text?: string; manifest?: Record<string, unknown>[] }; recommendations?: Record<string, unknown>[] } | undefined;
  const snapshot = context?.snapshot;
  return <details className="analysis-limits" open><summary>本次资料与方法</summary>
    <p>{context?.mode === "auto" ? "按本书已保存的自动策略选取。" : context?.mode === "recommend" ? "仅推荐，不会自动发送推荐内容。" : "仅使用本次明确选择的资料。"}资料仅作辅助参考，不改变故事事实与审批权限。</p>
    {!snapshot && <p>本次未向模型补充资料库内容。</p>}
    {snapshot && <><ul>{snapshot.manifest?.map((item, index) => <li key={index}>{String(item.title || "选择记录")} · {item.included === false ? "未发送" : "已纳入"} · {String(item.reason || "作者选择")}{typeof item.evidence_note === "string" && <p>{item.evidence_note}</p>}</li>)}</ul><details><summary>实际补充文本与版本</summary><small>固定资料版本 {snapshot.hash?.slice(0, 12)}</small><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 280, overflow: "auto" }}>{snapshot.text || "没有可用的补充资料，继续使用任务本身的来源。"}</pre></details></>}
    {!!context?.recommendations?.length && <p>供参考：{context.recommendations.map((item) => String(item.title || item.id)).join("、")}。请在资料中心核对与选择。</p>}
  </details>;
}
