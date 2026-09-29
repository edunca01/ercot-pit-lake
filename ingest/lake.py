"""The only writer to the lake. Same code for a local directory and S3 (pyarrow.fs).

Keys come from ``ercot_lake.contract``, so what this writes is exactly what readers look for.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import fs as pafs

from ingest.config import LakeConfig


def _json_default(o: object) -> str:
    if isinstance(o, datetime | date):
        return o.isoformat()
    return str(o)


class Lake:
    def __init__(self, cfg: LakeConfig, *, region: str | None = None) -> None:
        self.root = cfg.root
        if cfg.is_s3:
            self._fs: pafs.FileSystem = pafs.S3FileSystem(region=region)
            self._base = cfg.root.removeprefix("s3://").rstrip("/")
            self._local = False
        else:
            self._fs = pafs.LocalFileSystem()
            self._base = str(pafs.LocalFileSystem().normalize_path(cfg.root)).rstrip("/")
            self._local = True
            self._fs.create_dir(self._base, recursive=True)

    def uri(self, key: str) -> str:
        return f"{self.root.rstrip('/')}/{key}"

    def _path(self, key: str) -> str:
        return f"{self._base}/{key}"

    def _ensure_parent(self, key: str) -> None:
        if self._local:
            self._fs.create_dir(str(PurePosixPath(self._path(key)).parent), recursive=True)

    def write_bytes(self, key: str, data: bytes) -> None:
        self._ensure_parent(key)
        with self._fs.open_output_stream(self._path(key)) as out:
            out.write(data)

    def write_table(self, key: str, table: pa.Table) -> None:
        self._ensure_parent(key)
        pq.write_table(table, self._path(key), filesystem=self._fs, compression="zstd")

    def write_json(self, key: str, obj: dict[str, Any]) -> None:
        text = json.dumps(obj, indent=2, sort_keys=True, default=_json_default) + "\n"
        self.write_bytes(key, text.encode())

    def read_bytes(self, key: str) -> bytes:
        with self._fs.open_input_stream(self._path(key)) as inp:
            data: bytes = inp.read()
            return data

    def read_json(self, key: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.read_bytes(key))
        return result

    def exists(self, key: str) -> bool:
        return self._fs.get_file_info(self._path(key)).type != pafs.FileType.NotFound

    def list_keys(self, prefix: str) -> list[str]:
        sel = pafs.FileSelector(self._path(prefix), recursive=True, allow_not_found=True)
        base = self._base + "/"
        return sorted(
            fi.path.removeprefix(base)
            for fi in self._fs.get_file_info(sel)
            if fi.type == pafs.FileType.File
        )
