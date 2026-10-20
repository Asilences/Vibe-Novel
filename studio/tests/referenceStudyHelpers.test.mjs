import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/referenceStudyHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { graphWindow, studyEvidence, studyMethods } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("a reading checkpoint never renders later knowledge or later edges", () => {
  const nodes = [0, 1, 2].map((read_index) => ({id: `n${read_index}`, read_index, label: "候选", source_refs: []}));
  const edges = [{from: "n0", to: "n1", read_index: 1}, {from: "n1", to: "n2", read_index: 2}];
  assert.deepEqual(graphWindow({nodes, edges}, 0, 30, 0).nodes.map((n) => n.id), ["n0"]);
  assert.equal(graphWindow({nodes, edges}, 0, 30, 0).edges.length, 0);
  assert.equal(graphWindow({nodes, edges}, 0, 30, 1).edges.length, 1);
  assert.equal(graphWindow({nodes, edges}, 1, 1, 2).total, 3);
});

test("study evidence retains full cross-book identities and Unicode offsets", () => {
  const ref = {book_id: "book", edition_id: "edition", chapter_id: "chapter", start_char: 1, end_char: 3, quote: "😀桥"};
  assert.deepEqual(studyEvidence([ref, {...ref, start_char: 1.2}, {...ref, end_char: 0}, {chapter_id: "missing-book"}]), [ref]);
});

test("method indices refer to all persisted checkpoints in reading order", () => {
  assert.deepEqual(studyMethods([{result: {methods: [{title: "前"}]}}, {result: {}}, {result: {methods: [{title: "后"}]}}]).map((m) => [m.index, m.value.title]), [[0, "前"], [1, "后"]]);
});
