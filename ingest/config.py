"""Typed access to ``config.yaml`` and environment.

Nothing else in the package reads YAML or ``os.environ`` directly; this module is the single
place where product IDs, endpoints, the lake root and credentials are resolved. The lake
layout itself is fixed by ``ercot_lake.contract`` and is not configurable.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, model_validator

from ercot_lake.contract import Table

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthConfig(_Strict):
    token_url: str
    client_id: str
    scope: str


class ErcotConfig(_Strict):
    api_base: str
    archive_base: str
    auth: AuthConfig
    page_size: int = Field(gt=0, le=1000)
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    # Scheduled runs give up well inside the polling interval; the next run is the retry.
    live_max_retries: int = Field(default=3, ge=0)
    min_interval_s: float = Field(default=0.0, ge=0)


class LakeConfig(_Strict):
    root: str

    @property
    def is_s3(self) -> bool:
        return self.root.startswith("s3://")


class StateConfig(_Strict):
    backend: Literal["local", "dynamodb"]
    local_dir: str
    dynamodb_table: str


TimeStyle = Literal["hour_ending", "hour_interval", "sced_timestamp"]

# Canonical columns a declaration must provide, by how ERCOT expresses time and by table.
_TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "hour_ending": ("delivery_date", "hour_ending"),
    "hour_interval": ("delivery_date", "delivery_hour", "delivery_interval"),
    "sced_timestamp": ("sced_timestamp",),
}
_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "spp": ("settlement_point", "price_mwh"),
    "mcpc": ("as_type", "mcpc_mw"),
    "series": (),  # the value columns are named in `series`
}


class TransformDecl(_Strict):
    """How to read one ERCOT report, declared instead of coded.

    ``columns`` maps each canonical name to its ``[API field, archive CSV header]``; ``null``
    where a format lacks the column. The lists are exhaustive: a source column that is not
    declared, or a declared one that is missing, is schema drift.
    """

    time: TimeStyle
    columns: dict[str, tuple[str | None, str | None]]
    # series tables: canonical value column -> published series name (`<report>:<column>`)
    series: dict[str, str] | None = None
    # keep only rows where this canonical Y/N column is Y (e.g. the load report's in-use model)
    keep_flag: str | None = None
    # spp tables: keep only settlement points with these prefixes (raw keeps everything)
    keep_point_prefixes: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TransformDecl:
        missing = [c for c in _TIME_COLUMNS[self.time] if c not in self.columns]
        if missing:
            msg = f"time style {self.time} needs columns {missing}"
            raise ValueError(msg)
        for canonical, (api, csv) in self.columns.items():
            if api is None and csv is None:
                msg = f"column {canonical!r} is in neither format"
                raise ValueError(msg)
        for fmt in (0, 1):
            names = [n for pair in self.columns.values() if (n := pair[fmt]) is not None]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                msg = f"source columns declared twice: {dupes}"
                raise ValueError(msg)
        referenced = [*(self.series or {}), *([self.keep_flag] if self.keep_flag else [])]
        unknown = [c for c in referenced if c not in self.columns]
        if unknown:
            msg = f"series/keep_flag name undeclared columns: {unknown}"
            raise ValueError(msg)
        return self


class Product(_Strict):
    key: str
    name: str
    archive_id: str
    endpoint: str
    # A second ERCOT archive with the same prices, read by backfill only (never polled).
    fallback_archive_id: str | None = None
    table: Table
    cadence: Literal["daily", "hourly", "15min", "5min"]
    interval_minutes: int
    post_time_ct: str
    schedule: str
    date_params: tuple[str, str]
    initial_lookback_hours: int = Field(gt=0)
    # Newest posting older than this (minutes) = stale: counted into StaleProducts, which alarms.
    stale_after_min: int = Field(gt=0)
    # First interval (posting hour for hourly reports) the lake is expected to hold; earlier
    # ones are not gaps. None: history is backfilled, every interval counts.
    collected_from: AwareDatetime | None = None
    transform: TransformDecl | None = None

    @model_validator(mode="after")
    def _transform_fits_table(self) -> Product:
        t = self.transform
        if t is None:
            return self
        missing = [c for c in _TABLE_COLUMNS[self.table] if c not in t.columns]
        if missing:
            msg = f"{self.key}: a {self.table} transform needs columns {missing}"
            raise ValueError(msg)
        if (self.table == "series") != bool(t.series):
            msg = f"{self.key}: `series` is required for series tables and only for them"
            raise ValueError(msg)
        if t.keep_point_prefixes is not None and self.table != "spp":
            msg = f"{self.key}: keep_point_prefixes applies to spp tables only"
            raise ValueError(msg)
        return self


class Settings(_Strict):
    ercot: ErcotConfig
    lake: LakeConfig
    state: StateConfig
    products: dict[str, Product]

    def product(self, key: str) -> Product:
        try:
            return self.products[key]
        except KeyError:
            known = ", ".join(sorted(self.products))
            msg = f"unknown product {key!r}; known: {known}"
            raise KeyError(msg) from None


class Credentials(_Strict):
    username: str
    password: SecretStr
    subscription_key: SecretStr


def load_settings(path: Path | None = None) -> Settings:
    """Parse config.yaml and apply env overrides.

    These are the only knobs that differ between a laptop and a deployment; everything else
    comes from ``config.yaml`` in the image:

    - ``CONFIG_PATH``: another config file
    - ``LAKE_ROOT``: ``lake.root``
    - ``STATE_BACKEND``, ``STATE_DIR``, ``STATE_TABLE``: the ``state`` fields
    """
    load_dotenv(REPO_ROOT / ".env")
    cfg_path = path or Path(os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    with cfg_path.open() as fh:
        raw = yaml.safe_load(fh)

    products = {k: Product(key=k, **v) for k, v in raw.pop("products").items()}
    lake_raw = dict(raw.pop("lake"))
    if lake_root := os.environ.get("LAKE_ROOT"):
        lake_raw["root"] = lake_root
    state_raw = dict(raw.pop("state"))
    if state_backend := os.environ.get("STATE_BACKEND"):
        state_raw["backend"] = state_backend
    if state_dir := os.environ.get("STATE_DIR"):
        state_raw["local_dir"] = state_dir
    if state_table := os.environ.get("STATE_TABLE"):
        state_raw["dynamodb_table"] = state_table
    return Settings(
        products=products,
        lake=LakeConfig(**lake_raw),
        state=StateConfig(**state_raw),
        **raw,
    )


_CRED_ENV = ("ERCOT_USERNAME", "ERCOT_PASSWORD", "ERCOT_SUBSCRIPTION_KEY")
_CRED_KEYS = ("username", "password", "subscription_key")


def load_credentials() -> Credentials:
    """Resolve ERCOT credentials.

    Order: the three ``ERCOT_*`` env vars (``.env`` on a laptop), else the Secrets Manager
    secret named by ``ERCOT_SECRET_ID`` (the deployed Lambda; the value is entered in the
    console by hand and never passes through git or Terraform state). Anything else is an
    error: credentials are never read from a file in the image.
    """
    load_dotenv(REPO_ROOT / ".env")
    if all(os.environ.get(k) for k in _CRED_ENV):
        return Credentials(
            username=os.environ["ERCOT_USERNAME"],
            password=SecretStr(os.environ["ERCOT_PASSWORD"]),
            subscription_key=SecretStr(os.environ["ERCOT_SUBSCRIPTION_KEY"]),
        )
    if secret_id := os.environ.get("ERCOT_SECRET_ID"):
        return _credentials_from_secret(secret_id)
    missing = [k for k in _CRED_ENV if not os.environ.get(k)]
    msg = (
        f"missing ERCOT credentials: set {', '.join(missing)} (see .env.example) or ERCOT_SECRET_ID"
    )
    raise RuntimeError(msg)


@lru_cache(maxsize=1)
def _credentials_from_secret(secret_id: str) -> Credentials:  # pragma: no cover  (AWS only)
    """One Secrets Manager read per container lifetime; the value is ``{username, password,
    subscription_key}`` as JSON."""
    import boto3  # noqa: PLC0415  (only the deployed path needs the AWS SDK)

    client = boto3.client("secretsmanager")
    text = client.get_secret_value(SecretId=secret_id)["SecretString"]
    return credentials_from_json(text, source=secret_id)


def credentials_from_json(text: str, *, source: str) -> Credentials:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"secret {source!r} is not JSON"
        raise RuntimeError(msg) from exc
    missing = [k for k in _CRED_KEYS if not obj.get(k)]
    if missing:
        msg = f"secret {source!r} is missing keys: {', '.join(missing)}"
        raise RuntimeError(msg)
    return Credentials(
        username=obj["username"],
        password=SecretStr(obj["password"]),
        subscription_key=SecretStr(obj["subscription_key"]),
    )


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()
