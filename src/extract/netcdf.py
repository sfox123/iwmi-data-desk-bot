"""NetCDF / HDF5 metadata via xarray."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import FileMetadata, Variable

ATTR_KEYS = ("title", "source", "institution", "summary")
LAT_NAMES = ("lat", "latitude", "nav_lat", "y")
LON_NAMES = ("lon", "longitude", "nav_lon", "x")
SKIP_VARS = {"crs", "spatial_ref", "time_bnds", "time_bounds", "lat_bnds", "lon_bnds", "bounds"}


def _open(path: Path):
    import xarray as xr

    errors = []
    for engine in (None, "netcdf4", "h5netcdf"):
        for decode in (True, False):
            try:
                return xr.open_dataset(path, engine=engine, decode_times=decode)
            except Exception as exc:
                errors.append(f"{engine}/{decode}: {type(exc).__name__}")
    raise ValueError("could not open dataset (" + "; ".join(errors[-2:]) + ")")


def _find_coord(ds, names, standard_name):
    for name in list(ds.coords) + list(ds.variables):
        var = ds[name]
        if var.attrs.get("standard_name") == standard_name:
            return var
    for name in list(ds.coords) + list(ds.variables):
        if str(name).lower() in names:
            return ds[name]
    return None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return None
        return str(value.astype("datetime64[D]"))
    if hasattr(value, "year") and hasattr(value, "month"):  # cftime / datetime
        return f"{value.year:04d}-{value.month:02d}-{getattr(value, 'day', 1):02d}"
    return None


def _time_range(ds) -> tuple[str | None, str | None]:
    for name in list(ds.coords) + list(ds.variables):
        values = ds[name].values
        if values.size == 0:
            continue
        if np.issubdtype(values.dtype, np.datetime64):
            return _iso(values.min()), _iso(values.max())
        if values.dtype == object and str(name).lower() == "time":
            flat = [v for v in values.ravel() if v is not None]
            if flat and hasattr(flat[0], "year"):
                return _iso(min(flat)), _iso(max(flat))
    return None, None


def _step(values: np.ndarray) -> float | None:
    values = np.asarray(values, dtype=float).ravel()
    if values.size < 2:
        return None
    diffs = np.abs(np.diff(np.unique(values)))
    return float(np.median(diffs)) if diffs.size else None


def extract_netcdf(path: Path) -> FileMetadata:
    with _open(path) as ds:
        variables = []
        for name, da in ds.data_vars.items():
            if str(name).lower() in SKIP_VARS or da.ndim == 0:
                continue
            variables.append(Variable(
                name=str(name),
                long_name=_s(da.attrs.get("long_name")),
                standard_name=_s(da.attrs.get("standard_name")),
                units=_s(da.attrs.get("units")),
            ))

        attrs = {k: str(ds.attrs[k]) for k in ATTR_KEYS if k in ds.attrs}
        time_start, time_end = _time_range(ds)

        bbox = resolution = crs = None
        lat = _find_coord(ds, LAT_NAMES, "latitude")
        lon = _find_coord(ds, LON_NAMES, "longitude")
        if lat is not None and lon is not None:
            lat_v = np.asarray(lat.values, dtype=float)
            lon_v = np.asarray(lon.values, dtype=float)
            if lat_v.size and lon_v.size and np.nanmax(np.abs(lat_v)) <= 90 and np.nanmax(np.abs(lon_v)) <= 360:
                lon_v = np.where(lon_v > 180, lon_v - 360, lon_v)
                dlat, dlon = _step(lat_v), _step(lon_v)
                half_lat, half_lon = (dlat or 0) / 2, (dlon or 0) / 2
                bbox = (
                    round(max(-180.0, float(np.nanmin(lon_v)) - half_lon), 6),
                    round(max(-90.0, float(np.nanmin(lat_v)) - half_lat), 6),
                    round(min(180.0, float(np.nanmax(lon_v)) + half_lon), 6),
                    round(min(90.0, float(np.nanmax(lat_v)) + half_lat), 6),
                )
                if dlat and dlon:
                    resolution = f"{dlon:g}x{dlat:g} deg"
                crs = "EPSG:4326"

        return FileMetadata(
            kind="netcdf",
            variables=variables,
            attrs=attrs,
            time_start=time_start,
            time_end=time_end,
            bbox=bbox,
            crs=crs,
            resolution=resolution,
            extra={"dims": {str(k): int(v) for k, v in ds.sizes.items()}},
        )


def _s(value) -> str | None:
    return None if value is None else str(value)
