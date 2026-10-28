"""Vector metadata (shapefile, GeoPackage, GeoJSON) via pyogrio, falling back to fiona."""
from __future__ import annotations

from pathlib import Path

from . import FileMetadata


def _layers(path: Path) -> list[dict]:
    try:
        import pyogrio
    except ImportError:
        return _layers_fiona(path)

    out = []
    for name, _geom in pyogrio.list_layers(path):
        try:
            info = pyogrio.read_info(path, layer=name, force_feature_count=True, force_total_bounds=True)
        except TypeError:  # older pyogrio
            info = pyogrio.read_info(path, layer=name)
        bounds = info.get("total_bounds")
        out.append({
            "name": str(name),
            "crs": info.get("crs"),
            "features": int(info.get("features") or 0),
            "fields": [str(f) for f in info.get("fields", [])],
            "bounds": tuple(float(v) for v in bounds) if bounds is not None else None,
        })
    return out


def _layers_fiona(path: Path) -> list[dict]:
    import fiona

    out = []
    for name in fiona.listlayers(path):
        with fiona.open(path, layer=name) as src:
            out.append({
                "name": name,
                "crs": src.crs.to_string() if src.crs else None,
                "features": len(src),
                "fields": list(src.schema["properties"].keys()),
                "bounds": tuple(src.bounds) if src.bounds else None,
            })
    return out


def _to_wgs84(bounds, crs):
    if bounds is None or any(v != v for v in bounds):  # NaN for empty layers
        return None
    if not crs or crs.upper() in ("EPSG:4326", "OGC:CRS84"):
        return bounds
    try:
        from rasterio.warp import transform_bounds
        return tuple(transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21))
    except Exception:
        return None


def extract_vector(path: Path) -> FileMetadata:
    layers = _layers(path)
    boxes = [b for b in (_to_wgs84(lyr["bounds"], lyr["crs"]) for lyr in layers) if b]
    bbox = None
    if boxes:
        bbox = tuple(round(v, 6) for v in (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                           max(b[2] for b in boxes), max(b[3] for b in boxes)))
    fields: list[str] = []
    for lyr in layers:
        fields.append(lyr["name"])
        fields.extend(f for f in lyr["fields"] if f not in fields)
    return FileMetadata(
        kind="vector",
        fields=fields,
        bbox=bbox,
        crs=layers[0]["crs"] if layers else None,
        extra={"layers": [{k: lyr[k] for k in ("name", "features", "crs")} for lyr in layers],
               "feature_count": sum(lyr["features"] for lyr in layers)},
    )
