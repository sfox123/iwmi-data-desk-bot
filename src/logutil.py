"""Structured JSON logging. Pass extra fields with ``log.info(msg, extra={"data": {...}})``."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            entry.update(data)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # MSAL and urllib3 can log request details at DEBUG; keep them quiet.
    for noisy in ("msal", "urllib3", "rasterio", "fiona", "pyogrio", "h5py"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
