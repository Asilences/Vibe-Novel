import type { DocumentItem, Job } from "./api";
import { suggestProposalTarget } from "./proposalTarget";

export type ChapterSection = "canonical" | "drafts";
export type ChapterSort = "chapter" | "chapter_desc" | "title" | "characters";
export type ChapterRow = {
  document: DocumentItem;
  section: ChapterSection;
  title: string;
  number: number | null;
  numberLabel: string;
  numberNote: string;
  characters: number | null;
  countNote: string;
  status: string;
};

function chapterNumber(path: string): number | null {
  const digits = path.match(/(?:^|\/)ch(\d+)\.md$/)?.[1];
  const value = digits ? Number(digits) : NaN;
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

export function chapterRows(documents: readonly DocumentItem[], jobs: readonly Job[]): ChapterRow[] {
  return documents.filter((document) => /^(chapters|drafts)\/.+\.md$/.test(document.path)).map((document) => {
    const section: ChapterSection = document.path.startsWith("chapters/") ? "canonical" : "drafts";
    const imported = /^drafts\/manuscript-[0-9a-f]{32}\//.test(document.path);
    const fileNumber = chapterNumber(document.path);
    const suggestion = fileNumber === null && section === "drafts" ? suggestProposalTarget(document.path, jobs) : null;
    const number = fileNumber ?? (suggestion ? chapterNumber(suggestion.target) : null);
    const producers = jobs.filter((job) => job.result_path === document.path && job.state === "succeeded");
    const producer = producers.length === 1 ? producers[0] : null;
    const characters = document.summary_status === "available" && typeof document.character_count === "number"
      && Number.isSafeInteger(document.character_count) && document.character_count >= 0 ? document.character_count : null;
    return {
      document, section,
      title: document.title?.trim() || document.path.split("/").at(-1)!.replace(/\.md$/, ""),
      number,
      numberLabel: number === null ? "未标章号" : imported ? `切分 ${number}` : `第 ${number} 章`,
      numberNote: imported ? "这是导入副本的切分序号，请核对原稿章号。"
        : fileNumber !== null ? "按文件章号显示。"
          : number !== null ? suggestion!.note : "未从文件名或唯一成功任务来源确认章号。",
      characters,
      countNote: characters !== null ? "已保存文本的非空白字符数，含标题、Markdown 标记及标点。"
        : document.summary_status === "too_large" ? "文稿超过统计上限，未计算字数。"
          : document.summary_status === "invalid_utf8" ? "文件不是有效 UTF-8 文本，未计算字数。"
            : "当前服务未返回文本统计，请升级或刷新后查看。",
      status: section === "canonical" ? "正式正文 · 只读"
        : producer?.result_hash && producer.result_hash === document.hash ? "AI 候选 · 未定稿"
          : producer?.result_hash && producer.result_hash !== document.hash ? "候选已编辑 · 未定稿"
            : imported ? "导入副本 · 未定稿" : "工作草稿 · 未定稿",
    };
  });
}

export function selectChapterRows(rows: readonly ChapterRow[], section: ChapterSection, search: string, sort: ChapterSort): ChapterRow[] {
  const query = search.trim().toLocaleLowerCase("zh-CN");
  return rows.filter((row) => row.section === section && (!query || [row.title, row.document.path, row.numberLabel, row.number?.toString() || ""].some((value) => value.toLocaleLowerCase("zh-CN").includes(query))))
    .sort((a, b) => {
      if (sort === "title") return a.title.localeCompare(b.title, "zh-CN", { numeric: true }) || a.document.path.localeCompare(b.document.path);
      if (sort === "characters" && a.characters !== b.characters) return a.characters === null ? 1 : b.characters === null ? -1 : b.characters - a.characters;
      if (a.number !== b.number) return a.number === null ? 1 : b.number === null ? -1 : sort === "chapter_desc" ? b.number - a.number : a.number - b.number;
      return a.document.path.localeCompare(b.document.path, "zh-CN", { numeric: true });
    });
}

export function chapterTotals(rows: readonly ChapterRow[], section: ChapterSection) {
  const selected = rows.filter((row) => row.section === section);
  return { count: selected.length, characters: selected.reduce((total, row) => total + (row.characters ?? 0), 0), unknown: selected.filter((row) => row.characters === null).length };
}

export function nextDraftPath(documents: readonly DocumentItem[]): string {
  const occupied = new Set(documents.map((document) => document.path));
  const numbered = documents.filter((document) => /^(chapters|drafts)\/ch\d+\.md$/.test(document.path)).map((document) => chapterNumber(document.path) || 0);
  const maximum = Math.max(0, ...numbered);
  let number = maximum < Number.MAX_SAFE_INTEGER ? maximum + 1 : 1;
  while (occupied.has(`drafts/ch${number.toString().padStart(3, "0")}.md`)) number += 1;
  return `drafts/ch${number.toString().padStart(3, "0")}.md`;
}
