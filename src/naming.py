"""Naming rule: <variable>_<source>_<region>_<start>-<end>_v<N>.<ext>"""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Iterable

COMPOUND_EXTS = (".shp.xml", ".aux.xml", ".tar.gz", ".nc.gz")
VERSION_DIR_RE = re.compile(r"^v(\d+)$", re.I)


def ascii_clean(text: str) -> str:
    """ASCII only, spaces -> '_', drop anything that isn't safe in a SharePoint filename."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"[^A-Za-z0-9_\-]", "", text)
    return re.sub(r"_+", "_", text).strip("_-")


def file_ext(name: str) -> str:
    lower = name.lower()
    for ext in COMPOUND_EXTS:
        if lower.endswith(ext):
            return ext
    return PurePosixPath(lower).suffix


def base_stem(name: str) -> str:
    ext = file_ext(name)
    return name[: len(name) - len(ext)] if ext else name


def year_of(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    m = re.match(r"(\d{4})", iso_date)
    return int(m.group(1)) if m else None


def time_part(start: str | None, end: str | None) -> str | None:
    a, b = year_of(start), year_of(end)
    if a is None and b is None:
        return None
    if a is None or b is None or a == b:
        return str(a or b)
    return f"{min(a, b)}-{max(a, b)}"


def build_stem(variable: str | None, source: str | None, region: str | None,
               start: str | None, end: str | None, version: int) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate((variable, source, region)):
        if not raw:
            continue
        part = ascii_clean(raw)
        if i == 0:
            part = part.lower()
        if part and part.lower() not in seen:
            parts.append(part)
            seen.add(part.lower())
    period = time_part(start, end)
    if period:
        parts.append(period)
    parts.append(f"v{version}")
    return "_".join(parts)


def build_filename(variable, source, region, start, end, version: int, ext: str) -> str:
    return build_stem(variable, source, region, start, end, version) + ext.lower()


def dataset_key(source: str | None, variable: str | None) -> str:
    """'CHIRPS' + 'precip' -> 'CHIRPS_precip'. The variable is skipped if the name already has it."""
    base = ascii_clean(source or "")
    var = ascii_clean(variable or "").lower()
    if not var or var in base.lower().split("_") or var == base.lower():
        return base or var or "dataset"
    return f"{base}_{var}" if base else var


def next_version(existing_names: Iterable[str]) -> int:
    versions = [int(m.group(1)) for n in existing_names if (m := VERSION_DIR_RE.match(n))]
    return max(versions, default=0) + 1


def clean_stem(name: str) -> str:
    """Readable dataset/variable fallback from a filename stem."""
    stem = base_stem(name)
    stem = re.sub(r"(?<!\d)(19|20)\d{2}(\d{4})?(?!\d)", " ", stem)  # drop years / yyyymmdd
    stem = re.sub(r"(?i)(?<![a-z])v\d+(?![a-z])", " ", stem)            # drop version tags
    return ascii_clean(re.sub(r"[_\-.]+", " ", stem)) or "dataset"
