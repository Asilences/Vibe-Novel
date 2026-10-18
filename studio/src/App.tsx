import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  BookOpen,
  LayoutDashboard,
  ListTree,
  PenLine,
  ShieldCheck,
  Library,
  Activity,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  ChevronDown,
  ChevronRight,
  ArrowUpRight,
  ArrowRight,
  FileText,
  FolderOpen,
  Upload,
  Download,
  Search,
  History,
  Save,
  LockKeyhole,
  Sparkles,
  CircleCheck,
  CircleAlert,
  Check,
  X,
  Copy,
  ExternalLink,
  RefreshCw,
  Play,
  Pause,
  Square,
  RotateCcw,
  Settings2,
  Feather,
  Layers3,
  CheckCheck,
  GitCompareArrows,
  LoaderCircle,
  Command,
  CornerDownLeft,
} from "lucide-react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import {
  api,
  activeStates,
  ApiError,
  download,
  formatDate,
  kindLabels,
  leaf,
  projectUrl,
  setToken,
  stateLabels,
} from "./api";
import type {
  Approval,
  Bootstrap,
  Document as NovelDocument,
  DocumentItem,
  Imported,
  Job,
  Project,
  Version,
} from "./api";
import { Diff, Empty, IconButton, Modal, StepLabel } from "./ui";

type Area =
  | "dashboard"
  | "outlines"
  | "editor"
  | "approvals"
  | "memory"
  | "tasks";
type ProposalDraft = {
  target: string;
  content: string;
  before: string;
  expected_hash: string | null;
  reason: string;
  impact: string;
};
const navItems = [
  { id: "dashboard", label: "作品总览", icon: LayoutDashboard },
  { id: "outlines", label: "故事大纲", icon: ListTree },
  { id: "editor", label: "章节编辑", icon: PenLine },
  { id: "approvals", label: "作者审批", icon: ShieldCheck },
  { id: "memory", label: "故事记忆", icon: Library },
  { id: "tasks", label: "任务记录", icon: Activity },
] as const;
const genreLabels: Record<string, string> = {
  football: "足球竞技",
  adventure: "冒险探索",
  campus: "校园青春",
  alternate_history: "架空历史",
};
const readonlyPath = (path: string) =>
  /^(chapters|memory|bible|outlines\/approved)\//.test(path);
const isOutline = (path: string) => /^(outline|outlines)\//.test(path);
const isMemory = (path: string) =>
  /^(memory|bible|config|references)\//.test(path) || path === "project.json";
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const wordCount = (content: string) => [...content.replace(/\s/g, "")].length;

export default function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [area, setArea] = useState<Area>("dashboard");
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [imports, setImports] = useState<Imported[]>([]);
  const [doc, setDoc] = useState<NovelDocument | null>(null);
  const [source, setSource] = useState("");
  const [preview, setPreview] = useState(false);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const [fatal, setFatal] = useState("");
  const [leftOpen, setLeftOpen] = useState(() => window.innerWidth > 540);
  const [rightOpen, setRightOpen] = useState(() => window.innerWidth > 1040);
  const [fileSearch, setFileSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showNewDoc, setShowNewDoc] = useState(false);
  const [newPath, setNewPath] = useState("");
  const [newDocContent, setNewDocContent] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [versions, setVersions] = useState<Version[]>([]);
  const [versionId, setVersionId] = useState("");
  const [proposal, setProposal] = useState<ProposalDraft | null>(null);
  const [verifiedTarget, setVerifiedTarget] = useState("");
  const [selectedApproval, setSelectedApproval] = useState("");
  const [proposalTab, setProposalTab] = useState<"diff" | "after">("diff");
  const [decisionNote, setDecisionNote] = useState("");
  const [provider, setProvider] = useState("api");
  const providerChosen = useRef(false);
  const [taskKind, setTaskKind] = useState("expand_outline");
  const [instruction, setInstruction] = useState("");
  const [planPath, setPlanPath] = useState("");
  const [selectedJob, setSelectedJob] = useState("");
  const [handoff, setHandoff] = useState<Job | null>(null);
  const [candidate, setCandidate] = useState("");
  const [showSources, setShowSources] = useState(false);
  const [importEncoding, setImportEncoding] = useState("auto");
  const [exportRange, setExportRange] = useState(false);
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const currentProject = useRef("");
  const docRequest = useRef(0);
  const sourceRef = useRef(source);
  sourceRef.current = source;
  const proposalRef = useRef(proposal);
  proposalRef.current = proposal;
  const proposalSession = useRef(0);
  const operationId = useRef(0);
  const operations = useRef(new Map<number, string>());
  const dirty = !!doc && source !== doc.content;
  const locked = !!doc && (doc.readonly || readonlyPath(doc.path));
  const pendingCount = approvals.filter(
    (item) => item.state === "pending",
  ).length;
  const runningCount = jobs.filter((item) =>
    activeStates.has(item.state),
  ).length;
  const notify = useCallback(
    (text: string, type: "success" | "error" = "success") =>
      setNotice({ text, type }),
    [],
  );
  const allowLeave = useCallback(
    () =>
      !dirty ||
      window.confirm(
        "当前文稿有未保存的修改。离开会放弃这些修改，仍要继续吗？",
      ),
    [dirty],
  );

  const refresh = useCallback(async (id: string) => {
    const base = projectUrl(id);
    const [ds, aps, js, imps] = await Promise.all([
      api<{ documents: DocumentItem[] }>(`${base}/documents`),
      api<{ approvals: Approval[] }>(`${base}/approvals`),
      api<{ jobs: Job[] }>(`${base}/jobs`),
      api<{ imports: Imported[] }>(`${base}/imports`),
    ]);
    if (currentProject.current !== id) return;
    setDocuments(ds.documents || []);
    setApprovals(aps.approvals || []);
    setJobs(js.jobs || []);
    setImports(imps.imports || []);
  }, []);

  const init = useCallback(async () => {
    setFatal("");
    setReady(false);
    try {
      const [boot, list] = await Promise.all([
        api<Bootstrap>("/bootstrap"),
        api<{ projects: Project[] }>("/projects"),
      ]);
      setBootstrap(boot);
      setToken(boot.csrf_token);
      setProjects(list.projects);
      if (!providerChosen.current) {
        setProvider(boot.capabilities.default_provider || "api");
      }
      setReady(true);
    } catch (error) {
      setFatal(errorText(error));
    }
  }, []);
  useEffect(() => {
    void init();
  }, [init]);
  useEffect(() => {
    if (!notice || notice.type === "error") return;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (
      !showHistory ||
      !project ||
      !doc ||
      !versionId ||
      versions.find((item) => item.id === versionId)?.content !== undefined
    )
      return;
    let current = true;
    void api<Version>(
      `${projectUrl(project.novel_id)}/history/version?path=${encodeURIComponent(doc.path)}&version_id=${encodeURIComponent(versionId)}`,
    )
      .then((result) => {
        if (!current) return;
        const version = result;
        setVersions((items) =>
          items.map((item) =>
            item.id === versionId ? { ...item, ...version } : item,
          ),
        );
      })
      .catch((error) => {
        if (current) notify(errorText(error), "error");
      });
    return () => {
      current = false;
    };
  }, [showHistory, project, doc, versionId, versions, notify]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);
  useEffect(() => {
    if (!project) return;
    const timer = setInterval(async () => {
      try {
        const response = await api<{ jobs: Job[] }>(
          `${projectUrl(project.novel_id)}/jobs`,
        );
        if (currentProject.current === project.novel_id) {
          setJobs(response.jobs);
          if (
            response.jobs.some(
              (job) =>
                job.result_path &&
                !documents.some((d) => d.path === job.result_path),
            )
          )
            void refresh(project.novel_id).catch(() => {});
        }
      } catch {
        /* Explicit actions surface errors. Background refresh retains the last known state. */
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [project, documents, refresh]);

  async function selectProject(id: string) {
    if (!allowLeave()) return;
    currentProject.current = id;
    docRequest.current += 1;
    setDoc(null);
    setSource("");
    setDocuments([]);
    setApprovals([]);
    setJobs([]);
    setImports([]);
    setArea("dashboard");
    const selected = projects.find((item) => item.novel_id === id) || null;
    setProject(selected);
    if (!id) return;
    await act("project", async () => {
      const detail = await api<{ project: Project }>(projectUrl(id));
      if (currentProject.current !== id) return;
      setProject(detail.project);
      await refresh(id);
    });
  }
  async function act(key: string, action: () => Promise<void>) {
    const id = ++operationId.current;
    operations.current.set(id, key);
    setBusy(key);
    try {
      await action();
    } catch (error) {
      notify(errorText(error), "error");
    } finally {
      operations.current.delete(id);
      setBusy([...operations.current.values()].at(-1) || "");
    }
  }
  async function openDocument(path: string, navigate = true, discard = false) {
    if (!project || ((path !== doc?.path || discard) && !allowLeave())) return;
    if (path === doc?.path && dirty && !discard) return;
    const request = ++docRequest.current;
    const id = project.novel_id;
    const sourceAtStart = sourceRef.current;
    await act("document", async () => {
      const result = await api<NovelDocument>(
        `${projectUrl(id)}/document?path=${encodeURIComponent(path)}`,
      );
      if (currentProject.current !== id || request !== docRequest.current)
        return;
      if (sourceRef.current !== sourceAtStart) {
        notify(
          "加载期间文稿有了新修改，已保留当前文字。保存后再打开目标文件。",
          "error",
        );
        return;
      }
      setDoc(result);
      setSource(result.content);
      setPreview(false);
      setShowSources(false);
      if (navigate)
        setArea(
          isOutline(path) ? "outlines" : isMemory(path) ? "memory" : "editor",
        );
    });
  }
  async function save() {
    if (!project || !doc || locked || !dirty || busy) return;
    const content = source;
    const request = docRequest.current;
    const id = project.novel_id;
    await act("save", async () => {
      try {
        const result = await api<NovelDocument>(
          `${projectUrl(project.novel_id)}/document`,
          "PUT",
          { path: doc.path, content, expected_hash: doc.hash },
        );
        if (currentProject.current !== id || request !== docRequest.current)
          return;
        setDoc(result);
        await refresh(id);
        notify("已保存到本地，历史版本已保留");
      } catch (error) {
        if (error instanceof ApiError && error.status === 409)
          throw new Error(
            "文件已被其他操作修改。你的文字仍在编辑器中，请先复制保留，再重新载入文件比较。",
          );
        throw error;
      }
    });
  }
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const input = {
      novel_id: String(fields.get("id")).trim(),
      title: String(fields.get("title")).trim(),
      genre: String(fields.get("genre")),
    };
    if (!allowLeave()) return;
    const sourceAtStart = sourceRef.current;
    const request = docRequest.current;
    const id = currentProject.current;
    await act("create", async () => {
      const result = await api<Project | { project: Project }>(
        "/projects",
        "POST",
        input,
      );
      const created = "project" in result ? result.project : result;
      setProjects((old) => [...old, created]);
      setShowCreate(false);
      if (
        currentProject.current !== id ||
        request !== docRequest.current ||
        sourceRef.current !== sourceAtStart
      ) {
        notify("新作品已创建，当前文稿保持打开。可在顶部切换作品。");
        return;
      }
      currentProject.current = created.novel_id;
      setProject(created);
      setDoc(null);
      setSource("");
      setArea("dashboard");
      await refresh(created.novel_id);
      notify("作品已创建。先导入或编写故事大纲。");
    });
  }
  function newDocument() {
    setNewPath(
      area === "outlines" ? "outlines/proposals/book.md" : "drafts/ch001.md",
    );
    setNewDocContent("");
    setShowNewDoc(true);
  }
  function adaptImported() {
    if (!doc) return;
    setNewPath("outlines/proposals/imported-outline.md");
    setNewDocContent(source);
    setShowNewDoc(true);
  }
  async function createDocument(event: FormEvent) {
    event.preventDefault();
    if (!project || !allowLeave()) return;
    const request = ++docRequest.current;
    const id = project.novel_id;
    const sourceAtStart = sourceRef.current;
    await act("new-document", async () => {
      const result = await api<NovelDocument>(
        `${projectUrl(project.novel_id)}/document`,
        "PUT",
        { path: newPath.trim(), content: newDocContent, expected_hash: null },
      );
      if (currentProject.current !== id || request !== docRequest.current)
        return;
      setShowNewDoc(false);
      if (sourceRef.current !== sourceAtStart) {
        await refresh(id);
        notify("新文稿已创建，当前编辑中的文字已保留。");
        return;
      }
      setDoc(result);
      setSource(result.content);
      setPreview(false);
      setArea(isOutline(result.path) ? "outlines" : "editor");
      await refresh(project.novel_id);
      notify("已创建空白文稿");
    });
  }
  async function importFile(file: File) {
    if (!project) return;
    if (!/\.(md|txt)$/i.test(file.name)) {
      notify("请选择 .md 或 .txt 文本文件", "error");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      notify("网页原件上限为 10 MiB，请拆分材料或通过 CLI 导入。", "error");
      return;
    }
    await act("import", async () => {
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("无法读取文件"));
        reader.readAsDataURL(file);
      });
      await api(`${projectUrl(project.novel_id)}/imports`, "POST", {
        filename: file.name,
        content_base64: data,
        ...(importEncoding !== "auto" ? { encoding: importEncoding } : {}),
      });
      await refresh(project.novel_id);
      setShowSources(true);
      notify("原始材料已导入，可在来源资料中查看");
    });
  }
  async function startProposal(target?: string, content?: string) {
    if (!project) return;
    const destination = target || doc?.path;
    if (!destination) return;
    const request = docRequest.current;
    const id = project.novel_id;
    const session = ++proposalSession.current;
    await act("proposal", async () => {
      let previous: NovelDocument | null = null;
      try {
        previous = await api<NovelDocument>(
          `${projectUrl(project.novel_id)}/document?path=${encodeURIComponent(destination)}`,
        );
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) throw error;
      }
      if (
        currentProject.current !== id ||
        request !== docRequest.current ||
        session !== proposalSession.current
      )
        return;
      setProposal({
        target: destination,
        content: content ?? source,
        before: previous?.content || "",
        expected_hash: previous?.hash || null,
        reason: "",
        impact: "",
      });
      setVerifiedTarget(destination);
    });
  }
  async function verifyProposalTarget() {
    if (!project || !proposal) return;
    const destination = proposal.target.trim();
    const session = proposalSession.current;
    const id = project.novel_id;
    if (
      !/^(chapters\/ch\d{3,}\.md|outlines\/approved\/.+\.md|bible\/.+\.md|memory\/.+\.md)$/.test(
        destination,
      )
    ) {
      notify(
        "目标须为 chapters/ch001.md、outlines/approved/*.md、bible/*.md 或 memory/*.md。",
        "error",
      );
      return;
    }
    await act("verify-target", async () => {
      let previous: NovelDocument | null = null;
      try {
        previous = await api<NovelDocument>(
          `${projectUrl(project.novel_id)}/document?path=${encodeURIComponent(destination)}`,
        );
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) throw error;
      }
      if (
        currentProject.current !== id ||
        proposalSession.current !== session ||
        !proposalRef.current ||
        proposalRef.current.target.trim() !== destination
      )
        return;
      setProposal((current) =>
        current && current.target.trim() === destination
          ? {
              ...current,
              target: destination,
              before: previous?.content || "",
              expected_hash: previous?.hash || null,
            }
          : current,
      );
      setVerifiedTarget(destination);
      notify(
        previous
          ? "已核对目标文件及版本"
          : "目标为新文件，应用时将检查文件是否仍未创建",
      );
    });
  }
  function closeProposal() {
    proposalSession.current += 1;
    proposalRef.current = null;
    setProposal(null);
    setVerifiedTarget("");
  }
  function formalTarget() {
    if (!doc) return "";
    if (isOutline(doc.path)) return `outlines/approved/${leaf(doc.path)}`;
    const chapter = leaf(doc.path).match(/ch(\d+)/i)?.[1] || "1";
    return `chapters/ch${chapter.padStart(3, "0")}.md`;
  }
  async function submitProposal(event: FormEvent) {
    event.preventDefault();
    if (!project || !proposal || verifiedTarget !== proposal.target) return;
    await act("submit-proposal", async () => {
      const result = await api<Approval>(
        `${projectUrl(project.novel_id)}/approvals`,
        "POST",
        {
          target: proposal.target,
          content: proposal.content,
          expected_hash: proposal.expected_hash,
          reason: proposal.reason.trim(),
          impact: proposal.impact
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean),
        },
      );
      closeProposal();
      await refresh(project.novel_id);
      setSelectedApproval(result.id);
      setArea("approvals");
      notify("提案已提交，等待作者审阅差异并决定");
    });
  }
  async function decide(item: Approval, decision: "approve" | "reject") {
    if (!project) return;
    await act(`decision-${item.id}`, async () => {
      await api(
        `${projectUrl(project.novel_id)}/approvals/${encodeURIComponent(item.id)}/decision`,
        "POST",
        { decision, note: decisionNote },
      );
      setDecisionNote("");
      await refresh(project.novel_id);
      notify(
        decision === "approve"
          ? "作者已批准。确认后可应用到目标文件。"
          : "提案已驳回",
      );
    });
  }
  async function applyProposal(item: Approval) {
    if (!project || !allowLeave()) return;
    const id = project.novel_id;
    const request = docRequest.current;
    const sourceAtStart = sourceRef.current;
    await act(`apply-${item.id}`, async () => {
      await api(
        `${projectUrl(id)}/approvals/${encodeURIComponent(item.id)}/apply`,
        "POST",
        {},
      );
      await refresh(id);
      if (currentProject.current !== id || request !== docRequest.current)
        return;
      if (doc?.path === item.target) {
        const result = await api<NovelDocument>(
          `${projectUrl(id)}/document?path=${encodeURIComponent(item.target)}`,
        );
        if (currentProject.current !== id || request !== docRequest.current)
          return;
        setDoc(result);
        if (sourceRef.current === sourceAtStart) setSource(result.content);
      }
      notify("提案已应用到目标文件，保留变更证据");
    });
  }
  async function showVersions() {
    if (!project || !doc) return;
    const request = docRequest.current;
    const id = project.novel_id;
    setVersions([]);
    setVersionId("");
    setShowHistory(true);
    await act("history", async () => {
      const result = await api<{ versions: Version[] }>(
        `${projectUrl(project.novel_id)}/history?path=${encodeURIComponent(doc.path)}`,
      );
      if (currentProject.current !== id || request !== docRequest.current)
        return;
      setVersions(result.versions);
      setVersionId(result.versions[0]?.id || "");
    });
  }
  async function restoreVersion() {
    if (!project || !doc || !versionId || locked || !allowLeave()) return;
    const request = docRequest.current;
    const id = project.novel_id;
    const sourceAtStart = source;
    await act("restore", async () => {
      const result = await api<NovelDocument>(
        `${projectUrl(project.novel_id)}/restore`,
        "POST",
        { path: doc.path, version_id: versionId, expected_hash: doc.hash },
      );
      if (currentProject.current !== id || request !== docRequest.current)
        return;
      setDoc(result);
      if (sourceRef.current === sourceAtStart) setSource(result.content);
      setShowHistory(false);
      await refresh(project.novel_id);
      notify("已恢复所选版本，恢复前的文字仍保留在历史中");
    });
  }
  async function startTask(event: FormEvent) {
    event.preventDefault();
    if (!project || !doc || !selectedProvider?.available || busy) return;
    if (dirty) {
      notify("请先保存当前文稿，让任务使用明确的已保存版本。", "error");
      return;
    }
    await act("task", async () => {
      const result = await api<Job>(
        `${projectUrl(project.novel_id)}/jobs`,
        "POST",
        {
          kind: taskKind,
          provider,
          document_path: doc.path,
          document_hash: doc.hash,
          instruction: instruction.trim(),
          ...(taskKind === "draft"
            ? {
                plan_path: planPath,
                plan_hash: approvedPlans.find((item) => item.path === planPath)
                  ?.hash,
              }
            : {}),
        },
      );
      await refresh(project.novel_id);
      setSelectedJob(result.id);
      setArea("tasks");
      if (result.state === "awaiting_input") {
        setHandoff(result);
        setCandidate("");
      }
      notify(
        result.state === "awaiting_input"
          ? "接力上下文已准备好，交给 Codex 后可回传候选结果。"
          : "任务已提交，可在任务记录中查看真实状态",
      );
    });
  }
  async function jobAction(
    job: Job,
    action: "cancel" | "retry" | "pause" | "resume" | "recover",
  ) {
    if (!project) return;
    await act(`job-${job.id}`, async () => {
      const result = await api<Job>(
        `${projectUrl(project.novel_id)}/jobs/${encodeURIComponent(job.id)}/${action}`,
        "POST",
        {},
      );
      await refresh(project.novel_id);
      if (result?.state === "awaiting_input") {
        setHandoff(result);
        setCandidate("");
      }
      notify(
        {
          cancel: "取消请求已提交",
          retry: "已创建重试任务",
          pause: "暂停请求已提交",
          resume: "任务已恢复排队",
          recover: "已恢复保存的候选，没有再次调用模型",
        }[action],
      );
    });
  }
  async function completeHandoff(event: FormEvent) {
    event.preventDefault();
    if (!project || !handoff) return;
    await act("handoff", async () => {
      await api(
        `${projectUrl(project.novel_id)}/jobs/${encodeURIComponent(handoff.id)}/complete`,
        "POST",
        { content: candidate },
      );
      setHandoff(null);
      setCandidate("");
      await refresh(project.novel_id);
      notify("接力结果已保存为候选文稿，未经审批不会成为正式正文。");
    });
  }
  async function exportBook(event?: FormEvent) {
    event?.preventDefault();
    if (!project) return;
    await act("export", async () => {
      const result = await api<{ filename: string; content: string }>(
        `${projectUrl(project.novel_id)}/export`,
        "POST",
        {
          ...(rangeStart ? { start: Number(rangeStart) } : {}),
          ...(rangeEnd ? { end: Number(rangeEnd) } : {}),
        },
      );
      download(result.filename, result.content);
      setExportRange(false);
      notify("已导出正式章节 Markdown");
    });
  }
  async function copy(content: string) {
    try {
      await navigator.clipboard.writeText(content);
      notify("已复制到剪贴板");
    } catch {
      notify("浏览器未允许复制，请使用下载文件。", "error");
    }
  }

  const filteredDocs = useMemo(
    () =>
      documents
        .filter((item) =>
          area === "outlines"
            ? isOutline(item.path)
            : area === "memory"
              ? isMemory(item.path)
              : !isOutline(item.path) &&
                !isMemory(item.path) &&
                !item.path.startsWith("imports/"),
        )
        .filter((item) =>
          item.path.toLowerCase().includes(fileSearch.toLowerCase()),
        )
        .sort((a, b) => a.path.localeCompare(b.path, "zh-CN")),
    [documents, area, fileSearch],
  );
  const headings = useMemo(
    () =>
      [...source.matchAll(/^(#{1,6})\s+(.+)$/gm)].map((match) => ({
        level: match[1].length,
        title: match[2],
        offset: match.index,
      })),
    [source],
  );
  const selectedVersion = versions.find((item) => item.id === versionId);
  const approval =
    approvals.find((item) => item.id === selectedApproval) || approvals[0];
  const selectedTask = jobs.find((item) => item.id === selectedJob) || jobs[0];
  const selectedProvider = bootstrap?.capabilities.providers.find(
    (item) => item.id === provider,
  );
  const approvedPlans = documents.filter((item) =>
    /^outlines\/approved\/ch[0-9]{3,}\.md$/.test(item.path),
  );
  const rendered = useMemo(
    () => DOMPurify.sanitize(marked.parse(source, { async: false }) as string),
    [source],
  );
  const editorArea =
    area === "outlines" || area === "editor" || area === "memory";

  return (
    <div
      className={`app ${!leftOpen ? "left-hidden" : ""} ${!rightOpen ? "right-hidden" : ""}`}
    >
      <a className="skip-link" href="#workspace">
        跳到工作区
      </a>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Feather size={21} strokeWidth={1.7} />
          </div>
          <span>
            VibeNovel<span className="brand-studio">STUDIO</span>
          </span>
        </div>
        <div className="top-divider" />
        <div className="project-select">
          <BookOpen size={15} />
          <select
            aria-label="选择作品"
            value={project?.novel_id || ""}
            disabled={!!busy}
            onChange={(event) => void selectProject(event.target.value)}
          >
            <option value="">选择作品</option>
            {projects.map((item) => (
              <option key={item.novel_id} value={item.novel_id}>
                {item.title}
              </option>
            ))}
          </select>
          <ChevronDown size={12} />
        </div>
        <IconButton
          label="创建新作品"
          onClick={() => setShowCreate(true)}
          disabled={!ready || !!busy}
        >
          <Plus size={16} />
        </IconButton>
        <div className="top-spacer" />
        <span className="local-indicator">
          <span
            className={ready ? "connection-dot" : "connection-dot offline"}
          />
          {ready ? "本地工作区" : fatal ? "连接失败" : "正在连接"}
        </span>
        <button
          className="button small secondary export-button"
          disabled={!project || !!busy}
          onClick={() => setExportRange(true)}
        >
          <Download size={14} />
          导出作品
        </button>
        <div className="author-avatar" title="由作者掌握定稿权">
          作
        </div>
      </header>
      <aside className="sidebar" aria-label="工作区导航">
        <div className="sidebar-top">
          <span>工作空间</span>
          <IconButton label="收起导航" onClick={() => setLeftOpen(false)}>
            <PanelLeftClose size={16} />
          </IconButton>
        </div>
        <nav>
          {navItems.map(({ id, label, icon: Icon }) => (
            <button
              className={`nav-item ${area === id ? "selected" : ""}`}
              key={id}
              onClick={() => setArea(id)}
            >
              <Icon size={18} strokeWidth={1.7} />
              <span>{label}</span>
              {id === "approvals" && pendingCount > 0 ? (
                <span className="nav-count orange">{pendingCount}</span>
              ) : id === "tasks" && runningCount > 0 ? (
                <span className="nav-count">{runningCount}</span>
              ) : null}
              {area === id && <span className="nav-active-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-rule" />
        {project && editorArea ? (
          <div className="file-browser">
            <div className="section-label">
              <span>
                {area === "outlines"
                  ? "大纲文件"
                  : area === "memory"
                    ? "设定与记忆"
                    : "章节与候选"}
              </span>
              {
                <IconButton
                  label={area === "memory" ? "新增设定提案" : "新建文稿"}
                  onClick={
                    area === "memory"
                      ? () => void startProposal("bible/characters.md", "")
                      : newDocument
                  }
                >
                  <Plus size={15} />
                </IconButton>
              }
            </div>
            <div className="search-field">
              <Search size={13} />
              <input
                aria-label="查找文件"
                placeholder="查找文件…"
                value={fileSearch}
                onChange={(event) => setFileSearch(event.target.value)}
              />
            </div>
            <div className="file-list">
              {filteredDocs.length ? (
                filteredDocs.map((item) => (
                  <button
                    title={item.path}
                    className={`file-item ${doc?.path === item.path ? "selected" : ""}`}
                    key={item.path}
                    disabled={!!busy}
                    onClick={() => void openDocument(item.path)}
                  >
                    {item.readonly || readonlyPath(item.path) ? (
                      <LockKeyhole size={13} />
                    ) : (
                      <FileText size={14} />
                    )}
                    <span>
                      <strong>{leaf(item.path)}</strong>
                      <small>
                        {item.path.split("/").slice(0, -1).join("/")}
                      </small>
                    </span>
                    {doc?.path === item.path && dirty && (
                      <i className="dirty-dot" />
                    )}
                  </button>
                ))
              ) : (
                <div className="sidebar-empty">
                  {fileSearch ? "没有匹配的文件" : "还没有文件"}
                  {!fileSearch && area !== "memory" && (
                    <button className="text-button" onClick={newDocument}>
                      创建第一份文稿 <Plus size={12} />
                    </button>
                  )}
                </div>
              )}
            </div>
            {doc && headings.length > 0 && (
              <div className="document-outline">
                <div className="section-label">文内目录</div>
                {headings.slice(0, 30).map((heading, index) => (
                  <button
                    key={index}
                    style={{ paddingLeft: `${10 + (heading.level - 1) * 9}px` }}
                    onClick={() => {
                      setPreview(false);
                      requestAnimationFrame(() => {
                        textarea.current?.focus();
                        textarea.current?.setSelectionRange(
                          heading.offset,
                          heading.offset,
                        );
                        const lines = source
                          .slice(0, heading.offset)
                          .split("\n").length;
                        if (textarea.current)
                          textarea.current.scrollTop = Math.max(
                            0,
                            (lines - 3) * 31,
                          );
                      });
                    }}
                  >
                    {heading.title}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="sidebar-note">
            <span className="eyebrow">AUTHOR FIRST</span>
            <p>
              灵感可以自由生长。
              <br />
              定稿，始终由你决定。
            </p>
            <div className="mini-book">
              <div />
              <div />
              <div />
            </div>
          </div>
        )}
        <div className="sidebar-bottom">
          <button
            onClick={() => {
              if (project) setShowSources(true);
            }}
            disabled={!project}
          >
            <FolderOpen size={16} />
            <span>来源资料</span>
            {imports.length > 0 && <small>{imports.length}</small>}
            <ArrowUpRight size={13} />
          </button>
          <div className="workspace-caption">
            <span className="connection-dot" />
            {bootstrap
              ? `v${bootstrap.version} · 文件保存在本地`
              : "VibeNovel Studio"}
          </div>
        </div>
      </aside>

      <main id="workspace" className="workspace" tabIndex={-1}>
        <div className="workspace-bar">
          <div className="breadcrumbs">
            {!leftOpen && (
              <IconButton label="展开导航" onClick={() => setLeftOpen(true)}>
                <PanelLeftOpen size={17} />
              </IconButton>
            )}
            <span>{project?.title || "我的工作区"}</span>
            <ChevronRight size={13} />
            <strong>{navItems.find((item) => item.id === area)?.label}</strong>
            {doc && editorArea && (
              <>
                <ChevronRight size={13} />
                <span className="breadcrumb-file">{leaf(doc.path)}</span>
              </>
            )}
          </div>
          <div className="workspace-actions">
            {busy && <LoaderCircle className="spin" size={14} />}
            {!rightOpen && (
              <IconButton
                label="展开创作助手"
                onClick={() => setRightOpen(true)}
              >
                <PanelRightOpen size={17} />
              </IconButton>
            )}
          </div>
        </div>
        {fatal ? (
          <Empty
            icon={<CircleAlert size={28} />}
            title="工作区暂时无法连接"
            description={`请确认本地服务已启动。${fatal}`}
            action={
              <button className="button primary" onClick={() => void init()}>
                <RefreshCw size={15} />
                重新连接
              </button>
            }
          />
        ) : !ready ? (
          <div className="loading-state">
            <LoaderCircle className="spin" size={22} />
            <p>正在打开作者工作台…</p>
          </div>
        ) : !project ? (
          <div className="onboarding">
            <div className="onboarding-kicker">
              <span />
              YOUR NEXT CHAPTER
            </div>
            <h1>
              让故事，
              <br />
              从一个想法开始<span>。</span>
            </h1>
            <p className="onboarding-description">
              把散落的灵感整理成大纲，在安静的工作台写下每一章。
              <br />
              AI 协助你展开故事，你掌握每一次定稿。
            </p>
            <div className="onboarding-actions">
              <button
                className="button primary"
                onClick={() => setShowCreate(true)}
              >
                <Plus size={17} />
                创建第一部作品
                <ArrowRight size={16} />
              </button>
              {projects.length > 0 && (
                <select
                  className="button secondary"
                  aria-label="打开已有作品"
                  defaultValue=""
                  onChange={(event) => void selectProject(event.target.value)}
                >
                  <option value="" disabled>
                    打开已有作品
                  </option>
                  {projects.map((item) => (
                    <option key={item.novel_id} value={item.novel_id}>
                      {item.title}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="onboarding-flow">
              <StepLabel number="01">整理故事蓝图</StepLabel>
              <StepLabel number="02">协作与起草</StepLabel>
              <StepLabel number="03">审阅后定稿</StepLabel>
            </div>
            <div className="welcome-art" aria-hidden="true">
              <div className="art-page back" />
              <div className="art-page">
                <span>一个好故事的起点</span>
                <div className="art-line title" />
                <div className="art-line" />
                <div className="art-line" />
                <div className="art-line short" />
                <div className="art-rule" />
                <div className="art-line" />
                <div className="art-line" />
                <div className="art-line short" />
                <Feather size={34} strokeWidth={1} />
              </div>
              <span className="art-sticker">
                <Sparkles size={15} />
                留给想象的空间
              </span>
            </div>
          </div>
        ) : area === "dashboard" ? (
          <div className="dashboard scroll-area">
            <div className="page-heading">
              <div>
                <div className="eyebrow">THE AUTHOR’S DESK</div>
                <h1>今天，继续你的故事。</h1>
                <p>
                  {project.title}
                  <span className="inline-dot">·</span>
                  {genreLabels[project.genre] || project.genre}
                </p>
              </div>
              <div className="book-monogram">
                <BookOpen size={29} strokeWidth={1.1} />
              </div>
            </div>
            <div className="overview-stats">
              {[
                {
                  label: "正式章节",
                  value: documents.filter((d) => d.path.startsWith("chapters/"))
                    .length,
                  icon: BookOpen,
                },
                {
                  label: "工作文稿",
                  value: documents.filter((d) =>
                    /^(drafts|candidates)\//.test(d.path),
                  ).length,
                  icon: PenLine,
                },
                { label: "待作者审批", value: pendingCount, icon: ShieldCheck },
                { label: "来源资料", value: imports.length, icon: Layers3 },
              ].map(({ label, value, icon: Icon }) => (
                <div className="stat-card" key={label}>
                  <div>
                    <span>{label}</span>
                    <Icon size={17} />
                  </div>
                  <strong>{value.toString().padStart(2, "0")}</strong>
                </div>
              ))}
            </div>
            <div className="dashboard-grid">
              <section className="start-card">
                <span className="card-eyebrow">
                  <ListTree size={16} />
                  故事从蓝图开始
                </span>
                <h2>
                  {documents.some((d) => isOutline(d.path))
                    ? "回到你的故事脉络"
                    : "为第一章，搭好舞台。"}
                </h2>
                <p>
                  导入已有大纲，或写下人物、冲突与世界。
                  <br />
                  计划经过你批准后，再交给 AI 起草。
                </p>
                <div>
                  <button
                    className="button primary"
                    onClick={() => {
                      setArea("outlines");
                      if (!documents.some((d) => isOutline(d.path))) {
                        setNewPath("outlines/proposals/book.md");
                        setNewDocContent("");
                        setShowNewDoc(true);
                      }
                    }}
                  >
                    <PenLine size={15} />
                    {documents.some((d) => isOutline(d.path))
                      ? "打开故事大纲"
                      : "编写故事大纲"}
                    <ArrowRight size={15} />
                  </button>
                  <button
                    className="button secondary"
                    onClick={() => fileInput.current?.click()}
                    disabled={busy === "import"}
                  >
                    <Upload size={15} />
                    导入大纲
                  </button>
                </div>
              </section>
              <section className="approval-card">
                <div className="section-heading">
                  <h2>决定权在你</h2>
                  <ShieldCheck size={18} />
                </div>
                <p>候选内容先审阅，正式故事再落笔。</p>
                <div className="approval-stage">
                  <span>候选文稿</span>
                  <ChevronRight size={13} />
                  <strong>作者审批</strong>
                  <ChevronRight size={13} />
                  <span>应用定稿</span>
                </div>
                <button
                  className="text-button"
                  onClick={() => setArea("approvals")}
                >
                  {pendingCount
                    ? `${pendingCount} 项提案等待审阅`
                    : "查看审批中心"}
                  <ArrowUpRight size={14} />
                </button>
              </section>
            </div>
            <section className="recent-section">
              <div className="section-heading">
                <h2>
                  工作文稿<span>{documents.length}</span>
                </h2>
                <button
                  className="text-button"
                  onClick={() => {
                    setArea("editor");
                    setNewPath("drafts/ch001.md");
                    setNewDocContent("");
                    setShowNewDoc(true);
                  }}
                >
                  <Plus size={14} />
                  新建文稿
                </button>
              </div>
              {documents.filter((d) =>
                /^(drafts|outlines|candidates)\//.test(d.path),
              ).length ? (
                <div className="recent-list">
                  {documents
                    .filter((d) =>
                      /^(drafts|outlines|candidates)\//.test(d.path),
                    )
                    .slice(0, 8)
                    .map((item) => (
                      <button
                        key={item.path}
                        onClick={() => void openDocument(item.path)}
                      >
                        <div className="file-tile">
                          <FileText size={19} strokeWidth={1.4} />
                        </div>
                        <span>
                          <strong>{leaf(item.path)}</strong>
                          <small>{item.path}</small>
                        </span>
                        <span
                          className={`tag ${readonlyPath(item.path) ? "green" : ""}`}
                        >
                          {readonlyPath(item.path) ? "已批准" : "工作文稿"}
                        </span>
                        <ChevronRight size={15} />
                      </button>
                    ))}
                </div>
              ) : (
                <div className="recent-empty">
                  <FileText size={23} strokeWidth={1.2} />
                  <div>
                    <strong>这里会留下故事的每一步</strong>
                    <p>新建文稿或导入大纲，开始这部作品的第一步。</p>
                  </div>
                </div>
              )}
            </section>
            <div className="dashboard-footer">
              <LockKeyhole size={13} />
              本地文件存储<span>·</span>正式正文与记忆经过审批后更新
              <span>·</span>导入内容作为参考材料
            </div>
          </div>
        ) : editorArea ? (
          <div className="editor-workspace">
            {doc ? (
              <>
                <div className="editor-toolbar">
                  <div className="document-title">
                    <FileText size={16} />
                    <strong>{leaf(doc.path)}</strong>
                    {locked ? (
                      <span className="tag">
                        <LockKeyhole size={10} />
                        只读
                      </span>
                    ) : dirty ? (
                      <span className="save-state dirty">有未保存修改</span>
                    ) : (
                      <span className="save-state">
                        <Check size={12} />
                        已保存
                      </span>
                    )}
                  </div>
                  <div className="toolbar-buttons">
                    <div className="segmented">
                      <button
                        className={!preview ? "selected" : ""}
                        onClick={() => setPreview(false)}
                      >
                        源码
                      </button>
                      <button
                        className={preview ? "selected" : ""}
                        onClick={() => setPreview(true)}
                      >
                        阅读
                      </button>
                    </div>
                    <IconButton
                      label="重新载入文件"
                      disabled={!!busy}
                      onClick={() => void openDocument(doc.path, false, true)}
                    >
                      <RefreshCw size={15} />
                    </IconButton>
                    <IconButton
                      label="版本历史"
                      onClick={() => void showVersions()}
                    >
                      <History size={16} />
                    </IconButton>
                    <IconButton
                      label="下载当前文稿"
                      onClick={() => download(leaf(doc.path), source)}
                    >
                      <Download size={16} />
                    </IconButton>
                    {locked ? (
                      <button
                        className="button small secondary"
                        onClick={
                          doc.path.startsWith("imports/")
                            ? adaptImported
                            : () => void startProposal()
                        }
                        disabled={!!busy}
                      >
                        <GitCompareArrows size={14} />
                        {doc.path.startsWith("imports/")
                          ? "整理为大纲"
                          : "提出修改"}
                      </button>
                    ) : (
                      <>
                        {!doc.path.startsWith("reviews/") && (
                          <button
                            className="button small secondary"
                            onClick={() => void startProposal(formalTarget())}
                            disabled={!!busy || !source.trim()}
                          >
                            <ShieldCheck size={14} />
                            {isOutline(doc.path)
                              ? "提交大纲审批"
                              : "提交定稿提案"}
                          </button>
                        )}
                        <button
                          className="button small primary"
                          disabled={!dirty || !!busy}
                          onClick={() => void save()}
                        >
                          <Save size={14} />
                          保存
                        </button>
                      </>
                    )}
                  </div>
                </div>
                {locked && (
                  <div className="readonly-banner">
                    <LockKeyhole size={13} />
                    这份文件已纳入正式故事。通过「提出修改」提交差异，作者批准后应用。
                  </div>
                )}
                {doc.path.startsWith("imports/") && (
                  <div className="readonly-banner source-banner">
                    <Layers3 size={13} />
                    导入的来源材料仅作参考，不代表已批准的故事事实。
                  </div>
                )}
                <div className="paper-container">
                  <div className={`editor-paper ${preview ? "reading" : ""}`}>
                    <div className="paper-meta">
                      <span>
                        {isOutline(doc.path)
                          ? "STORY BLUEPRINT"
                          : isMemory(doc.path)
                            ? "STORY MEMORY"
                            : "MANUSCRIPT"}
                      </span>
                      <span>{doc.path}</span>
                    </div>
                    {preview ? (
                      <article
                        className="markdown-body"
                        dangerouslySetInnerHTML={{
                          __html:
                            rendered ||
                            '<p class="placeholder-text">文稿还是空白。切换到源码，写下第一个字。</p>',
                        }}
                      />
                    ) : (
                      <textarea
                        ref={textarea}
                        className="manuscript-input"
                        aria-label="Markdown 文稿正文"
                        value={source}
                        readOnly={!!locked}
                        spellCheck={false}
                        placeholder={
                          locked
                            ? "此文件为空"
                            : "# 写下这一章的名字\n\n故事，从这里继续。"
                        }
                        onChange={(event) => setSource(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Tab" && !locked) {
                            event.preventDefault();
                            const element = event.currentTarget;
                            const start = element.selectionStart;
                            const end = element.selectionEnd;
                            setSource(
                              source.slice(0, start) + "  " + source.slice(end),
                            );
                            requestAnimationFrame(() => {
                              element.setSelectionRange(start + 2, start + 2);
                            });
                          }
                        }}
                      />
                    )}
                  </div>
                </div>
                <div className="editor-status">
                  <span>
                    {locked ? <LockKeyhole size={11} /> : <PenLine size={11} />}
                    {locked ? "正式资料 · 只读" : "Markdown 原文编辑"}
                    <span className="status-divider" />
                    {wordCount(source).toLocaleString()} 字
                    <span className="status-divider" />
                    {source.split("\n").length} 行
                  </span>
                  <span>
                    <Command size={11} /> / Ctrl + S 保存
                    <span className="status-divider" />
                    UTF-8
                  </span>
                </div>
              </>
            ) : (
              <Empty
                icon={
                  area === "outlines" ? (
                    <ListTree size={27} />
                  ) : area === "memory" ? (
                    <Library size={27} />
                  ) : (
                    <PenLine size={27} />
                  )
                }
                title={
                  area === "outlines"
                    ? "先给故事一个方向"
                    : area === "memory"
                      ? "让故事记住每一次变化"
                      : "留一页，给接下来的故事"
                }
                description={
                  area === "outlines"
                    ? "在左侧选择大纲，或从空白文稿与已有材料开始。"
                    : area === "memory"
                      ? "从左侧打开人物、设定与状态记录。正式记忆的修改需要经过作者审批。"
                      : "选择一份文稿开始写作。AI 生成的内容会先保存为候选文稿。"
                }
                action={
                  area !== "memory" ? (
                    <div className="empty-actions">
                      <button className="button primary" onClick={newDocument}>
                        <Plus size={15} />
                        新建{area === "outlines" ? "大纲" : "文稿"}
                      </button>
                      {area === "outlines" && (
                        <button
                          className="button secondary"
                          onClick={() => fileInput.current?.click()}
                        >
                          <Upload size={15} />
                          导入材料
                        </button>
                      )}
                    </div>
                  ) : (
                    <button
                      className="button secondary"
                      onClick={() =>
                        void startProposal("bible/characters.md", "")
                      }
                    >
                      <Plus size={15} />
                      新增设定提案
                    </button>
                  )
                }
              />
            )}
          </div>
        ) : area === "approvals" ? (
          <div className="review-workspace">
            <div className="page-heading compact-heading">
              <div>
                <div className="eyebrow">YOUR STORY, YOUR DECISION</div>
                <h1>作者审批</h1>
                <p>审阅每一处差异，让改变有据可循。</p>
              </div>
              <span className="large-count">
                {pendingCount}
                <small>待审批</small>
              </span>
            </div>
            {approvals.length === 0 ? (
              <Empty
                icon={<ShieldCheck size={28} />}
                title="每一次定稿，都等你点头"
                description="在文稿中提交大纲或定稿提案，或对正式资料提出修改。所有提案会汇集在这里。"
              />
            ) : (
              <div className="approvals-grid">
                <div className="approval-list">
                  {approvals.map((item) => (
                    <button
                      key={item.id}
                      onClick={() => {
                        setSelectedApproval(item.id);
                        setDecisionNote("");
                      }}
                      className={approval?.id === item.id ? "selected" : ""}
                    >
                      <div>
                        <FileText size={15} />
                        <strong>{leaf(item.target)}</strong>
                      </div>
                      <p>{item.reason}</p>
                      <span
                        className={`tag ${item.state === "pending" ? "orange" : item.state === "applied" ? "green" : ""}`}
                      >
                        {stateLabels[item.state] || item.state}
                      </span>
                      <small>{formatDate(item.created_at)}</small>
                    </button>
                  ))}
                </div>
                {approval && (
                  <section className="approval-detail">
                    <div className="approval-detail-heading">
                      <span className="eyebrow">变更提案</span>
                      <h2>{leaf(approval.target)}</h2>
                      <code>{approval.target}</code>
                      <p>{approval.reason}</p>
                      {approval.impact?.length > 0 && (
                        <div className="impact-list">
                          <strong>影响范围</strong>
                          <ul>
                            {approval.impact.map((impact, index) => (
                              <li key={index}>{impact}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                    <div className="diff-toolbar">
                      <div className="segmented">
                        <button
                          className={proposalTab === "diff" ? "selected" : ""}
                          onClick={() => setProposalTab("diff")}
                        >
                          差异对照
                        </button>
                        <button
                          className={proposalTab === "after" ? "selected" : ""}
                          onClick={() => setProposalTab("after")}
                        >
                          提案全文
                        </button>
                      </div>
                      <span>
                        <i className="diff-legend removed" />
                        删除
                        <i className="diff-legend added" />
                        新增
                      </span>
                    </div>
                    <div className="approval-content">
                      {proposalTab === "diff" ? (
                        <Diff
                          before={approval.before || ""}
                          after={approval.after || ""}
                        />
                      ) : (
                        <pre className="plain-content">{approval.after}</pre>
                      )}
                    </div>
                    <div className="approval-controls">
                      {approval.state === "pending" ? (
                        <>
                          <textarea
                            aria-label="审批备注"
                            placeholder="审批备注（可选）"
                            value={decisionNote}
                            onChange={(event) =>
                              setDecisionNote(event.target.value)
                            }
                          />
                          <div>
                            <span>
                              <ShieldCheck size={13} />
                              批准后仍需单独应用
                            </span>
                            <button
                              className="button secondary small"
                              onClick={() => void decide(approval, "reject")}
                              disabled={!!busy}
                            >
                              <X size={14} />
                              驳回
                            </button>
                            <button
                              className="button primary small"
                              onClick={() => void decide(approval, "approve")}
                              disabled={!!busy}
                            >
                              <Check size={14} />
                              作者批准
                            </button>
                          </div>
                        </>
                      ) : approval.state === "approved" ? (
                        <div>
                          <span>
                            已批准。应用时将检查目标文件是否发生变化。
                          </span>
                          <button
                            className="button primary"
                            disabled={!!busy}
                            onClick={() => void applyProposal(approval)}
                          >
                            <CheckCheck size={15} />
                            应用到目标文件
                          </button>
                        </div>
                      ) : (
                        <div>
                          <span>
                            <CircleCheck size={15} />
                            {stateLabels[approval.state] || approval.state}
                          </span>
                          <span className="muted">提案记录保留可追溯</span>
                        </div>
                      )}
                    </div>
                  </section>
                )}
              </div>
            )}
          </div>
        ) : (
          <div className="tasks-workspace">
            <div className="page-heading compact-heading">
              <div>
                <div className="eyebrow">WORK IN PROGRESS</div>
                <h1>任务记录</h1>
                <p>查看任务的真实状态、引用上下文与候选结果。</p>
              </div>
              <IconButton
                label="刷新任务记录"
                onClick={() => {
                  if (project)
                    void act("refresh", () => refresh(project.novel_id));
                }}
              >
                <RefreshCw size={18} />
              </IconButton>
            </div>
            {jobs.length === 0 ? (
              <Empty
                icon={<Activity size={28} />}
                title="创作节奏，由你发起"
                description="打开一份文稿，在右侧创作助手中选择任务。任务会保留输入、运行状态和结果来源。"
                action={
                  <button
                    className="button secondary"
                    onClick={() => {
                      setArea("editor");
                      setRightOpen(true);
                    }}
                  >
                    <PenLine size={15} />
                    前往文稿
                  </button>
                }
              />
            ) : (
              <div className="jobs-grid">
                <div className="jobs-list">
                  {jobs.map((job) => (
                    <button
                      key={job.id}
                      className={selectedTask?.id === job.id ? "selected" : ""}
                      onClick={() => setSelectedJob(job.id)}
                    >
                      <div
                        className={`job-icon ${job.state === "failed" ? "error" : ""}`}
                      >
                        {job.state === "running" ? (
                          <LoaderCircle className="spin" size={18} />
                        ) : job.state === "failed" ? (
                          <CircleAlert size={18} />
                        ) : (
                          <Sparkles size={18} />
                        )}
                      </div>
                      <span>
                        <strong>{kindLabels[job.kind] || job.kind}</strong>
                        <small>
                          {formatDate(job.created_at)} ·{" "}
                          {bootstrap?.capabilities.providers.find(
                            (p) => p.id === job.provider,
                          )?.label || job.provider}
                        </small>
                      </span>
                      <span
                        className={`tag ${activeStates.has(job.state) ? "orange" : ["completed", "succeeded"].includes(job.state) ? "green" : ""}`}
                      >
                        {stateLabels[job.state] || job.state}
                      </span>
                    </button>
                  ))}
                </div>
                {selectedTask && (
                  <section className="job-detail">
                    <div className="section-heading">
                      <h2>
                        {kindLabels[selectedTask.kind] || selectedTask.kind}
                      </h2>
                      <span className="tag">
                        {stateLabels[selectedTask.state] || selectedTask.state}
                      </span>
                    </div>
                    <dl className="detail-table">
                      <dt>任务编号</dt>
                      <dd>
                        <code>{selectedTask.id}</code>
                      </dd>
                      <dt>创建时间</dt>
                      <dd>{formatDate(selectedTask.created_at)}</dd>
                      <dt>执行方式</dt>
                      <dd>
                        {bootstrap?.capabilities.providers.find(
                          (p) => p.id === selectedTask.provider,
                        )?.label || selectedTask.provider}
                      </dd>
                      {selectedTask.request?.document_path && (
                        <>
                          <dt>上下文文稿</dt>
                          <dd>
                            <button
                              className="text-button"
                              onClick={() =>
                                void openDocument(
                                  selectedTask.request!.document_path!,
                                )
                              }
                            >
                              {selectedTask.request.document_path}
                              <ExternalLink size={12} />
                            </button>
                          </dd>
                        </>
                      )}
                    </dl>
                    {selectedTask.request?.instruction && (
                      <div className="job-section">
                        <h3>作者指令</h3>
                        <p>{selectedTask.request.instruction}</p>
                      </div>
                    )}
                    {selectedTask.error && (
                      <div className="inline-error">
                        <CircleAlert size={16} />
                        {selectedTask.error}
                      </div>
                    )}
                    {selectedTask.state === "awaiting_input" && (
                      <div className="handoff-card">
                        <div>
                          <CornerDownLeft size={18} />
                          <strong>接力上下文已就绪</strong>
                        </div>
                        <p>
                          复制上下文交给 Codex，完成后将结果回传为候选文稿。
                        </p>
                        <button
                          className="button primary small"
                          onClick={() => {
                            setHandoff(selectedTask);
                            setCandidate("");
                          }}
                        >
                          打开接力面板
                          <ArrowUpRight size={14} />
                        </button>
                      </div>
                    )}
                    {selectedTask.result_path && (
                      <button
                        className="result-card"
                        onClick={() =>
                          void openDocument(selectedTask.result_path!)
                        }
                      >
                        <FileText size={21} />
                        <span>
                          <strong>查看候选结果</strong>
                          <small>{selectedTask.result_path}</small>
                        </span>
                        <ArrowUpRight size={17} />
                      </button>
                    )}
                    {selectedTask.context_sources &&
                      Object.keys(selectedTask.context_sources).length > 0 && (
                        <div className="job-section">
                          <h3>引用来源与版本</h3>
                          <div className="source-references">
                            {Object.entries(selectedTask.context_sources).map(
                              ([path, hash]) => (
                                <button
                                  key={path}
                                  onClick={() => void openDocument(path)}
                                  title={`${path} · ${hash}`}
                                >
                                  <FileText size={12} />
                                  <span>{path}</span>
                                  <code>{hash.slice(0, 8)}</code>
                                  <ArrowUpRight size={11} />
                                </button>
                              ),
                            )}
                          </div>
                        </div>
                      )}
                    {selectedTask.logs && selectedTask.logs.length > 0 && (
                      <div className="job-section">
                        <h3>执行记录</h3>
                        <div className="job-logs">
                          {selectedTask.logs.map((log, index) => (
                            <div key={index}>
                              <span />
                              {typeof log === "string" ? (
                                log
                              ) : (
                                <>
                                  <time>
                                    {formatDate(
                                      typeof log.at === "string"
                                        ? log.at
                                        : undefined,
                                    )}
                                  </time>
                                  {String(log.message || "")}
                                </>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {selectedTask.usage &&
                      Object.keys(selectedTask.usage).length > 0 && (
                        <details className="usage-details">
                          <summary>实际用量</summary>
                          <pre>
                            {JSON.stringify(selectedTask.usage, null, 2)}
                          </pre>
                          {selectedTask.cost_usd != null && (
                            <p>费用：${selectedTask.cost_usd.toFixed(4)}</p>
                          )}
                        </details>
                      )}
                    <div className="job-actions">
                      {selectedTask.recovery_available && (
                        <button className="button primary small" disabled={!!busy}
                          onClick={() => void jobAction(selectedTask, "recover")}>
                          <RotateCcw size={14} />恢复已生成候选
                        </button>
                      )}
                      {["queued", "running"].includes(selectedTask.state) && (
                        <button
                          className="button secondary small"
                          disabled={!!busy}
                          onClick={() => void jobAction(selectedTask, "pause")}
                        >
                          <Pause size={13} />
                          暂停任务
                        </button>
                      )}
                      {selectedTask.state === "paused" && (
                        <button
                          className="button secondary small"
                          disabled={!!busy}
                          onClick={() => void jobAction(selectedTask, "resume")}
                        >
                          <Play size={13} />
                          继续任务
                        </button>
                      )}
                      {activeStates.has(selectedTask.state) && (
                        <button
                          className="button secondary small"
                          disabled={!!busy}
                          onClick={() => void jobAction(selectedTask, "cancel")}
                        >
                          <Square size={13} />
                          取消任务
                        </button>
                      )}
                      {["failed", "cancelled", "interrupted"].includes(
                        selectedTask.state,
                      ) && (
                        <button
                          className="button secondary small"
                          disabled={!!busy}
                          onClick={() => void jobAction(selectedTask, "retry")}
                        >
                          <RotateCcw size={14} />
                          重试任务
                        </button>
                      )}
                    </div>
                  </section>
                )}
              </div>
            )}
          </div>
        )}
      </main>

      {rightOpen && (
        <button
          className="agent-scrim"
          aria-label="关闭创作助手"
          onClick={() => setRightOpen(false)}
        />
      )}
      {leftOpen && (
        <button
          className="nav-scrim"
          aria-label="关闭导航"
          onClick={() => setLeftOpen(false)}
        />
      )}
      <aside className="agent-panel" aria-label="创作助手">
        <div className="agent-heading">
          <div>
            <span className="agent-mark">
              <Sparkles size={15} />
            </span>
            <strong>创作助手</strong>
            <span className="beta-label">AGENT</span>
          </div>
          <IconButton label="收起创作助手" onClick={() => setRightOpen(false)}>
            <PanelRightClose size={16} />
          </IconButton>
        </div>
        <div className="agent-scroll">
          <div className="agent-intro">
            <h2>一起，把故事写好。</h2>
            <p>
              从明确的任务开始。
              <br />
              每一份输出，先作为候选。
            </p>
          </div>
          <form onSubmit={(event) => void startTask(event)}>
            <label className="field-label" htmlFor="provider">
              <Settings2 size={13} />
              执行方式
            </label>
            <div className="select-wrap">
              <select
                id="provider"
                value={provider}
                onChange={(event) => {
                  providerChosen.current = true;
                  setProvider(event.target.value);
                }}
                disabled={!bootstrap}
              >
                {bootstrap?.capabilities.providers.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                    {!item.available ? " · 未配置" : ""}
                  </option>
                ))}
              </select>
              <ChevronDown size={13} />
            </div>
            {selectedProvider?.reason && (
              <p className="field-hint provider-hint">
                {selectedProvider.reason}
              </p>
            )}
            {bootstrap && provider === "api" && !selectedProvider?.available && (
              <p className="field-hint" role="status">
                API 尚未配置。请填写本地 <code>config/api.local.json</code> 的{" "}
                <code>base_url</code>、<code>model</code> 和 <code>api_key</code>，
                保存后刷新网页生效。
              </p>
            )}
            <label className="field-label">本次任务</label>
            <div className="task-options">
              {Object.entries(kindLabels).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  className={taskKind === id ? "selected" : ""}
                  onClick={() => setTaskKind(id)}
                >
                  {id === "expand_outline" ? (
                    <ListTree size={15} />
                  ) : id === "draft" ? (
                    <PenLine size={15} />
                  ) : id === "review" ? (
                    <ShieldCheck size={15} />
                  ) : (
                    <RefreshCw size={15} />
                  )}
                  {label}
                </button>
              ))}
            </div>
            <label className="field-label" htmlFor="task-instruction">
              告诉助手，你想推进什么
            </label>
            <textarea
              id="task-instruction"
              className="instruction-input"
              placeholder={
                taskKind === "expand_outline"
                  ? "例如：梳理本卷主要冲突，细化场景节拍，并指出待确认的设定。"
                  : taskKind === "draft"
                    ? "例如：依据已批准计划起草本章，保持视角一致，标出待确认之处。"
                    : taskKind === "review"
                      ? "例如：检查本章连续性、重复情节与人物动机，给出独立审稿报告。"
                      : "例如：压缩这一段说明，用人物行动推进冲突。"
              }
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              rows={5}
              required
            />
            {taskKind === "draft" && (
              <>
                <label className="field-label" htmlFor="approved-plan">
                  已批准的章节计划
                </label>
                <div className="select-wrap">
                  <select
                    id="approved-plan"
                    required
                    value={planPath}
                    onChange={(event) => setPlanPath(event.target.value)}
                  >
                    <option value="">选择已批准计划</option>
                    {approvedPlans.map((item) => (
                      <option key={item.path} value={item.path}>
                        {leaf(item.path)}
                      </option>
                    ))}
                  </select>
                  <ChevronDown size={13} />
                </div>
                {!approvedPlans.length && (
                  <p className="field-hint">
                    先在大纲中提交审批并应用，再起草章节。
                  </p>
                )}
              </>
            )}
            <div className="context-card">
              <div>
                <Layers3 size={13} />
                <span>本次上下文</span>
                {doc && <span className="tiny-tag">1 份文稿</span>}
              </div>
              {doc ? (
                <p>
                  <FileText size={13} />
                  <span title={doc.path}>{leaf(doc.path)}</span>
                  {dirty && <i className="dirty-dot" />}
                </p>
              ) : (
                <p className="muted">先打开一份大纲或文稿</p>
              )}
              <small>
                {dirty
                  ? "请保存修改后再发起任务"
                  : "任务绑定已保存版本，保留来源引用"}
              </small>
            </div>
            <button
              className="button primary run-task"
              type="submit"
              disabled={
                !project ||
                !doc ||
                dirty ||
                !selectedProvider?.available ||
                !!busy ||
                !instruction.trim() ||
                (taskKind === "draft" &&
                  !approvedPlans.some((item) => item.path === planPath))
              }
            >
              {busy === "task" ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <Play size={14} fill="currentColor" />
              )}
              {provider === "codex" ? "准备 Codex 接力" : "开始任务"}
              <ArrowRight size={15} />
            </button>
          </form>
          <div className="agent-boundary">
            <ShieldCheck size={15} />
            <p>
              先计划，再起草。
              <br />
              正式正文与故事记忆，由作者审批后更新。
            </p>
          </div>
          {runningCount > 0 && (
            <button className="active-jobs" onClick={() => setArea("tasks")}>
              <span className="connection-dot orange-dot" />
              {runningCount} 项任务等待处理
              <ArrowUpRight size={14} />
            </button>
          )}
          <div className="agent-bottom-note">
            <span>为灵感留白，为故事负责。</span>
            <Feather size={15} strokeWidth={1.2} />
          </div>
        </div>
      </aside>

      <input
        type="file"
        accept=".md,.txt,text/plain,text/markdown"
        ref={fileInput}
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void importFile(file);
          event.target.value = "";
        }}
      />
      {notice && (
        <div
          role={notice.type === "error" ? "alert" : "status"}
          className={`toast ${notice.type}`}
        >
          {notice.type === "error" ? (
            <CircleAlert size={17} />
          ) : (
            <CircleCheck size={17} />
          )}
          <span>{notice.text}</span>
          <IconButton label="关闭提示" onClick={() => setNotice(null)}>
            <X size={14} />
          </IconButton>
        </div>
      )}
      {showCreate && (
        <Modal
          title="创建一部新作品"
          subtitle="给故事一个名字，其他的可以慢慢写。"
          onClose={() => setShowCreate(false)}
        >
          <form
            onSubmit={(event) => void createProject(event)}
            className="modal-form"
          >
            <label>
              作品名称
              <input
                name="title"
                placeholder="你的故事叫什么名字？"
                required
                maxLength={100}
              />
            </label>
            <label>
              作品标识<span className="label-optional">用于本地文件夹</span>
              <input
                name="id"
                placeholder="my-first-novel"
                pattern="[a-z][a-z0-9_-]{0,63}"
                title="小写字母开头，使用小写字母、数字、下划线或短横线，最多 64 个字符"
                required
                maxLength={64}
              />
              <small>小写字母开头，可使用数字、下划线和短横线。</small>
            </label>
            <label>
              主要题材
              <select name="genre" required defaultValue="">
                <option value="" disabled>
                  选择一个题材模块
                </option>
                {Object.entries(genreLabels).map(([id, label]) => (
                  <option value={id} key={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <div className="form-note">
              <LockKeyhole size={14} />
              作品独立保存在本地，不会自动填入示例故事。
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="button secondary"
                onClick={() => setShowCreate(false)}
              >
                暂不创建
              </button>
              <button
                className="button primary"
                disabled={!!busy}
                type="submit"
              >
                {busy === "create" && (
                  <LoaderCircle className="spin" size={15} />
                )}
                创建作品
                <ArrowRight size={15} />
              </button>
            </div>
          </form>
        </Modal>
      )}
      {showNewDoc && (
        <Modal
          title={area === "outlines" ? "新建故事大纲" : "新建工作文稿"}
          subtitle="工作文件可以直接编辑，正式文件通过提案更新。"
          onClose={() => setShowNewDoc(false)}
        >
          <form
            className="modal-form"
            onSubmit={(event) => void createDocument(event)}
          >
            <label>
              文稿路径
              <input
                value={newPath}
                onChange={(event) => setNewPath(event.target.value)}
                required
                pattern="(drafts|outlines/proposals)/[^.].*\.md"
                title="使用 drafts/ 或 outlines/proposals/ 下的 Markdown 文件路径"
              />
              <small>例如 drafts/ch001.md 或 outlines/proposals/ch001.md</small>
            </label>
            <div className="modal-footer">
              <button
                type="button"
                className="button secondary"
                onClick={() => setShowNewDoc(false)}
              >
                取消
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={!!busy}
              >
                <Plus size={15} />
                创建空白文稿
              </button>
            </div>
          </form>
        </Modal>
      )}
      {showHistory && (
        <Modal
          title="版本历史"
          subtitle={doc?.path}
          onClose={() => setShowHistory(false)}
          wide
        >
          <div className="history-layout">
            <div className="version-list">
              {versions.length ? (
                versions.map((version, index) => (
                  <button
                    key={version.id}
                    className={version.id === versionId ? "selected" : ""}
                    onClick={() => setVersionId(version.id)}
                  >
                    <History size={15} />
                    <span>
                      <strong>{formatDate(version.created_at)}</strong>
                      <small>
                        {version.hash.slice(0, 12)}
                        {index === 0 ? " · 最近版本" : ""}
                      </small>
                    </span>
                  </button>
                ))
              ) : (
                <p className="muted">
                  {busy === "history"
                    ? "正在读取历史…"
                    : "还没有可用的历史版本"}
                </p>
              )}
            </div>
            <div className="history-diff">
              <div className="diff-toolbar">
                <span>所选版本 → 当前已保存版本</span>
              </div>
              {selectedVersion?.content !== undefined ? (
                <Diff
                  before={selectedVersion.content}
                  after={doc?.content || ""}
                />
              ) : (
                <Empty
                  compact
                  title="没有可展示的正文"
                  description="该版本未返回正文内容。"
                />
              )}
            </div>
          </div>
          <div className="modal-footer">
            <span className="muted">
              {locked
                ? "正式文件的恢复请通过修改提案审批。"
                : "恢复会新增历史记录，不会删除原有版本。"}
            </span>
            <button
              className="button primary"
              disabled={!selectedVersion || !!busy || !!locked}
              onClick={() => void restoreVersion()}
            >
              <RotateCcw size={14} />
              恢复此版本
            </button>
          </div>
        </Modal>
      )}
      {proposal && (
        <Modal
          title="提交作者审批提案"
          subtitle="审阅目标、文字和影响范围。提交后仍需作者批准，才能应用。"
          onClose={closeProposal}
          wide
        >
          <form
            className="modal-form"
            onSubmit={(event) => void submitProposal(event)}
          >
            <label>
              目标文件
              <div className="target-field">
                <input
                  value={proposal.target}
                  onChange={(event) => {
                    setProposal({ ...proposal, target: event.target.value });
                    setVerifiedTarget("");
                  }}
                  required
                />
                <button
                  type="button"
                  className="button secondary small"
                  disabled={!!busy || verifiedTarget === proposal.target}
                  onClick={() => void verifyProposalTarget()}
                >
                  {verifiedTarget === proposal.target ? (
                    <Check size={14} />
                  ) : (
                    <RefreshCw size={14} />
                  )}
                  {verifiedTarget === proposal.target ? "已核对" : "核对目标"}
                </button>
              </div>
              <small>
                正式章节使用 chapters/ch001.md；设定使用 bible/ 或
                memory/。更换目标后须核对版本。
              </small>
            </label>
            <label>
              提案正文
              <textarea
                className="proposal-source"
                value={proposal.content}
                onChange={(event) =>
                  setProposal({ ...proposal, content: event.target.value })
                }
                rows={10}
                required
              />
            </label>
            <details className="proposal-diff">
              <summary>
                <GitCompareArrows size={14} />
                审阅与目标文件的差异
              </summary>
              <Diff before={proposal.before} after={proposal.content} />
            </details>
            <div className="form-columns">
              <label>
                变更理由
                <textarea
                  value={proposal.reason}
                  onChange={(event) =>
                    setProposal({ ...proposal, reason: event.target.value })
                  }
                  rows={3}
                  placeholder="为什么需要这次变更？"
                  required
                />
              </label>
              <label>
                影响范围<span className="label-optional">每项一行</span>
                <textarea
                  value={proposal.impact}
                  onChange={(event) =>
                    setProposal({ ...proposal, impact: event.target.value })
                  }
                  rows={3}
                  placeholder="人物状态、后续情节、伏笔或其他文件"
                  required
                />
              </label>
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="button secondary"
                onClick={closeProposal}
              >
                继续修改
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={
                  !!busy ||
                  verifiedTarget !== proposal.target ||
                  !proposal.reason.trim() ||
                  !proposal.impact.trim()
                }
              >
                <ShieldCheck size={15} />
                提交审批提案
              </button>
            </div>
          </form>
        </Modal>
      )}
      {showSources && (
        <Modal
          title="来源资料"
          subtitle="保留导入原件与提取文本。来源材料不是作者批准的事实。"
          onClose={() => setShowSources(false)}
          wide
        >
          <div className="source-list">
            {imports.length ? (
              imports.map((item) => (
                <button
                  key={item.import_id}
                  onClick={() =>
                    void openDocument(
                      `imports/original/${item.import_id}/extracted.md`,
                    )
                  }
                >
                  <div className="file-tile">
                    <FileText size={20} />
                  </div>
                  <span>
                    <strong>{item.original_name}</strong>
                    <small>{item.import_id}</small>
                    {item.source_hash && (
                      <code>SHA-256 {item.source_hash.slice(0, 20)}…</code>
                    )}
                  </span>
                  <ArrowUpRight size={17} />
                </button>
              ))
            ) : (
              <Empty
                compact
                icon={<Layers3 size={25} />}
                title="还没有来源资料"
                description="导入已有大纲、人物笔记或故事参考，保留可追溯的来源。"
              />
            )}
          </div>
          <div className="modal-footer">
            <label>原件编码 <select aria-label="原件编码" value={importEncoding}
              onChange={(event) => setImportEncoding(event.target.value)}>
              <option value="auto">UTF-8 / BOM 自动识别</option>
              <option value="gb18030">GB18030 / GBK</option>
              <option value="utf-16">UTF-16</option>
            </select></label>
            <button
              className="button primary"
              disabled={!!busy}
              onClick={() => fileInput.current?.click()}
            >
              <Upload size={15} />
              导入资料
            </button>
          </div>
        </Modal>
      )}
      {handoff && (
        <Modal
          title="Codex 交互接力"
          subtitle="将上下文交给 Codex，完成后把候选结果带回这里。"
          onClose={() => setHandoff(null)}
          wide
        >
          <form
            className="modal-form"
            onSubmit={(event) => void completeHandoff(event)}
          >
            <div className="handoff-steps">
              <StepLabel number="1">复制任务上下文</StepLabel>
              <StepLabel number="2">在 Codex 中执行</StepLabel>
              <StepLabel number="3">回传候选结果</StepLabel>
            </div>
            <label>
              任务上下文
              <textarea
                readOnly
                value={
                  handoff.handoff_prompt || "上下文尚未返回，请刷新任务后再试。"
                }
                rows={7}
                className="prompt-text"
              />
            </label>
            <div className="inline-buttons">
              <button
                type="button"
                className="button secondary small"
                onClick={() => void copy(handoff.handoff_prompt || "")}
                disabled={!handoff.handoff_prompt}
              >
                <Copy size={14} />
                复制上下文
              </button>
              <button
                type="button"
                className="button secondary small"
                onClick={() =>
                  download(
                    `codex-task-${handoff.id}.md`,
                    handoff.handoff_prompt || "",
                  )
                }
                disabled={!handoff.handoff_prompt}
              >
                <Download size={14} />
                下载任务文件
              </button>
            </div>
            <label>
              回传候选正文
              <textarea
                value={candidate}
                onChange={(event) => setCandidate(event.target.value)}
                rows={9}
                placeholder="在这里粘贴 Codex 生成的完整 Markdown 结果…"
                required
              />
            </label>
            <div className="form-note">
              <ShieldCheck size={14} />
              回传只保存候选内容，不会直接更新正式章节或记忆。
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="button secondary"
                onClick={() => setHandoff(null)}
              >
                稍后回传
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={!!busy || !candidate.trim()}
              >
                <CornerDownLeft size={15} />
                保存候选结果
              </button>
            </div>
          </form>
        </Modal>
      )}
      {exportRange && (
        <Modal
          title="导出正式作品"
          subtitle="将已定稿章节按顺序合并为 Markdown 文件。"
          onClose={() => setExportRange(false)}
        >
          <form
            className="modal-form"
            onSubmit={(event) => void exportBook(event)}
          >
            <div className="form-columns">
              <label>
                起始章节
                <input
                  type="number"
                  min="1"
                  placeholder="首章"
                  value={rangeStart}
                  onChange={(event) => setRangeStart(event.target.value)}
                />
              </label>
              <label>
                结束章节
                <input
                  type="number"
                  min={rangeStart || "1"}
                  placeholder="末章"
                  value={rangeEnd}
                  onChange={(event) => setRangeEnd(event.target.value)}
                />
              </label>
            </div>
            <div className="form-note">
              <BookOpen size={14} />
              留空导出全部正式章节。工作草稿可在编辑器单独下载。
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="button secondary"
                onClick={() => setExportRange(false)}
              >
                取消
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={!!busy}
              >
                <Download size={15} />
                下载 Markdown
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
