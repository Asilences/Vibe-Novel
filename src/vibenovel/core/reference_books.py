"""Private, offline reference-book corpus, separate from a9 research cards.

Publication is immutable edition first, then one journalled catalogue pointer.
No parser, local search or restore operation invokes a model. Parsed coverage
is deliberately not literary analysis, edition completeness or external consent.
"""
from __future__ import annotations

import codecs
import copy
import io
import json
import posixpath
import re
import zipfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

from vibenovel.errors import VibeNovelError
from .files import atomic_write, cleanup_stage, digest, file_digest, json_bytes, read_json, safe_path, tree_files
from .locking import ConflictError, project_lock
from .project import utc_now

PARSER_VERSION = "reference-books-v3"
MAX_ORIGINAL_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024 * 1024
MAX_SHARD_BYTES = 8 * 1024 * 1024
TARGET_VOLUME_BYTES = 5 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_CHAPTERS = 20000
MAX_SNAPSHOT_CHARACTERS = 60000
MAX_CORPUS_BYTES = 2 * 1024 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"(?m)^[ \t\u3000]*(?:(?:正文[ \t\u3000]{1,8})?第[零〇一二三四五六七八九十百千万两\d]+[章节回卷部篇集][^\r\n]{0,100}|(?:序章|楔子|前言|序言|引子|尾声|后记|番外)[^\r\n]{0,80}|[0-9]{1,4}[.、．][ \t]*[^\r\n]{1,70})[ \t\u3000\r]*$")
_VOLUME = re.compile(r"^第[零〇一二三四五六七八九十百千万两\d]+[卷部篇集](?!章)")


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise VibeNovelError("参考书ID需要32位小写十六进制")
    return value


def _bounded(value, maximum=500, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise VibeNovelError("参考书字段格式或长度无效")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise VibeNovelError("参考书含无效Unicode") from exc
    if "\x00" in value:
        raise VibeNovelError("参考书字段含NUL")
    return value


def _range(offset, limit, maximum):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise VibeNovelError("参考书分页范围无效")


def _xml(raw):
    if (len(raw) > MAX_MANIFEST_BYTES or re.search(br"<!\s*ENTITY\b", raw, re.I)
            or re.search(br"<!\s*DOCTYPE\b[^>]*\[", raw, re.I)):
        raise VibeNovelError("EPUB目录XML过大或含不受支持的实体声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise VibeNovelError("EPUB包或目录XML无法解析") from exc


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _member(base, href):
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or "\\" in href:
        raise VibeNovelError("EPUB资源必须为包内相对路径")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parts.path)))
    if name.startswith(("/", "../")) or name in (".", "..") or ":" in name:
        raise VibeNovelError("EPUB资源越过包边界")
    return name, unquote(parts.fragment)


class _HTMLText(HTMLParser):
    """Visible text with synthetic paragraph boundaries and node-text anchors."""
    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "section", "article", "blockquote", "tr", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.runs = []
        self.length = 0
        self.node = 0
        self.stack = []
        self.hidden = 0
        self.headings = []
        self.heading = None
        self.links = []
        self.link = None
        self.anchor = ""

    def _add(self, value, kind="node"):
        if not value:
            return
        start = self.length
        self.parts.append(value)
        self.length += len(value)
        self.runs.append({"start_char": start, "end_char": self.length, "node": self.node,
                          "anchor": self.anchor, "kind": kind})

    def _newline(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self._add("\n", "paragraph_separator")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.node += 1
        values = dict(attrs)
        if values.get("id"):
            self.anchor = values["id"]
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        self.stack.append(tag)
        if not self.hidden and tag in self.BLOCK:
            self._newline()
        if tag in {"h1", "h2", "h3", "h4"}:
            self.heading = []
        if tag == "a" and values.get("href"):
            self.link = [values["href"], []]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4"} and self.heading is not None:
            self.headings.append("".join(self.heading).strip())
            self.heading = None
        if tag == "a" and self.link is not None:
            self.links.append((self.link[0], "".join(self.link[1]).strip()))
            self.link = None
        if not self.hidden and tag in self.BLOCK:
            self._newline()
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if self.link is not None:
            self.link[1].append(data)
        if self.heading is not None:
            self.heading.append(data)
        if not self.hidden:
            self._add(data)


def _decode_segments(raw, encoding):
    """Strictly decode readable spans, preserving every undecodable byte gap."""
    if encoding is None:
        if raw.startswith(codecs.BOM_UTF8):
            encoding = "utf-8-sig"
        elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        else:
            for candidate in ("utf-8", "gb18030"):
                try:
                    raw.decode(candidate, errors="strict")
                    encoding = candidate
                    break
                except UnicodeError:
                    pass
            if encoding is None:
                # Both failed. Choice remains provisional and is reported.
                encoding = "gb18030"
    if encoding not in {"utf-8", "utf-8-sig", "utf-16", "gb18030"}:
        raise VibeNovelError("参考书编码仅支持utf-8、utf-8-sig、utf-16、gb18030")
    bom = 0
    decoder = encoding
    if encoding in {"utf-8", "utf-8-sig"} and raw.startswith(codecs.BOM_UTF8):
        bom, decoder = 3, "utf-8"
    elif encoding == "utf-16":
        if raw.startswith(codecs.BOM_UTF16_LE):
            bom, decoder = 2, "utf-16-le"
        elif raw.startswith(codecs.BOM_UTF16_BE):
            bom, decoder = 2, "utf-16-be"
        else:
            raise VibeNovelError("UTF-16必须有字节序BOM")
    elif encoding == "utf-8-sig":
        decoder = "utf-8"
    pieces, spans, gaps = [], [], []
    cursor, length = bom, 0
    while cursor < len(raw):
        try:
            value = raw[cursor:].decode(decoder, errors="strict")
            end, gap = len(raw), None
        except UnicodeDecodeError as exc:
            end = cursor + exc.start
            value = raw[cursor:end].decode(decoder, errors="strict")
            gap = (end, cursor + exc.end)
        if value:
            pieces.append(value)
            spans.append({"start_char": length, "end_char": length + len(value),
                          "start_byte": cursor, "end_byte": end, "encoding": decoder})
            length += len(value)
        if gap is None:
            break
        gaps.append({"start_byte": gap[0], "end_byte": gap[1], "at_char": length,
                     "sha256": digest(raw[gap[0]:gap[1]])})
        cursor = gap[1]
    text = "".join(pieces)
    return text, encoding, spans, gaps


def _split_document(text, fallback, *, resource=None, nodes=None, byte_spans=None, ancillary=False):
    headings = [] if ancillary else list(_HEAD.finditer(text))
    starts = [0] + [item.start() for item in headings if item.start() > 0]
    by_start = {item.start(): item.group(0).strip() for item in headings}
    if headings and headings[0].start() and not text[:headings[0].start()].strip("\ufeff \t\r\n\u3000"):
        starts.remove(headings[0].start())
        by_start[0] = headings[0].group(0).strip()
    chapters = []
    volume = ""
    byte_progress = {}
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        value = text[start:end]
        title = by_start.get(start, fallback if index == 0 else "未命名段落")
        if _VOLUME.match(title):
            volume = re.split(r"第[零〇一二三四五六七八九十百千万两\d]+[章回节]", title, maxsplit=1)[0].strip()
        source = {"resource": resource, "document_start_char": start, "document_end_char": end}
        if nodes is not None:
            source["nodes"] = [{**run, "start_char": max(start, run["start_char"]) - start,
                                "end_char": min(end, run["end_char"]) - start}
                               for run in nodes if run["end_char"] > start and run["start_char"] < end]
        if byte_spans is not None:
            ranges = []
            for span in byte_spans:
                a, b = max(start, span["start_char"]), min(end, span["end_char"])
                if a < b:
                    # Encoding preserves source newlines; re-encoding gives exact byte offsets.
                    previous_char, previous_byte = byte_progress.get(span["start_char"], (span["start_char"], span["start_byte"]))
                    byte_start = previous_byte + len(text[previous_char:a].encode(span["encoding"]))
                    byte_end = byte_start + len(text[a:b].encode(span["encoding"]))
                    ranges.append({"start_char": a - start, "end_char": b - start,
                                   "start_byte": byte_start,
                                   "end_byte": byte_end,
                                   "encoding": span["encoding"]})
                    byte_progress[span["start_char"]] = (b, byte_end)
            source["byte_spans"] = ranges
        kind = "ancillary" if ancillary or re.match(r"^(?:前言|序言|后记|番外|目录|封面|版权|书名|致谢)", title) else "body"
        chapters.append({"title": title[:500], "volume_title": volume, "kind": kind,
                         "text": value, "source": source})
    return chapters, bool(headings)


def _epub(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise VibeNovelError("EPUB不是有效ZIP容器") from exc
    with archive:
        names = set()
        total = 0
        for info in archive.infolist():
            name = info.filename
            if (name in names or "\\" in name or name.startswith("/") or ":" in name
                    or ".." in PurePosixPath(name).parts or info.flag_bits & 1):
                raise VibeNovelError("EPUB包含重复、越界或加密资源")
            names.add(name)
            total += info.file_size
            if total > MAX_UNPACKED_BYTES or len(names) > 50000:
                raise VibeNovelError("EPUB解包资源超过256MiB或条目上限")
        try:
            if archive.testzip() is not None:
                raise VibeNovelError("EPUB资源CRC校验失败")
            container = _xml(archive.read("META-INF/container.xml"))
            package = next(node.attrib["full-path"] for node in container.iter() if _local(node.tag) == "rootfile")
            package, _ = _member("", package)
            opf = _xml(archive.read(package))
            metadata = {}
            for parent in opf.iter():
                if _local(parent.tag) == "metadata":
                    for node in parent:
                        value = "".join(node.itertext()).strip()
                        if value:
                            metadata.setdefault(_local(node.tag), []).append(value)
            manifest = {node.attrib["id"]: node.attrib for node in opf.iter() if _local(node.tag) == "item"}
            spine = [node.attrib["idref"] for node in opf.iter() if _local(node.tag) == "itemref"]
            if not spine or len(spine) > MAX_CHAPTERS:
                raise VibeNovelError("EPUB阅读顺序为空或过大")
            toc, issues = [], []
            for item in manifest.values():
                if "nav" in item.get("properties", "").split():
                    member, _ = _member(package, item["href"])
                    parser = _HTMLText()
                    parser.feed(archive.read(member).decode("utf-8-sig", errors="strict"))
                    toc.extend({"title": title, "resource": _member(member, href)[0],
                                "anchor": _member(member, href)[1]} for href, title in parser.links)
                elif item.get("media-type") == "application/x-dtbncx+xml":
                    member, _ = _member(package, item["href"])
                    ncx = _xml(archive.read(member))
                    for point in ncx.iter():
                        if _local(point.tag) != "navPoint":
                            continue
                        label = next(("".join(n.itertext()).strip() for n in point if _local(n.tag) == "navLabel"), "")
                        content = next((n.attrib.get("src", "") for n in point if _local(n.tag) == "content"), "")
                        if content:
                            target, anchor = _member(member, content)
                            toc.append({"title": label, "resource": target, "anchor": anchor})
            chapters = []
            for order, reference in enumerate(spine):
                item = manifest.get(reference)
                if item is None:
                    raise VibeNovelError("EPUB spine引用缺少manifest资源")
                member, _ = _member(package, item["href"])
                media = item.get("media-type", "")
                if media not in {"application/xhtml+xml", "text/html"}:
                    issues.append({"code": "non_text_spine", "severity": "warning", "resource": member,
                                   "message": "非文本阅读资源保存在原件，未提取文字"})
                    continue
                document = archive.read(member)
                declaration = re.search(br"encoding\s*=\s*['\"]([^'\"]+)", document[:300], re.I)
                codec = declaration.group(1).decode("ascii") if declaration else "utf-8-sig"
                try:
                    markup = document.decode(codec, errors="strict")
                except (UnicodeError, LookupError) as exc:
                    raise VibeNovelError("EPUB正文编码无法严格解析：" + member) from exc
                parser = _HTMLText()
                parser.feed(markup)
                parser.close()
                text = "".join(parser.parts)
                fallback = next((entry["title"] for entry in toc if entry["resource"] == member and entry["title"]), "")
                fallback = fallback or next((h for h in parser.headings if h), "") or "资源 %d" % (order + 1)
                ancillary = ("nav" in item.get("properties", "").split()
                             or fallback.strip() in {"目录", "目錄", "封面", "书名页", "版权页"}
                             or bool(re.search(r"(?:^|/)(?:toc|nav|cover)(?:[._/-]|$)", member, re.I)))
                items, detected = _split_document(text, fallback, resource=member, nodes=parser.runs, ancillary=ancillary)
                if not text.strip():
                    issues.append({"code": "empty_resource", "severity": "info", "resource": member,
                                   "message": "资源无可提取正文，原件仍保留"})
                for chapter in items:
                    chapter["source"].update(resource_sha256=digest(document), spine_order=order)
                    if "nav" in item.get("properties", "").split():
                        chapter["kind"] = "ancillary"
                chapters.extend(items)
            if not toc:
                issues.append({"code": "missing_toc", "severity": "warning", "message": "未找到可用章级目录，按spine及正文标题建立派生目录"})
            return chapters, {"metadata": metadata, "toc": toc, "issues": issues,
                              "encoding": "epub-resource-declared", "encoding_status": "strict",
                              "original_volumes_verified": False, "spine_items": len(spine),
                              "unpacked_bytes": total, "gaps": []}
        except (KeyError, StopIteration, zipfile.BadZipFile, UnicodeError, ValueError) as exc:
            raise VibeNovelError("EPUB结构、资源或目录无效：" + str(exc)[:300]) from exc


class _Owner:
    def __init__(self, store):
        self.store = store

    def path(self, relative):
        return self.store.path(".state/" + relative)


class ReferenceBookStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise VibeNovelError("参考书工作区不存在")

    def path(self, relative):
        root = safe_path(self.workspace, "library/books")
        return safe_path(root, relative)

    @contextmanager
    def _locked(self):
        self.path(".state/.runtime").mkdir(parents=True, exist_ok=True)
        with project_lock(_Owner(self), timeout=2):
            self._verified_files = set()
            self._recover()
            yield self._catalog()

    def _catalog(self):
        path = self.path(".state/catalog.json")
        if not path.exists():
            return {"schema_version": 1, "revision": 0, "books": {}}
        value = read_json(path, max_bytes=MAX_MANIFEST_BYTES)
        if (set(value) != {"schema_version", "revision", "books"} or value["schema_version"] != 1
                or type(value["revision"]) is not int or not isinstance(value["books"], dict)):
            raise VibeNovelError("参考书清单无效")
        for identity, book in value["books"].items():
            _identity(identity)
            if (not isinstance(book, dict) or book.get("book_id") != identity or book.get("outbound") not in {"local", "summary", "excerpts"}
                    or type(book.get("authorization_revision")) is not int or book["authorization_revision"] < 1
                    or type(book.get("withdrawn")) is not bool or not isinstance(book.get("editions"), list)
                    or not book["editions"]):
                raise VibeNovelError("参考书清单记录无效")
            for edition in book["editions"]:
                _identity(edition.get("edition_id"))
                if not isinstance(edition.get("metadata_hash"), str) or not _HASH.fullmatch(edition["metadata_hash"]):
                    raise VibeNovelError("参考书版本清单无效")
        return value

    def _catalog_hash(self):
        path = self.path(".state/catalog.json")
        return file_digest(path) if path.exists() else None

    def _recover(self):
        path = self.path(".state/journal.json")
        if not path.exists():
            return
        journal = read_json(path, max_bytes=2 * MAX_MANIFEST_BYTES)
        if (set(journal) != {"before_hash", "after_hash", "after"}
                or digest(json_bytes(journal["after"])) != journal["after_hash"]
                or self._catalog_hash() not in {journal["before_hash"], journal["after_hash"]}):
            raise ConflictError("参考书恢复清单冲突，未覆盖")
        # Never expose a pointer to an incomplete immutable edition.
        for book in journal["after"].get("books", {}).values():
            for item in book.get("editions", []):
                metadata = self.path("%s/editions/%s/metadata.json" % (_identity(book["book_id"]), _identity(item["edition_id"])))
                if not metadata.is_file() or file_digest(metadata) != item["metadata_hash"]:
                    raise ConflictError("参考书恢复依赖缺失或被修改")
        atomic_write(self.path(".state/catalog.json"), json_bytes(journal["after"]), overwrite=True)
        path.unlink()

    def _publish(self, catalog):
        data = json_bytes(catalog)
        if len(data) > MAX_MANIFEST_BYTES:
            raise VibeNovelError("参考书目录超过8MiB，未发布")
        journal = {"before_hash": self._catalog_hash(), "after_hash": digest(data), "after": catalog}
        atomic_write(self.path(".state/journal.json"), json_bytes(journal))
        atomic_write(self.path(".state/catalog.json"), data, overwrite=True)
        self.path(".state/journal.json").unlink()

    def _book(self, catalog, book_id):
        book = catalog["books"].get(_identity(book_id))
        if book is None:
            raise VibeNovelError("参考书不存在")
        return book

    def _edition(self, book, edition_id=None):
        edition_id = _identity(edition_id or book["latest_edition_id"])
        item = next((entry for entry in book["editions"] if entry["edition_id"] == edition_id), None)
        if item is None:
            raise VibeNovelError("参考书版本不存在")
        root = "%s/editions/%s/" % (book["book_id"], edition_id)
        metadata_path = self.path(root + "metadata.json")
        if not metadata_path.is_file() or file_digest(metadata_path) != item["metadata_hash"]:
            raise ConflictError("参考书版本清单缺失或被修改")
        metadata = read_json(metadata_path, max_bytes=MAX_MANIFEST_BYTES)
        if metadata.get("book_id") != book["book_id"] or metadata.get("edition_id") != edition_id:
            raise ConflictError("参考书版本身份不一致")
        return root, metadata

    def _read_checked(self, root, relative, expected):
        path = self.path(root + relative)
        key = (str(path), expected)
        if not path.is_file() or key not in self._verified_files and file_digest(path) != expected:
            raise ConflictError("参考书数据缺失或被修改：" + relative)
        self._verified_files.add(key)
        return path

    def _chapters(self, root, metadata):
        chapters = []
        for page in metadata["chapter_pages"]:
            path = self._read_checked(root, page["path"], page["hash"])
            value = read_json(path, max_bytes=MAX_MANIFEST_BYTES)
            chapters.extend(value["chapters"])
        if len(chapters) != metadata["counts"]["chapters"]:
            raise ConflictError("参考书章节数量与版本不一致")
        return chapters

    def _chapter_text(self, root, chapter):
        pieces = []
        for block in chapter["blocks"]:
            path = self._read_checked(root, block["path"], block["shard_hash"])
            with path.open("rb") as stream:
                stream.seek(block["start_byte"])
                raw = stream.read(block["end_byte"] - block["start_byte"])
            if digest(raw) != block["hash"]:
                raise ConflictError("参考书正文块指纹不一致")
            pieces.append(raw.decode("utf-8", errors="strict"))
        value = "".join(pieces)
        if digest(value.encode("utf-8")) != chapter["hash"] or len(value) != chapter["characters"]:
            raise ConflictError("参考书章节正文指纹不一致")
        return value

    def _detail(self, book, edition_id=None, offset=0, limit=100):
        root, metadata = self._edition(book, edition_id)
        chapters = self._chapters(root, metadata)
        result = copy.deepcopy(metadata)
        result.update(title=book["title"], author=book["author"], outbound=book["outbound"],
                      withdrawn=book["withdrawn"], authorization_revision=book["authorization_revision"],
                      hash=digest(json_bytes(book)), total_chapters=len(chapters), offset=offset, limit=limit,
                      chapters=chapters[offset:offset + limit], editions=copy.deepcopy(book["editions"]))
        return result

    def list_books(self):
        with self._locked() as catalog:
            result = []
            for book in catalog["books"].values():
                _, metadata = self._edition(book)
                result.append({key: copy.deepcopy(book[key]) for key in ("book_id", "title", "author", "outbound", "withdrawn", "authorization_revision", "latest_edition_id")})
                result[-1].update(edition_id=metadata["edition_id"], counts=metadata["counts"],
                                  hash=digest(json_bytes(book)), issues_count=len(metadata["issues"]))
            return result

    def detail(self, book_id, edition_id=None, *, offset=0, limit=100):
        _range(offset, limit, 500)
        with self._locked() as catalog:
            return self._detail(self._book(catalog, book_id), edition_id, offset, limit)

    def chapter(self, book_id, chapter_id, edition_id=None, *, offset=0, limit=24000):
        _range(offset, limit, MAX_SNAPSHOT_CHARACTERS)
        _identity(chapter_id)
        with self._locked() as catalog:
            book = self._book(catalog, book_id)
            root, metadata = self._edition(book, edition_id)
            chapter = next((item for item in self._chapters(root, metadata) if item["chapter_id"] == chapter_id), None)
            if chapter is None:
                raise VibeNovelError("参考书章节不存在")
            text = self._chapter_text(root, chapter)
            selected = text[offset:offset + limit]
            source = read_json(self._read_checked(root, chapter["source_path"], chapter["source_hash"]), max_bytes=MAX_MANIFEST_BYTES)
            return {**copy.deepcopy(chapter), "book_id": book_id, "edition_id": metadata["edition_id"],
                    "text": selected, "total_characters": len(text), "start_char": min(offset, len(text)),
                    "end_char": min(offset + limit, len(text)), "range_hash": digest(selected.encode("utf-8")),
                    "source": source, "identity": {"book_id": book_id, "edition_id": metadata["edition_id"],
                    "chapter_id": chapter_id, "parser_version": metadata["parser_version"]}}

    def update_policy(self, book_id, *, expected_hash, outbound, withdrawn=False):
        if outbound not in {"local", "summary", "excerpts"} or type(withdrawn) is not bool:
            raise VibeNovelError("参考书外发政策无效")
        with self._locked() as catalog:
            book = self._book(catalog, book_id)
            if expected_hash != digest(json_bytes(book)):
                raise ConflictError("参考书政策已变化，请重新载入")
            if book["outbound"] != outbound or book["withdrawn"] != withdrawn:
                book.update(outbound=outbound, withdrawn=withdrawn, authorization_revision=book["authorization_revision"] + 1)
                catalog["revision"] += 1
                self._publish(catalog)
            return self._detail(book)

    def import_book(self, filename, raw, *, title="", author="", encoding=None):
        _bounded(filename, 255, empty=False)
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise VibeNovelError("参考书导入只接受文件名，不接受路径")
        safe_path(self.workspace, "_filename_check/" + filename)
        _bounded(title)
        _bounded(author)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ORIGINAL_BYTES:
            raise VibeNovelError("参考书原件需要1字节至128MiB")
        kind = Path(filename).suffix.lower()
        if kind == ".epub":
            chapters, info = _epub(raw)
            selected_title = title or next(iter(info["metadata"].get("title", [])), "") or Path(filename).stem
            selected_author = author or next(iter(info["metadata"].get("creator", [])), "")
        elif kind in {".txt", ".md"}:
            text, selected, spans, gaps = _decode_segments(raw, encoding)
            chapters, detected = _split_document(text, Path(filename).stem, byte_spans=spans)
            controls = []
            for match in re.finditer("\x00", text):
                at = match.start()
                span = next(item for item in spans if item["start_char"] <= at < item["end_char"])
                byte_start = span["start_byte"] + len(text[span["start_char"]:at].encode(span["encoding"]))
                controls.append({"at_char": at, "start_byte": byte_start,
                                 "end_byte": byte_start + len("\x00".encode(span["encoding"])), "codepoint": "U+0000"})
            info = {"metadata": {}, "toc": [], "issues": [], "encoding": selected,
                    "encoding_status": "provisional_with_gaps" if gaps else "strict",
                    "original_volumes_verified": False, "gaps": gaps, "control_characters": controls}
            if not detected:
                info["issues"].append({"code": "unrecognized_chapters", "severity": "warning", "message": "未识别自然章标题，保留为连续文本；技术块不代表作者章节"})
            for gap in gaps:
                info["issues"].append({"code": "encoding_gap", "severity": "error", "message": "原件非法字节已隔离保存；相邻可读文本不表示缺口已修复", **gap})
            for control in controls:
                info["issues"].append({"code": "control_character", "severity": "warning", "message": "孤立NUL码点按原样保留，未删除；包含该位置的模型输入需显式确认异常", **control})
            selected_title, selected_author = title or Path(filename).stem, author
        else:
            raise VibeNovelError("参考书仅支持EPUB、TXT、Markdown")
        selected_title = _bounded(selected_title, 500, empty=False)
        selected_author = _bounded(selected_author, 500)
        if not chapters or not any(item["text"].strip() for item in chapters):
            raise VibeNovelError("参考书没有可读正文")
        if len(chapters) > MAX_CHAPTERS:
            raise VibeNovelError("参考书超过20000章；未截断")
        total_bytes = sum(len(item["text"].encode("utf-8")) for item in chapters)
        if total_bytes > MAX_TEXT_BYTES:
            raise VibeNovelError("参考书规范正文超过128MiB；未截断")
        original_hash = digest(raw)
        book_id = digest(json_bytes([selected_title.strip(), selected_author.strip()]))[:32]
        edition_id = digest(json_bytes([original_hash, PARSER_VERSION, info["encoding"], selected_title, selected_author]))[:32]
        with self._locked() as catalog:
            book = catalog["books"].get(book_id)
            if book is not None and any(item["edition_id"] == edition_id for item in book["editions"]):
                result = self._detail(book, edition_id)
                if not self._verify_locked(book, edition_id)["ok"]:
                    raise ConflictError("已有参考书版本校验失败，未覆盖")
                return result
            relative = "%s/editions/%s" % (book_id, edition_id)
            final = self.path(relative)
            used_bytes = sum(path.stat().st_size for path in tree_files(self.path(".state").parent)
                             if ".staging" not in path.relative_to(self.path(".state").parent).parts)
            if used_bytes + len(raw) + total_bytes > MAX_CORPUS_BYTES:
                raise VibeNovelError("参考语料整库超过2GiB硬配额；未导入，可备份后整理或显式升级容量合同")
            stage_name = ".staging/edition-" + uuid4().hex
            stage = self.path(stage_name)
            stage.mkdir(parents=True)
            try:
                metadata = self._write_edition(stage, book_id, edition_id, filename, raw, chapters, info)
                staged_bytes = sum(path.stat().st_size for path in tree_files(stage))
                if used_bytes + staged_bytes > MAX_CORPUS_BYTES:
                    raise VibeNovelError("参考语料加定位与清单后超过2GiB硬配额；未发布")
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    # A process may have stopped after immutable publication and before its pointer.
                    old = read_json(self.path(relative + "/metadata.json"), max_bytes=MAX_MANIFEST_BYTES)
                    if old.get("original_hash") != original_hash or old.get("edition_id") != edition_id:
                        raise ConflictError("未登记版本与原件不一致，未覆盖")
                    metadata = old
                else:
                    stage.rename(final)
                metadata_hash = file_digest(self.path(relative + "/metadata.json"))
                entry = {"edition_id": edition_id, "metadata_hash": metadata_hash, "original_hash": original_hash}
                if book is None:
                    book = {"book_id": book_id, "title": selected_title, "author": selected_author,
                            "outbound": "local", "withdrawn": False, "authorization_revision": 1,
                            "editions": [], "latest_edition_id": edition_id}
                    catalog["books"][book_id] = book
                book["editions"].append(entry)
                book["latest_edition_id"] = edition_id
                if not self._verify_locked(book, edition_id)["ok"]:
                    raise ConflictError("参考书发布前校验失败")
                catalog["revision"] += 1
                self._publish(catalog)
                return self._detail(book, edition_id)
            finally:
                cleanup_stage(stage, self.path(".staging"), "edition-")

    def _write_edition(self, stage, book_id, edition_id, filename, raw, chapters, info):
        def write(relative, data):
            if relative.endswith(".json") and len(data) > MAX_MANIFEST_BYTES:
                raise VibeNovelError("参考书定位或清单超过8MiB；未截断")
            path = safe_path(stage, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
            return digest(data)

        write("originals/" + filename, raw)
        records, volumes, files, seen = [], [], [], {}
        shard = bytearray()
        shard_blocks = []
        shard_chapters = []
        all_text_hash = __import__("hashlib").sha256()
        def flush():
            if not shard_blocks:
                return
            number = len(volumes) + 1
            relative = "text/part-%05d.txt" % number
            value = bytes(shard)
            fingerprint = write(relative, value)
            for block in shard_blocks:
                block.update(path=relative, shard_hash=fingerprint)
            volumes.append({"volume_id": "technical-%05d" % number, "label": "技术分册 %d" % number,
                            "bytes": len(value), "characters": len(value.decode("utf-8")),
                            "chapter_ids": list(dict.fromkeys(shard_chapters))})
            files.append({"path": relative, "hash": fingerprint, "bytes": len(value)})
            shard.clear()
            shard_blocks.clear()
            shard_chapters.clear()
        char_cursor = 0
        current_volume = ""
        for index, item in enumerate(chapters):
            text = item["text"]
            encoded = text.encode("utf-8")
            all_text_hash.update(encoded)
            chapter_id = digest(json_bytes([edition_id, index, item["source"].get("resource"), item["source"]["document_start_char"]]))[:32]
            if item["volume_title"]:
                current_volume = item["volume_title"]
            if shard and len(shard) + len(encoded) > TARGET_VOLUME_BYTES:
                flush()
            blocks = []
            cursor = 0
            while cursor < len(text):
                room = MAX_SHARD_BYTES - len(shard)
                # UTF-8 length bounded via binary search; natural newline is preferred.
                low, high = cursor + 1, min(len(text), cursor + room) + 1
                while low < high:
                    mid = (low + high) // 2
                    if len(text[cursor:mid].encode("utf-8")) <= room:
                        low = mid + 1
                    else:
                        high = mid
                end = low - 1
                if end <= cursor:
                    flush()
                    continue
                if end < len(text):
                    newline = text.rfind("\n", cursor, end)
                    if newline > cursor + (end - cursor) // 2:
                        end = newline + 1
                data = text[cursor:end].encode("utf-8")
                block = {"block_id": "%s-%05d" % (chapter_id, len(blocks) + 1),
                         "start_char": cursor, "end_char": end, "start_byte": len(shard),
                         "end_byte": len(shard) + len(data), "hash": digest(data),
                         "technical_volume_id": "technical-%05d" % (len(volumes) + 1)}
                blocks.append(block)
                shard_blocks.append(block)
                shard_chapters.append(chapter_id)
                shard.extend(data)
                cursor = end
                if cursor < len(text):
                    flush()
            normalized = digest("".join(text.split()).encode("utf-8")) if text.strip() else None
            duplicate = seen.get(normalized) if normalized else None
            if normalized and normalized not in seen:
                seen[normalized] = chapter_id
            gaps = [gap for gap in info["gaps"] if char_cursor <= gap["at_char"] < char_cursor + len(text)
                    or gap["at_char"] == char_cursor + len(text) and index == len(chapters) - 1]
            controls = [control for control in info.get("control_characters", [])
                        if char_cursor <= control["at_char"] < char_cursor + len(text)]
            source_path = "locations/%s.json" % chapter_id
            source_data = {**item["source"], "chapter_id": chapter_id, "gaps": gaps, "control_characters": controls}
            source_hash = write(source_path, json_bytes(source_data))
            files.append({"path": source_path, "hash": source_hash})
            record = {"chapter_id": chapter_id, "order": index + 1, "title": item["title"],
                      "volume_title": current_volume, "kind": item["kind"], "characters": len(text),
                      "utf8_bytes": len(encoded), "hash": digest(encoded), "blocks": blocks,
                      "source_path": source_path, "source_hash": source_hash,
                      "technical_volume_ids": list(dict.fromkeys(b["technical_volume_id"] for b in blocks)),
                      "duplicate_of": duplicate, "incomplete": bool(gaps),
                      "not_fully_decoded": bool(gaps),
                      "needs_review": bool(gaps or controls), "control_characters": len(controls),
                      "analysis_status": "unread"}
            records.append(record)
            if duplicate:
                info["issues"].append({"code": "duplicate_candidate", "severity": "warning", "chapter_id": chapter_id,
                                       "duplicate_of": duplicate, "message": "去空白正文完全相同；保留原顺序与文本，待作者确认隔离"})
            char_cursor += len(text)
        flush()
        pages = []
        for start in range(0, len(records), 100):
            relative = "chapters/page-%05d.json" % (len(pages) + 1)
            data = json_bytes({"chapters": records[start:start + 100]})
            if len(data) > MAX_MANIFEST_BYTES:
                raise VibeNovelError("参考书章节页超过8MiB")
            pages.append({"path": relative, "hash": write(relative, data)})
        counts = {"chapters": len(records), "characters": char_cursor,
                  "utf8_bytes": sum(item["utf8_bytes"] for item in records), "technical_volumes": len(volumes),
                  "duplicate_candidates": sum(bool(item["duplicate_of"]) for item in records),
                  "encoding_gaps": len(info["gaps"]), "ancillary": sum(item["kind"] == "ancillary" for item in records),
                  "analyzed_chapters": 0, "unread_chapters": len(records),
                  "parsed_chars": char_cursor, "body_chars": sum(item["characters"] for item in records if item["kind"] == "body"),
                  "decode_gaps": len(info["gaps"]), "total_chapters": len(records)}
        counts["control_characters"] = len(info.get("control_characters", []))
        metadata = {"schema_version": 1, "book_id": book_id, "edition_id": edition_id,
                    "parser_version": PARSER_VERSION, "created_at": utc_now(), "filename": filename,
                    "original_hash": digest(raw), "original_bytes": len(raw), "original_path": "originals/" + filename,
                    "text_hash": all_text_hash.hexdigest(), "counts": counts, "technical_volumes": volumes,
                    "chapter_pages": pages, "files": files, "completeness": "not_verified",
                    "analysis_status": "unread", "transformations": ["EPUB可见文本与结构换行" if filename.lower().endswith(".epub") else "严格解码；原换行保留；坏字节单列缺口"],
                    **info}
        data = json_bytes(metadata)
        if len(data) > MAX_MANIFEST_BYTES:
            raise VibeNovelError("参考书版本清单超过8MiB；未截断")
        write("metadata.json", data)
        return metadata

    def _verify_locked(self, book, edition_id=None):
        root, metadata = self._edition(book, edition_id)
        issues = []
        try:
            self._read_checked(root, metadata["original_path"], metadata["original_hash"])
            for item in metadata["files"]:
                self._read_checked(root, item["path"], item["hash"])
            chapters = self._chapters(root, metadata)
            hasher = __import__("hashlib").sha256()
            for chapter in chapters:
                hasher.update(self._chapter_text(root, chapter).encode("utf-8"))
            if hasher.hexdigest() != metadata["text_hash"]:
                raise ConflictError("参考书整版本正文指纹不一致")
        except (VibeNovelError, OSError, UnicodeError) as exc:
            issues.append(str(exc))
        return {"ok": not issues, "book_id": book["book_id"], "edition_id": metadata["edition_id"],
                "original_hash": metadata["original_hash"], "text_hash": metadata["text_hash"],
                "issues": issues, "completeness": "not_verified", "analysis_status": "unread"}

    def verify(self, book_id):
        with self._locked() as catalog:
            return self._verify_locked(self._book(catalog, book_id))

    @staticmethod
    def _snapshot_selections(chapter_ids, chapter_ranges, allow_partial):
        if type(allow_partial) is not bool:
            raise VibeNovelError("参考书部分解码授权格式无效")
        if chapter_ranges is not None:
            if chapter_ids is not None or not isinstance(chapter_ranges, list) or not 1 <= len(chapter_ranges) <= 100:
                raise VibeNovelError("参考书快照需要1至100个章段，不能同时指定整章")
            selections = []
            for item in chapter_ranges:
                if not isinstance(item, dict) or set(item) != {"chapter_id", "start_char", "end_char"}:
                    raise VibeNovelError("参考书章段字段无效")
                _identity(item["chapter_id"])
                if type(item["start_char"]) is not int or type(item["end_char"]) is not int or not 0 <= item["start_char"] < item["end_char"]:
                    raise VibeNovelError("参考书章段必须为非空Unicode半开区间")
                selections.append(item)
            for i, first in enumerate(selections):
                if any(first["chapter_id"] == other["chapter_id"] and max(first["start_char"], other["start_char"]) < min(first["end_char"], other["end_char"])
                       for other in selections[i + 1:]):
                    raise VibeNovelError("参考书快照章段不能重叠重复")
        else:
            if (not isinstance(chapter_ids, list) or not 1 <= len(chapter_ids) <= 100
                    or any(not isinstance(item, str) for item in chapter_ids)
                    or len(set(chapter_ids)) != len(chapter_ids)):
                raise VibeNovelError("参考书快照需要1至100个不重复章节")
            selections = [{"chapter_id": _identity(chapter_id), "start_char": 0, "end_char": None} for chapter_id in chapter_ids]
        return selections

    def _snapshot_context(self, catalog, book_id, edition_id):
        book = self._book(catalog, book_id)
        if book["withdrawn"] or book["outbound"] != "excerpts":
            raise ConflictError("参考书未授权外发原文，不能创建模型片段快照")
        root, metadata = self._edition(book, edition_id)
        self._read_checked(root, metadata["original_path"], metadata["original_hash"])
        indexed = {item["chapter_id"]: item for item in self._chapters(root, metadata)}
        return book, root, metadata, indexed

    def _build_snapshot(self, book_id, context, selections, max_characters, allow_partial, chapter_cache):
        book, root, metadata, indexed = context
        refs, texts, total = [], [], 0
        for selection in selections:
            chapter_id = selection["chapter_id"]
            chapter = indexed.get(chapter_id)
            if chapter is None:
                raise VibeNovelError("参考书快照章节不存在")
            original = chapter_cache.get(chapter_id)
            if original is None:
                original = self._chapter_text(root, chapter)
                chapter_cache[chapter_id] = original
            start, end = selection["start_char"], selection["end_char"]
            end = len(original) if end is None else end
            if not 0 <= start <= end <= len(original):
                raise VibeNovelError("参考书章段超过章内正文范围")
            source = read_json(self._read_checked(root, chapter["source_path"], chapter["source_hash"]), max_bytes=MAX_MANIFEST_BYTES)
            source_start = source["document_start_char"]
            gaps = [gap for gap in source.get("gaps", []) if start <= gap["at_char"] - source_start <= end]
            controls = [control for control in source.get("control_characters", []) if start <= control["at_char"] - source_start < end]
            if (gaps or controls) and not allow_partial:
                raise ConflictError("选定章段含未解码缺口或控制字符，需明确允许部分文本/异常并在研究覆盖中保留记录")
            text = original[start:end]
            if not text:
                raise VibeNovelError("空白资源没有可发送的正文区间")
            separator = 2 if texts else 0
            if total + len(text) + separator > max_characters:
                raise VibeNovelError("选定完整章节超过快照字符预算；未静默截断")
            refs.append({"type": "book-evidence", "book_id": book_id, "edition_id": metadata["edition_id"],
                         "parser_version": metadata["parser_version"], "chapter_id": chapter_id,
                         "start_char": start, "end_char": end, "hash": chapter["hash"],
                         "range_hash": digest(text.encode("utf-8")), "quote": text, "incomplete": bool(gaps),
                         "gaps": gaps, "control_characters": controls})
            texts.append(text)
            total += len(text) + separator
        result = {"schema_version": 1, "book_id": book_id, "edition_id": metadata["edition_id"],
                  "parser_version": metadata["parser_version"], "original_hash": metadata["original_hash"],
                  "authorization_revision": book["authorization_revision"], "outbound": book["outbound"],
                  "allow_partial": allow_partial,
                  "refs": refs, "texts": texts, "text": "\n\n".join(texts), "characters": total,
                  "typed_deps": [{"type": "reference_book", "book_id": book_id, "edition_id": metadata["edition_id"],
                                  "metadata_hash": next(e["metadata_hash"] for e in book["editions"] if e["edition_id"] == metadata["edition_id"]),
                                  "authorization_revision": book["authorization_revision"]}]}
        result["hash"] = digest(json_bytes(result))
        return result

    def snapshot(self, book_id, edition_id, chapter_ids=None, max_characters=60000, *, chapter_ranges=None, allow_partial=False):
        selections = self._snapshot_selections(chapter_ids, chapter_ranges, allow_partial)
        _range(0, max_characters, MAX_SNAPSHOT_CHARACTERS)
        with self._locked() as catalog:
            context = self._snapshot_context(catalog, book_id, edition_id)
            return self._build_snapshot(book_id, context, selections, max_characters, allow_partial, {})

    def snapshots_ranges(self, book_id, edition_id, range_batches, *, max_characters=60000, allow_partial=False):
        """Local bounded verification scope; each returned snapshot keeps its own limit.

        Hash original, shards and index pages once in this locked operation,
        never across operations. The list is for local planning or dependency
        checking; it does not authorize one whole-book model request.
        """
        if not isinstance(range_batches, list) or not 1 <= len(range_batches) <= MAX_CHAPTERS:
            raise VibeNovelError("批量快照需要1至20000组有界章段")
        _range(0, max_characters, MAX_SNAPSHOT_CHARACTERS)
        prepared = [self._snapshot_selections(None, ranges, allow_partial) for ranges in range_batches]
        intervals = {}
        count = 0
        for selections in prepared:
            count += len(selections)
            if count > 2 * MAX_CHAPTERS:
                raise VibeNovelError("批量快照章段总数超过40000")
            for selection in selections:
                intervals.setdefault(selection["chapter_id"], []).append((selection["start_char"], selection["end_char"]))
        for ranges in intervals.values():
            ranges.sort()
            if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
                raise VibeNovelError("批量快照范围不能跨组重叠重复")
        with self._locked() as catalog:
            before = self._catalog_hash()
            context = self._snapshot_context(catalog, book_id, edition_id)
            cache, results, total_bytes = {}, [], 0
            for selections in prepared:
                result = self._build_snapshot(book_id, context, selections, max_characters, allow_partial, cache)
                total_bytes += sum(len(text.encode("utf-8")) for text in result["texts"])
                if total_bytes > MAX_TEXT_BYTES:
                    raise VibeNovelError("批量快照总正文超过128MiB；未截断")
                results.append(result)
            if self._catalog_hash() != before:
                raise ConflictError("批量快照期间参考授权或清单变化，未返回片段")
            return results

    def snapshot_ranges(self, book_id, edition_id, chapter_ranges, *, max_characters=60000, allow_partial=False):
        return self.snapshot(book_id, edition_id, max_characters=max_characters,
                             chapter_ranges=chapter_ranges, allow_partial=allow_partial)

    def validate_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            raise VibeNovelError("参考书快照格式无效")
        payload = copy.deepcopy(snapshot)
        fingerprint = payload.pop("hash", None)
        if fingerprint != digest(json_bytes(payload)):
            raise ConflictError("参考书快照内容已变化")
        expected = self.snapshot_ranges(snapshot.get("book_id"), snapshot.get("edition_id"),
                                        [{key: ref.get(key) for key in ("chapter_id", "start_char", "end_char")} for ref in snapshot.get("refs", [])],
                                        allow_partial=snapshot.get("allow_partial", False))
        if expected != snapshot:
            raise ConflictError("参考书快照版本、授权或原文已过期")
        return True

    def validate_snapshots(self, snapshots):
        """Verify a bounded set in one locked operation; no persistent trust cache.

        A long reading chain should hash each original and directory once per
        validation, rather than once per checkpoint. Every subsequent operation
        rereads the live catalog, original, metadata, text and source evidence.
        """
        if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= MAX_CHAPTERS:
            raise VibeNovelError("批量校验需要1至20000份参考书快照")
        prepared, total = [], 0
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise VibeNovelError("参考书快照格式无效")
            payload = copy.deepcopy(snapshot)
            fingerprint = payload.pop("hash", None)
            if fingerprint != digest(json_bytes(payload)):
                raise ConflictError("参考书快照内容已变化")
            selections = self._snapshot_selections(None,
                [{key: ref.get(key) for key in ("chapter_id", "start_char", "end_char")}
                 for ref in snapshot.get("refs", [])], snapshot.get("allow_partial", False))
            total += sum(len(value.encode("utf-8")) for value in snapshot.get("texts", []))
            if total > MAX_TEXT_BYTES:
                raise VibeNovelError("批量校验总正文超过128MiB；未截断")
            prepared.append((snapshot, selections))
        with self._locked() as catalog:
            before = self._catalog_hash()
            contexts, caches = {}, {}
            for snapshot, selections in prepared:
                key = (snapshot.get("book_id"), snapshot.get("edition_id"))
                if key not in contexts:
                    contexts[key] = self._snapshot_context(catalog, *key)
                    caches[key] = {}
                expected = self._build_snapshot(key[0], contexts[key], selections,
                    MAX_SNAPSHOT_CHARACTERS, snapshot.get("allow_partial", False), caches[key])
                if expected != snapshot:
                    raise ConflictError("参考书快照版本、授权或原文已过期")
            if self._catalog_hash() != before:
                raise ConflictError("批量校验期间参考授权或清单变化")
        return True

    def search(self, book_id, q, *, edition_id=None, limit=20):
        _bounded(q, 200, empty=False)
        _range(0, limit, 100)
        with self._locked() as catalog:
            book = self._book(catalog, book_id)
            root, metadata = self._edition(book, edition_id)
            chapters = self._chapters(root, metadata)
            results, scanned, matches = [], 0, 0
            expression = re.compile(re.escape(q), re.I)
            for chapter in chapters:
                text = self._chapter_text(root, chapter)
                scanned += len(text)
                # Literal Unicode search only; offsets stay in the original string.
                match = expression.search(text)
                if match is not None:
                    matches += 1
                if match is not None and len(results) < limit:
                    start, end = max(0, match.start() - 100), min(len(text), match.end() + 160)
                    excerpt = text[start:end]
                    results.append({"book_id": book_id, "edition_id": metadata["edition_id"],
                                    "chapter_id": chapter["chapter_id"], "title": chapter["title"],
                                    "start_char": start, "end_char": end, "text": excerpt,
                                    "hash": chapter["hash"], "range_hash": digest(excerpt.encode("utf-8"))})
            return {"book_id": book_id, "edition_id": metadata["edition_id"], "query": q, "results": results,
                    "retrieval": "local_literal", "outbound": book["outbound"], "withdrawn": book["withdrawn"],
                    "matched_chapters": matches, "results_truncated": matches > len(results),
                    "coverage": {"scanned_chapters": len(chapters), "total_chapters": len(chapters),
                                 "scanned_characters": scanned, "truncated": False, "analysis": False}}
