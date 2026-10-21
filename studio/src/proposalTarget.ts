type TargetJob = {
  kind: string;
  state: string;
  result_path?: string;
  request?: { plan_path?: string; document_path?: string };
};

export type ProposalTargetSuggestion = { target: string; note: string };

const chapterTarget = (digits: string | undefined): string =>
  digits && !/^0+$/.test(digits) ? `chapters/ch${digits.padStart(3, "0")}.md` : "";

/** A random candidate filename carries no chapter number. Follow only its saved
 * successful task lineage; never infer a destination from its text or job ID.
 */
export function suggestProposalTarget(path: string, jobs: readonly TargetJob[]): ProposalTargetSuggestion {
  if (/^outlines?\/.+\.md$/.test(path)) {
    return { target: `outlines/approved/${path.split("/").at(-1)}`, note: "按当前大纲文稿名称建议目标；请审阅与已批准版本的差异。" };
  }
  const seen = new Set<string>();
  let current = path;
  for (let depth = 0; depth < 16 && !seen.has(current); depth += 1) {
    seen.add(current);
    const explicit = current.match(/^(?:drafts|chapters)\/(?:[^/]+\/)*ch(\d+)\.md$/);
    const explicitTarget = chapterTarget(explicit?.[1]);
    if (explicitTarget) {
      const manuscript = /^drafts\/manuscript-[0-9a-f]{32}\/ch\d+\.md$/.test(current);
      return {
        target: explicitTarget,
        note: manuscript
          ? "按导入副本的切分序号建议目标；切分序号不等于原稿章号，请核对原稿起始章、序言和番外的位置。"
          : `${current === path ? "按当前文稿" : "按改写任务固定来源文稿"}的文件章号建议目标；请核对本次实际章节。`,
      };
    }
    const linked = jobs.filter((job) => job.result_path === current && job.state === "succeeded");
    if (linked.length !== 1) break;
    const job = linked[0];
    if (job.kind === "draft") {
      const plan = job.request?.plan_path;
      const target = chapterTarget(plan?.match(/^outlines\/approved\/ch(\d{3,})\.md$/)?.[1]);
      if (target) return { target, note: `根据${current === path ? "本候选" : "改写来源候选"}生成任务冻结的章计划 ${plan} 建议目标；请审阅目标文件与差异。` };
      break;
    }
    if (job.kind !== "rewrite" || !job.request?.document_path) break;
    current = job.request.document_path;
  }
  return { target: "", note: "无法从文稿名称或已保存的任务来源确定正式章号。请填写目标文件并点击“核对目标”，不会默认替换第一章。" };
}
