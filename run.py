"""Data Desk intake service CLI.

  python run.py --dry-run --backfill   # first use: show what would happen to files already in intake
  python run.py                        # one cycle (cron / scheduled task)
  python run.py --loop                 # poll every schedule.poll_interval_minutes
  python run.py --reset-delta          # forget the delta link (next run re-records existing files)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from src.auth import TokenProvider
from src.config import load_config, load_settings
from src.graph import GraphClient
from src.intake import IntakeRunner
from src.logutil import setup_logging
from src.metbulletin import MetPublisher
from src.notify import Notifier
from src.state import LocalStateStore, SharePointStateStore

log = logging.getLogger("datadesk")


def build_runner(args) -> tuple[IntakeRunner, float]:
    settings = load_settings(args.env)
    cfg = load_config(args.config)
    graph = GraphClient(TokenProvider(settings), settings.drive_id, settings.site_id, dry_run=args.dry_run)
    if cfg.state_backend == "sharepoint":
        store = SharePointStateStore(graph, cfg.state_sharepoint_path)
    else:
        store = LocalStateStore(cfg.state_local_path)
    notifier = Notifier(settings.teams_webhook_url, dry_run=args.dry_run,
                        summary_threshold=cfg.summary_card_threshold)
    met = MetPublisher(cfg.met.upload_url, settings.met_upload_token, dry_run=args.dry_run) if cfg.met else None
    if cfg.met and not settings.met_upload_token:
        log.warning("met_bulletin is enabled but MET_UPLOAD_TOKEN is not set: bulletins will fail to publish")
    runner = IntakeRunner(cfg, settings, graph, store, notifier, dry_run=args.dry_run,
                          backfill=args.backfill, reset_delta=args.reset_delta, met=met)
    return runner, args.interval or cfg.poll_interval_minutes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Data Desk Bot intake & filing service")
    p.add_argument("--loop", action="store_true", help="keep running, one cycle every interval")
    p.add_argument("--interval", type=float, help="minutes between cycles with --loop")
    p.add_argument("--dry-run", action="store_true", help="log decisions only; make no changes")
    p.add_argument("--backfill", action="store_true", help="process files already in intake on first run")
    p.add_argument("--reset-delta", action="store_true", help="discard the saved delta link before running")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    setup_logging(args.log_level)
    runner, interval = build_runner(args)

    if not args.loop:
        runner.run_once()
        return 0
    while True:
        try:
            runner.run_once()
        except Exception:
            log.exception("cycle failed")
        time.sleep(interval * 60)


if __name__ == "__main__":
    sys.exit(main())
