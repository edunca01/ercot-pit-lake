"""Per-product ingest watermark. Local JSON on a laptop, DynamoDB when deployed.

Both stores hold the same :class:`Watermark`; the DynamoDB item is the model's JSON dump
(ISO-8601 strings for datetimes) keyed by ``product``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from ingest.config import StateConfig

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


class Watermark(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    last_posted_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: Literal["ok", "error", "never"] = "never"
    last_error: str | None = None


class StateStore(Protocol):
    def get(self, product: str) -> Watermark: ...
    def put(self, wm: Watermark) -> None: ...


class LocalStateStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, product: str) -> Path:
        return self.dir / f"{product}.json"

    def get(self, product: str) -> Watermark:
        p = self._path(product)
        if not p.exists():
            return Watermark(product=product)
        return Watermark.model_validate_json(p.read_text())

    def put(self, wm: Watermark) -> None:
        # write-then-rename: a crash mid-write leaves the previous watermark, never half a file
        tmp = self._path(wm.product).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wm.model_dump(mode="json"), indent=2) + "\n")
        tmp.replace(self._path(wm.product))


class DynamoStateStore:
    """One item per product. ``None`` fields are omitted from the item (DynamoDB has no
    natural null for optional strings) and come back as the model defaults."""

    def __init__(self, table: Table) -> None:
        self.table = table

    @classmethod
    def from_table_name(cls, name: str) -> DynamoStateStore:  # pragma: no cover  (AWS only)
        import boto3  # noqa: PLC0415  (only the deployed path needs the AWS SDK)

        return cls(boto3.resource("dynamodb").Table(name))

    def get(self, product: str) -> Watermark:
        item: dict[str, Any] | None = self.table.get_item(
            Key={"product": product}, ConsistentRead=True
        ).get("Item")
        if not item:
            return Watermark(product=product)
        return Watermark.model_validate(item)

    def put(self, wm: Watermark) -> None:
        item = {k: v for k, v in wm.model_dump(mode="json").items() if v is not None}
        self.table.put_item(Item=item)


def make_state_store(cfg: StateConfig) -> StateStore:
    if cfg.backend == "local":
        return LocalStateStore(cfg.local_dir)
    return DynamoStateStore.from_table_name(cfg.dynamodb_table)  # pragma: no cover
