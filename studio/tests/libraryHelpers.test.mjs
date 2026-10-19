import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/libraryHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { bytesBase64, emptyLibraryEntry, entryPayload, libraryPreviewHash, libraryTaskConfiguration, methodContent, sourceExcerpt, sourceLineAt, sourceReference } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("source excerpts preserve original CRLF and Unicode codepoint ranges", () => {
  const text = "甲😀\r\n乙\n尾";
  assert.equal(sourceExcerpt(text, 1, 6), "😀\r\n乙\n");
  assert.deepEqual(sourceReference({ id: "original", version_id: "fixed" }, text, 1, 5), { source_id: "original", source_version_id: "fixed", start_char: 1, end_char: 5, quote: "😀\r\n乙" });
  assert.equal(sourceLineAt(text, 4), 2);
  assert.equal(sourceLineAt(text, 6), 3);
  for (const [start, end] of [[-1, 2], [2, 2], [3, 2], [0, 100], [0.5, 2], [0, NaN]]) assert.throws(() => sourceReference({ id: "a", version_id: "b" }, text, start, end));
});

test("binary import retains BOM, CRLF and bytes across encoding chunks", () => {
  const bytes = new Uint8Array(32771);
  for (let index = 0; index < bytes.length; index++) bytes[index] = index % 256;
  bytes.set([0xef, 0xbb, 0xbf, 13, 10], 0);
  assert.deepEqual(Buffer.from(bytesBase64(bytes.buffer), "base64"), Buffer.from(bytes));
});

test("card edits strip transport and adoption metadata but preserve scoped evidence and withdrawal", () => {
  const entry = { ...emptyLibraryEntry("method"), id: "card", hash: "old", revision: 9, adopted: true, adoption: { id: "approved" }, title: "场景变化", withdrawn: true, withdrawal_reason: "依据待补", source_refs: [{ source_id: "source", source_version_id: "fixed", start_char: 0, end_char: 2, quote: "原句", scope: "personal", owner: "personal" }] };
  const payload = entryPayload(entry);
  assert.equal(payload.withdrawn, true);
  assert.equal(payload.withdrawal_reason, "依据待补");
  assert.deepEqual(payload.source_refs, entry.source_refs);
  for (const field of ["hash", "revision", "version_id", "adopted", "adoption"]) assert.equal(field in payload, false);
  assert.equal("id" in entryPayload(emptyLibraryEntry()), false);
});

test("model configuration freezes all five version fields and rejects incomplete previews", () => {
  const route = { policy_revision: 3, policy_hash: "policy", registry_revision: 2, candidates: [{ registry_hash: "registry", legacy_marker: null, id: "model" }] };
  assert.deepEqual(libraryTaskConfiguration(route), { policy_revision: 3, policy_hash: "policy", registry_revision: 2, registry_hash: "registry", registry_legacy_marker: null });
  assert.equal(libraryTaskConfiguration({ ...route, candidates: [] }), null);
  assert.equal(libraryTaskConfiguration({ ...route, candidates: [{ registry_hash: "registry" }] }), null);
});

test("library version binding works for method and normal previews without treating recommendations as input", () => {
  assert.equal(libraryPreviewHash({ method_preview: { snapshot: { hash: "method-input" } } }), "method-input");
  assert.equal(libraryPreviewHash({ library_preview: { snapshot: { hash: "normal-input" } } }), "normal-input");
  assert.equal(libraryPreviewHash({ library_preview: { snapshot: null, recommendations: [{ hash: "not-sent" }] } }), undefined);
  assert.equal(libraryPreviewHash({}), undefined);
});

test("structured method evidence is distinguished from author prose and invalid JSON", () => {
  const entry = emptyLibraryEntry("method");
  assert.equal(methodContent({ ...entry, content: "作者的一段理解" }), null);
  assert.equal(methodContent({ ...entry, content: "[]" }), null);
  const bundle = { method: { steps: ["识别人物选择"] }, source_cases: [], synthetic_exercises: [] };
  assert.deepEqual(methodContent({ ...entry, content: JSON.stringify(bundle) }), bundle);
  assert.equal(methodContent({ ...entry, type: "material", content: JSON.stringify(bundle) }), null);
});


test("editing a generated method sends one consistent structure in its evidence bundle and method fields", () => {
  const original = { method: { steps: ["旧步骤"] }, source_cases: [{ description: "保留出处" }], synthetic_exercises: [] };
  const entry = { ...emptyLibraryEntry("method"), content: JSON.stringify(original), method: { ...emptyLibraryEntry("method").method, steps: ["作者修正的步骤"] } };
  const payload = entryPayload(entry);
  assert.deepEqual(JSON.parse(payload.content).method, payload.method);
  assert.deepEqual(JSON.parse(payload.content).source_cases, original.source_cases);
});
