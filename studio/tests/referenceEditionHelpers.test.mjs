import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/referenceEditionHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { splitEditionChapter } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const chapter = () => ({
  title: "合并后的章节", kind: "ancillary", volume_title: "原书第二卷",
  segments: [
    { chapter_id: "first", start_char: 1, end_char: 4 },
    { chapter_id: "second", start_char: 2, end_char: 6 },
    { chapter_id: "first", start_char: 6, end_char: 9 },
  ],
});
const originalText = { first: "甲乙😀丁戊己庚辛壬癸", second: "零一二😀四五六七" };
const textOf = (item) => item.segments.map((segment) => Array.from(originalText[segment.chapter_id]).slice(segment.start_char, segment.end_char).join("")).join("");

test("splitting inside a merged chapter preserves every Unicode source character in order", () => {
  const original = chapter(), snapshot = structuredClone(original);
  const [left, right] = splitEditionChapter(original, 1, 4);
  assert.equal(textOf(left) + textOf(right), textOf(original));
  assert.deepEqual(left.segments, [original.segments[0], { chapter_id: "second", start_char: 2, end_char: 4 }]);
  assert.deepEqual(right.segments, [{ chapter_id: "second", start_char: 4, end_char: 6 }, original.segments[2]]);
  assert.equal(left.kind, "ancillary");
  assert.equal(right.kind, "ancillary");
  assert.equal(left.volume_title, original.volume_title);
  assert.equal(right.volume_title, original.volume_title);
  assert.deepEqual(original, snapshot);
  left.segments[0].end_char = 2;
  assert.deepEqual(original, snapshot, "editing the result must not mutate the original composition");
});

test("a merge can be split back at either side of the same source boundary", () => {
  const original = chapter();
  const atPreviousEnd = splitEditionChapter(original, 0, 4);
  const atNextStart = splitEditionChapter(original, 1, 2);
  assert.deepEqual(atPreviousEnd, atNextStart);
  assert.deepEqual(atPreviousEnd[0].segments, original.segments.slice(0, 1));
  assert.deepEqual(atPreviousEnd[1].segments, original.segments.slice(1));
});

test("single-source chapter splits still preserve nonzero offsets and exact coverage", () => {
  const original = { ...chapter(), segments: [{ chapter_id: "second", start_char: 2, end_char: 6 }] };
  const [left, right] = splitEditionChapter(original, 0, 3);
  assert.equal(textOf(left) + textOf(right), textOf(original));
  assert.equal(left.segments[0].start_char, 2);
  assert.equal(right.segments[0].end_char, 6);
  assert.throws(() => splitEditionChapter(original, 0, 2), /两段都须/);
  assert.throws(() => splitEditionChapter(original, 0, 6), /两段都须/);
});

test("outer endpoints, invalid source indices, fractional and unsafe positions are rejected", () => {
  const original = chapter();
  assert.throws(() => splitEditionChapter(original, 0, 1), /两段都须/);
  assert.throws(() => splitEditionChapter(original, 2, 9), /两段都须/);
  for (const index of [-1, 3, 0.5, NaN]) assert.throws(() => splitEditionChapter(original, index, 3), /有效来源段/);
  for (const at of [-1, 1, 7]) assert.throws(() => splitEditionChapter(original, 1, at), /范围内/);
  for (const at of [2.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => splitEditionChapter(original, 1, at), /有效来源段/);
  assert.throws(() => splitEditionChapter({ ...original, segments: [] }, 0, 0), /有效来源段/);
});

test("a corrupt range is never hidden by a split elsewhere", () => {
  const original = chapter();
  for (const [start_char, end_char] of [[1, 1], [-1, 4], [5, 4], [0, Infinity]]) {
    original.segments[2] = { chapter_id: "first", start_char, end_char };
    assert.throws(() => splitEditionChapter(original, 0, 2), /有效来源段/);
  }
});

test("generated continuation title stays within the backend Unicode character limit", () => {
  const original = { ...chapter(), title: "😀".repeat(500) };
  const [left, right] = splitEditionChapter(original, 0, 2);
  assert.equal(left.title, original.title);
  assert.equal(Array.from(right.title).length, 500);
  assert.ok(right.title.endsWith(" · 后段"));
});
