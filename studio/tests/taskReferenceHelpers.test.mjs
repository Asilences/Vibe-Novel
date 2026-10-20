import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const source=await readFile(new URL("../src/taskReferenceHelpers.ts",import.meta.url),"utf8");
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}});
const {consumeTaskReference,taskReferenceIdentity,referenceUsedBySelection,retireReferenceRetrieval}=await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const reference=(id,novelId="one",version="fixed-v1")=>({id,novelId,selection:{retrieval_selection:{index_id:"index",ids:["one"],expected_preview:version}}});
test("only the exact submitted selection is consumed once",()=>{
 const saved=reference("first");assert.equal(consumeTaskReference(saved,structuredClone(saved)),null);
 assert.equal(consumeTaskReference(null,saved),null);
});
test("an old successful request cannot consume a new selection or another novel",()=>{
 const current=reference("new");assert.equal(consumeTaskReference(current,reference("old")),current);
 assert.equal(consumeTaskReference(current,reference("new","another")),current);
 assert.equal(consumeTaskReference(current,null),current);
});
test("same ticket with changed evidence cannot reuse the preview or clear the selection",()=>{
 const current=reference("same","one","new-version");const old=reference("same");
 assert.notEqual(taskReferenceIdentity(current),taskReferenceIdentity(old));
 assert.equal(consumeTaskReference(current,old),current);
});

test("manual card additions consume the matching retrieval ticket but replacements do not",()=>{
 const current=reference("selected");
 assert.equal(referenceUsedBySelection(current,{...current.selection,entry_ids:["card"]}),current);
 assert.equal(referenceUsedBySelection(current,{}),null);
 assert.equal(referenceUsedBySelection(current,reference("other","one","new-version").selection),null);
 assert.equal(consumeTaskReference(current,referenceUsedBySelection(current,null)),current);
});

test("cancelling a copied ticket removes only its exact retrieval and keeps other materials",()=>{
 const saved=reference("selected");
 const local={...saved.selection,entry_ids:["card"],binding_ids:["binding"],source_ranges:[{source_id:"source"}],query:"经营"};
 assert.deepEqual(retireReferenceRetrieval(local,saved,null),{entry_ids:["card"],binding_ids:["binding"],source_ranges:[{source_id:"source"}],query:"经营"});
 assert.deepEqual(local.retrieval_selection,saved.selection.retrieval_selection);
});

test("consuming a sole copied retrieval leaves an explicit empty selection, not global fallback",()=>{
 const saved=reference("selected");
 const retired=retireReferenceRetrieval({...saved.selection},saved,consumeTaskReference(saved,saved));
 assert.deepEqual(retired,{});
 assert.notEqual(retired,null);
 assert.equal(retireReferenceRetrieval(null,saved,null),null);
 const empty={};assert.equal(retireReferenceRetrieval(empty,saved,null),empty);
});

test("retiring an old ticket never deletes independently replaced or newly selected retrieval",()=>{
 const old=reference("old"),next=reference("new","one","fixed-v2");
 const local={...next.selection,entry_ids:["card"]};
 assert.equal(retireReferenceRetrieval(local,old,null),local);
 assert.equal(retireReferenceRetrieval(local,old,next),local);
 const renewed=reference("renewed");
 const unchanged={...old.selection,entry_ids:["card"]};
 assert.equal(retireReferenceRetrieval(unchanged,old,renewed),unchanged);
 assert.equal(retireReferenceRetrieval(unchanged,old,structuredClone(old)),unchanged);
 assert.deepEqual(retireReferenceRetrieval(unchanged,old,reference("other-book","another")),{entry_ids:["card"]});
});

test("a form restored after cancellation reconciles its saved ticket without losing author inputs",()=>{
 const saved=reference("before-library");
 const draft={sourcePath:"outlines/source/outline.md",start:4,count:2,instruction:"保留经营主线",materialSelection:{...saved.selection,entry_ids:["card"]},materialReference:saved};
 const restored={...draft,materialSelection:retireReferenceRetrieval(draft.materialSelection,draft.materialReference,null)};
 assert.deepEqual(restored.materialSelection,{entry_ids:["card"]});
 assert.equal(restored.instruction,draft.instruction);
 assert.equal(restored.start,4);
 assert.equal(restored.sourcePath,draft.sourcePath);
});
