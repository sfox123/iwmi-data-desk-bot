"""Spec section 6 acceptance tests against an in-memory Graph."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from src.catalog import rows_from_csv
from tests.conftest import Env

DROP = "Data Drop"
MAIL = "Apps/Microsoft Teams Mailhook/Data Drop/EmailMessages_10_2026"
CHIRPS_PATH = "Data/Climate/Ethiopia/CHIRPS_precip/v1/precip_CHIRPS_Ethiopia_2020-2021_v1.nc"

EML = (b"From: Alice Tester <alice@cgiar.org>\r\nTo: channel@emea.teams.ms\r\n"
       b"Subject: Kenya rainfall gauges 2019\r\nDate: Wed, 14 Oct 2026 09:00:00 +0000\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\nDaily rainfall from county gauges, GPCC station set.\r\n")
CSV = b"date,station,rainfall_mm\n2019-01-01,Nyeri,3.2\n2019-12-31,Nyeri,0.0\n"


def catalog(env):
    item = env.drive.find("Catalog and Water Accounting/catalog.csv")
    return rows_from_csv(item["content"]) if item else []


def card_titles(env):
    return [p["attachments"][0]["content"]["body"][0]["text"] for p in env.session.webhook_posts]


def test_1_email_csv_is_filed_with_email_context(env):
    env.drive.add_file(f"{MAIL}/Kenya rainfall gauges 2019_973918918.eml", EML, created_by=("SharePoint App", None))
    env.drive.add_file(f"{MAIL}/gauges_973918918.csv", CSV, created_by=("SharePoint App", None))

    result = env.run()

    assert [o.status for o in result.outcomes] == ["filed"]
    o = result.outcomes[0]
    assert o.path == "Data/Climate/Kenya/GPCC_rainfall/v1"  # source named only in the email body
    assert o.new_name == "rainfall_GPCC_Kenya_2019_v1.csv"
    assert o.uploader == "Alice Tester"
    assert env.drive.files_under("_Inbox/emails") == ["_Inbox/emails/Kenya rainfall gauges 2019_973918918.eml"]
    row = catalog(env)[0]
    assert row["uploader_email"] == "alice@cgiar.org"
    assert row["email_subject"] == "Kenya rainfall gauges 2019"
    assert len(env.session.webhook_posts) == 1


def test_2_chirps_netcdf_filed_and_catalogued(env, chirps_bytes):
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    result = env.run()

    assert [o.status for o in result.outcomes] == ["filed"]
    assert env.drive.files_under("Data") == [CHIRPS_PATH]
    rows = catalog(env)
    assert len(rows) == 1
    row = rows[0]
    assert (row["category"], row["region"], row["dataset"], row["version"]) == ("Climate", "Ethiopia", "CHIRPS_precip", 1)
    assert row["variables"] == ["precip"] and row["units"] == ["mm/day"]
    assert row["time_start"].startswith("2020") and row["time_end"].startswith("2021")
    assert row["status"] == "filed"
    assert json.loads(env.drive.find("Catalog and Water Accounting/catalog.json")["content"])[0]["bbox"]
    assert card_titles(env) == ["✅ Filed: precip_CHIRPS_Ethiopia_2020-2021_v1.nc"]


def test_3_same_file_again_is_duplicate(env, chirps_bytes):
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.run()
    env.drive.add_file(f"{DROP}/chirps_again.nc", chirps_bytes)
    result = env.run()

    assert [o.status for o in result.outcomes] == ["duplicate"]
    assert result.outcomes[0].duplicate_of == CHIRPS_PATH
    assert env.drive.files_under("Data") == [CHIRPS_PATH]  # no v2
    assert env.drive.files_under("_Inbox/duplicates") == ["_Inbox/duplicates/chirps_again.nc"]
    assert card_titles(env)[-1].startswith("♻️ Duplicate")


def test_4_modified_file_becomes_v2(env, chirps_bytes, chirps_bytes_v2):
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.run()
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes_v2)
    env.run()

    assert env.drive.files_under("Data") == [
        CHIRPS_PATH, "Data/Climate/Ethiopia/CHIRPS_precip/v2/precip_CHIRPS_Ethiopia_2020-2021_v2.nc"]
    assert [r["version"] for r in catalog(env)] == [1, 2]


def test_5_unclassifiable_goes_to_needs_review(env):
    env.drive.add_file(f"{DROP}/notes.txt", b"remember to call Bob")
    result = env.run()

    assert [o.status for o in result.outcomes] == ["needs_review"]
    assert env.drive.files_under("_Inbox/needs-review") == ["_Inbox/needs-review/notes.txt"]
    assert card_titles(env) == ["⚠️ Needs review: notes.txt"]
    assert catalog(env)[0]["status"] == "needs_review"


def test_6_rerun_without_new_files_does_nothing(env, chirps_bytes):
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.run()
    posts, writes = len(env.session.webhook_posts), len(env.session.writes())

    result = env.run()

    assert result.outcomes == []
    assert len(env.session.webhook_posts) == posts
    assert len(env.session.writes()) == writes


def test_7_token_expiry_mid_run_refreshes(env, chirps_bytes):
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.session.valid_tokens = {"Bearer tok-2"}  # tok-1 has "expired"

    result = env.run()

    assert [o.status for o in result.outcomes] == ["filed"]
    assert env.tokens.invalidations == 1


def test_8_dry_run_makes_zero_writes(cfg, settings, tmp_path, chirps_bytes):
    env = Env(cfg, settings, tmp_path, dry_run=True)
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.drive.add_file(f"{DROP}/notes.txt", b"nothing useful")
    env.drive.add_file(f"{MAIL}/Kenya rainfall gauges 2019_55555.eml", EML)
    env.drive.add_file(f"{MAIL}/gauges_55555.csv", CSV)

    result = env.run(backfill=True)

    assert sorted(o.status for o in result.outcomes) == ["filed", "filed", "needs_review"]
    assert env.session.writes() == []
    assert env.session.webhook_posts == []
    assert not (tmp_path / "state.json").exists()
    assert env.drive.files_under("Data Drop") == ["Data Drop/chirps_sample.nc", "Data Drop/notes.txt"]


# ---------------------------------------------------------------- further behaviour
def test_first_run_records_existing_files_without_processing(cfg, settings, tmp_path, chirps_bytes):
    env = Env(cfg, settings, tmp_path)
    env.drive.add_file(f"{DROP}/old.nc", chirps_bytes)
    result = env.run()
    assert result.first_run and result.recorded == 1 and result.outcomes == []
    assert env.drive.files_under("Data Drop") == ["Data Drop/old.nc"]
    assert env.run().outcomes == []


def test_backfill_processes_existing_files(cfg, settings, tmp_path, chirps_bytes):
    env = Env(cfg, settings, tmp_path)
    env.drive.add_file(f"{DROP}/old.nc", chirps_bytes)
    assert [o.status for o in env.run(backfill=True).outcomes] == ["filed"]


def test_attachment_waits_for_its_email(env):
    env.drive.add_file(f"{MAIL}/gauges_777777.csv", CSV)
    assert env.run().outcomes == []          # cycle 1: waiting for .eml
    env.drive.add_file(f"{MAIL}/Kenya rainfall gauges 2019_777777.eml", EML)
    result = env.run()                        # cycle 2: .eml arrived, pair them
    assert [o.status for o in result.outcomes] == ["filed"]
    assert result.outcomes[0].region == "Kenya"


def test_attachment_proceeds_without_email_after_max_cycles(env):
    env.drive.add_file(f"{MAIL}/notes_888888.txt", b"x")
    assert env.run().outcomes == []
    assert env.run().outcomes == []
    result = env.run()
    assert [o.original_name for o in result.outcomes] == ["notes.txt"]


def test_recently_modified_file_waits_until_size_is_stable(env):
    env.drive.add_file(f"{DROP}/notes.txt", b"abc", modified=datetime.now(timezone.utc))
    assert env.run().outcomes == []
    assert [o.status for o in env.run().outcomes] == ["needs_review"]


def test_temp_and_lock_files_are_skipped(env):
    env.drive.add_file(f"{DROP}/~$budget.xlsx", b"lock")
    assert env.run().outcomes == []
    assert env.drive.files_under("Data Drop") == ["Data Drop/~$budget.xlsx"]


def test_summary_card_when_many_files(env):
    for i in range(6):
        env.drive.add_file(f"{DROP}/notes{i}.txt", f"n{i}".encode())
    env.run()
    assert card_titles(env) == ["Data Desk processed 6 files"]


def test_metadata_columns_set_when_present(cfg, settings, tmp_path, chirps_bytes):
    env = Env(cfg, settings, tmp_path, columns={"Category", "Region", "DatasetVersion"})
    env.run()
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.run()
    filed = env.drive.find(CHIRPS_PATH)
    assert filed["fields"] == {"Category": "Climate", "Region": "Ethiopia", "DatasetVersion": 1}


def test_catalog_concurrent_edit_is_merged(env, chirps_bytes):
    env.drive.add_file(f"{DROP}/notes.txt", b"first")
    env.run()
    runner = env.runner()
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)

    original_load = runner.catalog.load

    def load_then_someone_edits():
        original_load()
        item = env.drive.find("Catalog and Water Accounting/catalog.csv")
        item["content"] += b"manual-row,manual.txt,,,,,,,,,,,,,,,,,,,,,,,needs_review\n"
        env.drive._touch(item)

    runner.catalog.load = load_then_someone_edits
    runner.run_once()
    ids = [r["item_id"] for r in catalog(env)]
    assert "manual-row" in ids and len(ids) == 3


def test_different_variables_from_one_source_get_separate_datasets(env, chirps_bytes, tmp_path):
    import xarray as xr
    ds = xr.open_dataset(tmp_path / "chirps.nc").load()
    ds.close()
    ds.rename({"precip": "tmax"}).to_netcdf(tmp_path / "tmax.nc")
    env.drive.add_file(f"{DROP}/chirps_sample.nc", chirps_bytes)
    env.drive.add_file(f"{DROP}/chirps_tmax.nc", (tmp_path / "tmax.nc").read_bytes())
    env.run()
    assert env.drive.files_under("Data") == [
        CHIRPS_PATH, "Data/Climate/Ethiopia/CHIRPS_tmax/v1/tmax_CHIRPS_Ethiopia_2020-2021_v1.nc"]


def test_no_recognised_source_goes_to_needs_review(env):
    env.drive.add_file(f"{DROP}/rainfall_Kenya_2019.csv", CSV)
    result = env.run()
    o = result.outcomes[0]
    assert o.status == "needs_review" and o.dataset == "UnknownSource_rainfall"
    assert any("no recognised source" in r for r in o.reasons)
    assert env.drive.files_under("_Inbox/needs-review") == ["_Inbox/needs-review/rainfall_Kenya_2019.csv"]
