from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Settings, load_config  # noqa: E402
from src.graph import GraphClient  # noqa: E402
from src.intake import IntakeRunner  # noqa: E402
from src.notify import Notifier  # noqa: E402
from src.state import LocalStateStore  # noqa: E402
from tests.fakegraph import WEBHOOK, FakeDrive, FakeSession, FakeTokens  # noqa: E402


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def settings():
    return Settings(tenant_id="t", client_id="c", site_id="S", drive_id="D", client_secret="x",
                    teams_webhook_url=WEBHOOK)


def make_chirps_nc(path: Path, scale: float = 1.0) -> bytes:
    """CHIRPS-like monthly precip over Ethiopia, 2020-2021."""
    import xarray as xr

    lat = np.arange(3.75, 14.76, 0.5)
    lon = np.arange(33.25, 47.76, 0.5)
    time = pd.date_range("2020-01-01", "2021-12-01", freq="MS")
    data = (np.random.default_rng(0).random((len(time), len(lat), len(lon))) * 10 * scale).astype("float32")
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), data,
                    {"units": "mm/day", "long_name": "Climate Hazards group InfraRed Precipitation with Stations"})},
        coords={"time": time, "latitude": lat, "longitude": lon},
        attrs={"title": "CHIRPS Version 2.0", "institution": "Climate Hazards Center, UC Santa Barbara"},
    )
    ds.to_netcdf(path)
    return path.read_bytes()


@pytest.fixture
def chirps_bytes(tmp_path):
    return make_chirps_nc(tmp_path / "chirps.nc")


@pytest.fixture
def chirps_bytes_v2(tmp_path):
    return make_chirps_nc(tmp_path / "chirps2.nc", scale=1.1)


class Env:
    """A fake drive + real GraphClient/IntakeRunner wired together."""

    def __init__(self, cfg, settings, tmp_path, dry_run=False, columns=()):
        self.drive = FakeDrive(columns=columns)
        for folder in ["Data Drop", "Apps/Microsoft Teams Mailhook/Data Drop", "Catalog and Water Accounting"]:
            self.drive.mkdirs(folder)
        self.session = FakeSession(self.drive)
        self.tokens = FakeTokens()
        self.cfg, self.settings, self.tmp_path = cfg, settings, tmp_path
        self.store = LocalStateStore(tmp_path / "state.json")
        self.dry_run = dry_run

    def runner(self, dry_run=None, backfill=False):
        dry = self.dry_run if dry_run is None else dry_run
        graph = GraphClient(self.tokens, "D", "S", dry_run=dry, session=self.session, sleep=lambda s: None)
        notifier = Notifier(self.settings.teams_webhook_url, dry_run=dry, session=self.session,
                            summary_threshold=self.cfg.summary_card_threshold)
        return IntakeRunner(self.cfg, self.settings, graph, self.store, notifier, dry_run=dry, backfill=backfill)

    def run(self, **kw):
        return self.runner(**kw).run_once()


@pytest.fixture
def env(cfg, settings, tmp_path):
    e = Env(cfg, settings, tmp_path)
    e.run()  # first run: records the (empty) drive and saves the delta link
    return e
