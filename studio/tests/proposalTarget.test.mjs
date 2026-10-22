import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/proposalTarget.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
});
const { suggestProposalTarget } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const draft = { kind: "draft", state: "succeeded", result_path: "drafts/candidate-aabbcc.md", request: { plan_path: "outlines/approved/ch003.md" } };
const rewrite = { kind: "rewrite", state: "succeeded", result_path: "drafts/rewrite-aabbcc.md", request: { document_path: draft.result_path } };

test("a random third-chapter candidate uses its frozen plan instead of chapter one", () => {
  const result = suggestProposalTarget(draft.result_path, [draft]);
  assert.equal(result.target, "chapters/ch003.md");
  assert.match(result.note, /outlines\/approved\/ch003\.md/);
});

test("rewrites trace the saved source candidate and preserve its original chapter", () => {
  const laterRewrite = { ...rewrite, result_path: "drafts/rewrite-later.md", request: { document_path: rewrite.result_path } };
  assert.equal(suggestProposalTarget(laterRewrite.result_path, [draft, rewrite, laterRewrite]).target, "chapters/ch003.md");
  assert.equal(suggestProposalTarget("drafts/rewrite-explicit.md", [{ ...rewrite, result_path: "drafts/rewrite-explicit.md", request: { document_path: "chapters/ch012.md" } }]).target, "chapters/ch012.md");
});

test("unknown, failed or ambiguous lineage never silently targets the first chapter", () => {
  const cases = [
    [draft.result_path, []],
    [draft.result_path, [{ ...draft, state: "failed" }]],
    [draft.result_path, [draft, { ...draft, request: { plan_path: "outlines/approved/ch002.md" } }]],
    [draft.result_path, [{ ...draft, request: { plan_path: "outlines/proposals/ch003.md" } }]],
    [draft.result_path, [{ ...draft, request: { plan_path: "outlines/approved/ch000.md" } }]],
    [draft.result_path, [{ ...draft, request: {} }]],
    ["drafts/candidate-ch003-aabb.md", []],
    ["drafts/ch000.md", []],
    ["drafts/notes.md", []],
    ["bible/ch003.md", []],
  ];
  for (const [path, jobs] of cases) {
    const result = suggestProposalTarget(path, jobs);
    assert.equal(result.target, "", path);
    assert.match(result.note, /填写目标文件/);
  }
});

test("cyclic rewrite history fails closed rather than looping or guessing", () => {
  const cycle = { ...rewrite, request: { document_path: rewrite.result_path } };
  assert.equal(suggestProposalTarget(cycle.result_path, [cycle]).target, "");
});

test("explicit chapter files retain their number and imported sections explain numbering", () => {
  assert.equal(suggestProposalTarget("drafts/ch2.md", []).target, "chapters/ch002.md");
  const section = suggestProposalTarget(`drafts/manuscript-${"a".repeat(32)}/ch002.md`, []);
  assert.equal(section.target, "chapters/ch002.md");
  assert.match(section.note, /切分序号不等于原稿章号/);
  assert.equal(suggestProposalTarget("chapters/ch1000.md", []).target, "chapters/ch1000.md");
});

test("outline proposals keep the outline approval destination", () => {
  assert.equal(suggestProposalTarget("outlines/proposals/book.md", []).target, "outlines/approved/book.md");
  assert.equal(suggestProposalTarget("outlines/proposals/book.json", []).target, "");
});

// Invoke the actual App handlers with isolated state/API adapters. This checks
// that an empty suggestion survives the component boundary and cannot submit.
const appSource = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const appAst = ts.createSourceFile("App.tsx", appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const app = appAst.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "App");
function appHandler(name, environment) {
  const declaration = app.body.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, `App handler ${name} must exist`);
  const compiled = ts.transpileModule(declaration.getText(appAst), {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return new Function(...Object.keys(environment), `${compiled}; return ${name};`)(...Object.values(environment));
}

test("App opens an unknown target as blank without reading or verifying the draft as a formal destination", async () => {
  const opened = [];
  const verified = [];
  let reads = 0;
  const start = appHandler("startProposal", {
    project: { novel_id: "isolated" }, doc: { path: "drafts/unclassified.md" }, source: "候选正文",
    docRequest: { current: 1 }, proposalSession: { current: 0 }, currentProject: { current: "isolated" },
    act: async (_key, operation) => operation(),
    api: async () => { reads += 1; throw new Error("unknown destination must not be read"); },
    setProposal: (value) => opened.push(value), setVerifiedTarget: (value) => verified.push(value),
  });
  await start("", undefined, "请填写目标");
  assert.equal(reads, 0);
  assert.equal(opened.length, 1);
  assert.deepEqual(opened[0], { target: "", targetNote: "请填写目标", content: "候选正文", before: "", expected_hash: null, reason: "", impact: "" });
  assert.deepEqual(verified, [""]);
});

test("App rejects an empty target even when the verification marker is also empty", async () => {
  let submitted = false;
  let prevented = false;
  const submit = appHandler("submitProposal", {
    project: { novel_id: "isolated" }, proposal: { target: "", reason: "有理由", impact: "有影响" }, verifiedTarget: "",
    act: async () => { submitted = true; },
  });
  await submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(submitted, false);
});
