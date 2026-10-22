import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const initial = {
  hash: "settings-1", api: { base_url: "https://example.invalid/v1", model: "fixture-model", has_api_key: true,
    price_input_usd: 1, price_output_usd: 2, allow_loopback: false },
  presets: [], environment_overrides: [],
};

// Execute the real component with an offline hook/JSX adapter. This tests
// close/save control flow, not browser layout and not provider compatibility.
async function mount({ multiModel = false, respond } = {}) {
  const source = await readFile(new URL("../src/APISettings.tsx", import.meta.url), "utf8");
  const ast = ts.createSourceFile("APISettings", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const imports = ast.statements.filter(ts.isImportDeclaration).flatMap((item) => {
    const clause = item.importClause;
    return [clause?.name?.text, ...(clause?.namedBindings && ts.isNamedImports(clause.namedBindings)
      ? clause.namedBindings.elements.map((part) => part.name.text) : [])].filter(Boolean);
  });
  const body = ast.statements.filter((item) => !ts.isImportDeclaration(item))
    .map((item) => item.getText(ast).replace(/^export\s+(default\s+)?/, "")).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  const states = [], refs = [], effects = [], calls = [], confirmations = [], questions = [], handlers = new Map();
  let cursor = 0, changed = false, tree, closed = 0;
  const environment = Object.fromEntries(imports.map((name) => [name, () => null]));
  Object.assign(environment, {
    React: { createElement: (type, props, ...children) => ({ type, props: props || {}, children }) },
    useState(value) { const index = cursor++; if (!(index in states)) states[index] = typeof value === "function" ? value() : value;
      return [states[index], (next) => { next = typeof next === "function" ? next(states[index]) : next;
        if (!Object.is(states[index], next)) { states[index] = next; changed = true; } }]; },
    useRef(value) { const index = cursor++; return refs[index] ||= { current: value }; },
    useEffect(action, dependencies) { const index = cursor++, old = effects[index];
      if (!old || dependencies.some((value, at) => !Object.is(value, old.dependencies?.[at])))
        effects[index] = { action, dependencies, pending: true, cleanup: old?.cleanup }; },
    api: async (url, method = "GET", payload) => { calls.push({ url, method, payload });
      if (respond) { const result = respond(url, method, payload); if (result !== undefined) return result; }
      return url === "/settings" ? structuredClone(initial) : { migration_required: !multiModel }; },
    ApiError: class extends Error {},
    requestConfirmation: (message) => { questions.push(message); return Promise.resolve(confirmations.shift() ?? false); },
    window: { addEventListener: (type, fn) => handlers.set(type, fn),
      removeEventListener: (type, fn) => { if (handlers.get(type) === fn) handlers.delete(type); } },
  });
  const Component = new Function(...Object.keys(environment), `${compiled};return APISettings;`)(...Object.values(environment));
  const props = { onClose: () => closed++, onSaved: async () => {}, onModels: () => {} };
  function render() { for (let i = 0; i < 20; i++) { changed = false; cursor = 0; tree = Component(props);
    for (const effect of effects.filter((value) => value?.pending)) { effect.pending = false; effect.cleanup?.(); effect.cleanup = effect.action(); }
    if (!changed) return; } throw new Error("render loop"); }
  async function flush() { for (let i = 0; i < 4; i++) { await new Promise(setImmediate); render(); } }
  function all(predicate, root) { if (arguments.length < 2) root = tree; return root == null || typeof root !== "object" ? [] : Array.isArray(root)
    ? root.flatMap((child) => all(predicate, child)) : [...(predicate(root) ? [root] : []), ...root.children.flatMap((child) => all(predicate, child))]; }
  const text = (node) => node == null || typeof node === "boolean" ? "" : Array.isArray(node) ? node.map(text).join("")
    : typeof node === "object" ? node.children.map(text).join("") : String(node);
  function label(name) { const found = all((node) => node.type === "label" && text(node).startsWith(name));
    assert.equal(found.length, 1, `label ${name}`); return all((node) => ["input", "select"].includes(node.type), found[0])[0]; }
  function button(name) { const found = all((node) => node.type === "button" && text(node) === name); assert.equal(found.length, 1, `button ${name}`); return found[0]; }
  render(); await flush();
  return { render, flush, calls, questions, confirmations, handlers, label, button, all,
    change(name, value) { label(name).props.onChange({ target: { value } }); render(); },
    dismiss: () => tree.props.onClose(), submit: () => all((node) => node.type === "form")[0].props.onSubmit({ preventDefault() {} }),
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); }, get closed() { return closed; } };
}

test("single API pending key and model survive cancellation; confirmed dismissal closes without saving", async () => {
  const h = await mount();
  h.change("模型 ID", "pending-model"); h.change("API Key", "synthetic-private-input");
  assert.equal(h.handlers.has("beforeunload"), true);
  h.confirmations.push(false); await h.dismiss(); await h.flush();
  assert.equal(h.closed, 0); assert.equal(h.label("模型 ID").props.value, "pending-model");
  assert.equal(h.label("API Key").props.value, "synthetic-private-input");
  h.confirmations.push(true); await h.button("关闭").props.onClick();
  assert.equal(h.closed, 1); assert.equal(h.calls.some((call) => call.method === "PUT"), false);
  h.unmount(); assert.equal(h.handlers.has("beforeunload"), false);
});

test("saving blocks dismissal and success clears pending key without a second confirmation", async () => {
  let finish;
  const h = await mount({ respond: (url, method, payload) => method === "PUT" ? new Promise((resolve) => {
    finish = () => resolve({ ...initial, hash: "settings-2", api: { ...initial.api, ...payload.api, has_api_key: true } });
  }) : undefined });
  h.change("API Key", "synthetic-private-input"); h.submit(); h.render(); await h.flush();
  await h.dismiss(); assert.equal(h.closed, 0); assert.equal(h.questions.length, 0);
  assert.equal(h.button("关闭").props.disabled, true);
  finish(); await h.flush(); assert.equal(h.label("API Key").props.value, "");
  assert.equal(h.handlers.has("beforeunload"), false);
  await h.dismiss(); assert.equal(h.closed, 1); assert.equal(h.questions.length, 0);
  assert.equal(h.calls.filter((call) => call.method === "PUT").length, 1);
});

test("failed save and read-only refresh keep pending values and still protect dismissal", async () => {
  const h = await mount({ respond: (url, method) => { if (method === "PUT") throw new Error("offline-save-failure"); } });
  h.change("模型 ID", "pending-model"); h.change("API Key", "synthetic-private-input");
  h.submit(); await h.flush(); h.button("重新读取配置").props.onClick(); await h.flush();
  assert.equal(h.label("模型 ID").props.value, "pending-model");
  assert.equal(h.label("API Key").props.value, "synthetic-private-input");
  assert.equal(h.questions.length, 0); h.confirmations.push(false); await h.dismiss();
  assert.equal(h.closed, 0); assert.equal(h.calls.filter((call) => call.method === "PUT").length, 1);
});

test("clean and multi-model read-only settings close without asking", async () => {
  for (const multiModel of [false, true]) {
    const h = await mount({ multiModel }); await h.dismiss();
    assert.equal(h.closed, 1); assert.equal(h.questions.length, 0);
    assert.equal(h.handlers.has("beforeunload"), false);
    if (multiModel) assert.equal(h.all((node) => node.type === "input").length, 0);
  }
});

test("price and key-clear edits protect refresh; reverting all edits needs no confirmation", async () => {
  const h = await mount(); h.change("输入价格", "");
  let prevented = false;
  const event = { preventDefault() { prevented = true; }, returnValue: null };
  h.handlers.get("beforeunload")(event); assert.equal(prevented, true); assert.equal(event.returnValue, "");
  h.confirmations.push(false); await h.dismiss(); assert.equal(h.closed, 0);
  h.change("输入价格", "not-a-price"); assert.equal(h.handlers.has("beforeunload"), true);
  h.change("输入价格", "1.0"); assert.equal(h.handlers.has("beforeunload"), false);
  h.label("清除本机已保存的密钥").props.onChange({ target: { checked: true } }); h.render();
  assert.equal(h.handlers.has("beforeunload"), true);
  h.label("清除本机已保存的密钥").props.onChange({ target: { checked: false } }); h.render();
  await h.dismiss(); assert.equal(h.closed, 1); assert.equal(h.questions.length, 1);
  assert.equal(h.calls.some((call) => call.method === "PUT"), false);
});

test("a stale asynchronous confirmation cannot discard newer input or an unmounted form", async () => {
  for (const unmount of [false, true]) {
    const h = await mount(); h.change("模型 ID", "first-pending-model");
    let accept; h.confirmations.push(new Promise((resolve) => { accept = resolve; }));
    const pending = h.dismiss();
    if (unmount) h.unmount(); else h.change("模型 ID", "newer-pending-model");
    accept(true); await pending; assert.equal(h.closed, 0);
    assert.equal(h.calls.some((call) => call.method === "PUT"), false);
  }
});
