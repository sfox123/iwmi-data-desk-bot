"""Load settings (secrets/IDs from env) and config.yaml (behaviour)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    tenant_id: str
    client_id: str
    site_id: str
    drive_id: str
    client_secret: str | None = None
    cert_path: str | None = None
    cert_thumbprint: str | None = None
    cert_password: str | None = None
    teams_webhook_url: str | None = None
    max_download_mb: int = 2048
    met_upload_token: str | None = None

    def __repr__(self) -> str:  # never print secrets
        return (f"Settings(tenant_id={self.tenant_id!r}, client_id={self.client_id!r}, "
                f"auth={'certificate' if self.cert_path else 'secret'})")


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    if env_file and Path(env_file).exists():
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)

    def env(name: str) -> str | None:
        value = os.environ.get(name, "").strip()
        return value or None

    missing = [n for n in ("TENANT_ID", "CLIENT_ID", "SITE_ID", "DRIVE_ID") if not env(n)]
    if missing:
        raise ConfigError(f"Missing required settings: {', '.join(missing)}")
    if not env("CERT_PATH") and not env("CLIENT_SECRET"):
        raise ConfigError("Set CERT_PATH (+ CERT_THUMBPRINT for PEM) or CLIENT_SECRET")

    return Settings(
        tenant_id=env("TENANT_ID"),
        client_id=env("CLIENT_ID"),
        site_id=env("SITE_ID"),
        drive_id=env("DRIVE_ID"),
        client_secret=env("CLIENT_SECRET"),
        cert_path=env("CERT_PATH"),
        cert_thumbprint=env("CERT_THUMBPRINT"),
        cert_password=env("CERT_PASSWORD"),
        teams_webhook_url=env("TEAMS_WEBHOOK_URL"),
        max_download_mb=int(env("MAX_DOWNLOAD_MB") or 2048),
        met_upload_token=env("MET_UPLOAD_TOKEN"),
    )


@dataclass
class Region:
    name: str
    aliases: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class Config:
    intake_paths: list[str]
    mailhook_path: str
    data_root: str
    catalog_folder: str
    needs_review: str
    duplicates: str
    emails_folder: str | None
    categories: dict[str, list[str]]
    sources: dict[str, list[str]]
    regions: list[Region]
    ignore_patterns: list[str]
    min_confidence: float = 0.5
    upload_settle_minutes: float = 2
    email_wait_cycles: int = 2
    max_attempts: int = 3
    summary_card_threshold: int = 5
    global_lon_span: float = 90
    poll_interval_minutes: float = 10
    move_mode: str = "move"
    latest_copy: bool = False
    metadata_columns: bool = True
    state_backend: str = "local"
    state_local_path: str = "state/state.json"
    state_sharepoint_path: str = "_system/state.json"
    met: "MetConfig | None" = None


@dataclass
class MetConfig:
    upload_url: str
    site_url: str | None
    folder: str
    allowed_senders: list[str]


def _strip(path: str | None) -> str | None:
    return path.strip("/") if path else path


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    paths = raw.get("paths", {})
    thresholds = raw.get("thresholds", {})
    filing = raw.get("filing", {})
    state = raw.get("state", {})

    regions = []
    for r in raw.get("regions", []):
        bbox = tuple(float(v) for v in r["bbox"]) if r.get("bbox") else None
        regions.append(Region(name=str(r["name"]), aliases=[str(a) for a in r.get("aliases", [])], bbox=bbox))

    met_raw = raw.get("met_bulletin") or {}
    met = MetConfig(upload_url=met_raw["upload_url"], site_url=met_raw.get("site_url"),
                    folder=_strip(met_raw.get("folder", "Data/Climate/Sri Lanka/DoM_15day_forecast")),
                    allowed_senders=[str(a) for a in met_raw.get("allowed_senders", [])],
                    ) if met_raw.get("enabled") and met_raw.get("upload_url") else None

    move_mode = filing.get("move_mode", "move")
    if move_mode not in ("move", "copy"):
        raise ConfigError("filing.move_mode must be 'move' or 'copy'")

    return Config(
        intake_paths=[_strip(p) for p in paths.get("intake", ["Data Drop"])],
        mailhook_path=_strip(paths.get("mailhook", "Apps/Microsoft Teams Mailhook/Data Drop")),
        data_root=_strip(paths.get("data_root", "Data")),
        catalog_folder=_strip(paths.get("catalog_folder", "Catalog and Water Accounting")),
        needs_review=_strip(paths.get("needs_review") or raw.get("fallback") or "_Inbox/needs-review"),
        duplicates=_strip(paths.get("duplicates", "_Inbox/duplicates")),
        emails_folder=_strip(paths.get("emails")),
        categories={str(k): [str(w).lower() for w in v] for k, v in (raw.get("categories") or {}).items()},
        sources={str(k): [str(a) for a in v] for k, v in (raw.get("sources") or {}).items()},
        regions=regions,
        ignore_patterns=list(raw.get("ignore_patterns", [])),
        min_confidence=float(thresholds.get("min_confidence", 0.5)),
        upload_settle_minutes=float(thresholds.get("upload_settle_minutes", 2)),
        email_wait_cycles=int(thresholds.get("email_wait_cycles", 2)),
        max_attempts=int(thresholds.get("max_attempts", 3)),
        summary_card_threshold=int(thresholds.get("summary_card_threshold", 5)),
        global_lon_span=float(thresholds.get("global_lon_span", 90)),
        poll_interval_minutes=float(raw.get("schedule", {}).get("poll_interval_minutes", 10)),
        move_mode=move_mode,
        latest_copy=bool(filing.get("latest_copy", False)),
        metadata_columns=bool(filing.get("metadata_columns", True)),
        state_backend=state.get("backend", "local"),
        state_local_path=state.get("local_path", "state/state.json"),
        state_sharepoint_path=_strip(state.get("sharepoint_path", "_system/state.json")),
        met=met,
    )
