import { useMemo, useState } from "react";
import { ArrowRight, BookOpen, Download, FileText, FolderOpen, LockKeyhole, Plus, RefreshCw, Search, ShieldCheck } from "lucide-react";
import type { Approval, DocumentItem, Job, Project } from "./api";
import { chapterRows, chapterTotals, selectChapterRows } from "./chapterManagement";
import type { ChapterSection, ChapterSort } from "./chapterManagement";
import "./chapters.css";

type Props = {
  project: Project;
  documents: DocumentItem[];
  jobs: Job[];
  approvals: Approval[];
  busy: boolean;
  unsavedPath?: string;
  onOpen: (document: DocumentItem) => void;
  onNew: () => void;
  onExport: () => void;
  onRefresh: () => void;
  onApprovals: () => void;
};

export default function ChaptersWorkspace({ project, documents, jobs, approvals, busy, unsavedPath, onOpen, onNew, onExport, onRefresh, onApprovals }: Props) {
  const [section, setSection] = useState<ChapterSection>("canonical");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<ChapterSort>("chapter");
  const rows = useMemo(() => chapterRows(documents, jobs), [documents, jobs]);
  const canonical = chapterTotals(rows, "canonical");
  const drafts = chapterTotals(rows, "drafts");
  const visible = useMemo(() => selectChapterRows(rows, section, search, sort), [rows, section, search, sort]);
  const chapterApprovals = approvals.filter((approval) => approval.target.startsWith("chapters/") && ["pending", "approved"].includes(approval.state));
  const totalLabel = (total: ReturnType<typeof chapterTotals>) => `${total.characters.toLocaleString()} 字${total.unknown ? `（另 ${total.unknown} 篇未统计）` : ""}`;

  return <div className="chapters-workspace scroll-area">
    <div className="page-heading chapters-heading">
      <div><div className="eyebrow">MANUSCRIPT DESK</div><h1>章节管理</h1><p>{project.title} · 集中查看正文、草稿与待审变更</p></div>
      <div className="chapters-heading-actions">
        <button type="button" className="button secondary small" disabled={busy} onClick={onRefresh}><RefreshCw size={14} />刷新</button>
        <button type="button" className="button secondary small" disabled={busy || canonical.count === 0} onClick={onExport}><Download size={14} />导出正式章节</button>
        <button type="button" className="button primary small" disabled={busy} onClick={onNew}><Plus size={15} />新建草稿</button>
      </div>
    </div>
    <section className="chapters-summary" aria-label="章节统计">
      <div><BookOpen size={19} /><span>正式章节<strong>{canonical.count}<small>篇</small></strong><em>{totalLabel(canonical)}</em></span></div>
      <div><FolderOpen size={19} /><span>草稿箱<strong>{drafts.count}<small>篇</small></strong><em>{totalLabel(drafts)}</em></span></div>
      <button type="button" disabled={busy} onClick={onApprovals}><ShieldCheck size={19} /><span>待处理章节提案<strong>{chapterApprovals.length}<small>项</small></strong><em>审阅、批准后单独应用</em></span><ArrowRight size={16} /></button>
    </section>
    <section className="chapters-panel" aria-label="章节列表">
      <div className="chapters-list-toolbar">
        <div className="chapters-tabs" role="group" aria-label="选择章节分类">
          <button type="button" aria-pressed={section === "canonical"} className={section === "canonical" ? "selected" : ""} onClick={() => setSection("canonical")}><BookOpen size={15} />正式章节<span>{canonical.count}</span></button>
          <button type="button" aria-pressed={section === "drafts"} className={section === "drafts" ? "selected" : ""} onClick={() => setSection("drafts")}><FolderOpen size={15} />草稿箱<span>{drafts.count}</span></button>
        </div>
        <label className="chapters-search"><Search size={15} /><input type="search" aria-label="搜索章节标题、章号或文件路径" placeholder="搜索标题、章号或文件路径" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <label className="chapters-sort"><span>排序</span><select aria-label="章节排序" value={sort} onChange={(event) => setSort(event.target.value as ChapterSort)}><option value="chapter">章号升序</option><option value="chapter_desc">章号降序</option><option value="title">标题</option><option value="characters">字数从多到少</option></select></label>
      </div>
      <p className="chapters-list-caption">{section === "canonical" ? <><LockKeyhole size={13} />正式正文只读。打开后可提出修改，审批应用前保留当前版本。</> : <><FileText size={13} />草稿、AI 候选与导入修订副本保留为独立文稿；不会自动替换正式章节。</>}</p>
      {visible.length ? <div className="chapters-table-wrap"><table className="chapters-table"><thead><tr><th scope="col">章号</th><th scope="col">章节标题</th><th scope="col">字数</th><th scope="col">当前状态</th><th scope="col">操作</th></tr></thead><tbody>{visible.map((row) => <tr key={row.document.path}>
        <td className="chapters-number" title={row.numberNote}>{row.numberLabel}{row.number !== null && row.document.path.startsWith("drafts/") && !/\/ch\d+\.md$/.test(row.document.path) && <small>按任务章计划</small>}</td>
        <td className="chapters-title-cell"><button type="button" disabled={busy} onClick={() => onOpen(row.document)}>{row.title}</button><small>{row.document.path}</small></td>
        <td className="chapters-count" title={row.countNote}>{row.characters === null ? "未统计" : row.characters.toLocaleString()}</td>
        <td><span className={`chapters-state ${row.section === "canonical" ? "canonical" : ""}`}>{row.status}</span>{unsavedPath === row.document.path && <small className="chapters-unsaved">编辑器有未保存修改</small>}</td>
        <td><button type="button" className="button secondary small" disabled={busy} onClick={() => onOpen(row.document)}>{row.section === "canonical" ? "阅读正式章" : "打开草稿"}<ArrowRight size={13} /></button></td>
      </tr>)}</tbody></table></div> : <div className="chapters-empty"><FileText size={29} strokeWidth={1.3} /><h2>{search.trim() ? "没有匹配的章节" : section === "canonical" ? "还没有正式章节" : "草稿箱还是空的"}</h2><p>{search.trim() ? "试着调整标题、章号或路径关键词。" : section === "canonical" ? "先在草稿箱打开文稿，提交定稿提案；作者批准并应用后显示在这里。" : "新建草稿开始写作，也可以从「章节写作」生成候选，或在「已有小说修订」导入正文。"}</p>{search.trim() ? <button type="button" className="button secondary small" onClick={() => setSearch("")}>清除搜索</button> : section === "canonical" && drafts.count > 0 ? <button type="button" className="button primary small" onClick={() => setSection("drafts")}>查看草稿箱<ArrowRight size={14} /></button> : <button type="button" className="button primary small" disabled={busy} onClick={onNew}><Plus size={14} />新建草稿</button>}</div>}
      <div className="chapters-footnote">当前显示 {visible.length} / {section === "canonical" ? canonical.count : drafts.count} 篇。字数按已保存文本的非空白字符统计，含标题、Markdown 标记与标点；未保存修改不计入。这里记录本地稿件状态。</div>
    </section>
  </div>;
}
