import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// Exercise the shipped helper itself without a browser or another test runtime.
const source = await readFile(new URL("../src/textSelection.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
});
const { originalSelectionRange, originalCharacterOffset, splitSourceLines, savedSelectionMatches } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);

const selectionCases = [
  { name: "CRLF second-line selection keeps the original newline width", source: "甲\r\n乙丙", dom: [2, 3], original: [3, 4], quote: "乙" },
  { name: "repeated paragraphs retain their own source location", source: "重复\r\n重复\r\n重复", dom: [6, 8], original: [8, 10], quote: "重复" },
  { name: "emoji before CRLF and CJK extension characters inside selection", source: "前😀\r\n𠀀目标\r末行", dom: [4, 8], original: [4, 7], quote: "𠀀目标" },
  { name: "mixed lone CR and CRLF with two surrogate pairs", source: "甲\r乙\r\n😀𠀀丙", dom: [4, 8], original: [5, 7], quote: "😀𠀀" },
  { name: "lone CR normalizes display without changing source character count", source: "甲\r乙", dom: [2, 3], original: [2, 3], quote: "乙" },
  { name: "a programmatic half-surrogate selection expands to a whole character", source: "😀", dom: [1, 2], original: [0, 1], quote: "😀" },
];

for (const item of selectionCases) {
  test(item.name, () => {
    const actual = originalSelectionRange(item.source, ...item.dom);
    assert.deepEqual(actual, { start_char: item.original[0], end_char: item.original[1] });
    assert.equal(Array.from(item.source).slice(actual.start_char, actual.end_char).join(""), item.quote);
  });
}

test("caret endpoints clamp safely to original document bounds", () => {
  assert.equal(originalCharacterOffset("甲\r\n😀乙", -1), 0);
  assert.equal(originalCharacterOffset("甲\r\n😀乙", 999), 5);
  assert.deepEqual(originalSelectionRange("", 0, 0), { start_char: 0, end_char: 0 });
});

test("an assistant selection remains bound to one saved document and Unicode range", () => {
  const document = {path:"drafts/ch001.md",hash:"saved-hash",content:"前😀\r\n𠀀目标\r末行"};
  const selection = {novelId:"football",path:document.path,hash:document.hash,...originalSelectionRange(document.content,4,8)};
  const matches = (chosen=selection, novelId="football", doc=document, text=document.content, listedHash=document.hash) => savedSelectionMatches(chosen,novelId,doc,text,listedHash);
  assert.equal(matches(),true);
  assert.equal(Array.from(document.content).slice(selection.start_char,selection.end_char).join(""),"𠀀目标");
  assert.equal(matches(null),false);
  assert.equal(matches(selection,"other-book"),false);
  assert.equal(matches(selection,"football",{...document,path:"drafts/ch002.md"}),false);
  assert.equal(matches(selection,"football",{...document,hash:"new-hash"}),false);
  assert.equal(matches(selection,"football",document,document.content+"尚未保存"),false);
  assert.equal(matches(selection,"football",document,document.content,"external-change"),false);
  assert.equal(savedSelectionMatches(selection,"football",document,document.content,undefined),false);
  assert.equal(savedSelectionMatches(selection,undefined,document,document.content,document.hash),false);
  assert.equal(matches(selection,"football",null),false);
});

test("stale or malformed assistant ranges never become whole-document edits", () => {
  const document = {path:"drafts/ch001.md",hash:"saved-hash",content:"甲😀乙"};
  const base = {novelId:"football",path:document.path,hash:document.hash,start_char:0,end_char:3};
  for (const range of [{start_char:-1},{end_char:4},{start_char:3},{end_char:0},{start_char:0.5},{end_char:NaN},{start_char:true},{hash:"old-hash"},{path:"drafts/other.md"}]) {
    assert.equal(savedSelectionMatches({...base,...range},"football",document,document.content,document.hash),false);
  }
  assert.equal(savedSelectionMatches(base,"football",document,document.content,document.hash),true);
});

const lineCases = [
  { name: "chapter line numbers agree for CRLF, lone CR and Unicode line separators", source: "甲\r\n乙\r丙\u2028丁\n", lines: ["甲", "乙", "丙", "丁"] },
  { name: "consecutive trailing newlines retain the actual empty source line", source: "甲\n\n", lines: ["甲", ""] },
  { name: "empty text contains no source lines", source: "", lines: [] },
  { name: "Python additional line separators stay aligned with backend chapter boundaries", source: "甲\x85乙\x1c丙\v丁\f戊\x1d己\x1e庚\u2029辛", lines: ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"] },
];

for (const item of lineCases) {
  test(item.name, () => assert.deepEqual(splitSourceLines(item.source), item.lines));
}
