import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/creationHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { addAuthorTag, briefWarnings, generationIdentity, outlineSourceChanged, previewConfiguration, selectedTags, setTagDecision } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const management = { id: "plot:management", label: "经营", dimensions: ["plot"] };
const friend = { id: "theme:friendship", label: "友情", dimensions: ["theme", "emotion"] };
const catalog = { tags: [management, friend] };

test("a plot tag selected from either projection has only one author decision", () => {
  const once = setTagDecision([], management, "selected");
  assert.equal(selectedTags(once, "plot").length, 1);
  assert.deepEqual(setTagDecision(once, management, "selected"), []);
});

test("cross-dimensional catalog tags share one record and both projections", () => {
  const tags = setTagDecision([], friend, "selected");
  assert.equal(tags.length, 1);
  assert.equal(selectedTags(tags, "theme")[0].id, selectedTags(tags, "emotion")[0].id);
});

test("avoiding a selected tag replaces its decision without duplicating it", () => {
  const tags = setTagDecision(setTagDecision([], management, "selected"), management, "excluded");
  assert.equal(tags.length, 1);
  assert.equal(tags[0].decision, "excluded");
  assert.equal(selectedTags(tags).length, 0);
});

test("pending and undecided model suggestions do not enter selected conditions", () => {
  const tags = [{ ...management, source: "ai", decision: "pending" }, { ...friend, source: "external", decision: "undecided" }];
  assert.deepEqual(selectedTags(tags), []);
  assert.equal(setTagDecision(tags, management, "selected")[1].source, "ai");
});

test("typing an existing catalog label preserves its canonical ID and dimensions", () => {
  const tags = addAuthorTag([], catalog, "友情", "plot", "selected", "custom:ignored");
  assert.equal(tags[0].id, friend.id);
  assert.deepEqual(tags[0].dimensions, ["theme", "emotion"]);
  const edited = addAuthorTag(tags, catalog, "友情", "world", "excluded", "custom:ignored");
  assert.equal(edited.length, 1);
  assert.deepEqual(edited[0].dimensions, ["theme", "emotion"]);
  assert.equal(edited[0].decision, "excluded");
});

test("author custom labels retain a stable identity across inclusion changes", () => {
  const once = addAuthorTag([], catalog, " 俱乐部经营 ", "plot", "selected", "custom:first");
  const again = addAuthorTag(once, catalog, "俱乐部经营", "world", "excluded", "custom:second");
  assert.equal(again.length, 1);
  assert.equal(again[0].id, "custom:first");
  assert.equal(again[0].source, "author");
  assert.deepEqual(again[0].dimensions, ["plot", "world"]);
});

test("same-content new history version invalidates generation preview identity", () => {
  const original = { path: "outlines/source/creative-brief.json", hash: "same", version_id: "a" };
  assert.notEqual(generationIdentity(original), generationIdentity({ ...original, version_id: "b" }));
  assert.notEqual(generationIdentity(original), generationIdentity({ ...original, path: "different" }));
});

test("edited or deleted selected outline cannot expose a current planning shortcut", () => {
  const selected = { document_path: "outlines/proposals/story-outline-test.md", document_hash: "chosen" };
  assert.equal(outlineSourceChanged(selected, [{ path: selected.document_path, hash: "chosen" }]), false);
  assert.equal(outlineSourceChanged(selected, [{ path: selected.document_path, hash: "edited" }]), true);
  assert.equal(outlineSourceChanged(selected, []), true);
  assert.equal(outlineSourceChanged(null, []), false);
});

test("generation carries the exact preview policy and registry identity, including null markers", () => {
  const route = { policy_revision: 3, policy_hash: "policy", registry_revision: 8, candidates: [{ registry_hash: "registry", legacy_marker: null }] };
  assert.deepEqual(previewConfiguration(route), { policy_revision: 3, policy_hash: "policy", registry_revision: 8, registry_hash: "registry", registry_legacy_marker: null });
  assert.deepEqual(previewConfiguration({ route }), previewConfiguration(route));
  assert.equal(previewConfiguration({ candidates: [] }), null);
  assert.equal(previewConfiguration({ ...route, candidates: [{ registry_hash: "registry" }] }), null);
});

test("explicitly no advantage remains distinct from undecided when surfacing conflicts", () => {
  const brief = { scale: { target_words_min: null, target_words_max: null }, advantage: { status: "undecided" }, tags: [{ id: "plot:system", label: "系统流", dimensions: ["plot"], source: "catalog", decision: "selected" }] };
  assert.deepEqual(briefWarnings(brief), []);
  assert.equal(briefWarnings({ ...brief, advantage: { status: "no" } }).length, 1);
  assert.equal(briefWarnings({ ...brief, advantage: { status: "no" }, tags: [{ ...brief.tags[0], decision: "pending" }] }).length, 0);
});
