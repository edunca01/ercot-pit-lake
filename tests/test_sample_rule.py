"""The sample trimming rule: what a fixture keeps decides what the transform tests can see."""

from __future__ import annotations

import pytest

from ingest.config import Settings
from scripts.samples import RESOURCE_NODES, complete_intervals, select, trim_csv

RT_HEADER = (
    "DeliveryDate,DeliveryHour,DeliveryInterval,SettlementPointName,SettlementPointType,"
    "SettlementPointPrice,DSTFlag"
)


def _rt_csv(intervals: int, nodes: int) -> str:
    lines = [RT_HEADER]
    for i in range(1, intervals + 1):
        lines += [f"09/04/2026,12,{i},NODE_{n},RN,20.0,N" for n in range(nodes)]
        lines += [
            f"09/04/2026,12,{i},HB_NORTH,HU,22.0,N",
            f"09/04/2026,12,{i},LZ_HOUSTON,LZ,23.0,N",
            f'"09/04/2026",12,{i},LZ_HOUSTON,LZEW,23.5,N',  # kept byte for byte, quotes and all
        ]
    return "\r\n".join(lines) + "\r\n"


def test_price_samples_keep_every_hub_and_zone_and_a_few_nodes(cfg: Settings) -> None:
    out = trim_csv(cfg.product("np6-905-cd"), _rt_csv(intervals=3, nodes=12))
    lines = out.splitlines()
    assert lines[0] == RT_HEADER
    data = lines[1:]
    assert len(data) == 2 * (RESOURCE_NODES + 3)  # two intervals kept, the third dropped
    assert sum("HB_NORTH" in ln for ln in data) == 2
    assert sum(",LZEW," in ln for ln in data) == 2
    assert '"09/04/2026",12,1,LZ_HOUSTON,LZEW,23.5,N' in data
    assert out.endswith("\r\n")


def test_other_tables_keep_every_row_of_two_intervals(cfg: Settings) -> None:
    header = [
        "DeliveryDate",
        "DeliveryHour",
        "DeliveryInterval",
        "RepeatedHourFlag",
        "ASType",
        "MCPC",
    ]
    rows = [
        ["09/04/2026", "12", str(i), "N", t, "1"]
        for i in (1, 2, 3)
        for t in ("REGUP", "REGDN", "RRS", "ECRS", "NSPIN")
    ]
    kept = select(cfg.product("np6-331-cd"), "archive", header, rows)
    assert len(kept) == 10
    assert complete_intervals(cfg.product("np6-331-cd"), "archive", header, rows)
    assert not complete_intervals(cfg.product("np6-331-cd"), "archive", header, rows[:10])


def test_the_repeated_hour_is_its_own_interval(cfg: Settings) -> None:
    header = [
        "DeliveryDate",
        "DeliveryHour",
        "DeliveryInterval",
        "RepeatedHourFlag",
        "ASType",
        "MCPC",
    ]
    rows = [["11/01/2026", "2", "1", flag, "REGUP", "1"] for flag in ("N", "Y")]
    assert select(cfg.product("np6-331-cd"), "archive", header, rows) == [0, 1]
    assert not complete_intervals(cfg.product("np6-331-cd"), "archive", header, rows)


def test_multi_line_cells_are_refused(cfg: Settings) -> None:
    text = RT_HEADER + '\n09/04/2026,12,1,"HB_\nNORTH",HU,22.0,N\n'
    with pytest.raises(ValueError, match="multi-line cells"):
        trim_csv(cfg.product("np6-905-cd"), text)
