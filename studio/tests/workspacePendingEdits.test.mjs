import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function helpers(file) {
  const source = await readFile(new URL(`../src/${file}.ts`, import.meta.url), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}
const { modelProfileChanged } = await helpers("pendingEdits");
const { reviewMatchesCandidate } = await helpers("reviewIdentity");
const { referenceBatchFlow } = await helpers("referenceBatchFlow");
const icon = () => null;
const project = { novel_id: "acceptance", title: "隔离测试" };
const documents = [
  { path: "drafts/source.md", hash: "source" },
  { path: "drafts/edited.md", hash: "edited" },
  { path: "reviews/evidence.json", hash: "review" },
];
const policy = { bindings: {}, max_calls: 8, max_tokens: 100000, max_cost_usd: .5, max_attempts: 1 };
const registry = { revision: 1, hash: "registry", profiles: [], presets: [], migration_required: false };
const routing = { revision: 1, hash: "routing", policy };
const flow = {
  id: "flow-a", state: "ready", created_at: "2026-10-22T00:00:00Z", revision_rounds: 0,
  nodes: [
    { kind: "rewrite", state: "succeeded", result_path: "drafts/edited.md", result_hash: "edited", result_version_id: "version-edited" },
    { kind: "review_evidence", state: "succeeded", result_path: "reviews/evidence.json", result_hash: "review", result_version_id: "version-review" },
  ],
};
const evidence = { target_path: "drafts/edited.md", target_hash: "edited", target_version_id: "version-edited", unchecked: [],
  issues: [{ id: "issue-1", category: "表达", severity: "warning", quote: "空泛总结", start_char: 0, end_char: 4, explanation: "需核对", suggestion: "改为动作" }] };

// Execute each real component with minimal hook and JSX adapters. These are
// control-flow regressions, not browser/UI evidence and never use a provider.
async function mount(file, respond) {
  const source = await readFile(new URL(`../src/${file}.tsx`, import.meta.url), "utf8");
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const imports = ast.statements.filter(ts.isImportDeclaration);
  const imported = imports.flatMap((item) => {
    const clause = item.importClause;
    return [clause?.name?.text, ...(clause?.namedBindings && ts.isNamedImports(clause.namedBindings) ? clause.namedBindings.elements.map((part) => part.name.text) : [])].filter(Boolean);
  });
  const body = ast.statements.filter((item) => !ts.isImportDeclaration(item)).map((item) => item.getText(ast).replace(/^export\s+(default\s+)?/, "")).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  const values = [], effects = [], refs = [], notifications = [], calls = [], confirmations = [];
  let cursor = 0, tree, changed = false;
  const props = { project, projects: [project], documents, onSettings: () => {}, onOpenMethods: () => {}, onSaved: async () => {}, onOpen: () => {}, onTask: () => {}, onDirtyChange: (value) => notifications.push(value) };
  const environment = Object.fromEntries(imported.map((name) => [name, icon]));
  Object.assign(environment, {
    React: { createElement: (type, props, ...children) => ({ type, props: props || {}, children }) },
    useState(initial) {
      const index = cursor++;
      if (!(index in values)) values[index] = typeof initial === "function" ? initial() : initial;
      return [values[index], (value) => { const next = typeof value === "function" ? value(values[index]) : value; if (!Object.is(next, values[index])) { values[index] = next; changed = true; } }];
    },
    useRef(initial) { const index = cursor++; return refs[index] ||= { current: initial }; },
    useEffect(action, dependencies) {
      const index = cursor++, old = effects[index];
      if (!old || !dependencies || dependencies.some((value, at) => !Object.is(value, old.dependencies?.[at]))) {
        effects[index] = { action, dependencies, pending: true, cleanup: old?.cleanup };
      }
    },
    api: async (url, method = "GET", body) => { calls.push({ url, method, body }); return respond(url, method, body); },
    ApiError: class extends Error {}, projectUrl: () => "/projects/acceptance", formatDate: (value) => value, leaf: (value) => value,
    modelProfileChanged, reviewMatchesCandidate, referenceBatchFlow,
    window: { setInterval: () => 1, clearInterval() {} },
    libraryTaskConfiguration: () => ({ configuration_hash: "preview" }), libraryPreviewHash: () => null,
    useTaskReference: () => ({ consume() {} }),
    requestConfirmation: async () => confirmations.shift() ?? false,
  });
  const Component = new Function(...Object.keys(environment), `${compiled}\nreturn ${file};`)(...Object.values(environment));
  function render() {
    for (let count = 0; count < 20; count++) {
      changed = false; cursor = 0; tree = Component(props);
      for (const effect of effects.filter((item) => item?.pending)) {
        effect.pending = false; effect.cleanup?.(); effect.cleanup = effect.action();
      }
      if (!changed) return;
    }
    throw new Error("Unexpected render loop");
  }
  async function flush() { for (let i = 0; i < 4; i++) { await new Promise(setImmediate); render(); } }
  function all(predicate, root) { if (arguments.length < 2) root = tree; return !root || typeof root !== "object" ? [] : Array.isArray(root) ? root.flatMap((child) => all(predicate, child)) : [...(predicate(root) ? [root] : []), ...root.children.flatMap((child) => all(predicate, child))]; }
  const text = (node) => node == null || typeof node === "boolean" ? "" : Array.isArray(node) ? node.map(text).join("") : typeof node === "object" ? node.children.map(text).join("") : String(node);
  function label(name) { const found = all((node) => node.type === "label" && text(node).startsWith(name)); assert.equal(found.length, 1, `label ${name}`); return all((node) => ["input", "textarea", "select"].includes(node.type), found[0])[0]; }
  function button(name) { const found = all((node) => node.type === "button" && text(node) === name); assert.equal(found.length, 1, `button ${name}`); return found[0]; }
  const change = (name, value) => { label(name).props.onChange({ target: { value } }); render(); };
  render(); await flush();
  return { render, flush, label, button, change, all, calls, confirmations, notifications, get dirty() { return notifications.at(-1); } };
}

function modelResponse(url, method, body) {
  if (url === "/model-registry") return registry;
  if (url.endsWith("/routing")) return method === "PUT" ? { ...routing, hash: "routing-2", policy: body.policy } : routing;
  if (url.endsWith("/routes/preview")) return {};
  if (url.endsWith("/workflows")) return method === "POST" ? { ...flow, nodes: [], id: "new-flow" } : { workflows: [flow] };
  if (url.includes("/document?")) return { hash: "review", content: JSON.stringify(evidence) };
  if (url.includes("/workflows/")) return flow;
  throw new Error(`Unexpected request ${url}`);
}

test("saving role policy cannot clear a pending workflow draft; submitting it clears that draft", async () => {
  const h = await mount("MultiModelWorkspace", modelResponse);
  assert.equal(h.dirty, false);
  h.change("来源文稿", "drafts/source.md"); h.change("本次编辑目标", "保留事实，精简表达");
  assert.equal(h.dirty, true);
  h.all((node) => node.type === "form")[1].props.onSubmit({ preventDefault() {} }); await h.flush();
  assert.equal(h.dirty, true);
  h.button("预览首节点路线与预算").props.onClick(); await h.flush();
  h.button("启动候选流程").props.onClick(); await h.flush();
  assert.equal(h.label("本次编辑目标").props.value, ""); assert.equal(h.dirty, false);
  assert.equal(h.calls.filter((call) => call.url.endsWith("/workflows") && call.method === "POST").length, 1);
});

test("revision edits survive a cancelled switch and an overlong instruction never sends a request", async () => {
  const h = await mount("MultiModelWorkspace", modelResponse);
  h.change("已有流程", "flow-a"); await h.flush();
  const issue = h.label("表达");
  assert.equal(issue.props.disabled, false); issue.props.onChange({ target: { checked: true } }); h.render();
  h.change("本轮修订补充", "请保留这个尚未提交的要求"); assert.equal(h.dirty, true);
  h.confirmations.push(false); h.change("已有流程", ""); await h.flush();
  assert.equal(h.label("已有流程").props.value, "flow-a");
  assert.equal(h.label("本轮修订补充").props.value, "请保留这个尚未提交的要求");
  h.change("本轮修订补充", "字".repeat(4001));
  h.button("按所选证据启动局部修订").props.onClick(); await h.flush();
  assert.equal(h.calls.some((call) => call.url.endsWith("/revise")), false);
  assert.match(JSON.stringify(h.all((node) => node.props.role === "alert")), /最多4000字符/);
  assert.equal(h.label("本轮修订补充").props.value.length, 4001);
  h.confirmations.push(true); h.change("已有流程", ""); await h.flush(); assert.equal(h.dirty, false);
});

test("NovelBench keeps an unsaved reason on cancelled reread and only clears it after confirmed reread", async () => {
  const record = { id: "pair", hash: "pair-hash", created_at: "today", status: "awaiting_vote", candidates: [] };
  const h = await mount("NovelBenchWorkspace", (url) => url.endsWith("/benchmarks") ? { benchmarks: [record] } : record);
  h.change("历史比较", "pair"); await h.flush();
  h.change("偏好依据", "仅用于未保存保护的操作备注，不是文学投票"); assert.equal(h.dirty, true);
  const before = h.calls.length; h.confirmations.push(false);
  h.button("核对当前版本").props.onClick(); await h.flush();
  assert.equal(h.calls.length, before); assert.match(h.label("偏好依据").props.value, /未保存保护/);
  h.confirmations.push(true); h.button("核对当前版本").props.onClick(); await h.flush();
  assert.equal(h.label("偏好依据").props.value, ""); assert.equal(h.dirty, false);
  assert.equal(h.calls.some((call) => call.url.endsWith("/vote") || call.url.endsWith("/reveal")), false);
});

test("a research question without book selection remains protected until explicit discard", async () => {
  const h = await mount("ReferenceStudiesWorkspace", (url) => url === "/reference-books" ? { books: [] } : { studies: [] });
  h.button("新建研究").props.onClick(); h.render(); assert.equal(h.dirty, false);
  h.change("统一研究问题", "如何在开局保留读者知道但角色不知道的信息？"); assert.equal(h.dirty, true);
  const before = h.calls.length;
  h.confirmations.push(false); h.button("放弃草案").props.onClick(); await h.flush();
  assert.match(h.label("统一研究问题").props.value, /角色不知道/); assert.equal(h.dirty, true);
  h.confirmations.push(true); h.button("放弃草案").props.onClick(); await h.flush();
  assert.equal(h.dirty, false); assert.equal(h.calls.length, before);
});

test("an evaluation brief is protected before any method selection and no request is sent", async () => {
  const h = await mount("ReferenceEvaluationsWorkspace", (url) => url === "/reference-studies" ? { studies: [] } : { evaluations: [] });
  assert.equal(h.dirty, false); const before = h.calls.length;
  h.change("全新练习简报", "全新人物在暴雨中决定是否守约。"); assert.equal(h.dirty, true);
  assert.equal(h.button("预览四次请求与预算").props.disabled, true);
  h.change("全新练习简报", ""); assert.equal(h.dirty, false);
  h.change("本评测金额上限（美元）", "0.8"); assert.equal(h.dirty, true);
  assert.equal(h.calls.length, before);
});

test("a10 response repair keeps cancelled edits and synchronizes completion without starting paid work", async () => {
  const book = { book_id: "book-a", edition_id: "edition-a", hash: "book-hash", title: "人工书A", counts: {chapters: 1}, outbound: "excerpts", chapters: [] };
  const failed = { id: "batch-a", state: "failed", nodes: [{id: "chapter-00000", kind: "chapter", state: "failed", title: "人工章"}], coverage: {completed_chapters: 0}, created_at: "today" };
  const repaired = {...failed, state: "paused", nodes: [{...failed.nodes[0], state: "succeeded", result_path: "candidate.json"}], coverage: {completed_chapters: 1}};
  const h = await mount("ReferenceBooksWorkspace", (url, method) => {
    if (url === "/reference-books") return { books: [book] };
    if (url === "/reference-books/book-a?offset=0&limit=100") return book;
    if (url === "/reference-books/book-a/batches") return { batches: [failed] };
    if (url.endsWith("/response")) return method === "PUT" ? repaired : {hash: "original-hash", text: "{}", repairable: true};
    if (url.includes("/batches/batch-a")) return failed;
    throw new Error(`Unexpected request ${url}`);
  });
  h.all((node) => node.type === "button" && node.props.className?.startsWith("rb-book "))[0].props.onClick(); await h.flush();
  h.button("逐章研究").props.onClick(); await h.flush();
  h.all((node) => node.type === "button" && node.props.className?.includes("rb-batch-card"))[0].props.onClick(); await h.flush();
  h.button("查看原响应与离线修复（不收费）").props.onClick(); await h.flush();
  h.change("修复说明", "仅修正已发送原句的唯一坐标"); assert.equal(h.dirty, true);
  h.confirmations.push(false); h.button("关闭").props.onClick(); await h.flush();
  assert.match(h.label("修复说明").props.value, /唯一坐标/); assert.equal(h.dirty, true);
  h.button("校验证据并保存修复候选").props.onClick(); await h.flush();
  assert.equal(h.dirty, false);
  assert.equal(h.calls.filter((call) => call.method === "PUT" && call.url.endsWith("/response")).length, 1);
  assert.equal(h.calls.some((call) => call.url.endsWith("/start")), false);
  assert.equal(h.button("继续研究（付费）").props.disabled, false);
});
