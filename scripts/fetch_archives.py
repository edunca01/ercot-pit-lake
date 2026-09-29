"""Download the latest posting per product and commit a trimmed copy of its CSV.

The committed file is samples/archive/<product>.csv: one posting, trimmed with the rule in
scripts/samples.py, its lines byte for byte as ERCOT wrote them. The full zip is kept under
samples/archive/<product>/ (gitignored) so it can be re-trimmed without the network.

    uv run python -m scripts.fetch_archives [--products KEY,KEY]
    uv run python -m scripts.fetch_archives --products KEY --from path/to/posting.zip|.csv
    uv run python -m scripts.fetch_archives --discover NP6-345-CD
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from ercot_lake.timeutil import ct_to_utc
from ingest.archive import CsvMember, iter_csv_members
from ingest.config import Product, settings
from ingest.timeutil import parse_post_datetime
from scripts.samples import DISCOVER_DIR, SAMPLES_DIR, client, record, trim_csv

HEAD_LINES = 50  # --discover: enough rows to read the column names and the time style


def _only_csv(product_key: str, blob: bytes) -> CsvMember:
    members = iter_csv_members(blob)
    if len(members) != 1:
        msg = f"{product_key}: expected one CSV in a posting, found {len(members)}"
        raise ValueError(msg)
    return members[0]


def write_sample(p: Product, csv_text: str) -> Path:
    path = SAMPLES_DIR / "archive" / f"{p.key}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = trim_csv(p, csv_text)
    path.write_text(text)
    print(f"{p.key}: kept {text.count(chr(10)) - 1} rows -> {path.name}")
    return path


def fetch_latest(p: Product) -> tuple[bytes, dict[str, object]]:
    with client() as c:
        docs = c.list_archives(p.archive_id, {"size": 1, "page": 1}).get("archives", [])
        if not docs:
            msg = f"{p.key}: nothing listed for {p.archive_id}"
            raise RuntimeError(msg)
        doc_id = docs[0]["docId"]
        blob = c.download_archive(p.archive_id, doc_id)
    keep = SAMPLES_DIR / "archive" / p.key / f"{p.archive_id}_{doc_id}.zip"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(blob)
    print(f"{p.key}: {p.archive_id} doc {doc_id} posted {docs[0].get('postDatetime')}")
    listed = parse_post_datetime(docs[0]["postDatetime"])
    return blob, {"archive_id": p.archive_id, "doc_id": doc_id, "listed_at": listed.isoformat()}


def discover(archive_id: str) -> None:
    out = DISCOVER_DIR / archive_id.lower()
    out.mkdir(parents=True, exist_ok=True)
    with client() as c:
        listing = c.list_archives(archive_id, {"size": 5, "page": 1})
        docs = listing.get("archives", [])
        meta = listing.get("_meta", {})
        print(f"{archive_id}: {meta.get('totalRecords')} postings listed")
        if not docs:
            return
        blob = c.download_archive(archive_id, docs[0]["docId"])
        # the oldest posting still listed tells how far back a backfill can reach
        last = c.list_archives(archive_id, {"size": 1, "page": meta.get("totalRecords", 1)})
    for m in iter_csv_members(blob):
        head = "".join(m.text.splitlines(keepends=True)[: HEAD_LINES + 1])
        (out / "head.csv").write_text(head)
        print(f"  newest {docs[0].get('postDatetime')}: {m.name} -> {out / 'head.csv'}")
    oldest = (last.get("archives") or [{}])[0].get("postDatetime")
    depth = {
        "postings": meta.get("totalRecords"),
        "newest": docs[0].get("postDatetime"),
        "oldest": oldest,
    }
    (out / "depth.json").write_text(json.dumps(depth, indent=2) + "\n")
    print(f"  oldest listed: {oldest} -> {out / 'depth.json'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", help="comma-separated product keys (default: all)")
    ap.add_argument("--from", dest="source", type=Path, help="re-trim a saved posting (zip or CSV)")
    ap.add_argument("--discover", metavar="ARCHIVE_ID", help="probe an unconfigured archive")
    args = ap.parse_args()
    if args.discover:
        discover(args.discover)
        return

    cfg = settings()
    keys = args.products.split(",") if args.products else list(cfg.products)
    if args.source and len(keys) != 1:
        ap.error("--from needs exactly one product in --products")
    for key in keys:
        p = cfg.product(key)
        if args.source is not None and args.source.suffix != ".zip":
            write_sample(p, args.source.read_text(encoding="utf-8-sig"))
            continue  # a bare CSV carries no posting time; the manifest keeps what it had
        if args.source is None:
            blob, entry = fetch_latest(p)
        else:
            blob, entry = (
                args.source.read_bytes(),
                {"archive_id": p.archive_id, "file": args.source.name},
            )
        member = _only_csv(key, blob)
        listed = datetime.fromisoformat(str(entry["listed_at"])) if "listed_at" in entry else None
        if member.posted_local is not None:
            posted = member.posted_at(listed) if listed else ct_to_utc(member.posted_local)
            entry["posted_at"] = posted.isoformat()
        write_sample(p, member.text)
        record(key, "archive", entry)


if __name__ == "__main__":
    main()
