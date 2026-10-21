import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// Mount the actual Modal function with isolated hook/DOM adapters. This runs
// its real keyboard listeners, including cleanup during a close callback.
const source = await readFile(new URL("../src/ui.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("ui.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declaration = ast.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "Modal");
assert.ok(declaration);
const compiled = ts.transpileModule(declaration.getText(ast).replace(/^export\s+/, ""), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
}).outputText;

function harness() {
  const listeners = [], dialogs = [];
  const document = {
    activeElement: null,
    querySelectorAll: () => dialogs,
    addEventListener: (_type, callback) => listeners.push(callback),
    removeEventListener: (_type, callback) => {
      const index = listeners.indexOf(callback);
      if (index >= 0) listeners.splice(index, 1);
    },
  };
  function mount(name, onClose) {
    const elements = ["INPUT", "BUTTON"].map((tagName, i) => ({
      tagName, name: `${name}-${i}`,
      focus() { document.activeElement = this; },
    }));
    const node = { querySelectorAll: () => elements };
    dialogs.push(node);
    let refCount = 0, cleanup;
    const environment = {
      document,
      useRef: (initial) => ({ current: refCount++ === 0 ? node : initial }),
      useId: () => name,
      useEffect: (effect) => { cleanup = effect(); },
      React: { createElement: () => null },
      IconButton: () => null,
      X: () => null,
    };
    const Modal = new Function(...Object.keys(environment), `${compiled}\nreturn Modal;`)(...Object.values(environment));
    Modal({ title: name, children: null, onClose });
    return {
      elements,
      unmount() {
        cleanup();
        const index = dialogs.indexOf(node);
        if (index >= 0) dialogs.splice(index, 1);
      },
    };
  }
  function dispatch(key, { shiftKey = false, reverse = false } = {}) {
    const event = { key, shiftKey, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const listener of reverse ? [...listeners].reverse() : [...listeners]) listener(event);
    return event;
  }
  return { mount, dispatch, document };
}

test("Escape asks only the nested picker to close and cancelling keeps the parent open", () => {
  const h = harness();
  let parentClosed = 0, confirmations = 0;
  const parent = h.mount("materials", () => { parentClosed++; });
  const child = h.mount("retrieval", () => { confirmations++; /* Author cancels discarding unsent picks. */ });
  assert.equal(h.dispatch("Escape").defaultPrevented, true);
  assert.equal(confirmations, 1);
  assert.equal(parentClosed, 0);
  assert.equal(h.dispatch("Escape", { reverse: true }).defaultPrevented, true);
  assert.equal(confirmations, 2);
  assert.equal(parentClosed, 0);
  child.unmount(); parent.unmount();
});

test("closing the top modal cannot also close its parent in the same Escape event", () => {
  for (const reverse of [false, true]) {
    const h = harness();
    let parentClosed = 0;
    const parent = h.mount("materials", () => { parentClosed++; });
    const child = h.mount("retrieval", () => child.unmount());
    h.dispatch("Escape", { reverse });
    assert.equal(parentClosed, 0);
    h.dispatch("Escape");
    assert.equal(parentClosed, 1);
    parent.unmount();
  }
});

test("Tab and Shift+Tab wrap only within the top dialog then return to the parent", () => {
  const h = harness();
  const parent = h.mount("materials", () => {});
  const child = h.mount("retrieval", () => {});
  child.elements[1].focus();
  assert.equal(h.dispatch("Tab").defaultPrevented, true);
  assert.equal(h.document.activeElement, child.elements[0]);
  assert.equal(h.dispatch("Tab", { shiftKey: true, reverse: true }).defaultPrevented, true);
  assert.equal(h.document.activeElement, child.elements[1]);
  child.elements[0].focus();
  assert.equal(h.dispatch("Tab").defaultPrevented, false);
  assert.equal(h.document.activeElement, child.elements[0]);
  child.unmount();
  parent.elements[1].focus();
  assert.equal(h.dispatch("Tab").defaultPrevented, true);
  assert.equal(h.document.activeElement, parent.elements[0]);
  parent.unmount();
});
