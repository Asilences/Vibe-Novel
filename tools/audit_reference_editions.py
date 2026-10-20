"""Offline corpus curation audit; only candidates, never publish or API calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vibenovel.core.files import atomic_write, digest, json_bytes
from vibenovel.core.reference_editions import ReferenceEditionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--save-candidates", action="store_true")
    args = parser.parse_args()
    store = ReferenceEditionStore(args.workspace)
    report = {"schema_version": 1, "purpose": "只读目录/异常审计；候选未批准，原件和当前版未改变", "books": []}
    for book in store.books.list_books():
        audit = store.audit(book["book_id"])
        entry = {"book_id": book["book_id"], "edition_id": audit["edition_id"], "title": audit["title"], "counts": book["counts"],
                 "issues": audit["issues"], "findings": audit["findings"], "heading_candidates": audit["heading_candidates"], "candidates": []}
        proposals = []
        if audit["proposal"]["patches"]:
            proposals.append(("control-removal", audit["proposal"]))
        if audit["heading_candidates"] and len(audit["alternatives"]["headings"]["chapters"]) != len(audit["proposal"]["chapters"]):
            proposals.append(("heading-rebuild", audit["alternatives"]["headings"]))
        if any(f["kind"] == "duplicate" for f in audit["findings"]):
            proposals.append(("isolate-earlier-duplicates", audit["alternatives"]["retain_later"]))
        if args.save_candidates:
            for label, request in proposals:
                preview = store.preview(book["book_id"], request)
                candidate = store.create(book["book_id"], request, expected_preview=preview["hash"], idempotency_key="curation-a12-" + label + "-" + digest(json_bytes(request))[:12])
                entry["candidates"].append({"id": candidate["id"], "hash": candidate["hash"], "kind": label, "summary": candidate["summary"]})
        report["books"].append(entry)
        print(json.dumps({"title": book["title"], "issues": len(audit["issues"]), "candidate_count": len(entry["candidates"]), "headings": len(audit["heading_candidates"])}, ensure_ascii=True), flush=True)
    target = store.books.workspace / "library" / "catalogs" / "reference-curation-a12.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json_bytes(report), overwrite=True)
    print(json.dumps({"report": str(target), "books": len(report["books"]), "published": 0}, ensure_ascii=True))


if __name__ == "__main__":
    main()
