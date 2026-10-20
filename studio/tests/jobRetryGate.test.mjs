import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/jobRetryGate.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { jobRetryGate } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const job = (patch = {}) => ({ id: "new", kind: "expand_outline", state: "failed", provider: "api", created_at: "", route: {group_id: "same", budget: {max_calls: 1, max_tokens: 20000, max_cost_usd: null}, estimated_input_tokens: 1000, max_output_tokens: 2000, candidates: [{max_output_tokens: 2000, price_input_usd: 1, price_output_usd: 2}]}, budget_summary: null, ...patch });

test("a failed retry with no own summary still respects the original group's consumed call", () => {
  const current = job(), old = job({id: "old", state: "interrupted", budget_summary: {calls: 1, tokens: 3000, uncertain: true}});
  assert.match(jobRetryGate(current, [old, current]), /调用次数已用尽/);
  assert.equal(current.budget_summary, null);
});

test("different groups cannot consume this retry's allowance and released not-sent attempts remain retryable", () => {
  const current = job(), other = job({id: "other", route: {...current.route, group_id: "other"}, budget_summary: {calls: 99, tokens: 999999}});
  assert.equal(jobRetryGate(current, [other, job({id: "not-sent", budget_summary: {calls: 0, tokens: 0, reserved_cost_usd: 0}})]), null);
});

test("candidate recovery has priority and workflow nodes cannot be generically retried", () => {
  assert.match(jobRetryGate(job({workflow_id: "flow", recovery_available: true}), []), /恢复已保存结果/);
  assert.match(jobRetryGate(job({workflow_id: "flow"}), []), /工作流节点/);
});

test("remaining token and amount allowance must fit a full new request", () => {
  const current = job(); current.route.budget.max_calls = 3;
  assert.match(jobRetryGate(current, [job({id:"old", budget_summary: {calls:1,tokens:18000}})]), /Token 预算不足/);
  current.route.budget.max_cost_usd = 0.008;
  assert.match(jobRetryGate(current, [job({id:"old",budget_summary: {calls:1,tokens:3000,reserved_cost_usd:0.005,cost_usd:null}})]), /金额预算不足/);
});

test("missing cost information is not treated as a claim of exhaustion", () => {
  const current = job(); current.route.budget.max_calls = 3; current.route.budget.max_cost_usd = 1; current.route.candidates[0].price_input_usd = null;
  assert.equal(jobRetryGate(current, [job({id:"old",budget_summary:{calls:1,tokens:3000,cost_usd:null,uncertain:true}})]), null);
});

test("specialized tasks and old unfrozen jobs return their actual recovery entry", () => {
  assert.match(jobRetryGate(job({kind:"generate_story_outline"}), []), /构思页/);
  assert.match(jobRetryGate(job({kind:"extract_methods"}), []), /资料中心/);
  assert.match(jobRetryGate(job({route:null}), []), /未冻结/);
});

test("the frozen group's attempt history has a separate hard limit and duplicate ids are counted once", () => {
  const current = job(); current.route.budget.max_calls = 32;
  const attempts = Array.from({length:32}, (_, i) => ({id:`attempt-${i}`}));
  assert.match(jobRetryGate(current, [job({id:"old",attempts})]), /32次尝试/);
  assert.equal(jobRetryGate(current, [job({id:"old",attempts:[attempts[0], attempts[0]]})]), null);
});
