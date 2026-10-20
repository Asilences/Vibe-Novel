type BatchFlowInput = {
  hasSelection: boolean;
  hasBillingProject: boolean;
  budget: string;
  outputLimit: number;
  outbound?: string;
  withdrawn: boolean;
  policyDirty: boolean;
  uncertain: boolean;
  previewCurrent: boolean;
  previewHash?: string;
  savedCurrent: boolean;
  batchState?: string;
};

export function referenceBatchFlow(input: BatchFlowInput) {
  const authorizationReasons = input.policyDirty ? ["请先保存“核对与外发”中的授权修改。"]
    : input.withdrawn ? ["这本书的模型使用授权已撤回，请先到“核对与外发”处理。"]
    : input.outbound !== "excerpts" ? ["请先到“核对与外发”允许发送所选章节片段，并保存授权。"] : [];
  const previewReasons = [...authorizationReasons];
  if (!input.hasSelection) previewReasons.push("请选择要研究的章段。");
  if (!input.hasBillingProject) previewReasons.push("请选择提供模型策略与共享预算的计费作品。");
  if (!input.budget.trim() || !Number.isFinite(Number(input.budget)) || Number(input.budget) <= 0) previewReasons.push("请填写大于 0 的本批美元上限。");
  if (!Number.isInteger(input.outputLimit) || input.outputLimit < 256 || input.outputLimit > 16000) previewReasons.push("每节点输出 Token 上限须为 256–16000 的整数。");
  if (input.uncertain) previewReasons.push("上次保存结果尚不明确，请沿原提交键核对批次后再操作。");
  const saveReasons = [...authorizationReasons];
  if (!input.previewCurrent || !input.previewHash) saveReasons.push("请先完成当前范围、模型与预算的预览；修改输入后须重新预览。");
  const startReasons = [...authorizationReasons];
  if (!input.savedCurrent) startReasons.push("请先预览并保存本次批次；保存完成后才能明确启动付费研究。");
  else if (["running", "pausing"].includes(input.batchState || "")) startReasons.push(input.batchState === "pausing" ? "当前请求结束后将暂停；暂停完成后可以继续。" : "本次批次正在分析，可在下方暂停后续节点。");
  else if (!["prepared", "queued", "ready", "paused"].includes(input.batchState || "")) startReasons.push("本次批次当前不能启动，请在下方核对状态与错误记录；不自动重发结果不明的请求。");
  return {
    previewReasons, saveReasons, startReasons,
    canPreview: previewReasons.length === 0,
    canSave: saveReasons.length === 0,
    canStart: startReasons.length === 0,
  };
}
