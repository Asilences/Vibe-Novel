import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/CreationWorkspace.tsx", import.meta.url), "utf8");
const helperSource = await readFile(new URL("../src/creationHelpers.ts", import.meta.url), "utf8");
const helperModule = ts.transpileModule(helperSource, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const helpers = await import(`data:text/javascript;base64,${Buffer.from(helperModule).toString("base64")}`);
const ast = ts.createSourceFile("CreationWorkspace.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const imported = ast.statements.filter(ts.isImportDeclaration).flatMap((item) => {
  const clause = item.importClause;
  return [clause?.name?.text, ...(clause?.namedBindings && ts.isNamedImports(clause.namedBindings) ? clause.namedBindings.elements.map((part) => part.name.text) : [])].filter(Boolean);
});
const body = ast.statements.filter((item) => !ts.isImportDeclaration(item)).map((item) => item.getText(ast).replace(/^export\s+(default\s+)?/, "")).join("\n");
const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
const path = "outlines/proposals/story-outline-test.md";
const brief = {
  schema_version: 1, novel_id: "acceptance", catalog_version: "test", reading_category: "undecided", story_seed: "原有故事想法", tags: [], protagonists: [],
  narration: { ensemble: "undecided", person: "undecided", focus: "" },
  advantage: { status: "no", kind: "", ability: "", limits: "", cost: "", reveal: "" },
  opening: { era: "", time: "", place: "", situation: "", inciting_incident: "", obstacle: "" },
  scale: { target_words_min: null, target_words_max: null, volume_count: null, chapter_words: null, ending: "" },
  style: { tone: "", pace: "", dialogue: "" }, constraints: { hard: [], preferences: [], avoid: [] }, unresolved: [],
};
const envelope = { brief, path: "outlines/source/creative-brief.json", hash: "saved", version_id: "brief-v1" };
const selection = { selection: { document_path: path, document_hash: "outline", document_version_id: "outline-v1" }, path: "reviews/creation-selection.json", hash: "selection", version_id: "selected-v1", stale: false, drift: [] };
const catalog = { catalog_version: "test", categories: [], dimensions: [], tags: [], genres: [{ id: "general", label: "通用", capability: "general" }], limits: { story_seed: 10000, tag_label: 100, tags_per_dimension: 20, protagonists: 10, list_items: 40 } };

// Run the actual component's event handlers and state effects, without a DOM,
// browser, provider, or mutation outside this in-memory fixture.
async function mount() {
  const states = [], refs = [], effects = [], dirtyValues = [], calls = [], decisions = [], confirmations = [];
  const control = { failReread: false };
  let cursor = 0, changed = false, tree, saved = structuredClone(envelope);
  const environment = Object.fromEntries(imported.map((name) => [name, () => null]));
  Object.assign(environment, helpers, {
    React: { createElement: (type, props, ...children) => ({ type, props: props || {}, children }) },
    useState(initial) {
      const index = cursor++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], (value) => { const next = typeof value === "function" ? value(states[index]) : value; if (!Object.is(next, states[index])) { states[index] = next; changed = true; } }];
    },
    useRef(initial) { const index = cursor++; return refs[index] ||= { current: initial }; },
    useMemo: (calculate) => calculate(),
    useEffect(action, dependencies) {
      const index = cursor++, old = effects[index];
      if (!old || !dependencies || dependencies.some((value, at) => !Object.is(value, old.dependencies?.[at]))) effects[index] = { action, dependencies, pending: true, cleanup: old?.cleanup };
    },
    api: async (url, method = "GET", data) => {
      calls.push({ url, method, data });
      if (url === "/creation/catalog") return structuredClone(catalog);
      if (url.endsWith("/creative-brief")) {
        if (control.failReread && method === "GET") throw new Error("Offline reread failure");
        if (method === "PUT") saved = { ...saved, brief: structuredClone(data.brief), hash: "saved-2", version_id: "brief-v2" };
        return structuredClone(saved);
      }
      if (url.endsWith("/creation-selection")) return structuredClone(selection);
      if (url.includes("/document?")) return { path, hash: "outline", content: "候选总纲" };
      if (url.includes("/history?")) return { versions: [{ id: "outline-v1", hash: "outline" }] };
      throw new Error(`Unexpected request ${url}`);
    },
    ApiError: class extends Error {}, projectUrl: () => "/projects/acceptance", activeStates: new Set(), leaf: (value) => value,
    marked: { parse: (value) => value }, DOMPurify: { sanitize: (value) => value },
    useTaskReference: () => ({ ticket: null, selection: null, consume() {} }),
    requestConfirmation: async (message) => { confirmations.push(message); return decisions.shift() ?? false; },
  });
  const Component = new Function(...Object.keys(environment), `${compiled}\nreturn CreationWorkspace;`)(...Object.values(environment));
  const props = { project: { novel_id: "acceptance", title: "隔离测试", genre: "general" }, documents: [{ path, hash: "outline" }], jobs: [], onChanged: async () => {}, onJob() {}, onOpenDocument() {}, onPlanning() {}, onSettings() {}, onDirtyChange: (value) => dirtyValues.push(value) };
  function render() {
    for (let count = 0; count < 20; count++) {
      changed = false; cursor = 0; tree = Component(props);
      for (const effect of effects.filter((item) => item?.pending)) { effect.pending = false; effect.cleanup?.(); effect.cleanup = effect.action(); }
      if (!changed) return;
    }
    throw new Error("Unexpected render loop");
  }
  async function flush() { for (let i = 0; i < 4; i++) { await new Promise(setImmediate); render(); } }
  function all(predicate, root) { if (arguments.length < 2) root = tree; return !root || typeof root !== "object" ? [] : Array.isArray(root) ? root.flatMap((child) => all(predicate, child)) : [...(predicate(root) ? [root] : []), ...root.children.flatMap((child) => all(predicate, child))]; }
  const text = (node) => node == null || typeof node === "boolean" ? "" : Array.isArray(node) ? node.map(text).join("") : typeof node === "object" ? node.children.map(text).join("") : String(node);
  const button = (name) => { const found = all((node) => node.type === "button" && text(node) === name); assert.ok(found.length, `button ${name}`); return found[0]; };
  const field = (name) => {
    if (name === "story_seed") return all((node) => node.props.id === "creation-seed")[0];
    const label = all((node) => node.type === "label" && text(node).startsWith(name))[0];
    assert.ok(label, `field ${name}`);
    return all((node) => ["input", "textarea", "select"].includes(node.type), label)[0];
  };
  const change = (name, value) => { field(name).props.onChange({ target: { value } }); render(); };
  render(); await flush();
  return { all, button, field, change, render, flush, calls, decisions, confirmations, control, get dirty() { return dirtyValues.at(-1); } };
}

test("an unadded tag needs discard confirmation before reread, and cancellation sends no request", async () => {
  const h = await mount();
  h.change("标签名称", "俱乐部经营"); assert.equal(h.dirty, true);
  const count = h.calls.length;
  h.decisions.push(false); h.button("读取最新").props.onClick(); await h.flush();
  assert.equal(h.calls.length, count); assert.equal(h.field("标签名称").props.value, "俱乐部经营");
  assert.equal(h.dirty, true); assert.match(h.confirmations[0], /尚未添加的自定义标签/);
  h.decisions.push(true); h.button("读取最新").props.onClick(); await h.flush();
  assert.equal(h.calls.length, count + 3); assert.equal(h.field("标签名称").props.value, ""); assert.equal(h.dirty, false);
});

test("a failed reread retains both the unadded tag and unsaved brief", async () => {
  const h = await mount();
  h.change("story_seed", "尚未保存的故事想法"); h.change("标签名称", "排除模板");
  h.control.failReread = true; h.decisions.push(true);
  h.button("读取最新").props.onClick(); await h.flush();
  assert.equal(h.field("story_seed").props.value, "尚未保存的故事想法");
  assert.equal(h.field("标签名称").props.value, "排除模板"); assert.equal(h.dirty, true);
  assert.equal(h.calls.filter((call) => call.method !== "GET").length, 0);
});

test("saving the brief cannot clear an unadded tag or allow selecting a candidate that ignores it", async () => {
  const h = await mount();
  h.change("story_seed", "已保存的第二版故事想法"); h.change("标签名称", "未添加标签");
  h.button("保存构思").props.onClick(); await h.flush();
  assert.equal(h.field("标签名称").props.value, "未添加标签"); assert.equal(h.dirty, true);
  const put = h.calls.find((call) => call.method === "PUT"); assert.equal(put.data.brief.story_seed, "已保存的第二版故事想法"); assert.deepEqual(put.data.brief.tags, []);
  h.change("选择要查看的总纲", path); await h.flush();
  assert.equal(h.field("标签名称").props.value, "未添加标签");
  assert.equal(h.button("打开编辑器修改").props.disabled, true);
  assert.equal(h.button("创建规划包").props.disabled, true);
  assert.equal(h.button("选用这版继续规划").props.disabled, true);
  h.button("选用这版继续规划").props.onClick(); await h.flush();
  assert.equal(h.calls.some((call) => call.method === "PUT" && call.url.endsWith("/creation-selection")), false);
  h.button("核对版本").props.onClick(); await h.flush();
  assert.equal(h.field("标签名称").props.value, "未添加标签"); assert.equal(h.confirmations.length, 0);
});

test("a clean reread needs no discard dialog and never sends generation requests", async () => {
  const h = await mount(); const count = h.calls.length;
  h.button("读取最新").props.onClick(); await h.flush();
  assert.equal(h.calls.length, count + 3); assert.equal(h.confirmations.length, 0);
  assert.equal(h.calls.every((call) => call.method === "GET"), true); assert.equal(h.dirty, false);
});
