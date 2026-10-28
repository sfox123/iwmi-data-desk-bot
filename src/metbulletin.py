"""Dept. of Meteorology 15-day forecast bulletins -> AWARE Sri Lanka's forecast maps.

A bulletin is a PDF named like ``20261027_00Z.pdf`` (the 00 UTC model run date), sent by the Met Dept to the
Data Drop channel email. It is published through AWARE's upload endpoint, which digitizes the PDF and refuses
anything that isn't a bulletin; in that case the file goes through normal filing instead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .emailparse import EmailContext

log = logging.getLogger(__name__)

RUN_RE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])_00Z", re.I)


class MetUploadError(Exception):
    """AWARE could not be reached or refused the key: retried next cycle, then the file is given up on."""


@dataclass
class MetResult:
    status: str                       # published | checked (dry-run) | exists | rejected
    run: str
    summary: dict = field(default_factory=dict)
    error: str | None = None


def run_of(name: str, email: EmailContext | None = None) -> str | None:
    """'20261027_00Z.pdf' -> '20261027_00Z'. The email subject is tried when the filename has no run."""
    for text in (name, email.subject if email else ""):
        m = RUN_RE.search(text or "")
        if m:
            return f"{m.group(1)}{m.group(2)}{m.group(3)}_00Z"
    return None


def sender_allowed(email: EmailContext | None, from_mailhook: bool, allowed: list[str]) -> bool:
    """Emailed files must come from an allowed address or domain ('@meteo.gov.lk'). Files dropped into the
    channel folder come from people with access to the team and are always allowed."""
    if not from_mailhook:
        return True
    addr = (email.from_email if email else "").strip().lower()
    if not addr:
        return False
    return any(addr == a or (a.startswith("@") and addr.endswith(a)) for a in (x.lower() for x in allowed))


class MetPublisher:
    def __init__(self, upload_url: str, token: str | None, *, dry_run: bool = False,
                 session: requests.Session | None = None, timeout: int = 120):
        self.upload_url = upload_url
        self.token = token
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.timeout = timeout

    def _post(self, path: Path, run: str, publish: bool) -> tuple[int, dict]:
        data = {"run": run, **({"publish": "1"} if publish else {})}
        try:
            with open(path, "rb") as fh:
                resp = self.session.post(self.upload_url, headers={"Authorization": f"Bearer {self.token}"},
                                         data=data, files={"file": (f"{run}.pdf", fh, "application/pdf")},
                                         timeout=self.timeout)
        except requests.RequestException as exc:
            raise MetUploadError(f"AWARE upload unreachable: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body

    def publish(self, path: Path, run: str) -> MetResult:
        """Check the PDF with AWARE, then publish it. An already published run is left as it is."""
        if not self.token:
            raise MetUploadError("MET_UPLOAD_TOKEN is not set")
        status, body = self._post(path, run, publish=False)
        if status in (400, 413, 422):
            return MetResult("rejected", run, error=body.get("error") or f"HTTP {status}")
        if status != 200:
            raise MetUploadError(f"AWARE check failed: HTTP {status} {body.get('error') or ''}".strip())
        if body.get("exists"):
            return MetResult("exists", run, summary=body)
        if self.dry_run:
            return MetResult("checked", run, summary=body)
        status, body = self._post(path, run, publish=True)
        if status == 409:
            return MetResult("exists", run, summary=body)
        if status != 200 or not body.get("published"):
            raise MetUploadError(f"AWARE publish failed: HTTP {status} {body.get('error') or ''}".strip())
        return MetResult("published", run, summary=body)
