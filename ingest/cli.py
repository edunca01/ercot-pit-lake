"""Entry points ``ingest`` and ``backfill``: the same loop; backfill has an explicit window and
leaves the watermark alone.

``run_products`` is the one function both the CLI and the Lambda handler call.

    uv run ingest --product np6-905-cd              # live, from the watermark (needs ERCOT_*)
    uv run ingest --product all --offline           # the committed samples, no network
    uv run backfill --product np4-190-cd --from 2026-09-01 --to 2026-09-02 --source bundles
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import zipfile
from datetime import UTC, date, datetime, timedelta

from ercot_lake.timeutil import CT, now_utc, utc_to_ct
from ingest.catalog import publish_catalog
from ingest.config import REPO_ROOT, Product, Settings, load_credentials, settings
from ingest.ercot_api import ErcotClient
from ingest.lake import Lake
from ingest.run import (
    Doc,
    Run,
    RunSummary,
    Window,
    archive_docs,
    bundle_docs,
    hist_docs,
    resolve_window,
    run_product,
)
from ingest.state import make_state_store

log = logging.getLogger(__name__)

SOURCES = {"archive": archive_docs, "bundles": bundle_docs, "hist": hist_docs}
SAMPLES = REPO_ROOT / "samples"


def parse_when(text: str) -> datetime:
    """ISO date or datetime; naive values are Central Prevailing Time, as ERCOT publishes."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CT)
    return dt.astimezone(UTC)


def sample_docs(product: Product) -> tuple[list[Doc], Window]:
    """The committed archive sample as one posting, with the posting time it was fetched at,
    so the whole pipeline runs offline on real ERCOT data."""
    manifest = json.loads((SAMPLES / "manifest.json").read_text())
    posted = datetime.fromisoformat(manifest[product.key]["archive"]["posted_at"])
    csv = (SAMPLES / "archive" / f"{product.key}.csv").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"cdr.0.0.{utc_to_ct(posted):%Y%m%d.%H%M%S}.{product.archive_id}.csv", csv)
    blob = buf.getvalue()
    doc = Doc(posted_at=posted, name=f"sample {product.key}", load=lambda: blob)
    return [doc], Window(posted - timedelta(seconds=1), posted)


def configure_logging(level: str) -> None:
    """Works both on a laptop (no handlers yet) and in Lambda (runtime already installed one)."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
    else:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
        logging.basicConfig(level=level, format=fmt)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_products(  # noqa: PLR0913  (a CLI/handler facade; the knobs are the CLI's)
    cfg: Settings,
    product_key: str,
    *,
    explicit: Window | None,
    backfill: bool,
    source: str = "archive",
    delivery_range: tuple[date, date] | None = None,
) -> list[RunSummary]:
    """Run every requested product; a failure is recorded in its summary, not raised, so one
    bad product never blocks the others in the same invocation."""
    products = list(cfg.products.values()) if product_key == "all" else [cfg.product(product_key)]
    lake = Lake(cfg.lake)
    state = make_state_store(cfg.state)
    # Once per invocation: cheap when nothing changed, and a new image or config shows up in
    # the catalog on its first run.
    publish_catalog(cfg, lake)
    if source == "samples":
        runs = []
        for p in products:
            docs, window = sample_docs(p)
            runs.append(Run(p, lake, state, docs, window, False, delivery_range))
        return [_safe(r) for r in runs]
    # A scheduled run that cannot list within a minute only overlaps the next one.
    live = explicit is None and not backfill
    summaries = []
    with ErcotClient(cfg.ercot, load_credentials(), live=live) as client:
        for p in products:
            window = resolve_window(p, state.get(p.key), now=now_utc(), explicit=explicit)
            live_docs = SOURCES[source](client, p, window)
            summaries.append(
                _safe(Run(p, lake, state, live_docs, window, not backfill, delivery_range))
            )
    return summaries


def _safe(run: Run) -> RunSummary:
    """Run one product, turning a failure into an error summary (see run_products)."""
    started = now_utc()
    try:
        return run_product(run)
    except Exception as exc:
        return RunSummary(
            product=run.product.key,
            started_at=started,
            window=run.window,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
        )


def _parser(prog: str, *, backfill: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n\n")[0])
    ap.add_argument("--product", required=True, help="product key from config.yaml, or 'all'")
    ap.add_argument(
        "--from",
        dest="post_from",
        type=parse_when,
        required=backfill,
        help="posting-time lower bound (exclusive), CT if no offset",
    )
    ap.add_argument(
        "--to", dest="post_to", type=parse_when, help="posting-time upper bound; default now"
    )
    if backfill:
        ap.add_argument(
            "--source",
            choices=list(SOURCES),
            default="archive",
            help="archive: one listing per posting (recent data); bundles: ERCOT's monthly "
            "zips, falling back to the archive for months without one (history); hist: the "
            "product's fallback workbook, usually with --delivery-from/--delivery-to",
        )
        ap.add_argument("--delivery-from", type=date.fromisoformat, help="keep delivery days >=")
        ap.add_argument("--delivery-to", type=date.fromisoformat, help="keep delivery days <=")
    else:
        ap.add_argument(
            "--offline",
            action="store_true",
            help="ingest the committed samples instead of ERCOT (no credentials, no network)",
        )
    ap.add_argument("--log-level", default="INFO")
    return ap


def _main(argv: list[str] | None, *, backfill: bool) -> int:
    args = _parser("backfill" if backfill else "ingest", backfill=backfill).parse_args(argv)
    configure_logging(args.log_level)
    explicit = None
    if args.post_from or args.post_to:
        explicit = Window(args.post_from or now_utc(), args.post_to or now_utc())
    d_from, d_to = getattr(args, "delivery_from", None), getattr(args, "delivery_to", None)
    delivery_range = (d_from or date.min, d_to or date.max) if (d_from or d_to) else None
    source = "samples" if getattr(args, "offline", False) else getattr(args, "source", "archive")
    summaries = run_products(
        settings(),
        args.product,
        explicit=explicit,
        backfill=backfill,
        source=source,
        delivery_range=delivery_range,
    )
    for summary in summaries:
        print(json.dumps(summary.as_dict()))
    return 1 if any(s.status == "error" for s in summaries) else 0


def main_ingest(argv: list[str] | None = None) -> int:
    return _main(argv, backfill=False)


def main_backfill(argv: list[str] | None = None) -> int:
    return _main(argv, backfill=True)


if __name__ == "__main__":
    sys.exit(main_ingest())
