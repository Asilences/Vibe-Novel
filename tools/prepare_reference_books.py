#!/usr/bin/env python3
"""Import the author's explicit local catalog, without model calls or canon writes.

Example: python tools/prepare_reference_books.py --import --backup
The private report and archives are deliberately outside Git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibenovel.core.files import atomic_write, file_digest, json_bytes, read_json, safe_path, tree_files
from vibenovel.core.project import utc_now
from vibenovel.core.reference_books import ReferenceBookStore


def prepare(workspace: Path, catalog: Path, *, do_import=False, backup=False):
    data = read_json(catalog, max_bytes=8 * 1024 * 1024)
    records = data.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 200:
        raise ValueError("Catalog must contain 1-200 explicit local file records")
    store = ReferenceBookStore(workspace)
    report = {"schema": 1, "created_at": utc_now(), "catalog": str(catalog),
              "paid_api_calls": 0, "canon_modified": False, "records": []}
    for record in records:
        path = Path(record["path"])
        title = record.get("catalog_title", path.stem)
        receipt = {"path": str(path), "title": title, "status": "not_imported"}
        try:
            if not path.is_file() or path.is_symlink():
                raise ValueError("Explicit source is missing or linked")
            actual = file_digest(path)
            if actual != record.get("sha256"):
                raise ValueError("Original fingerprint differs from the preflight catalog")
            receipt.update(original_hash=actual, original_bytes=path.stat().st_size)
            if do_import:
                # Author hints remain in the original catalog; only preserve
                # declared metadata here, never invent a missing author.
                authors = record.get("metadata", {}).get("creator", [])
                author = "; ".join(item for item in authors if isinstance(item, str))
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != actual:
                    raise ValueError("Original changed during import")
                book = store.import_book(path.name, raw, title=title, author=author)
                verification = store.verify(book["book_id"])
                receipt.update(status="imported_local", book_id=book["book_id"],
                               edition_id=book["edition_id"], counts=book.get("counts", {}),
                               total_chapters=book.get("total_chapters"), issues=book.get("issues", []),
                               verification=verification, outbound=book.get("outbound"))
                if file_digest(path) != actual:
                    raise ValueError("Original changed after import")
            else:
                receipt["status"] = "verified_only"
        except Exception as exc:
            receipt.update(status="failed", error=str(exc))
        report["records"].append(receipt)
        print(json.dumps({key: receipt.get(key) for key in
                          ("title", "status", "book_id", "total_chapters", "error")}, ensure_ascii=True), flush=True)
    if do_import:
        destination = safe_path(workspace, "library/catalogs/reference-corpus-20261020.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, json_bytes(report), overwrite=True)
        if backup:
            # Include both personal data and book bindings; no local credentials.
            backup_dir = safe_path(workspace, ".runtime-build/library-backups")
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = report["created_at"].replace(":", "").replace("+", "_").replace(".", "_")
            archive = backup_dir / ("reference-corpus-" + stamp + ".zip")
            roots = [safe_path(workspace, "library")]
            novels = safe_path(workspace, "novels")
            if novels.exists():
                for novel in novels.iterdir():
                    if novel.is_dir() and not novel.is_symlink():
                        candidate = safe_path(novel, "library")
                        if candidate.is_dir():
                            roots.append(candidate)
            count = 0
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
                for root in roots:
                    for path in tree_files(root):
                        bundle.write(path, path.relative_to(workspace).as_posix())
                        count += 1
            with zipfile.ZipFile(archive) as bundle:
                bad = bundle.testzip()
                if bad:
                    raise ValueError("Backup CRC failed: " + bad)
            print(json.dumps({"backup": str(archive), "files": count, "crc": "ok"}, ensure_ascii=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--catalog", type=Path, default=ROOT / "library/catalogs/reference-novels-20261020.json")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()
    report = prepare(args.workspace.resolve(), args.catalog.resolve(), do_import=args.do_import, backup=args.backup)
    return 1 if any(item["status"] == "failed" for item in report["records"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
