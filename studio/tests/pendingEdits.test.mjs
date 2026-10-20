import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/pendingEdits.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { modelProfileChanged, selectionChanged } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const profile = {
  id: "writer", label: "主写", base_url: "https://example.invalid/v1", model: "offline",
  allow_loopback: false, roles: ["writer", "planner"], context_tokens: 65536,
  max_output_tokens: 4096, price_input_usd: .3, price_output_usd: 1.2, has_api_key: true,
};

test("a newly entered key or explicit key removal remains pending even when public settings are unchanged", () => {
  assert.equal(modelProfileChanged(profile, profile, true, false), true);
  assert.equal(modelProfileChanged(profile, profile, false, true), true);
  assert.equal(modelProfileChanged(profile, profile, false, false), false);
});

test("quote, endpoint and model edits are all pending; read-only key presence and role order do not create false edits", () => {
  for (const change of [{ price_input_usd: null }, { price_output_usd: 2 }, { base_url: "https://other.invalid/v1" }, { model: "new-model" }, { roles: ["writer"] }]) {
    assert.equal(modelProfileChanged({ ...profile, ...change }, profile, false, false), true);
  }
  assert.equal(modelProfileChanged({ ...profile, has_api_key: false, roles: ["planner", "writer"] }, profile, false, false), false);
  assert.deepEqual(profile.roles, ["writer", "planner"]);
});

test("sample selection changes include removing every saved sample and are independent of display order", () => {
  assert.equal(selectionChanged([], ["drafts/a.md"]), true);
  assert.equal(selectionChanged(["drafts/a.md"], []), true);
  assert.equal(selectionChanged(["drafts/a.md", "drafts/b.md"], ["drafts/b.md", "drafts/a.md"]), false);
});
