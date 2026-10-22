import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function queue() {
  const source = await readFile(new URL("../src/confirm.tsx", import.meta.url), "utf8");
  const ast = ts.createSourceFile("confirm.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = ast.statements.filter((item) => !ts.isImportDeclaration(item))
    .map((item) => item.getText(ast).replace(/^export\s+/, "")).join("\n");
  const js = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React } }).outputText;
  return new Function(`${js}; return { requestConfirmation, subscribe, currentConfirmation, settleConfirmation };`)();
}

test("confirmation requires a host, serializes requests and ignores an old dialog response", async () => {
  const q = await queue();
  assert.equal(await q.requestConfirmation("no host"), false);
  const unsubscribe = q.subscribe(() => {});
  const first = q.requestConfirmation("first");
  const id = q.currentConfirmation().id;
  const second = q.requestConfirmation("second");
  q.settleConfirmation(id, false);
  assert.equal(await first, false);
  assert.equal(q.currentConfirmation().message, "second");
  q.settleConfirmation(id, true);
  assert.equal(q.currentConfirmation().message, "second");
  q.settleConfirmation(q.currentConfirmation().id, true);
  assert.equal(await second, true);
  assert.equal(q.currentConfirmation(), null);
  unsubscribe();
});

test("unmount cancels all pending consent and never grants navigation", async () => {
  const q = await queue();
  const unsubscribe = q.subscribe(() => {});
  const first = q.requestConfirmation("pending");
  const second = q.requestConfirmation("queued");
  unsubscribe();
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.equal(q.currentConfirmation(), null);
});

test("action labels stay with their request and do not replace discard defaults", async () => {
  const q = await queue();
  const unsubscribe = q.subscribe(() => {});
  const update = q.requestConfirmation("确认替换固定版本？", {
    title: "更新本书固定选用", confirmLabel: "更新固定版本", cancelLabel: "保留旧版本",
  });
  const updateId = q.currentConfirmation().id;
  const discard = q.requestConfirmation("放弃未保存修改？");
  assert.equal(q.currentConfirmation().title, "更新本书固定选用");
  assert.equal(q.currentConfirmation().confirmLabel, "更新固定版本");
  assert.equal(q.currentConfirmation().cancelLabel, "保留旧版本");
  q.settleConfirmation(updateId, false);
  assert.equal(await update, false);
  assert.equal(q.currentConfirmation().title, "确认是否放弃修改");
  assert.equal(q.currentConfirmation().confirmLabel, "放弃修改");
  assert.equal(q.currentConfirmation().cancelLabel, "继续编辑");
  q.settleConfirmation(q.currentConfirmation().id, true);
  assert.equal(await discard, true);
  unsubscribe();
});
