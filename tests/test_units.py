from __future__ import annotations

import pytest

from src.classify import classify, match_region_by_bbox, tokenize
from src.emailparse import parse_eml, split_suffix
from src.extract import FileMetadata, Variable, extract
from src.graph import DryRunViolation, GraphClient, parent_path, safe_url
from src.naming import build_filename, clean_stem, file_ext, next_version, time_part
from tests.fakegraph import FakeDrive, FakeSession, FakeTokens


# ---------------------------------------------------------------- naming
def test_build_filename_full_and_partial():
    assert build_filename("Precip", "CHIRPS", "Ethiopia", "2020-01-01", "2024-12-01", 1, ".NC") == \
        "precip_CHIRPS_Ethiopia_2020-2024_v1.nc"
    assert build_filename("landcover", "ESA", "NileBasin", None, None, 1, ".tif") == "landcover_ESA_NileBasin_v1.tif"
    assert build_filename("rain", None, "Sri Lanka", "2019-01-01", "2019-12-31", 3, ".csv") == \
        "rain_Sri_Lanka_2019_v3.csv"
    assert build_filename("débit", None, "Côte d'Ivoire", None, None, 1, ".csv") == "debit_Cote_dIvoire_v1.csv"


def test_versions_and_extensions():
    assert next_version([]) == 1
    assert next_version(["v1", "v3", "latest"]) == 4
    assert file_ext("rivers.shp.xml") == ".shp.xml" and file_ext("A.TIF") == ".tif"
    assert time_part("2020-01-01", None) == "2020"
    assert clean_stem("chirps_v2_20200101-2021.nc") == "chirps"


# ---------------------------------------------------------------- email
def test_split_suffix():
    assert split_suffix("rain data_973918918.csv") == ("rain data.csv", "973918918")
    assert split_suffix("chirps_2020.nc") == ("chirps_2020.nc", None)


def test_parse_eml_html_only():
    raw = (b"From: =?utf-8?q?S=C3=A9verine?= <sev@cgiar.org>\r\nSubject: =?utf-8?q?Test_=E2=80=93_x?=\r\n"
           b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>Hello<br>Volta <b>basin</b></p>")
    ctx = parse_eml(raw)
    assert ctx.from_name == "Séverine" and ctx.from_email == "sev@cgiar.org"
    assert ctx.subject == "Test – x"
    assert "Volta" in ctx.body and "<" not in ctx.body


# ---------------------------------------------------------------- classify
def test_tokenize_camel_and_compounds():
    assert tokenize("NileBasin_ERA5Land totalPrecip") == ["nile", "basin", "era5", "land", "total", "precip"]


def test_classify_by_filename_and_region_name(cfg):
    meta = FileMetadata(kind="raster")
    c = classify(meta, "lulc_ESA_Ghana_2018.tif", None, cfg)
    assert (c.status, c.category, c.region, c.variable, c.time_start[:4]) == ("filed", "LandUse", "Ghana", "lulc", "2018")
    assert c.dataset == "ESA_lulc"


def test_classify_tie_needs_review(cfg):
    c = classify(FileMetadata(kind="tabular"), "rain_runoff_Kenya.csv", None, cfg)
    assert c.status == "needs_review" and any("tie" in r for r in c.reasons)


def test_classify_unknown_region_needs_review(cfg):
    c = classify(FileMetadata(kind="tabular"), "rainfall.csv", None, cfg)
    assert c.status == "needs_review" and "unknown region" in c.reasons


def test_short_keywords_ignored_in_email_body(cfg):
    from src.emailparse import EmailContext
    email = EmailContext(subject="data", body="as noted by Smith et al, see IND report")
    c = classify(FileMetadata(kind="document"), "file.pdf", email, cfg)
    assert c.category is None and c.region is None


def test_global_and_bbox_regions(cfg):
    meta = FileMetadata(kind="netcdf", variables=[Variable("tas")], bbox=(-180, -60, 180, 80))
    assert classify(meta, "x.nc", None, cfg).region == "Global"
    assert match_region_by_bbox((80.0, 6.0, 81.8, 9.8), cfg.regions) == "Sri Lanka"
    assert match_region_by_bbox((-170, 10, -160, 20), cfg.regions) is None


# ---------------------------------------------------------------- extract
def test_extract_netcdf(tmp_path):
    from tests.conftest import make_chirps_nc
    make_chirps_nc(tmp_path / "c.nc")
    m = extract(tmp_path / "c.nc")
    assert m.kind == "netcdf" and m.error is None
    assert [v.name for v in m.variables] == ["precip"]
    assert (m.time_start, m.time_end) == ("2020-01-01", "2021-12-01")
    w, s, e, n = m.bbox
    assert 32.9 < w < 33.1 and 3.4 < s < 3.6 and 47.9 < e < 48.1 and 14.9 < n < 15.1
    assert m.attrs["title"] == "CHIRPS Version 2.0"


def test_extract_multiblock_csv(tmp_path):
    p = tmp_path / "stats.csv"
    p.write_text("metric,value,unit\ncenter_lat,24.4,deg\ncenter_lon,84.5,deg\nflooded_area,206,km2\n\n"
                 "scene_id,acquired\nS1A_x,2025-08-05T00:12:37.000Z\nS1A_y,2019-05-02T00:20:18.000Z\n")
    m = extract(p)
    assert m.error is None and "flooded_area" in m.fields
    assert m.bbox == (84.5, 24.4, 84.5, 24.4)
    assert (m.time_start, m.time_end) == ("2019-05-02", "2025-08-05")


def test_extract_excel(tmp_path):
    import pandas as pd
    p = tmp_path / "survey.xlsx"
    pd.DataFrame({"year": [2015, 2016], "household_id": [1, 2]}).to_excel(p, index=False)
    m = extract(p)
    assert m.kind == "tabular" and m.fields == ["year", "household_id"] and m.time_start == "2015-01-01"


def test_extract_geotiff(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin
    p = tmp_path / "ndvi.tif"
    with rasterio.open(p, "w", driver="GTiff", width=10, height=10, count=1, dtype="float32",
                       crs="EPSG:32637", transform=from_origin(500000, 1000000, 1000, 1000)) as dst:
        dst.write(np.ones((1, 10, 10), dtype="float32"))
        dst.set_band_description(1, "NDVI")
    m = extract(p)
    assert m.kind == "raster" and m.crs == "EPSG:32637" and m.variables[0].name == "NDVI"
    assert 38 < m.bbox[0] < 40 and 8 < m.bbox[1] < 10


def test_extract_geojson_and_zip(tmp_path):
    pytest.importorskip("pyogrio")
    import zipfile
    gj = tmp_path / "basins.geojson"
    gj.write_text('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"HYBAS_ID":1},'
                  '"geometry":{"type":"Point","coordinates":[38.7,9.0]}}]}')
    m = extract(gj)
    assert m.kind == "vector" and "HYBAS_ID" in m.fields and m.extra["feature_count"] == 1
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(gj, "inner/basins.geojson")
    mz = extract(z)
    assert mz.kind == "vector" and mz.extra["zip_inner"] == "inner/basins.geojson"


def test_extract_corrupt_file_does_not_raise(tmp_path):
    p = tmp_path / "bad.nc"
    p.write_bytes(b"not a netcdf")
    m = extract(p)
    assert m.error and not m.inspected


# ---------------------------------------------------------------- graph
def _client(dry_run=False):
    session = FakeSession(FakeDrive())
    return GraphClient(FakeTokens(), "D", "S", dry_run=dry_run, session=session, sleep=lambda s: None), session


def test_dry_run_blocks_writes_at_transport():
    g, session = _client(dry_run=True)
    with pytest.raises(DryRunViolation):
        g._request("PATCH", "/drives/D/items/x", json={})
    assert g.move("x", "y", "z")["dryRun"]
    assert g.ensure_folder("Data/New")["dryRun"]
    assert session.writes() == []


def test_retries_throttling():
    g, session = _client()
    session.fail_next = [429, 503]
    assert g.get_item_by_path("")["id"] == "root"
    assert len(session.calls) == 3


def test_ensure_folder_creates_parents_and_is_idempotent():
    g, session = _client()
    f = g.ensure_folder("Data/Climate/Ethiopia")
    assert session.drive.find("Data/Climate/Ethiopia")["id"] == f["id"]
    g._folder_cache.clear()
    assert g.ensure_folder("Data/Climate/Ethiopia")["id"] == f["id"]


def test_helpers():
    assert parent_path({"parentReference": {"path": "/drives/b!x/root:/Data%20Drop/sub"}}) == "Data Drop/sub"
    assert parent_path({"parentReference": {"path": "/drives/b!x/root:"}}) == ""
    assert safe_url("https://x.sharepoint.com/download.aspx?tempauth=SECRET") == "https://x.sharepoint.com/download.aspx"


def test_dataset_key():
    from src.naming import dataset_key
    assert dataset_key("CHIRPS", "precip") == "CHIRPS_precip"
    assert dataset_key("ERA5-Land", "tas") == "ERA5-Land_tas"
    assert dataset_key("gauges", "gauges") == "gauges"
    assert dataset_key(None, "rainfall") == "rainfall"
