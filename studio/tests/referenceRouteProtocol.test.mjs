import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// Render the actual route summary so only an explicitly frozen protocol
// choice is described as disabled; legacy/default routes make no such claim.
const source = await readFile(new URL("../src/ReferenceBooksWorkspace.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("ReferenceBooksWorkspace.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declaration = ast.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "ModelRoute");
assert.ok(declaration);
const compiled = ts.transpileModule(declaration.getText(ast), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
}).outputText;
const React = { createElement: (type, props, ...children) => ({ type, props, children }) };
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const ModelRoute = new Function("React", "object", `${compiled}\nreturn ModelRoute;`)(React, object);
const text = (node) => Array.isArray(node) ? node.map(text).join("")
  : node && typeof node === "object" ? text(node.children)
  : typeof node === "string" || typeof node === "number" ? String(node) : "";

test("研究预览展示冻结的关闭思考选项，同时保留模型与预算", () => {
  const output = text(ModelRoute({ value: {
    candidates: [{ label: "研究模型", model: "deepseek-flash" }],
    request_options: { thinking: "disabled" }, estimated_input_tokens: 500, max_output_tokens: 65536,
  } }));
  assert.match(output, /结构化研究关闭模型思考，输出额度用于候选 JSON/);
  assert.match(output, /研究模型 · deepseek-flash/);
  assert.match(output, /预算预留输出Token上限65,536/);
});

test("旧批次及未关闭思考的路线不显示关闭声明", () => {
  for (const request_options of [undefined, {}, { thinking: "enabled" }, []]) {
    const output = text(ModelRoute({ value: { request_options } }));
    assert.doesNotMatch(output, /关闭模型思考/);
  }
});
