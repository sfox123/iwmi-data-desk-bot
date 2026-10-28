"""catalog.csv / catalog.json in SharePoint — read, upsert rows, write back with eTag guards."""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any

from .graph import ConflictError, GraphClient

log = logging.getLogger(__name__)

FIELDS = [
    "item_id", "name", "original_name", "web_url", "path", "category", "region", "dataset", "version",
    "variables", "units", "standard_names", "time_start", "time_end", "bbox", "crs", "resolution",
    "size_bytes", "quick_xor_hash", "uploader", "uploader_email", "email_subject", "filed_at",
    "confidence", "status",
]
LIST_FIELDS = {"variables", "units", "standard_names"}
INT_FIELDS = {"version", "size_bytes"}
FLOAT_FIELDS = {"confidence"}
LIST_SEP = "; "
MAX_RETRIES = 3


def _to_csv_value(field: str, value: Any) -> str:
    if value is None:
        return ""
    if field in LIST_FIELDS:
        return LIST_SEP.join(str(v) for v in value)
    if field == "bbox":
        return ",".join(f"{v:g}" for v in value)
    return str(value)


def _from_csv_value(field: str, value: str) -> Any:
    if value == "":
        return [] if field in LIST_FIELDS else None
    try:
        if field in LIST_FIELDS:
            return value.split(LIST_SEP)
        if field == "bbox":
            return [float(v) for v in value.split(",")]
        if field in INT_FIELDS:
            return int(value)
        if field in FLOAT_FIELDS:
            return float(value)
    except ValueError:
        return value
    return value


def rows_to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({f: _to_csv_value(f, row.get(f)) for f in FIELDS})
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens it as UTF-8


def rows_from_csv(data: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    return [{f: _from_csv_value(f, r.get(f) or "") for f in FIELDS} for r in reader]


class Catalog:
    """In-memory catalog. CSV is the source of truth; JSON is regenerated alongside it."""

    def __init__(self, graph: GraphClient, folder: str):
        self.graph = graph
        self.csv_path = f"{folder}/catalog.csv"
        self.json_path = f"{folder}/catalog.json"
        self.rows: dict[str, dict] = {}
        self._dirty: dict[str, dict] = {}
        self._csv_etag: str | None = None
        self._json_etag: str | None = None

    def load(self) -> None:
        rows = self._fetch()
        self.rows = {r["item_id"]: r for r in rows if r.get("item_id")}

    def _fetch(self) -> list[dict]:
        data, self._csv_etag = self.graph.read_file(self.csv_path)
        jdata, self._json_etag = self.graph.read_file(self.json_path)
        if data:
            return rows_from_csv(data)
        if jdata:
            return list(json.loads(jdata.decode("utf-8")))
        return []

    def find_by_hash(self, quick_xor_hash: str | None) -> dict | None:
        if not quick_xor_hash:
            return None
        for row in self.rows.values():
            if row.get("quick_xor_hash") == quick_xor_hash and row.get("status") == "filed":
                return row
        return None

    def upsert(self, row: dict) -> None:
        row = {f: row.get(f) for f in FIELDS}
        self.rows[row["item_id"]] = row
        self._dirty[row["item_id"]] = row

    def commit(self) -> int:
        """Write pending changes. On a concurrent edit, re-read, re-apply and retry."""
        if not self._dirty:
            return 0
        count = len(self._dirty)
        if self.graph.dry_run:
            log.info("dry-run: catalog not written", extra={"data": {"rows_changed": count}})
            self._dirty.clear()
            return count
        for attempt in range(1, MAX_RETRIES + 1):
            ordered = list(self.rows.values())  # load order, new rows appended
            try:
                self.graph.upload(self.csv_path, rows_to_csv(ordered),
                                  if_match=self._csv_etag, create_only=self._csv_etag is None)
                self.graph.upload(self.json_path,
                                  json.dumps(ordered, indent=1, ensure_ascii=False).encode("utf-8"),
                                  if_match=self._json_etag, create_only=self._json_etag is None)
                log.info("catalog updated", extra={"data": {"rows_changed": count, "rows_total": len(ordered)}})
                self._dirty.clear()
                return count
            except ConflictError:
                log.warning("catalog changed concurrently, retrying", extra={"data": {"attempt": attempt}})
                remote = {r["item_id"]: r for r in self._fetch() if r.get("item_id")}
                remote.update(self._dirty)
                self.rows = remote
        raise RuntimeError("catalog update failed after retries")
