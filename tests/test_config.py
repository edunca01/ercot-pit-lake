"""config.yaml is what every other layer depends on; validate it, not just parse it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import ingest.config as m
from ingest.config import Product, Settings, load_settings

BUILT_IN = {
    "np4-190-cd",
    "np4-188-cd",
    "np6-905-cd",
    "np6-331-cd",
    "np3-565-cd",
    "np4-732-cd",
    "np4-737-cd",
    "np6-788-cd",
    "np6-322-cd",
}
PRODUCT_KEY = re.compile(r"^np\d-\d{3}-[a-z]{2}$")


def test_built_in_products_present(cfg: Settings) -> None:
    assert set(cfg.products) == BUILT_IN


def test_product_keys_are_ercot_ids(cfg: Settings) -> None:
    for key, p in cfg.products.items():
        assert PRODUCT_KEY.match(key), key
        assert p.key == key
        assert p.archive_id == key.upper()


def test_endpoints_belong_to_their_product(cfg: Settings) -> None:
    for p in cfg.products.values():
        assert p.endpoint.startswith(f"/{p.key}/")


def test_interval_matches_cadence(cfg: Settings) -> None:
    expected = {"daily": 60, "hourly": 60, "15min": 15, "5min": 5}
    for p in cfg.products.values():
        assert p.interval_minutes == expected[p.cadence], p.key


def test_schedules_look_like_eventbridge_cron(cfg: Settings) -> None:
    for p in cfg.products.values():
        assert re.match(r"^cron\((\S+\s+){5}\S+\)$", p.schedule), p.key


def test_rt_as_product_has_its_workbook_archive(cfg: Settings) -> None:
    p = cfg.product("np6-331-cd")
    assert p.endpoint == "/np6-331-cd/rt_clear_price_cap"
    assert p.fallback_archive_id == "NP6-796-ER"


def test_collected_from_is_utc_aware(cfg: Settings) -> None:
    for p in cfg.products.values():
        if p.collected_from is not None:
            assert p.collected_from.utcoffset() is not None, p.key


def test_every_product_has_a_stale_threshold_past_its_cadence(cfg: Settings) -> None:
    every = {"5min": 5, "15min": 15, "hourly": 60, "daily": 1440}
    for p in cfg.products.values():
        assert p.stale_after_min > every[p.cadence], p.key
    # a scheduled run has to give up before the next one starts
    assert cfg.ercot.live_max_retries < cfg.ercot.max_retries


def test_unknown_product_raises(cfg: Settings) -> None:
    with pytest.raises(KeyError, match="unknown product"):
        cfg.product("np0-000-xx")


def test_unknown_config_keys_are_rejected(tmp_path: Path) -> None:
    text = m.DEFAULT_CONFIG_PATH.read_text() + "\nlegacy_section: {}\n"
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ValidationError, match="legacy_section"):
        load_settings(path)


def _product(**changes: object) -> Product:
    fields: dict[str, object] = {
        "key": "np6-331-cd",
        "name": "x",
        "archive_id": "NP6-331-CD",
        "endpoint": "/np6-331-cd/rt_clear_price_cap",
        "table": "mcpc",
        "cadence": "15min",
        "interval_minutes": 15,
        "post_time_ct": "x",
        "schedule": "cron(* * * * ? *)",
        "date_params": ("a", "b"),
        "initial_lookback_hours": 6,
        "stale_after_min": 60,
    }
    return Product.model_validate({**fields, **changes})


def test_every_product_needs_an_endpoint() -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        _product(endpoint=None)


def test_the_retired_enabled_flag_is_rejected() -> None:
    # Every configured product is collected; a leftover selection flag must not pass silently.
    with pytest.raises(ValidationError, match="enabled"):
        _product(enabled=False)


def test_table_must_be_a_contract_table() -> None:
    with pytest.raises(ValidationError, match="table"):
        _product(table="prices")


def test_naive_collected_from_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _product(collected_from="2026-09-17T11:00:00")


# -- env overrides -----------------------------------------------------------------------


def test_lake_root_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKE_ROOT", "s3://example-ercot-lake")
    s = load_settings()
    assert s.lake.root == "s3://example-ercot-lake"
    assert s.lake.is_s3


def test_state_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATE_BACKEND", "dynamodb")
    monkeypatch.setenv("STATE_DIR", "/tmp/state")
    monkeypatch.setenv("STATE_TABLE", "ercot-ingest-state-test")
    s = load_settings()
    assert s.state.backend == "dynamodb"
    assert s.state.local_dir == "/tmp/state"
    assert s.state.dynamodb_table == "ercot-ingest-state-test"


def test_config_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "other.yaml"
    path.write_text(m.DEFAULT_CONFIG_PATH.read_text().replace("root: ./data", "root: ./elsewhere"))
    monkeypatch.setenv("CONFIG_PATH", str(path))
    assert load_settings().lake.root == "./elsewhere"


def test_settings_is_cached() -> None:
    m.settings.cache_clear()
    assert m.settings() is m.settings()
    m.settings.cache_clear()


# -- credentials -------------------------------------------------------------------------


def test_credentials_prefer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERCOT_USERNAME", "u")
    monkeypatch.setenv("ERCOT_PASSWORD", "p")
    monkeypatch.setenv("ERCOT_SUBSCRIPTION_KEY", "k")
    monkeypatch.setenv("ERCOT_SECRET_ID", "ercot/unused")
    c = m.load_credentials()
    assert (c.username, c.password.get_secret_value(), c.subscription_key.get_secret_value()) == (
        "u",
        "p",
        "k",
    )


def test_credentials_fall_back_to_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERCOT_SECRET_ID", "ercot/test")
    seen: list[str] = []

    def fake_secret(secret_id: str) -> m.Credentials:
        seen.append(secret_id)
        return m.credentials_from_json(
            '{"username": "su", "password": "sp", "subscription_key": "sk"}', source=secret_id
        )

    monkeypatch.setattr(m, "_credentials_from_secret", fake_secret)
    c = m.load_credentials()
    assert seen == ["ercot/test"]
    assert c.username == "su"
    assert c.password.get_secret_value() == "sp"


def test_credentials_missing_everywhere() -> None:
    with pytest.raises(RuntimeError, match="ERCOT_SECRET_ID"):
        m.load_credentials()


def test_secrets_never_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERCOT_USERNAME", "u")
    monkeypatch.setenv("ERCOT_PASSWORD", "hunter2-password")
    monkeypatch.setenv("ERCOT_SUBSCRIPTION_KEY", "sub-key-123")
    c = m.load_credentials()
    for text in (repr(c), str(c), c.model_dump_json()):
        assert "hunter2-password" not in text
        assert "sub-key-123" not in text


@pytest.mark.parametrize(
    ("text", "match"),
    [("not json", "not JSON"), ('{"username": "u"}', "missing keys: password, subscription_key")],
)
def test_secret_value_is_validated(text: str, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        m.credentials_from_json(text, source="ercot/test")


# -- transform declarations ----------------------------------------------------------------

_SPP_DECL: dict[str, object] = {
    "time": "hour_interval",
    "columns": {
        "delivery_date": ["deliveryDate", "DeliveryDate"],
        "delivery_hour": ["deliveryHour", "DeliveryHour"],
        "delivery_interval": ["deliveryInterval", "DeliveryInterval"],
        "settlement_point": ["settlementPoint", "SettlementPointName"],
        "price_mwh": ["settlementPointPrice", "SettlementPointPrice"],
    },
}


def _decl(**changes: object) -> dict[str, object]:
    return {**_SPP_DECL, **changes}


def _cols(**changes: object) -> dict[str, object]:
    return {**_SPP_DECL["columns"], **changes}  # type: ignore[dict-item]


def test_a_valid_declaration() -> None:
    p = _product(table="spp", transform=_decl())
    assert p.transform is not None
    assert p.transform.columns["settlement_point"] == ("settlementPoint", "SettlementPointName")


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"time": "sced_timestamp"}, "needs columns \\['sced_timestamp'\\]"),
        ({"columns": _cols(delivery_hour=[None, None])}, "in neither format"),
        ({"columns": _cols(price_mwh=["settlementPoint", "Price"])}, "declared twice"),
        ({"keep_flag": "in_use"}, "undeclared columns: \\['in_use'\\]"),
        ({"series": {"price_mwh": "x:y"}}, "only for them"),
        ({"columns": {k: v for k, v in _cols().items() if k != "price_mwh"}}, "needs columns"),
    ],
)
def test_bad_declarations_are_rejected(changes: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _product(table="spp", transform=_decl(**changes))


def test_series_tables_need_series_and_prefixes_are_spp_only() -> None:
    with pytest.raises(ValidationError, match="required for series tables"):
        _product(table="series", transform=_decl())
    with pytest.raises(ValidationError, match="spp tables only"):
        _product(
            table="series",
            transform=_decl(series={"price_mwh": "x:price"}, keep_point_prefixes=["HB_"]),
        )
