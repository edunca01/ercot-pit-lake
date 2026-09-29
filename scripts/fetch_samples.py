"""Fetch a trimmed API sample per product into samples/api/<product>.json.

Pages through a recent delivery window until the intervals the trimming rule keeps are
complete, then keeps only those rows (see scripts/samples.py). The files are transform test
fixtures: re-running overwrites them, so review the diff before committing.

    uv run python -m scripts.fetch_samples [--products KEY,KEY]
    uv run python -m scripts.fetch_samples --products KEY --from saved_page.json
    uv run python -m scripts.fetch_samples --discover np6-345-cd --endpoint /np6-345-cd/x

``--discover`` is for a product that is not configured yet: it saves ERCOT's product metadata
and one untrimmed page under data/discover/ (gitignored), to write its declaration from.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ingest.config import Product, settings
from scripts.samples import (
    DISCOVER_DIR,
    RULE,
    SAMPLES_DIR,
    client,
    complete_intervals,
    record,
    select,
)

MAX_PAGES = 6  # a price report is ~1,000 rows per interval; the rule needs about three pages
PAGE_SIZE = 1000


def recent_window(p: Product) -> tuple[str, str]:
    """A window from yesterday, complete and already posted for every product.

    Delivery-date reports take whole days. SCED reports filter on the run timestamp, where a
    bare date is a zero-length window, so they get a quarter hour (three runs) at midday.
    """
    day: date = datetime.now(UTC).date() - timedelta(days=1)
    if p.date_params[0].startswith("SCEDTimestamp"):
        return f"{day}T12:00:00", f"{day}T12:15:00"
    return day.isoformat(), day.isoformat()


def trimmed(
    p: Product, fields: list[str], rows: list[list[Any]], meta: dict[str, Any]
) -> dict[str, Any]:
    kept = [rows[i] for i in select(p, "api", fields, rows)]
    return {
        "fields": [{"name": f} for f in fields],
        "data": kept,
        "_sample": {
            "query": meta.get("query", {}),
            "rows_fetched": len(rows),
            "rows_kept": len(kept),
            "rule": RULE,
        },
    }


def from_file(p: Product, path: Path) -> dict[str, Any]:
    body = json.loads(path.read_text())
    fields = [f["name"] for f in body.get("fields", [])]
    rows = body.get("data", [])
    meta = body.get("_meta") or body.get("_sample") or {}
    return trimmed(p, fields, rows, meta)


def fetch_product(p: Product) -> dict[str, Any]:
    d_from, d_to = recent_window(p)
    params: dict[str, Any] = {p.date_params[0]: d_from, p.date_params[1]: d_to}
    fields: list[str] = []
    rows: list[list[Any]] = []
    first_meta: dict[str, Any] = {}
    with client() as c:
        for page, body in enumerate(c.iter_pages(p.endpoint, {**params, "size": PAGE_SIZE}), 1):
            fields = [f["name"] for f in body.get("fields", [])]
            rows.extend(body.get("data", []))
            first_meta = first_meta or body.get("_meta", {})
            if complete_intervals(p, "api", fields, rows):
                break
            if page >= MAX_PAGES:
                msg = f"{p.key}: {MAX_PAGES} pages did not complete {RULE!r}"
                raise RuntimeError(msg)
    if not rows:
        # a zero-length or misnamed date filter returns nothing rather than an error
        msg = f"{p.key}: no rows for {params}; check date_params and the window"
        raise RuntimeError(msg)
    return trimmed(p, fields, rows, first_meta)


def discover(key: str, endpoint: str | None) -> None:
    out = DISCOVER_DIR / key
    out.mkdir(parents=True, exist_ok=True)
    with client() as c:
        meta = c.get(f"/{key}")
        (out / "product.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        print(f"{key}: product metadata -> {out / 'product.json'}")
        if endpoint:
            # no date filter: the latest rows, whatever this report calls its date parameters
            body = c.get(endpoint, {"size": 50})
            (out / "page.json").write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
            print(f"  fields: {[f.get('name') for f in body.get('fields', [])]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", help="comma-separated product keys (default: all)")
    ap.add_argument("--discover", metavar="KEY", help="probe a product that is not configured")
    ap.add_argument("--endpoint", help="with --discover: the report endpoint to page")
    ap.add_argument("--from", dest="source", type=Path, help="re-trim a saved API page (JSON)")
    args = ap.parse_args()
    if args.discover:
        discover(args.discover, args.endpoint)
        return

    cfg = settings()
    keys = args.products.split(",") if args.products else list(cfg.products)
    out_dir = SAMPLES_DIR / "api"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.source and len(keys) != 1:
        ap.error("--from needs exactly one product in --products")
    for key in keys:
        p = cfg.product(key)
        body = from_file(p, args.source) if args.source else fetch_product(p)
        path = out_dir / f"{key}.json"
        path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
        s = body["_sample"]
        record(key, "api", {"query": s["query"], "rows_kept": s["rows_kept"]})
        print(f"{key}: kept {s['rows_kept']} of {s['rows_fetched']} rows -> {path.name}")


if __name__ == "__main__":
    main()
