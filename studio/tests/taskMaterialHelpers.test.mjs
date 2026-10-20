import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/taskMaterialHelpers.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } });
const { mergeTaskMaterials, taskMaterialOptions, retrievalMaterialCount } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const retrieval = {index_id:"index",ids:["chunk"],expected_preview:"exact-preview"};

test("manual cards and a fixed retrieval preview survive selection in either order", () => {
  const ranges = [{source_id:"local-source",source_version_id:"version",start_char:1,end_char:3}];
  const pending = {source_ranges:ranges, query:"选择的代价", retrieval_selection:retrieval};
  const combined = mergeTaskMaterials(pending, {entry_ids:["book-card"],binding_ids:["bound-version"]});
  assert.deepEqual(combined, {...pending,entry_ids:["book-card"],binding_ids:["bound-version"]});
  assert.deepEqual(mergeTaskMaterials({entry_ids:["book-card"],binding_ids:["bound-version"],source_ranges:ranges,query:"选择的代价"},{retrieval_selection:retrieval}), combined);
  assert.equal(retrievalMaterialCount(combined),1);
  assert.equal(pending.entry_ids,undefined);
});

test("replacing retrieval retains cards but replaces the whole fixed index and fingerprint", () => {
  const next={index_id:"new-index",ids:["new-chunk"],expected_preview:"new-preview"};
  const combined=mergeTaskMaterials({entry_ids:["a"],retrieval_selection:retrieval},{retrieval_selection:next});
  assert.deepEqual(combined,{entry_ids:["a"],retrieval_selection:next});
  assert.notEqual(JSON.stringify(combined),JSON.stringify({entry_ids:["a"],retrieval_selection:retrieval}));
});

test("removing one material group preserves the others and clearing every group returns null", () => {
  const combined={entry_ids:["a"],binding_ids:["b"],retrieval_selection:retrieval};
  assert.deepEqual(mergeTaskMaterials(combined,{entry_ids:[]}),{binding_ids:["b"],retrieval_selection:retrieval});
  assert.deepEqual(mergeTaskMaterials(combined,{retrieval_selection:null}),{entry_ids:["a"],binding_ids:["b"]});
  assert.equal(mergeTaskMaterials(combined,{entry_ids:[],binding_ids:[],retrieval_selection:null}),null);
});

test("only this book's listed cards and explicit bindings become options, with purpose and author adoption gates", () => {
  const entry=(id,extra={})=>({id,title:id,type:"material",summary:"作者摘要",status:"ready",outbound:"excerpts",version_id:"version",...extra});
  const overview={entries:[entry("usable"),entry("local",{outbound:"local"}),entry("method",{type:"method",adoption:null}),entry("adopted",{type:"method",adoption:{id:"adoption"}})],bindings:[{id:"planning",purpose:"planning",snapshot:entry("personal"),version_id:"bound-version"},{id:"review-only",purpose:"review",snapshot:entry("review"),version_id:"bound-version"},{id:"withdrawn",purpose:"reference",withdrawn:true,snapshot:entry("old"),version_id:"bound-version"}]};
  const options=taskMaterialOptions(overview,"planning");
  assert.deepEqual(options.filter(item=>!item.disabledReason).map(item=>item.id),["usable","adopted","planning"]);
  assert.match(options.find(item=>item.id==="method").disabledReason,/作者采用/);
  assert.match(options.find(item=>item.id==="review-only").disabledReason,/用途/);
  assert.match(options.find(item=>item.id==="withdrawn").disabledReason,/撤回/);
  assert.equal(overview.entries[2].adoption,null);
});

test("summary-only cards need an author summary and malformed selection groups are not silently sent", () => {
  const options=taskMaterialOptions({entries:[{id:"empty",title:"未整理",type:"style",summary:"",status:"ready",outbound:"summary",version_id:"version"}],bindings:[]},"writing");
  assert.match(options[0].disabledReason,/摘要/);
  assert.throws(()=>mergeTaskMaterials(null,{entry_ids:"all"}),/格式/);
  assert.throws(()=>mergeTaskMaterials(null,{query:"x".repeat(201)}),/200/);
  assert.equal(mergeTaskMaterials(null,{unsupported_authorization:true}),null);
});
