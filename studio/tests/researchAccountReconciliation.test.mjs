import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const attempt = { id: "a".repeat(32), state: "reserved", model: "offline-model", created_at: "2000-01-01",
  reserved_cost_usd: .15, attempt_hash: "b".repeat(64), cached_response_hash: null };
const initial = { config: { model_id: "default_api", max_cost_usd: 5, max_calls: 256,
  price_input_usd: 1, price_output_usd: 2, price_source: "offline-price" }, config_hash: "c".repeat(64),
  calls: 1, reserved_cost_usd: .15, reported_cost_usd: 0, uncertain_calls: 1, pending_reconciliations: [attempt] };

// Exercise the real component's confirmation, request and draft control flow
// with an offline React adapter. No browser layout or API provider is involved.
async function mount({ cached = false, fail = false } = {}) {
  const source = await readFile(new URL("../src/ResearchAccountPanel.tsx", import.meta.url), "utf8");
  const ast = ts.createSourceFile("ResearchAccountPanel", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = ast.statements.filter(item => !ts.isImportDeclaration(item))
    .map(item => item.getText(ast).replace(/^export\s+(default\s+)?/, "")).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  const states = [], effects = [], calls = [], confirmations = [], questions = [], dirty = [];
  const account = structuredClone(initial);
  if (cached) account.pending_reconciliations[0].cached_response_hash = "d".repeat(64);
  let cursor = 0, changed = false, tree;
  class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
  const env = {
    React: { createElement: (type, props, ...children) => ({ type, props: props || {}, children }) },
    useState(value) { const index = cursor++; if (!(index in states)) states[index] = value;
      return [states[index], next => { next = typeof next === "function" ? next(states[index]) : next;
        if (!Object.is(states[index], next)) { states[index] = next; changed = true; } }]; },
    useEffect(action, dependencies) { const index = cursor++, old = effects[index];
      if (!old || dependencies.some((value, at) => !Object.is(value, old.dependencies?.[at])))
        effects[index] = { action, dependencies, pending: true, cleanup: old?.cleanup }; },
    api: async (url, method = "GET", payload) => { calls.push({ url, method, payload });
      if (method === "POST") {
        if (fail) throw new ApiError("原研究执行器仍在运行，请稍后核对", 409);
        return { ...account, pending_reconciliations: cached ? [] : [{ ...account.pending_reconciliations[0], state: "unknown", attempt_hash: "e".repeat(64) }] };
      }
      if (method === "PUT") return { ...account, config: payload };
      return url === "/research-account" ? structuredClone(account)
        : { profiles: [{ id: "default_api", label: "离线模型", model: "offline-model", available: true }] }; },
    ApiError,
    researchModelDraft: (draft, model_id) => ({ ...draft, model_id }),
    requestConfirmation: message => { questions.push(message); return Promise.resolve(confirmations.shift() ?? false); },
  };
  const Component = new Function(...Object.keys(env), `${compiled}; return ResearchAccountPanel;`)(...Object.values(env));
  const props = { onDirtyChange: value => dirty.push(value) };
  function render() { for (let i = 0; i < 20; i++) { changed = false; cursor = 0; tree = Component(props);
    for (const effect of effects.filter(value => value?.pending)) { effect.pending = false; effect.cleanup?.(); effect.cleanup = effect.action(); }
    if (!changed) return; } throw new Error("render loop"); }
  async function flush() { for (let i = 0; i < 4; i++) { await new Promise(setImmediate); render(); } }
  function all(predicate, root) { if (arguments.length < 2) root = tree; return root == null || typeof root !== "object" ? [] : Array.isArray(root)
    ? root.flatMap(child => all(predicate, child)) : [...(predicate(root) ? [root] : []), ...root.children.flatMap(child => all(predicate, child))]; }
  const text = node => node == null || typeof node === "boolean" ? "" : Array.isArray(node) ? node.map(text).join("")
    : typeof node === "object" ? node.children.map(text).join("") : String(node);
  function button(name) { const found = all(node => node.type === "button" && text(node) === name);
    assert.equal(found.length, 1, `button ${name}`); return found[0]; }
  function field(name) { const found = all(node => node.type === "label" && text(node).startsWith(name));
    assert.equal(found.length, 1, `field ${name}`); return all(node => ["input", "select", "textarea"].includes(node.type), found[0])[0]; }
  render(); await flush();
  return { calls, questions, confirmations, dirty, flush, render, button, field, all, text,
    change(name, value) { field(name).props.onChange({ target: { value } }); render(); },
    get content() { return text(tree); } };
}

test("historical reconciliation requires a reason; cancellation keeps input and makes no mutation", async () => {
  const h = await mount();
  assert.equal(h.button("核对执行器并保留未知费用").props.disabled, true);
  h.change("核对说明", "核对原中断，不要重发");
  h.confirmations.push(false); h.button("核对执行器并保留未知费用").props.onClick(); await h.flush();
  assert.equal(h.calls.filter(call => call.method === "POST").length, 0);
  assert.equal(h.field("核对说明").props.value, "核对原中断，不要重发");
  assert.equal(h.dirty.at(-1), true);
  assert.match(h.questions[0], /仍保留 \$0\.150000 占用/);
});

test("confirmed no-cache reconciliation sends frozen hashes, preserves costs and unsaved budget draft", async () => {
  const h = await mount();
  h.change("共享总上限", "35"); h.change("核对说明", "作者核对中断记录");
  h.confirmations.push(true); h.button("核对执行器并保留未知费用").props.onClick(); await h.flush();
  const posts = h.calls.filter(call => call.method === "POST");
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].payload, { action: "mark_unknown", expected_attempt_hash: attempt.attempt_hash,
    expected_response_hash: null, reason: "作者核对中断记录" });
  assert.equal(h.field("共享总上限").props.value, 35);
  assert.equal(h.dirty.at(-1), true);
  assert.match(h.content, /\$0\.150000 \/ \$5/);
  assert.match(h.content, /原费用仍占用额度/);
  assert.equal(h.all(node => node.type === "textarea").length, 0);
});

test("cache recovery uses a distinct explicit action and clears only its completed reason", async () => {
  const h = await mount({ cached: true });
  h.change("核对说明", "保留原响应，按冻结报价核算");
  h.confirmations.push(true); h.button("核验缓存并恢复到账").props.onClick(); await h.flush();
  const post = h.calls.find(call => call.method === "POST");
  assert.equal(post.payload.action, "restore_response");
  assert.equal(post.payload.expected_response_hash, "d".repeat(64));
  assert.match(h.questions[0], /不会重发 API/);
  assert.match(h.content, /原保守占用保留/);
  assert.equal(h.dirty.at(-1), false);
});

test("a still-active sender rejection retains the reason, budget edits and original charge", async () => {
  const h = await mount({ fail: true });
  h.change("共享总上限", "35"); h.change("核对说明", "请核对原执行器");
  h.confirmations.push(true); h.button("核对执行器并保留未知费用").props.onClick(); await h.flush();
  assert.equal(h.field("核对说明").props.value, "请核对原执行器");
  assert.equal(h.field("共享总上限").props.value, 35);
  assert.match(h.content, /原研究执行器仍在运行/);
  assert.match(h.content, /\$0\.150000 \/ \$5/);
  assert.equal(h.dirty.at(-1), true);
});
