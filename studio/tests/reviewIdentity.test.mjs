import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/reviewIdentity.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
});
const { reviewMatchesCandidate } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const candidate = { result_path: "drafts/first.md", result_hash: "same-content-hash", result_version_id: "version-1" };
const review = { target_path: candidate.result_path, target_hash: candidate.result_hash, target_version_id: candidate.result_version_id };

test("review matches only the exact candidate path, hash and history version", () => {
  assert.equal(reviewMatchesCandidate(review, candidate), true);
});

test("unchanged bytes at a new candidate path require a new review", () => {
  assert.equal(reviewMatchesCandidate(review, { ...candidate, result_path: "drafts/rewrite.md" }), false);
});

test("unchanged bytes after a new history version do not revive a review", () => {
  assert.equal(reviewMatchesCandidate(review, { ...candidate, result_version_id: "version-2" }), false);
});

test("changed content invalidates a review even at the same path and version", () => {
  assert.equal(reviewMatchesCandidate(review, { ...candidate, result_hash: "new-content-hash" }), false);
});

test("a newly queued candidate without a published result is not reviewed", () => {
  assert.equal(reviewMatchesCandidate(review, { result_path: "drafts/rewrite.md" }), false);
});

test("legacy review without a target history version remains invalid", () => {
  const { target_version_id, ...legacy } = review;
  assert.equal(reviewMatchesCandidate(legacy, candidate), false);
});

test("missing review or candidate cannot be treated as a match", () => {
  assert.equal(reviewMatchesCandidate(null, candidate), false);
  assert.equal(reviewMatchesCandidate(review, undefined), false);
});

test("two missing history versions cannot establish a reviewed identity", () => {
  assert.equal(reviewMatchesCandidate({ ...review, target_version_id: null }, { ...candidate, result_version_id: null }), false);
});
