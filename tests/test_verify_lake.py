"""make verify on a real (offline) lake: clean as built, and each kind of damage is caught."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest

import scripts.check_endpoints as endpoints
import scripts.verify_lake as v
from ercot_lake.contract import curated_key, merged_key
from ingest.cli import run_products
from ingest.config import LakeConfig, Settings, StateConfig
from ingest.lake import Lake


@pytest.fixture
def built(tmp_path: Path, cfg: Settings) -> tuple[Settings, Lake]:
    """The committed samples ingested into a fresh lake, as `make ingest OFFLINE=1` does."""
    s = cfg.model_copy(
        update={
            "lake": LakeConfig(root=str(tmp_path / "lake")),
            "state": StateConfig(
                backend="local", local_dir=str(tmp_path / "state"), dynamodb_table="t"
            ),
        }
    )
    assert all(
        r.status == "ok"
        for r in run_products(s, "all", explicit=None, backfill=False, source="samples")
    )
    return s, Lake(s.lake)


def findings(s: Settings, lake: Lake, key: str | None = None) -> list[v.Finding]:
    products = [s.product(key)] if key else list(s.products.values())
    return v.verify(lake.root, products)[0]


def one_file(lake: Lake, key: str) -> str:
    (f,) = lake.list_keys(f"curated/{key}/")
    return f


def test_a_freshly_built_lake_is_clean(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    found, counted = v.verify(lake.root, list(s.products.values()))
    assert found == []
    assert set(counted) == set(s.products)
    assert all(n == 1 for n in counted.values())


def test_a_repeated_key_within_one_posting(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np6-905-cd")
    t = lake.read_table(f)
    day = date.fromisoformat(f.split("date=")[1][:10])
    posted = t.column("posted_at")[0].as_py()
    lake.write_table(merged_key("np6-905-cd", day, posted + timedelta(hours=1)), t.slice(0, 1))
    (found,) = findings(s, lake, "np6-905-cd")
    assert (found.check, "repeated within one posting") == ("keys", found.detail[:27])


def test_a_file_off_the_contract(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np6-331-cd")
    lake.write_table(f, lake.read_table(f).append_column("extra", pa.array([1] * 5)))
    (found,) = findings(s, lake, "np6-331-cd")
    assert found.check == "files"
    assert "extra" in found.detail


def test_a_null_in_a_required_column(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np6-331-cd")
    t = lake.read_table(f)
    loose = pa.schema([x.with_nullable(True) for x in t.schema])
    i = t.schema.get_field_index("mcpc_mw")
    nulls = pa.array([None] * t.num_rows, pa.float64())
    lake.write_table(f, t.cast(loose).set_column(i, loose.field(i), nulls))
    (found,) = findings(s, lake, "np6-331-cd")
    assert (found.check, found.detail.endswith("nulls in required columns ['mcpc_mw']")) == (
        "files",
        True,
    )


def test_rows_at_an_unknown_schema_version(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np6-331-cd")
    t = lake.read_table(f)
    i = t.schema.get_field_index("schema_version")
    lake.write_table(f, t.set_column(i, t.schema.field(i), pa.array([2] * t.num_rows, pa.int32())))
    (found,) = findings(s, lake, "np6-331-cd")
    assert (found.check, found.detail.endswith("schema_version [2]")) == ("files", True)


def test_a_missing_delivery_day(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np4-190-cd")
    day = date.fromisoformat(f.split("date=")[1][:10])
    t = lake.read_table(f)
    later = day + timedelta(days=3)
    shifted = t.set_column(0, "interval_start", pc.add(t.column(0), pa.scalar(timedelta(days=3))))
    lake.write_table(curated_key("np4-190-cd", later, t.column("posted_at")[0].as_py()), shifted)
    (found,) = findings(s, lake, "np4-190-cd")
    assert found.check == "calendar"
    assert found.detail.startswith("2 missing delivery days")


def test_days_before_collected_from_are_not_gaps(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np6-345-cd")
    day = date.fromisoformat(f.split("date=")[1][:10])
    t = lake.read_table(f)
    early = curated_key("np6-345-cd", day - timedelta(days=10), t.column("posted_at")[0].as_py())
    shifted = t.set_column(0, "interval_start", pc.add(t.column(0), pa.scalar(timedelta(days=-10))))
    lake.write_table(early, shifted)  # stray old data, before collection started
    p = s.product("np6-345-cd")
    from datetime import UTC, datetime

    started = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=5)
    s2 = s.model_copy(
        update={"products": {**s.products, p.key: p.model_copy(update={"collected_from": started})}}
    )
    assert findings(s2, lake, "np6-345-cd") == []
    assert findings(s, lake, "np6-345-cd")[0].check == "calendar"


@pytest.mark.parametrize("key", ["np6-345-cd", "np3-565-cd"])
def test_load_zones_that_do_not_add_up(built: tuple[Settings, Lake], key: str) -> None:
    s, lake = built
    f = one_file(lake, key)
    t = lake.read_table(f)
    total = pc.ends_with(t.column("series"), ":SystemTotal")
    bumped = pc.if_else(total, pc.add(t.column("value"), 500.0), t.column("value"))
    i = t.schema.get_field_index("value")
    lake.write_table(f, t.set_column(i, "value", bumped))
    found = findings(s, lake, key)
    assert found
    assert {x.check for x in found} == {"zones"}


def test_a_missing_zone_is_caught_too(built: tuple[Settings, Lake]) -> None:
    s, lake = built
    f = one_file(lake, "np6-345-cd")
    t = lake.read_table(f)
    lake.write_table(f, t.filter(pc.not_equal(t.column("series"), "load_act:Coast")))
    assert {x.check for x in findings(s, lake, "np6-345-cd")} == {"zones"}


def test_main_exit_code_and_output(
    built: tuple[Settings, Lake],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s, lake = built
    monkeypatch.setattr(v, "settings", lambda: s)
    assert v.main([]) == 0
    assert capsys.readouterr().out.rstrip().endswith("OK: no problems")
    f = one_file(lake, "np6-331-cd")
    lake.write_table(f, lake.read_table(f).append_column("extra", pa.array([1] * 5)))
    assert v.main(["--product", "np6-331-cd"]) == 1
    assert "1 problems" in capsys.readouterr().out


# -- check_endpoints -------------------------------------------------------------------------


class FakeClient:
    def __init__(self, statuses: dict[str, int]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, Any]] = []

    def probe(self, path: str, params: Any = None) -> int:
        self.calls.append((path, params))
        return self.statuses.get(path, 200)


def test_check_endpoints_probes_endpoint_archive_and_fallback(cfg: Settings) -> None:
    base = cfg.ercot.archive_base
    c = FakeClient({f"{base}/NP6-796-ER": 404})
    failed = endpoints.check(c, [cfg.product("np6-331-cd"), cfg.product("np4-190-cd")], base)  # type: ignore[arg-type]
    assert [p for p, _ in c.calls] == [
        "/np6-331-cd/rt_clear_price_cap",
        f"{base}/NP6-331-CD",
        f"{base}/NP6-796-ER",
        "/np4-190-cd/dam_stlmnt_pnt_prices",
        f"{base}/NP4-190-CD",
    ]
    assert len(failed) == 1
    assert "fallback" in failed[0]
    assert "404" in failed[0]
