import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/referenceMethodHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { methodSelection, researchEvidence, researchMethods, researchOrigin } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("method selections preserve saved node indices and stay within the six candidate limit", () => {
  assert.deepEqual(methodSelection([5, 2, 2, 0, -1, 6, 1.5, NaN, 99], 6), [0, 2, 5]);
  assert.deepEqual(methodSelection([0, 4, 1], 2), [0, 1]);
  const refs = [{ chapter_id: "ch", start_char: 3, end_char: 5, quote: "原句" }];
  assert.deepEqual(researchMethods({ methods: [{title: "第一项", steps: ["先观察"], limitations: ["不能证明全书"], source_refs: refs}, null, {title: "第三项", steps: [], limitations: [], source_refs: []}] }).map((item) => item.title), ["第一项", "未命名方法候选", "第三项"]);
  assert.equal(researchMethods({ result: { methods: [{ title: "不是当前保存结果" }] } }).length, 0);
  assert.equal(researchMethods(null).length, 0);
});

test("research evidence retains exact codepoint ranges and rejects ordinary source refs", () => {
  const ref = { chapter_id: "chapter-a", start_char: 1, end_char: 4, quote: "😀原句", source_id: "must-not-turn-into-source" };
  assert.deepEqual(researchEvidence([ref, {...ref, start_char: -1}, {...ref, start_char: 1.1}, {...ref, end_char: 1}, {source_id: "ordinary", source_version_id: "source-v", start_char: 0, end_char: 2, quote: "原句"}]), [{ chapter_id: "chapter-a", start_char: 1, end_char: 4, quote: "😀原句" }]);
});

test("read-only research origin requires durable book, edition, batch, node and result identity", () => {
  const value = {book_id: "book", edition_id: "fixed-edition", batch_id: "batch", node_id: "chapter-00001", result_hash: "saved-result", evidence: [], raw_dependencies: {count: 1200, hash: "whole-coverage"}};
  assert.deepEqual(researchOrigin(value), value);
  assert.equal(researchOrigin({...value, edition_id: ""}), null);
  assert.equal(researchOrigin({source_id: "ordinary-card"}), null);
});
