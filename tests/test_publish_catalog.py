"""manifests/_catalog.json, and a product added by configuration alone, end to end: declared in
YAML, ingested from a sample posting, published in the catalog, read back as of a moment."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from ercot_lake import LakeReader
from ercot_lake.catalog import Catalog
from ercot_lake.contract import CATALOG_KEY, CONTRACT_VERSION, manifest_key
from ercot_lake.timeutil import utc_to_ct
from ingest.catalog import publish_catalog
from ingest.config import DEFAULT_CONFIG_PATH, REPO_ROOT, LakeConfig, Settings, load_settings
from ingest.lake import Lake
from ingest.run import Doc, Run, Window, run_product
from ingest.state import LocalStateStore

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(LakeConfig(root=str(tmp_path / "lake")))


def test_every_configured_product_is_live(cfg: Settings, lake: Lake) -> None:
    cat = publish_catalog(cfg, lake, now=NOW)
    assert set(cat.products) == set(cfg.products)
    assert all(p.live for p in cat.products.values())
    assert cat.contract_version == CONTRACT_VERSION
    stored = Catalog.parse(lake.read_bytes(CATALOG_KEY))
    assert stored == cat
    for key, p in cfg.products.items():
        entry = cat.products[key]
        assert (entry.table, entry.interval_minutes, entry.collected_from) == (
            p.table,
            p.interval_minutes,
            p.collected_from,
        )


def test_the_catalog_is_rewritten_only_when_it_changes(cfg: Settings, lake: Lake) -> None:
    publish_catalog(cfg, lake, now=NOW)
    again = publish_catalog(cfg, lake, now=NOW + timedelta(hours=1))
    assert again.generated_at == NOW  # unchanged content, unchanged file


def test_a_retired_product_with_data_stays_listed_but_not_live(cfg: Settings, lake: Lake) -> None:
    publish_catalog(cfg, lake, now=NOW)
    lake.write_json(manifest_key("np6-322-cd"), {"product": "np6-322-cd"})  # it has data
    retired = cfg.model_copy(
        update={
            "products": {
                k: v for k, v in cfg.products.items() if k not in ("np6-322-cd", "np6-788-cd")
            }
        }
    )
    cat = publish_catalog(retired, lake, now=NOW + timedelta(days=1))
    assert cat.products["np6-322-cd"].live is False
    assert cat.products["np6-322-cd"].table == "series"
    assert "np6-788-cd" not in cat.products  # retired and never collected: not in this lake
    assert {k for k, p in cat.products.items() if p.live} == set(retired.products)


def test_data_without_config_or_history_is_reported_not_listed(
    cfg: Settings, lake: Lake, caplog: pytest.LogCaptureFixture
) -> None:
    lake.write_json(manifest_key("np9-999-cd"), {"product": "np9-999-cd"})
    cat = publish_catalog(cfg, lake, now=NOW)
    assert "np9-999-cd" not in cat.products
    assert "np9-999-cd has data but no config entry" in caplog.text


# -- a new product, by configuration only ----------------------------------------------------


def test_a_product_added_by_yaml_and_a_sample_alone_is_ingested_and_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Python: a new config entry (here NP6-345-CD's shape under a new key) and a posting
    are enough to reach the curated table and the point-in-time reader."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    entry = dict(raw["products"]["np6-345-cd"], name="A report added by configuration")
    entry["archive_id"], entry["endpoint"] = "NP9-001-CD", "/np9-001-cd/x"
    raw["products"] = {"np9-001-cd": entry}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("CONFIG_PATH", str(path))
    settings = load_settings()
    product = settings.product("np9-001-cd")

    sample = (REPO_ROOT / "samples" / "archive" / "np6-345-cd.csv").read_text()
    posted = datetime(2026, 9, 29, 10, 50, tzinfo=UTC)  # 05:50 CDT, the day after
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"cdr.1.0.{utc_to_ct(posted):%Y%m%d.%H%M%S}.LOAD.csv", sample)
    blob = buf.getvalue()

    lake = Lake(LakeConfig(root=str(tmp_path / "lake")))
    summary = run_product(
        Run(
            product=product,
            lake=lake,
            state=LocalStateStore(tmp_path / "state"),
            docs=[Doc(posted_at=posted, name="load", load=lambda: blob)],
            window=Window(posted - timedelta(hours=1), posted),
        )
    )
    assert summary.rows_written > 0
    publish_catalog(settings, lake, now=NOW)

    with LakeReader(lake.root) as reader:
        assert reader.products() == ["np9-001-cd"]
        assert summary.latest_interval_start is not None
        delivery = utc_to_ct(summary.latest_interval_start).date()
        before = reader.series_by_date("np9-001-cd", delivery, as_of=posted - timedelta(seconds=1))
        after = reader.series_by_date("np9-001-cd", delivery, as_of=posted)
    assert before.num_rows == 0  # not known before it was posted
    assert after.num_rows > 0
    assert set(after.column("series").to_pylist()) == set(product.transform.series.values())  # type: ignore[union-attr]
