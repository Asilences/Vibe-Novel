import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const source = await readFile(new URL("../src/referenceRetrievalHelpers.ts", import.meta.url), "utf8");
const {outputText} = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022}});
const {selectedRetrievalIds, retrievalIdentity, retrievalSelection} = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("only current result ids survive selection; duplicates and old responses cannot join", () => {
  assert.deepEqual(selectedRetrievalIds(["old", "b", "a", "b"], [{id: "a"}, {id: "b"}]), ["b", "a"]);
});
test("query, index, layer and retrieval mode all invalidate a prior result identity", () => {
  const id = retrievalIdentity("index", "悬念", ["structure", "original"], true, true);
  assert.equal(id, retrievalIdentity("index", "悬念", ["original", "structure"], true, true));
  assert.notEqual(id, retrievalIdentity("other", "悬念", ["original", "structure"], true, true));
  assert.notEqual(id, retrievalIdentity("index", "人物", ["original", "structure"], true, true));
  assert.notEqual(id, retrievalIdentity("index", "悬念", ["original", "structure"], false, true));
});
test("task selection carries a precise frozen preview and rejects unbound scopes", () => {
  assert.deepEqual(retrievalSelection("a".repeat(32), ["b".repeat(32)], "c".repeat(64)), {retrieval_selection: {index_id: "a".repeat(32), ids: ["b".repeat(32)], expected_preview: "c".repeat(64)}});
  assert.throws(() => retrievalSelection("../other", ["b".repeat(32)], "c".repeat(64)));
  assert.throws(() => retrievalSelection("a".repeat(32), [], "c".repeat(64)));
  assert.throws(() => retrievalSelection("a".repeat(32), ["b".repeat(32)], ""));
});
