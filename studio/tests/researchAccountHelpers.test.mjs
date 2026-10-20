import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/researchAccountHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { researchModelDraft } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("switching the research destination requires a new quote without resetting the shared budget", () => {
  const saved = { model_id: "old", max_cost_usd: 5, max_calls: 256, price_input_usd: 0.3, price_output_usd: 1.2, price_source: "previous model" };
  const changed = researchModelDraft(saved, "new");
  assert.deepEqual(changed, { model_id: "new", max_cost_usd: 5, max_calls: 256, price_input_usd: 0, price_output_usd: 0, price_source: "" });
  assert.equal(saved.price_input_usd, 0.3);
  assert.equal(researchModelDraft(saved, "old"), saved);
});
