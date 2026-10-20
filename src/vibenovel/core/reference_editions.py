"""Author reviewed, immutable derived editions and exact range maps.

No original is edited. Every readable source codepoint must be retained exactly
once in an output chapter (body or ancillary); only explicit exact patches can
change derived text. Unknown bytes remain unknown, never invented repairs.
"""
from __future__ import annotations

import copy
import re
from uuid import uuid4

from vibenovel.errors import VibeNovelError
from .files import atomic_write, cleanup_stage, digest, file_digest, json_bytes, read_json, safe_path, tree_files
from .locking import ConflictError
from .project import utc_now
from .reference_books import ReferenceBookStore, MAX_MANIFEST_BYTES, MAX_CORPUS_BYTES, MAX_CHAPTERS, MAX_TEXT_BYTES, _bounded, _identity

TRANSFORM_VERSION = "reference-editions-v1"


class ReferenceEditionStore:
    def __init__(self, workspace):
        self.books = ReferenceBookStore(workspace)

    def _base(self, catalog, book_id, edition_id, expected_hash=None):
        book = self.books._book(catalog, book_id)
        if expected_hash is not None and digest(json_bytes(book)) != expected_hash:
            raise ConflictError("参考书版本或授权已变化，请重新预览")
        root, metadata = self.books._edition(book, edition_id)
        self.books._read_checked(root, metadata["original_path"], metadata["original_hash"])
        chapters = self.books._chapters(root, metadata)
        return book, root, metadata, chapters

    def _path(self, book_id, candidate_id):
        return self.books.path(f"{_identity(book_id)}/edition-candidates/{_identity(candidate_id)}.json")

    @staticmethod
    def _sealed(payload):
        result = copy.deepcopy(payload)
        result["hash"] = digest(json_bytes(payload))
        if len(json_bytes(result)) > MAX_MANIFEST_BYTES:
            raise VibeNovelError("整理候选超过8MiB；请分批操作，未截断")
        return result

    def _candidate(self, book_id, candidate_id):
        value = read_json(self._path(book_id, candidate_id), max_bytes=MAX_MANIFEST_BYTES)
        payload = copy.deepcopy(value)
        fingerprint = payload.pop("hash", None)
        if fingerprint != digest(json_bytes(payload)) or value.get("book_id") != book_id or value.get("id") != candidate_id:
            raise ConflictError("整理候选指纹或身份不一致")
        return value

    def candidates(self, book_id):
        with self.books._locked() as catalog:
            self.books._book(catalog, book_id)
            folder = self.books.path(f"{book_id}/edition-candidates")
            return [{k: v[k] for k in ("id", "hash", "created_at", "base_edition_id", "reason", "summary")}
                    for p in sorted(folder.glob("*.json")) for v in [self._candidate(book_id, p.stem)]]

    def candidate(self, book_id, candidate_id):
        with self.books._locked() as catalog:
            self.books._book(catalog, book_id)
            return self._candidate(book_id, candidate_id)

    def _prepare(self, catalog, book_id, request):
        if not isinstance(request, dict) or set(request) != {"base_edition_id", "expected_hash", "reason", "chapters", "patches"}:
            raise VibeNovelError("整理方案字段无效")
        _bounded(request["reason"], 2000, empty=False)
        book, root, metadata, records = self._base(catalog, book_id, request["base_edition_id"], request["expected_hash"])
        if book["withdrawn"]:
            raise ConflictError("撤回资料只能归档，不能发布整理版本")
        indexed = {c["chapter_id"]: c for c in records}
        texts = {}
        def text(chapter_id):
            _identity(chapter_id)
            if chapter_id not in indexed:
                raise VibeNovelError("整理方案引用不存在的来源章段")
            if chapter_id not in texts:
                texts[chapter_id] = self.books._chapter_text(root, indexed[chapter_id])
            return texts[chapter_id]
        spec = request["chapters"]
        patches = request["patches"]
        if not isinstance(spec, list) or not 1 <= len(spec) <= MAX_CHAPTERS or not isinstance(patches, list) or len(patches) > 1000:
            raise VibeNovelError("整理方案需要1至20000个章段，补丁最多1000条")
        patch_by = {}
        normalized_patches = []
        for patch in patches:
            if not isinstance(patch, dict) or set(patch) != {"chapter_id", "start_char", "end_char", "expected_text", "replacement", "reason"}:
                raise VibeNovelError("文本补丁字段无效")
            value = text(patch["chapter_id"])
            start, end = patch["start_char"], patch["end_char"]
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(value):
                raise VibeNovelError("补丁必须引用非空Unicode码点半开区间")
            if value[start:end] != patch["expected_text"]:
                raise ConflictError("补丁旧文本与精确位置不一致")
            _bounded(patch["replacement"], 10000)
            _bounded(patch["reason"], 1000, empty=False)
            if len(patch["expected_text"]) > 10000:
                raise VibeNovelError("单补丁原文超过10000字符")
            patch_by.setdefault(patch["chapter_id"], []).append(patch)
            normalized_patches.append(copy.deepcopy(patch))
        for items in patch_by.values():
            items.sort(key=lambda p: p["start_char"])
            if any(a["end_char"] > b["start_char"] for a, b in zip(items, items[1:])):
                raise VibeNovelError("补丁不能重叠")
        coverage, output, mappings, global_gaps, global_controls = {}, [], [], [], []
        global_cursor = 0
        for index, target in enumerate(spec):
            if not isinstance(target, dict) or set(target) != {"title", "kind", "volume_title", "segments"}:
                raise VibeNovelError("整理章节字段无效")
            for key in ("title", "volume_title"):
                _bounded(target[key], 500, empty=key != "title")
            if target["kind"] not in {"body", "ancillary"} or not isinstance(target["segments"], list) or not 1 <= len(target["segments"]) <= 1000:
                raise VibeNovelError("章段类型或组成范围无效")
            pieces, source_segments, position = [], [], 0
            for segment in target["segments"]:
                if not isinstance(segment, dict) or set(segment) != {"chapter_id", "start_char", "end_char"}:
                    raise VibeNovelError("组成区间字段无效")
                cid = segment["chapter_id"]
                value = text(cid)
                start, end = segment["start_char"], segment["end_char"]
                if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(value) or start == end and value:
                    raise VibeNovelError("组成区间超过来源章段")
                coverage.setdefault(cid, []).append((start, end))
                cursor = start
                operations = [p for p in patch_by.get(cid, []) if max(start, p["start_char"]) < min(end, p["end_char"])]
                if any(p["start_char"] < start or p["end_char"] > end for p in operations):
                    raise VibeNovelError("补丁不能跨越拆分边界")
                local_maps = []
                def append(old_start, old_end, rendered, status):
                    nonlocal position
                    entry = {"old_chapter_id": cid, "old_start_char": old_start, "old_end_char": old_end,
                             "new_order": index + 1, "new_start_char": position, "new_end_char": position + len(rendered),
                             "status": status, "disposition": "isolated" if target["kind"] == "ancillary" else "body"}
                    mappings.append(entry)
                    local_maps.append(entry)
                    pieces.append(rendered)
                    position += len(rendered)
                for patch in operations:
                    if cursor < patch["start_char"]:
                        append(cursor, patch["start_char"], value[cursor:patch["start_char"]], "unchanged")
                    append(patch["start_char"], patch["end_char"], patch["replacement"], "changed" if patch["replacement"] else "deleted")
                    cursor = patch["end_char"]
                if cursor < end or start == end:
                    append(cursor, end, value[cursor:end], "unchanged")
                source_segments.append({**segment, "hash": indexed[cid]["hash"]})
                source = read_json(self.books._read_checked(root, indexed[cid]["source_path"], indexed[cid]["source_hash"]), max_bytes=MAX_MANIFEST_BYTES)
                # Existing parsers record gap/control at whole-edition character offsets.
                # Derive local offset from the frozen source chapter's order, never title.
                chapter_offset = sum(c["characters"] for c in records[:indexed[cid]["order"] - 1])
                for gap in source.get("gaps", []):
                    at = gap["at_char"] - chapter_offset
                    if start <= at < end or at == end == len(value):
                        owner = next((m for m in local_maps if m["old_start_char"] <= at <= m["old_end_char"]), None)
                        new_at = owner["new_start_char"] + min(at - owner["old_start_char"], owner["new_end_char"] - owner["new_start_char"]) if owner else position
                        global_gaps.append({**gap, "at_char": global_cursor + new_at, "unresolved": True,
                                            "parent_chapter_id": cid, "parent_at_char": at})
            rendered = "".join(pieces)
            for match in re.finditer("\x00", rendered):
                global_controls.append({"at_char": global_cursor + match.start(), "codepoint": "U+0000", "derived": True})
            output.append({"text": rendered, "title": target["title"], "kind": target["kind"], "volume_title": target["volume_title"],
                           "source": {"resource": None, "document_start_char": global_cursor, "document_end_char": global_cursor + len(rendered),
                                      "parent_edition_id": metadata["edition_id"], "segments": source_segments}})
            global_cursor += len(rendered)
        for chapter in records:
            intervals = sorted(coverage.get(chapter["chapter_id"], []))
            cursor = 0
            for start, end in intervals:
                if start != cursor:
                    raise VibeNovelError("来源内容必须恰好保留一次；可标为附记隔离，不可遗漏或重叠")
                cursor = end
            if not intervals or cursor != chapter["characters"]:
                raise VibeNovelError("来源内容未完整覆盖；不能静默删章")
        if sum(len(c["text"].encode("utf-8")) for c in output) > MAX_TEXT_BYTES:
            raise VibeNovelError("派生正文超过128MiB")
        summary = {"old_chapters": len(records), "new_chapters": len(output), "patches": len(patches),
                   "isolated_chapters": sum(c["kind"] == "ancillary" for c in output), "unresolved_gaps": len(global_gaps),
                   "remaining_controls": len(global_controls), "characters": global_cursor,
                   "changed_ranges": [p for p in normalized_patches]}
        preview = {"schema_version": 1, "book_id": book_id, "base_edition_id": metadata["edition_id"],
                   "base_metadata_hash": next(e["metadata_hash"] for e in book["editions"] if e["edition_id"] == metadata["edition_id"]),
                   "authorization_revision": book["authorization_revision"], "request": request, "summary": summary,
                   "mapping_count": len(mappings), "transform_version": TRANSFORM_VERSION}
        return self._sealed(preview), (book, root, metadata, output, mappings, global_gaps, global_controls)

    def preview(self, book_id, request):
        with self.books._locked() as catalog:
            preview, _ = self._prepare(catalog, book_id, request)
            return preview

    def create(self, book_id, request, *, expected_preview, idempotency_key):
        _bounded(idempotency_key, 80, empty=False)
        if len(idempotency_key) < 16:
            raise VibeNovelError("幂等键至少16字符")
        candidate_id = digest(json_bytes([book_id, idempotency_key]))[:32]
        with self.books._locked() as catalog:
            path = self._path(book_id, candidate_id)
            if path.exists():
                old = self._candidate(book_id, candidate_id)
                if old["request"] != request or old["preview_hash"] != expected_preview:
                    raise ConflictError("整理幂等键已用于其他方案")
                return old
            preview, _ = self._prepare(catalog, book_id, request)
            if preview["hash"] != expected_preview:
                raise ConflictError("整理预览已变化")
            candidate = self._sealed({**{k: v for k, v in preview.items() if k != "hash"}, "id": candidate_id,
                                      "created_at": utc_now(), "reason": request["reason"], "preview_hash": expected_preview})
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json_bytes(candidate))
            return candidate

    def publish(self, book_id, candidate_id, *, expected_candidate_hash, expected_hash, make_current=False):
        if type(make_current) is not bool:
            raise VibeNovelError("设为当前版选项无效")
        with self.books._locked() as catalog:
            candidate = self._candidate(book_id, candidate_id)
            if candidate["hash"] != expected_candidate_hash:
                raise ConflictError("整理候选已变化")
            book = self.books._book(catalog, book_id)
            edition_id = digest(json_bytes([TRANSFORM_VERSION, candidate["hash"]]))[:32]
            if any(e["edition_id"] == edition_id for e in book["editions"]):
                return self.books._detail(book, edition_id)
            if digest(json_bytes(book)) != expected_hash:
                raise ConflictError("参考书已变化，请重新核对发布")
            request = copy.deepcopy(candidate["request"])
            request["expected_hash"] = expected_hash
            preview, values = self._prepare(catalog, book_id, request)
            if (preview["base_metadata_hash"] != candidate["base_metadata_hash"] or book["authorization_revision"] != candidate["authorization_revision"]):
                raise ConflictError("整理来源或授权已变化；不可复活旧候选")
            book, root, metadata, output, mappings, gaps, controls = values
            raw = self.books._read_checked(root, metadata["original_path"], metadata["original_hash"]).read_bytes()
            stage = self.books.path(".staging/edition-" + uuid4().hex)
            stage.mkdir(parents=True)
            try:
                info = {"metadata": metadata.get("metadata", {}), "toc": [], "issues": [], "encoding": metadata.get("encoding"),
                        "encoding_status": metadata.get("encoding_status"), "original_volumes_verified": False,
                        "gaps": gaps, "control_characters": controls, "derived_from": {"edition_id": metadata["edition_id"],
                        "metadata_hash": candidate["base_metadata_hash"], "candidate_id": candidate_id, "candidate_hash": candidate["hash"]},
                        "curation": {"reason": candidate["reason"], "published_at": candidate["created_at"], "reviewed": True, "review_provenance": "explicit_local_publish"}}
                if gaps:
                    info["issues"].append({"code": "unresolved_encoding_gaps", "severity": "error", "message": "原始坏字节缺口仍未修复；不能凭空补字", "count": len(gaps)})
                result = self.books._write_edition(stage, book_id, edition_id, metadata["filename"], raw, output, info)
                target_records = [c for page in result["chapter_pages"] for c in read_json(safe_path(stage, page["path"]), max_bytes=MAX_MANIFEST_BYTES)["chapters"]]
                for item in mappings:
                    item["new_chapter_id"] = target_records[item["new_order"] - 1]["chapter_id"]
                map_data = {"schema_version": 1, "book_id": book_id, "from_edition_id": metadata["edition_id"], "to_edition_id": edition_id,
                            "ranges": mappings, "gap_policy": "unknown_preserved", "candidate_hash": candidate["hash"]}
                data = json_bytes(map_data)
                if len(data) > MAX_MANIFEST_BYTES:
                    raise VibeNovelError("版本映射超过8MiB；请缩小方案")
                atomic_write(safe_path(stage, "edition-map.json"), data)
                result["files"].append({"path": "edition-map.json", "hash": digest(data)})
                result["edition_map"] = {"path": "edition-map.json", "hash": digest(data)}
                result["created_at"] = candidate["created_at"]
                result["parser_version"] = TRANSFORM_VERSION
                result["transformations"] = ["作者审阅的派生整理；原件与旧版本保留", "精确Unicode区间映射；缺文保持未知"]
                atomic_write(safe_path(stage, "metadata.json"), json_bytes(result), overwrite=True)
                used = sum(p.stat().st_size for p in tree_files(self.books.path(".state").parent) if ".staging" not in p.relative_to(self.books.path(".state").parent).parts)
                if used + sum(p.stat().st_size for p in tree_files(stage)) > MAX_CORPUS_BYTES:
                    raise VibeNovelError("派生整理版超过2GiB语料导入配额")
                final = self.books.path(f"{book_id}/editions/{edition_id}")
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    if file_digest(safe_path(final, "metadata.json")) != digest(json_bytes(result)):
                        raise ConflictError("未登记的整理版本不一致")
                else:
                    stage.rename(final)
                book["editions"].append({"edition_id": edition_id, "metadata_hash": file_digest(safe_path(final, "metadata.json")), "original_hash": result["original_hash"]})
                if make_current:
                    book["latest_edition_id"] = edition_id
                if not self.books._verify_locked(book, edition_id)["ok"]:
                    raise ConflictError("整理版发布前校验失败")
                catalog["revision"] += 1
                self.books._publish(catalog)
                return self.books._detail(book, edition_id)
            finally:
                cleanup_stage(stage, self.books.path(".staging"), "edition-")

    def select(self, book_id, edition_id, *, expected_hash):
        with self.books._locked() as catalog:
            book, _, _, _ = self._base(catalog, book_id, edition_id, expected_hash)
            if not self.books._verify_locked(book, edition_id)["ok"]:
                raise ConflictError("不能设为当前版：版本校验失败")
            book["latest_edition_id"] = _identity(edition_id)
            catalog["revision"] += 1
            self.books._publish(catalog)
            return self.books._detail(book)

    def mapping(self, book_id, edition_id, *, chapter_id=None, start_char=None, end_char=None, direction="forward", offset=0, limit=100):
        if direction not in {"forward", "backward"} or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise VibeNovelError("版本映射查询无效")
        with self.books._locked() as catalog:
            book, root, metadata, _ = self._base(catalog, book_id, edition_id)
            if "edition_map" not in metadata:
                return {"status": "unavailable", "ranges": [], "total": 0}
            ref = metadata["edition_map"]
            value = read_json(self.books._read_checked(root, ref["path"], ref["hash"]), max_bytes=MAX_MANIFEST_BYTES)
            entries = value["ranges"]
            if chapter_id is not None:
                _identity(chapter_id)
                if type(start_char) is not int or type(end_char) is not int or not 0 <= start_char < end_char:
                    raise VibeNovelError("映射查询需要精确非空区间")
                prefix = "old" if direction == "forward" else "new"
                entries = [copy.deepcopy(e) for e in entries if e[f"{prefix}_chapter_id"] == chapter_id and max(start_char, e[f"{prefix}_start_char"]) < min(end_char, e[f"{prefix}_end_char"])]
                for e in entries:
                    if e["status"] == "unchanged":
                        left, right = max(start_char, e[f"{prefix}_start_char"]), min(end_char, e[f"{prefix}_end_char"])
                        delta = e["new_start_char"] - e["old_start_char"]
                        e.update(old_start_char=left if direction == "forward" else left - delta, old_end_char=right if direction == "forward" else right - delta,
                                 new_start_char=left + delta if direction == "forward" else left, new_end_char=right + delta if direction == "forward" else right)
                covered = sum(max(0, min(end_char, e[f"{prefix}_end_char"]) - max(start_char, e[f"{prefix}_start_char"])) for e in entries)
                status = "ambiguous" if covered != end_char - start_char else "exact" if all(e["status"] == "unchanged" for e in entries) else "changed"
            else:
                status = "listed"
            return {**{k: v for k, v in value.items() if k != "ranges"}, "status": status, "total": len(entries), "offset": offset, "ranges": entries[offset:offset + limit], "note": "映射仅供回查；不会把旧引文或旧授权重绑到新版"}

    def audit(self, book_id):
        with self.books._locked() as catalog:
            book, root, metadata, chapters = self._base(catalog, book_id, None)
            layout = [{"title": c["title"], "kind": c["kind"], "volume_title": c["volume_title"], "segments": [{"chapter_id": c["chapter_id"], "start_char": 0, "end_char": c["characters"]}]} for c in chapters]
            patches, findings, headings, heading_layout = [], [], [], []
            cursor = 0
            for i, c in enumerate(chapters):
                if c.get("duplicate_of"):
                    layout[i]["kind"] = "ancillary"
                    findings.append({"kind": "duplicate", "chapter_id": c["chapter_id"], "duplicate_of": c["duplicate_of"], "action": "建议仅在派生版隔离；原文保留"})
                if c.get("needs_review") or c["characters"] > 100000:
                    text = self.books._chapter_text(root, c)
                    for m in re.finditer("\x00", text):
                        patches.append({"chapter_id": c["chapter_id"], "start_char": m.start(), "end_char": m.end(), "expected_text": "\x00", "replacement": "", "reason": "移除可定位孤立NUL，仅修改派生文本；原件保留"})
                    # Broader heading discovery is a reviewable proposal, never an auto repair.
                    matches = list(re.finditer(r"(?m)^[ \t\u3000]*(?:序章|楔子|第[零〇一二三四五六七八九十百千万两\d]+[章节部篇卷][^\r\n]{0,70}|[上中下][ \t\u3000]*[篇部卷][^\r\n]{0,70}|[1-9]\d{0,2}[.、．][^\r\n]{1,60})[ \t\u3000\r]*$", text))
                    headings.extend({"chapter_id": c["chapter_id"], "start_char": m.start(), "end_char": m.end(), "title": m.group().strip()} for m in matches)
                    if len(matches) >= 2:
                        starts = sorted(set([0] + [m.start() for m in matches]))
                        titles = {m.start(): m.group().strip() for m in matches}
                        for at, start in enumerate(starts):
                            heading_layout.append({"title": titles.get(start, c["title"] + " · 标题前文本"), "kind": c["kind"], "volume_title": c["volume_title"],
                                                   "segments": [{"chapter_id": c["chapter_id"], "start_char": start, "end_char": starts[at + 1] if at + 1 < len(starts) else len(text)}]})
                    else:
                        heading_layout.append(copy.deepcopy(layout[i]))
                else:
                    heading_layout.append(copy.deepcopy(layout[i]))
                cursor += c["characters"]
            if metadata.get("gaps"):
                findings.append({"kind": "unknown_bytes", "count": len(metadata["gaps"]), "action": "保留坏字节和未知缺口；需另一合法可核对版本才可补字"})
            if metadata["counts"].get("duplicate_candidates"):
                findings.append({"kind": "order", "action": "存在重复候选，可能含前置终章；不根据章名猜测重排，需逐段核对后编辑组成顺序"})
            request = {"base_edition_id": metadata["edition_id"], "expected_hash": digest(json_bytes(book)), "reason": "本地目录与异常审计建议：隔离完全重复候选并移除孤立NUL；缺文保持未知", "chapters": layout, "patches": patches}
            retain_later = copy.deepcopy(layout)
            by_id = {c["chapter_id"]: i for i, c in enumerate(chapters)}
            for c in chapters:
                if c.get("duplicate_of"):
                    retain_later[by_id[c["duplicate_of"]]]["kind"] = "ancillary"
                    retain_later[by_id[c["chapter_id"]]]["kind"] = c["kind"]
            return {"book_id": book_id, "edition_id": metadata["edition_id"], "title": book["title"], "findings": findings,
                    "alternatives": {"headings": {**copy.deepcopy(request), "chapters": heading_layout, "reason": "按正文真实标题重建可核对结构区段；不宣称等同出版自然章"},
                                     "retain_later": {**copy.deepcopy(request), "chapters": retain_later, "reason": "隔离较早的完全重复候选，保留后出现的位置；不按章名重排未核对内容"}},
                    "issues": metadata["issues"], "heading_candidates": headings, "proposal": request,
                    "note": "只读审计和可编辑建议，未批准、未改原件或当前版；区段数不代表出版自然章数"}
