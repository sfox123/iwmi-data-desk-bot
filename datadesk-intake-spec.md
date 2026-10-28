# Data Desk Bot — Intake & Filing Service (Spec v1)

Build spec for Claude Code. Goal: researchers drop files into the **Eye on Water** Teams channel "Data Drop" (by email or drag-and-drop); this service automatically identifies, renames, files them into a structured `Data/` tree in SharePoint, records them in a catalog, and posts a confirmation to the channel.

---

## 1. Context (verified facts)

| Item | Value |
|---|---|
| Tenant ID | `6afa0e00-fa14-40b7-8a2e-22a7f8c357d5` |
| App (client) ID | `dcb36f1d-c6f4-4f90-adde-1fd24c6ba02c` ("Data Desk Bot") |
| Permission | Graph **Sites.Selected** (application), granted on site below with **write** role (verified: create + delete folder succeeded) |
| Site | `https://cgiar.sharepoint.com/sites/IWMI-EyeonWater` |
| Site ID | `cgiar.sharepoint.com,b52f51a1-da6f-44cf-ae92-19bd799c4a27,e6764bfc-ee60-4c44-b932-335f755f9773` |
| Documents drive ID | `b!oVEvtW_az0Sukhm9eZxKJ_xLduZg7kRMuTIzX3Vfl3P5xmZl_roKT6yvRd9D8CX6` |
| Users | CGIAR staff only (Eye on Water team, ~23 members) |

**Known Graph behaviour (verified in testing):**
- `drive/root/search(q=...)` returns **500** with app-only auth → do **not** use search. Use `delta`.
- `drive/root/delta` works app-only and returns full `parentReference.path`.

### Intake routes (verified)

| Route | Lands in (relative to drive root) |
|---|---|
| Email to channel address `2a8366f7.groups.cgiar.org@emea.teams.ms` | `Apps/Microsoft Teams Mailhook/Data Drop/EmailMessages_<MM>_<YYYY>/` |
| Drag & drop into channel "Shared" tab | `Data Drop/` |

Email route details:
- Teams saves the email as `<Subject>_<N>.eml` and each attachment as `<originalname>_<N>.<ext>`, where `<N>` is the same numeric suffix (e.g. `_973918918`). Use `<N>` to pair attachments with their `.eml`.
- A new `EmailMessages_MM_YYYY` folder appears each month → watch the whole `Apps/Microsoft Teams Mailhook/Data Drop/` subtree.

---

## 2. Target structure

```
Documents/
  Data/
    <Category>/
      <Region>/
        <DatasetName>/
          v1/  v2/  ...        ← versioned copies
          latest/              ← optional: copy of newest version
  Catalog and Water Accounting/
    catalog.csv                ← machine-readable catalog (source of truth for bot later)
    catalog.json
  _Inbox/
    needs-review/              ← files the service couldn't classify
  Data Drop/                   ← intake (drag & drop)
  Apps/Microsoft Teams Mailhook/Data Drop/   ← intake (email)
```

Categories are **config-driven** (YAML) so new working areas can be added without code changes. Initial set:

```yaml
categories:
  Climate:       [precip, precipitation, pr, tp, rain, rainfall, chirps, era5, tas, tmax, tmin, temperature, et, evapotranspiration, pet, aet]
  Hydrology:     [discharge, streamflow, runoff, flow, groundwater, gw, reservoir, lake, water_level, storage, tws, grace]
  LandUse:       [landcover, land_cover, lulc, irrigation, cropland, ndvi, evi, lai]
  Boundaries:    [basin, catchment, watershed, admin, boundary, gadm, hydrobasins]
  Socioeconomic: [survey, household, population, census, income, socio]
fallback: _Inbox/needs-review
```

Regions: config list (country names, ISO3, basin names). Matched against filename, NetCDF/GeoTIFF attributes, email subject/body, and (if available) bbox → country lookup.

---

## 3. Architecture

```
          ┌──────────────── scheduler (every 10 min) ────────────────┐
          ▼                                                           │
  [1] Delta poll ──► [2] Filter new files in intake paths ──► [3] Download to temp
                                                                     │
  [7] Post Teams card ◄── [6] Update catalog ◄── [5] Move+rename ◄── [4] Extract metadata + classify
```

- Language: **Python 3.11+**
- Libraries: `msal`, `requests`, `pyyaml`, `xarray`, `netCDF4`, `h5netcdf`, `rasterio`, `fiona` (or `pyogrio`), `pandas`, `openpyxl`, `python-dateutil`
- Runtime: start as a cron job / scheduled task; design so it can move to an Azure Function (timer trigger) later.
- State: `state.json` storing the delta link + processed item IDs. Store locally for v1; option to store in SharePoint (`_system/state.json`) so it survives redeploys.

---

## 4. Detailed behaviour

### 4.1 Auth
- MSAL client-credentials flow, scope `https://graph.microsoft.com/.default`.
- Read `TENANT_ID`, `CLIENT_ID`, and **either** `CLIENT_SECRET` **or** `CERT_PATH` + `CERT_THUMBPRINT` from env / `.env`. **Prefer certificate** (app already has one uploaded).
- Never log secrets or tokens. Refresh token automatically (tokens last ~60 min).

### 4.2 Delta polling
- First run: `GET /drives/{driveId}/root/delta` and page through `@odata.nextLink` until `@odata.deltaLink`; save deltaLink. **On first run, do not process existing files** — just record them (flag `--backfill` to process them).
- Next runs: call saved deltaLink.
- Keep items where:
  - `file` facet present (not folders), not `deleted`
  - `parentReference.path` is under `root:/Data Drop` **or** `root:/Apps/Microsoft Teams Mailhook/Data Drop`
  - not already in `processed` set
- Skip temp/lock files (`~$*`, `.tmp`) and skip files still being uploaded (re-check size stable on next run if `lastModifiedDateTime` < 2 min ago).

### 4.3 Email pairing
- For files in the Mailhook path: parse suffix `_(\d+)\.[^.]+$`.
- `.eml` files: download, parse with `email` stdlib → `from`, `subject`, `date`, plain-text body. Don't file the `.eml` itself into `Data/`; keep it in place (or move to `_Inbox/emails/`) and store its fields on matching attachments.
- Attachments: strip `_<N>` suffix to recover original filename.
- If an attachment is processed before its `.eml` is seen, wait one cycle (max 2) before proceeding without email context.

### 4.4 Download
- Use `@microsoft.graph.downloadUrl` (pre-authenticated) and stream to a temp dir.
- Config `MAX_DOWNLOAD_MB` (default 2048). Above the cap: skip content inspection, classify by name/email only.

### 4.5 Metadata extraction (by extension)

| Type | Extract |
|---|---|
| `.nc`, `.nc4`, `.h5`, `.hdf` | `xarray.open_dataset(decode_times=True)`: data variables (name, `long_name`, `standard_name`, `units`), global attrs (`title`, `source`, `institution`, `summary`), time min/max, lat/lon bbox, resolution, dims |
| `.tif`, `.tiff` | `rasterio`: CRS, bounds (→ reproject to EPSG:4326), resolution, band count, dtype, nodata, tags |
| `.shp` (+sidecars, or zipped), `.gpkg`, `.geojson` | `fiona/pyogrio`: layer names, CRS, bounds, feature count, field names |
| `.csv`, `.xlsx` | `pandas`: columns, row count, sheet names; detect date columns → time range |
| other (`.pdf`, `.docx`, …) | name + size only; category = `Documents` subfolder or needs-review |

Always: size, `file.hashes.quickXorHash`, uploader (`createdBy.user.displayName/email`, or email sender), created date.

Shapefiles: if components arrive as separate files, group by basename and move together; prefer asking users to zip.

### 4.6 Classification
1. Build a token bag from: variable names, `standard_name`, `long_name`, title/source attrs, filename, email subject + body.
2. Score each category by keyword hits from YAML; pick highest. Ties or zero → `needs-review`.
3. Region: match tokens against region list; else bbox → country/basin (optional: Natural Earth countries GeoJSON shipped with repo); else `Global` if bbox spans > 90° lon; else `Unknown` → needs-review.
4. Dataset name: from `title` attr or source keyword (`CHIRPS`, `ERA5`, `GRACE`, …), else cleaned filename stem.
5. Return a confidence score; below `MIN_CONFIDENCE` (default 0.5) → needs-review.

### 4.7 Naming rule

```
<variable>_<source>_<region>_<start>-<end>_v<N>.<ext>
e.g. precip_CHIRPS_Ethiopia_2020-2024_v1.nc
```
- Lowercase variable, keep source/region capitalisation, ASCII only, spaces → `_`.
- Missing parts are omitted (e.g. no time range → `landcover_ESA_NileBasin_v1.tif`).
- Version `N`: next free version in `Data/<Cat>/<Region>/<Dataset>/`.

### 4.8 Duplicate detection
- Before moving, compare `quickXorHash` against catalog. If identical file exists: don't create a new version; move the new copy to `_Inbox/duplicates/` and note it in the Teams card ("already filed as …").

### 4.9 Move + rename
- `PATCH /drives/{driveId}/items/{itemId}` with `{"parentReference":{"id": <targetFolderId>}, "name": <newName>}`.
- Create missing folders with `POST .../children` (`"folder":{}`, `"@microsoft.graph.conflictBehavior":"fail"`; on 409 fetch existing).
- Moving within the same drive keeps the item ID → the Teams message attachment link should keep working. **Verify this in testing**; if links break, switch to copy (`POST .../copy`) and leave the original in place.
- Set **metadata columns** if they exist on the library (`PATCH .../items/{id}/listItem/fields`): `Category`, `Region`, `Variable`, `Source`, `TimeStart`, `TimeEnd`, `Uploader`, `DatasetVersion`. Skip silently if columns don't exist (create them manually in Library settings).

### 4.10 Catalog
- Append/update a row in `Catalog and Water Accounting/catalog.csv` and `catalog.json` (download → modify → upload with `PUT .../content`; use `If-Match` eTag to avoid overwrite races).
- Fields: `item_id, name, original_name, web_url, path, category, region, dataset, version, variables, units, standard_names, time_start, time_end, bbox (w,s,e,n), crs, resolution, size_bytes, quick_xor_hash, uploader, uploader_email, email_subject, filed_at, confidence, status (filed|needs_review|duplicate)`.
- This catalog is the input for the future co-pilot bot (Qdrant index).

### 4.11 Teams notification
- App-only Graph **cannot post channel messages** (ChannelMessage.Send is delegated-only). Use a **Workflows (Power Automate) incoming webhook** on the Data Drop channel: "Post to a channel when a webhook request is received". URL in env `TEAMS_WEBHOOK_URL`.
- POST an Adaptive Card per processed file:
  - ✅ **Filed**: new name, folder path (link), detected variables, time range, region, version.
  - ⚠️ **Needs review**: original name, why (low confidence / unknown region), link to `_Inbox/needs-review`.
  - ♻️ **Duplicate**: link to existing file.
- Mentions: include uploader name (plain text); @mention support is optional/stretch.
- Batch: if > 5 files in one run, send one summary card.

---

## 5. Config & repo layout

```
datadesk-intake/
  .env.example          # TENANT_ID, CLIENT_ID, CLIENT_SECRET | CERT_PATH+CERT_THUMBPRINT, SITE_ID, DRIVE_ID, TEAMS_WEBHOOK_URL
  config.yaml           # categories, regions, paths, thresholds
  src/
    auth.py             # MSAL token cache
    graph.py            # thin Graph client: delta, download, move, mkdir, upload, fields
    intake.py           # main loop: poll → process → notify
    emailparse.py
    extract/{netcdf.py, raster.py, vector.py, tabular.py}
    classify.py
    naming.py
    catalog.py
    notify.py           # Teams webhook cards
  state/state.json
  tests/                # unit tests with sample small .nc/.tif/.csv fixtures; Graph calls mocked
  run.py                # CLI: run once | --loop | --dry-run | --backfill | --reset-delta
```

- **`--dry-run` is mandatory for first use**: log what would be renamed/moved, change nothing.
- Logging: structured JSON logs; one line per file with decision + confidence.
- `.env` and `state/` in `.gitignore`.

---

## 6. Acceptance tests

1. Email a small `.csv` to the channel → within one cycle it's filed (or sent to needs-review) and a card appears in Data Drop.
2. Drag a CHIRPS-like NetCDF (`precip`, units `mm/day`, 2020–2021, Ethiopia bbox) into Shared tab → filed as `Data/Climate/Ethiopia/CHIRPS/v1/precip_CHIRPS_Ethiopia_2020-2021_v1.nc`, catalog row added.
3. Upload the same NetCDF again → marked duplicate, no v2.
4. Upload a modified version → filed as `v2`.
5. Upload `notes.txt` with no clues → `_Inbox/needs-review`, ⚠️ card.
6. Re-run with no new files → no actions, no cards.
7. Token expiry mid-run → refreshes transparently.
8. `--dry-run` makes zero write calls (assert in tests).

---

## 7. Out of scope (v1) / next phases

- Co-pilot bot (Teams/WhatsApp) reading `catalog.json` + Qdrant index.
- Catalog web page with bbox map.
- Weekly digest card.
- Auto-creating library metadata columns (needs `manage` role; do manually).
- External partner intake (not needed — CGIAR staff only).

## 8. Security notes

- Secret currently in use was exposed during testing → **rotate before deploying**; prefer certificate auth.
- The app can read/write the whole Eye on Water site; restrict who can access the runtime and logs.
- Never write file contents or tokens to logs.
