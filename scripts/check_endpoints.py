"""Probe every configured product on the ERCOT Public API: its report endpoint, its archive,
and its fallback archive if it has one. Read-only; needs ERCOT_* credentials.

    uv run python -m scripts.check_endpoints [--products KEY,KEY]

Run it after adding a product, or when a product goes stale for no visible reason: ERCOT
retires and renames endpoints, and a 404 here says so directly.
"""

from __future__ import annotations

import argparse
import sys

from ingest.config import Product, settings
from ingest.ercot_api import ErcotClient
from scripts.samples import client


def probes(p: Product, archive_base: str) -> list[tuple[str, str]]:
    """(label, path or URL) for each thing to check."""
    out = [("endpoint", p.endpoint), ("archive", f"{archive_base}/{p.archive_id}")]
    if p.fallback_archive_id:
        out.append(("fallback", f"{archive_base}/{p.fallback_archive_id}"))
    return out


def check(c: ErcotClient, products: list[Product], archive_base: str) -> list[str]:
    """Print one line per probe; return the failures."""
    failures = []
    for p in products:
        for label, target in probes(p, archive_base):
            status = c.probe(target, {"size": 1})
            line = f"{p.key:11} {label:9} {status}  {target}"
            print(line)
            if status != 200:
                failures.append(line)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check_endpoints", description=__doc__.split("\n\n")[0])
    ap.add_argument("--products", help="comma-separated product keys (default: all)")
    args = ap.parse_args(argv)
    cfg = settings()
    keys = args.products.split(",") if args.products else list(cfg.products)
    with client() as c:
        failures = check(c, [cfg.product(k) for k in keys], cfg.ercot.archive_base)
    print(f"{len(failures)} failed" if failures else "OK: every endpoint answered 200")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
