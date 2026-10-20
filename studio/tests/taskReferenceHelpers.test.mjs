import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const source=await readFile(new URL("../src/taskReferenceHelpers.ts",import.meta.url),"utf8");
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}});
const {consumeTaskReference,taskReferenceIdentity}=await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
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
