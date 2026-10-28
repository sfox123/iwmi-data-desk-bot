# Data Desk Bot — Intake & Filing Service

Watches the Eye on Water **Data Drop** intake folders (drag & drop and the Teams email mailhook),
identifies each file, renames and files it under `Data/<Category>/<Region>/<Dataset>/v<N>/`,
records it in `Catalog and Water Accounting/catalog.csv|json`, and posts an Adaptive Card to the
channel. Full behaviour: [datadesk-intake-spec.md](datadesk-intake-spec.md).

## Setup

```sh
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # fill in auth + TEAMS_WEBHOOK_URL
```

Auth uses MSAL client credentials. Prefer the certificate (`CERT_PATH` + `CERT_THUMBPRINT`, or a
`.pfx`); `CLIENT_SECRET` also works. Behaviour (categories, regions, sources, paths, thresholds)
lives in [config.yaml](config.yaml) — add categories or regions there, no code change needed.

## Running

```sh
python run.py --dry-run --backfill   # FIRST: log what would happen to files already in intake
python run.py                        # one cycle; first real run only records existing files
python run.py --backfill             # ...or process files already sitting in intake
python run.py --loop                 # poll every schedule.poll_interval_minutes (default 10)
python run.py --reset-delta          # forget the delta link
```

`--dry-run` makes no write calls at all (the Graph client refuses non-GET requests), sends no
cards and does not save state. Logs are JSON lines, one `file decision` line per file.

For a scheduled task run `python run.py` every 10 minutes. To move to an Azure Function (timer
trigger), call `IntakeRunner.run_once()` and set `state.backend: sharepoint` so state survives
redeploys.

## Met Dept bulletins → AWARE Sri Lanka

A PDF named `<YYYYMMDD>_00Z.pdf` or `<YYYYMMDD>12Z.pdf` (the Dept. of Meteorology 15-day rainfall/temperature forecast) that is
emailed to the Data Drop channel from an allowed sender (`met_bulletin.allowed_senders`), or dropped into
the channel folder, is sent to AWARE's `/api/met/upload` (`src/metbulletin.py`). AWARE digitizes and checks
it; if accepted it is published to the forecast maps, the PDF is filed under
`Data/Climate/Sri Lanka/DoM_15day_forecast/<year>/` and the card says so. A PDF AWARE doesn't recognise is
filed as usual; a run that is already published is left unchanged. If AWARE can't be reached the file is
retried on the next cycles. Needs `MET_UPLOAD_TOKEN` in `.env` (same value as in the Vercel project).
`--dry-run` only asks AWARE to check the PDF, never to publish.

## Layout

| Path | Purpose |
|---|---|
| `run.py` | CLI |
| `src/intake.py` | poll → filter → pair emails → inspect → classify → move → catalog → notify |
| `src/graph.py` | Graph client (delta, download, move, mkdir, upload, fields), retries, dry-run guard |
| `src/auth.py` | MSAL token provider |
| `src/emailparse.py` | `.eml` parsing, `_<N>` suffix pairing |
| `src/extract/` | NetCDF/HDF, GeoTIFF, vector, CSV/Excel, zip metadata |
| `src/classify.py`, `src/naming.py` | category/region/dataset scoring, naming rule |
| `src/catalog.py` | catalog read/upsert/write with eTag guards |
| `src/notify.py` | Adaptive Cards via the Workflows webhook |
| `src/state.py` | delta link, processed IDs, pending items (local or SharePoint) |
| `tests/` | unit + acceptance tests against an in-memory Graph fake |

```sh
.venv\Scripts\python -m pytest -q
```

## Manual steps

- Create the Workflows webhook on the Data Drop channel ("Post to a channel when a webhook request
  is received") and put its URL in `TEAMS_WEBHOOK_URL`.
- Optional library columns (Library settings): `Category`, `Region`, `Variable`, `Source`,
  `TimeStart`, `TimeEnd`, `Uploader`, `DatasetVersion`. Missing columns are skipped.
- Rotate the client secret used during testing before deploying; prefer the certificate.
