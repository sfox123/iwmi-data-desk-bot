"""GeoTIFF metadata via rasterio."""
from __future__ import annotations

import warnings
from pathlib import Path

from . import FileMetadata, Variable


def extract_raster(path: Path) -> FileMetadata:
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning
    from rasterio.warp import transform_bounds

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(path) as src:
            crs = src.crs.to_string() if src.crs else None
            bbox = None
            if src.crs:
                b = src.bounds
                w, s, e, n = transform_bounds(src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top,
                                              densify_pts=21)
                bbox = (round(w, 6), round(s, 6), round(e, 6), round(n, 6))

            tags = {str(k): str(v) for k, v in src.tags().items()}
            lower = {k.lower(): v for k, v in tags.items()}
            attrs = {}
            for key, candidates in (("title", ("title", "tifftag_imagedescription")),
                                    ("source", ("source",)), ("institution", ("institution",)),
                                    ("summary", ("summary", "description"))):
                for c in candidates:
                    if lower.get(c):
                        attrs[key] = lower[c]
                        break

            variables = []
            for i, desc in enumerate(src.descriptions, start=1):
                units = src.units[i - 1] if src.units else None
                if desc:
                    variables.append(Variable(name=desc, units=units or None))

            res_x, res_y = src.res
            unit = "deg" if src.crs and src.crs.is_geographic else "m" if src.crs else "px"
            return FileMetadata(
                kind="raster",
                variables=variables,
                attrs=attrs,
                bbox=bbox,
                crs=crs,
                resolution=f"{res_x:g}x{res_y:g} {unit}",
                extra={
                    "band_count": src.count,
                    "dtype": src.dtypes[0] if src.dtypes else None,
                    "nodata": src.nodata,
                    "width": src.width,
                    "height": src.height,
                    "tags": {k: v[:200] for k, v in list(tags.items())[:30]},
                },
            )
