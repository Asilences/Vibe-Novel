import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/importSourceHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { importedSourcePath, planningSourceLabel, initialImportEncoding, encodingRequest } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const firstId = "0123456789abcdef".repeat(4);
const secondId = "fedcba9876543210".repeat(4);
const extractedHash = "a".repeat(64);

test("Chinese source names label only their exact imported source path", () => {
  const item = { import_id: firstId, original_name: "人物与世界观（第二版）.txt" };
  const path = `imports/original/${firstId}/extracted.md`;
  assert.equal(importedSourcePath(item), path);
  assert.equal(planningSourceLabel(path, [item]), "人物与世界观（第二版）.txt · 01234567");
});

test("same-name imports retain distinct identities instead of merging by filename", () => {
  const imports = [firstId, secondId].map((import_id) => ({ import_id, original_name: "总纲.txt" }));
  assert.notEqual(importedSourcePath(imports[0]), importedSourcePath(imports[1]));
  assert.equal(planningSourceLabel(importedSourcePath(imports[0]), imports), "总纲.txt · 01234567");
  assert.equal(planningSourceLabel(importedSourcePath(imports[1]), imports), "总纲.txt · fedcba98");
  assert.equal(planningSourceLabel("outlines/source/总纲.txt", imports), "outlines/source/总纲.txt");
});

test("source and extracted content hashes never replace the import identity", () => {
  const item = { import_id: firstId, original_name: "故事大纲.md", source_hash: secondId, extracted_hash: extractedHash };
  assert.equal(importedSourcePath(item), `imports/original/${firstId}/extracted.md`);
  for (const hash of [item.source_hash, item.extracted_hash]) {
    const otherPath = `imports/original/${hash}/extracted.md`;
    assert.equal(planningSourceLabel(otherPath, [item]), otherPath);
  }
});

test("missing metadata and ordinary manuscript paths remain unchanged", () => {
  const path = `imports/original/${firstId}/extracted.md`;
  assert.equal(planningSourceLabel(path, []), path);
  assert.equal(planningSourceLabel(path, [{ import_id: firstId, original_name: "" }]), path);
  assert.equal(planningSourceLabel(path, [{ import_id: firstId, original_name: "  " }]), path);
  const manuscript = "drafts/第一章.md";
  assert.equal(planningSourceLabel(manuscript, [{ import_id: firstId, original_name: "第一章.md" }]), manuscript);
});

test("lookalike imported paths cannot acquire a misleading source label", () => {
  const item = { import_id: firstId, original_name: "原始大纲.txt" };
  const path = importedSourcePath(item);
  for (const otherPath of [path + "\n", path + ".bak", path + "?version=1", "/" + path, path.replaceAll("/", "\\"), path.replace(firstId, firstId.toUpperCase()), path.replace("extracted.md", "original.txt")]) {
    assert.equal(planningSourceLabel(otherPath, [item]), otherPath);
  }
});

test("only complete lowercase 64-digit hexadecimal import IDs are accepted", () => {
  for (const import_id of ["", firstId.slice(0, 8), firstId.slice(0, 63), firstId + "0", firstId.toUpperCase(), "g".repeat(64), firstId + "\n", "../" + firstId, null, undefined, 123]) {
    assert.throws(() => importedSourcePath({ import_id }), /导入来源 ID/);
  }
  const malformed = { import_id: "short", original_name: "不会被当作有效来源.txt" };
  assert.equal(planningSourceLabel("imports/original/short/extracted.md", [malformed]), "imports/original/short/extracted.md");
});

test("each new import starts in auto mode without leaking a previous explicit encoding", () => {
  let encoding = initialImportEncoding();
  assert.equal(encoding, "auto");
  encoding = "gb18030";
  assert.deepEqual(encodingRequest(encoding), { encoding: "gb18030" });
  encoding = initialImportEncoding();
  assert.equal(encoding, "auto");
  assert.deepEqual(encodingRequest(encoding), {});
  assert.equal(Object.hasOwn(encodingRequest(encoding), "encoding"), false);
});

test("explicit encodings are sent precisely and unsupported encodings fail", () => {
  assert.deepEqual(encodingRequest("gb18030"), { encoding: "gb18030" });
  assert.deepEqual(encodingRequest("utf-16"), { encoding: "utf-16" });
  for (const encoding of ["utf-8", "GB18030", "", null, undefined]) {
    assert.throws(() => encodingRequest(encoding), /不支持的导入编码/);
  }
});
