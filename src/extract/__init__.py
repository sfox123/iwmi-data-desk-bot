"""Metadata extraction by file type. Heavy libraries are imported lazily per format."""
from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

NETCDF_EXTS = {".nc", ".nc4", ".h5", ".hdf", ".hdf5", ".he5"}
RASTER_EXTS = {".tif", ".tiff"}
VECTOR_EXTS = {".shp", ".gpkg", ".geojson", ".json", ".kml"}
TABULAR_EXTS = {".csv", ".xlsx", ".xlsm", ".xls"}
ZIP_EXTS = {".zip"}
# Inner files to look for inside a zip, in order of preference.
ZIP_PREFERENCE = [".shp", ".gpkg", ".geojson", ".nc", ".nc4", ".tif", ".tiff", ".h5", ".csv", ".xlsx"]
MAX_UNZIPPED_BYTES = 10 * 1024 ** 3


@dataclass
class Variable:
    name: str
    long_name: str | None = None
    standard_name: str | None = None
    units: str | None = None


@dataclass
class FileMetadata:
    kind: str = "unknown"                      # netcdf | raster | vector | tabular | document | unknown
    variables: list[Variable] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)   # table columns / vector fields / layer names
    attrs: dict[str, str] = field(default_factory=dict)  # title, source, institution, summary
    time_start: str | None = None              # ISO date
    time_end: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # (w, s, e, n) in EPSG:4326
    crs: str | None = None
    resolution: str | None = None
    extra: dict = field(default_factory=dict)  # dims, band count, row count, ...
    inspected: bool = False
    error: str | None = None


def kind_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in NETCDF_EXTS:
        return "netcdf"
    if ext in RASTER_EXTS:
        return "raster"
    if ext in VECTOR_EXTS:
        return "vector"
    if ext in TABULAR_EXTS:
        return "tabular"
    if ext in ZIP_EXTS:
        return "zip"
    return "document"


def extract(path: Path) -> FileMetadata:
    """Inspect a local file. Never raises: failures are recorded in ``error``."""
    kind = kind_for(path.name)
    try:
        if kind == "netcdf":
            from .netcdf import extract_netcdf
            meta = extract_netcdf(path)
        elif kind == "raster":
            from .raster import extract_raster
            meta = extract_raster(path)
        elif kind == "vector":
            from .vector import extract_vector
            meta = extract_vector(path)
        elif kind == "tabular":
            from .tabular import extract_tabular
            meta = extract_tabular(path)
        elif kind == "zip":
            meta = _extract_zip(path)
        else:
            meta = FileMetadata(kind="document")
        if kind != "document":
            meta.inspected = True
        return meta
    except Exception as exc:  # corrupt or unsupported file: classify by name/email only
        log.warning("metadata extraction failed",
                    extra={"data": {"file": path.name, "error": f"{type(exc).__name__}: {exc}"[:300]}})
        return FileMetadata(kind=kind if kind != "zip" else "unknown", error=f"{type(exc).__name__}: {exc}"[:300])


def _extract_zip(path: Path) -> FileMetadata:
    target = path.parent / (path.stem + "_unzipped")
    with zipfile.ZipFile(path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX")]
        if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
            raise ValueError("zip too large to inspect")
        root = target.resolve()
        for m in members:
            dest = (target / m.filename).resolve()
            if root not in dest.parents:  # zip-slip guard
                raise ValueError(f"unsafe path in zip: {m.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(dest, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
    files = [p for p in target.rglob("*") if p.is_file()]
    inner = None
    for ext in ZIP_PREFERENCE:
        inner = next((p for p in files if p.suffix.lower() == ext), None)
        if inner:
            break
    if inner is None:
        meta = FileMetadata(kind="document")
    else:
        meta = extract(inner)
    meta.extra["zip_members"] = [p.relative_to(target).as_posix() for p in files][:50]
    if inner is not None:
        meta.extra["zip_inner"] = inner.relative_to(target).as_posix()
        meta.fields.append(inner.stem)
    return meta
