import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/referenceBatchFlow.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { referenceBatchFlow } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const selected = { hasSelection: true, hasBillingProject: true, budget: "1", outputLimit: 4000, outbound: "excerpts", withdrawn: false, policyDirty: false, uncertain: false, previewCurrent: false, savedCurrent: false };

test("preview, save and paid start remain separate explicit steps", () => {
  const beforePreview = referenceBatchFlow(selected);
  assert.equal(beforePreview.canPreview, true);
  assert.equal(beforePreview.canSave, false);
  assert.equal(beforePreview.canStart, false);
  const previewed = referenceBatchFlow({ ...selected, previewCurrent: true, previewHash: "frozen" });
  assert.equal(previewed.canSave, true);
  assert.equal(previewed.canStart, false);
  assert.equal(referenceBatchFlow({ ...selected, savedCurrent: true, batchState: "prepared" }).canStart, true);
});

test("changed inputs cannot start an older saved batch", () => {
  const result = referenceBatchFlow({ ...selected, savedCurrent: false, batchState: "prepared", previewHash: "old" });
  assert.equal(result.canStart, false);
  assert.equal(result.canSave, false);
  assert.match(result.startReasons.join(" "), /本次批次/);
});

test("missing selection, billing and invalid limits explain why preview is unavailable", () => {
  for (const overrides of [{ hasSelection: false }, { hasBillingProject: false }, { budget: "" }, { budget: "NaN" }, { budget: "0" }, { outputLimit: 0 }, { outputLimit: 256.5 }, { outputLimit: 16001 }]) {
    const result = referenceBatchFlow({ ...selected, ...overrides });
    assert.equal(result.canPreview, false);
    assert.ok(result.previewReasons.length);
  }
});

test("all execution steps respect saved authorization and uncertain creation only permits same-key save", () => {
  for (const overrides of [{ withdrawn: true }, { outbound: "local" }, { policyDirty: true }]) {
    const result = referenceBatchFlow({ ...selected, previewCurrent: true, previewHash: "frozen", savedCurrent: true, batchState: "prepared", ...overrides });
    assert.equal(result.canPreview, false);
    assert.equal(result.canSave, false);
    assert.equal(result.canStart, false);
  }
  const unknown = referenceBatchFlow({ ...selected, uncertain: true, previewCurrent: true, previewHash: "frozen" });
  assert.equal(unknown.canPreview, false);
  assert.equal(unknown.canSave, true);
  assert.equal(unknown.canStart, false);
});

test("completed, failed, interrupted and running batches cannot be started again", () => {
  for (const batchState of ["completed", "succeeded", "failed", "interrupted", "running", "pausing", "blocked", "stale", "cancelled"]) {
    const result = referenceBatchFlow({ ...selected, savedCurrent: true, batchState });
    assert.equal(result.canStart, false);
    assert.ok(result.startReasons.length);
  }
  assert.equal(referenceBatchFlow({ ...selected, savedCurrent: true, batchState: "paused" }).canStart, true);
});
