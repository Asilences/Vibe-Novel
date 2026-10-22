import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const transpile = (source) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
}).outputText;
const url = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const targets = url(transpile(await readFile(new URL("../src/proposalTarget.ts", import.meta.url), "utf8")));
const source = transpile(await readFile(new URL("../src/chapterManagement.ts", import.meta.url), "utf8")).replace('"./proposalTarget"', JSON.stringify(targets));
const { chapterRows, selectChapterRows, chapterTotals, nextDraftPath } = await import(url(source));
const document = (path, extra = {}) => ({ path, hash: "a".repeat(64), bytes: 200, kind: path.startsWith("chapters/") ? "canonical" : "editable", ...extra });

test("chapter manager separates real markdown chapters and drafts from reviews, outlines and other files", () => {
  const rows = chapterRows([document("chapters/ch002.md"), document("drafts/new.md"), document("reviews/check.md"), document("outlines/approved/ch003.md"), document("drafts/metadata.json")], []);
  assert.deepEqual(rows.map((row) => [row.section, row.status]), [["canonical", "正式正文 · 只读"], ["drafts", "工作草稿 · 未定稿"]]);
});

test("chapter counts use verified saved character counts, never byte lengths or missing values as zero", () => {
  const rows = chapterRows([
    document("chapters/ch001.md", { summary_status: "available", character_count: 0 }),
    document("chapters/ch002.md", { summary_status: "available", character_count: 337, bytes: 1000 }),
    document("chapters/ch003.md", { summary_status: "too_large", character_count: null, bytes: 3000000 }),
    document("chapters/ch004.md", { bytes: 9999 }),
    document("chapters/ch005.md", { summary_status: "available", character_count: -1 }),
  ], []);
  assert.deepEqual(rows.map((row) => row.characters), [0, 337, null, null, null]);
  assert.deepEqual(chapterTotals(rows, "canonical"), { count: 5, characters: 337, unknown: 3 });
});

test("sorting uses chapter numbers, keeps unknown numbers last, and searches actual titles", () => {
  const rows = chapterRows([document("chapters/ch010.md", { title: "终场哨声" }), document("chapters/extra.md"), document("chapters/ch002.md", { title: "第二章 蓝色球衣" })], []);
  assert.deepEqual(selectChapterRows(rows, "canonical", "", "chapter").map((row) => row.number), [2, 10, null]);
  assert.deepEqual(selectChapterRows(rows, "canonical", "", "chapter_desc").map((row) => row.number), [10, 2, null]);
  assert.equal(selectChapterRows(rows, "canonical", "球衣", "chapter")[0].document.path, "chapters/ch002.md");
  assert.equal(selectChapterRows(rows, "canonical", "10", "chapter")[0].document.path, "chapters/ch010.md");
});

test("random candidates show frozen chapter-plan lineage and saved provenance without guessing manuscript chapter numbers", () => {
  const path = "drafts/candidate-random.md";
  const job = { kind: "draft", state: "succeeded", result_path: path, result_hash: "a".repeat(64), request: { plan_path: "outlines/approved/ch003.md" } };
  const [candidate, edited, unknown, imported] = chapterRows([
    document(path), document("drafts/edited.md", { hash: "b".repeat(64) }), document("drafts/candidate-ch009.md"), document(`drafts/manuscript-${"c".repeat(32)}/ch002.md`),
  ], [job, { ...job, result_path: "drafts/edited.md" }]);
  assert.equal(candidate.number, 3);
  assert.equal(candidate.status, "AI 候选 · 未定稿");
  assert.equal(edited.status, "候选已编辑 · 未定稿");
  assert.equal(unknown.number, null);
  assert.equal(imported.numberLabel, "切分 2");
  assert.match(imported.numberNote, /原稿章号/);
});

test("new draft defaults follow the next direct chapter file and leave imported split sequences alone", () => {
  assert.equal(nextDraftPath([]), "drafts/ch001.md");
  assert.equal(nextDraftPath([document("chapters/ch003.md"), document("drafts/ch004.md"), document(`drafts/manuscript-${"c".repeat(32)}/ch020.md`)]), "drafts/ch005.md");
  assert.equal(nextDraftPath([document("chapters/ch999.md")]), "drafts/ch1000.md");
});
