"""Thin Microsoft Graph client for one drive: delta, download, move, mkdir, upload, fields.

In dry-run mode every write method returns a simulated result and ``_request`` refuses
anything but GET, so a dry run cannot change SharePoint even through a bug elsewhere.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
UPLOAD_CHUNK = 5 * 320 * 1024  # multiple of 320 KiB as Graph requires

log = logging.getLogger(__name__)


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str, method: str, url: str):
        self.status, self.code = status, code
        super().__init__(f"{method} {url} -> {status} {code}: {message}")

    @classmethod
    def from_response(cls, resp, method: str, url: str) -> "GraphError":
        code, message = "", ""
        try:
            err = resp.json().get("error", {})
            code, message = err.get("code", ""), err.get("message", "")
        except Exception:
            message = (getattr(resp, "text", "") or "")[:200]
        return cls(resp.status_code, code, message, method, safe_url(url))


class DeltaResyncRequired(GraphError):
    pass


class ConflictError(GraphError):
    """409 name conflict or 412 eTag mismatch."""


class DryRunViolation(RuntimeError):
    pass


def safe_url(url: str) -> str:
    """URL without query string — pre-authenticated download URLs carry a token there."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else parts.path


def quote_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


def parent_path(item: dict) -> str | None:
    """Folder path of an item relative to the drive root, e.g. 'Data Drop'. None if unknown."""
    p = (item.get("parentReference") or {}).get("path")
    if p is None:
        return None
    _, sep, rest = p.partition("root:")
    if not sep:
        return None
    return unquote(rest).strip("/")


def item_path(item: dict) -> str | None:
    parent = parent_path(item)
    if parent is None:
        return None
    return f"{parent}/{item['name']}" if parent else item["name"]


def is_under(path: str | None, base: str) -> bool:
    if path is None:
        return False
    p, b = path.lower().strip("/"), base.lower().strip("/")
    return p == b or p.startswith(b + "/")


class GraphClient:
    def __init__(self, tokens, drive_id: str, site_id: str, *, dry_run: bool = False,
                 session: requests.Session | None = None, max_retries: int = 5, timeout: int = 120,
                 sleep: Callable[[float], None] = time.sleep):
        self.tokens = tokens
        self.drive_id = drive_id
        self.site_id = site_id
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self._folder_cache: dict[str, dict] = {}
        self._columns: set[str] | None = None

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, url: str, *, params=None, json=None, data=None, headers=None,
                 stream: bool = False, auth: bool = True, allow: tuple[int, ...] = ()):
        method = method.upper()
        if self.dry_run and method != "GET":
            raise DryRunViolation(f"{method} {safe_url(url)} blocked in dry-run")
        if not url.startswith("http"):
            url = GRAPH + url
        refreshed = False
        attempt = 0
        while True:
            hdrs = dict(headers or {})
            if auth:
                hdrs["Authorization"] = f"Bearer {self.tokens.get_token()}"
            try:
                resp = self.session.request(method, url, params=params, json=json, data=data, headers=hdrs,
                                            stream=stream, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                log.warning("graph network error, retrying",
                            extra={"data": {"url": safe_url(url), "error": type(exc).__name__}})
                self._sleep(min(2 ** attempt, 60))
                continue

            status = resp.status_code
            if status == 401 and auth and not refreshed:
                refreshed = True
                self.tokens.invalidate()
                continue
            if status in (429, 502, 503, 504) and attempt < self.max_retries:
                attempt += 1
                wait = _retry_after(resp, default=min(2 ** attempt, 60))
                log.warning("graph throttled, retrying",
                            extra={"data": {"status": status, "wait_s": wait, "url": safe_url(url)}})
                self._sleep(wait)
                continue
            if 200 <= status < 300 or status in allow:
                return resp
            if status in (409, 412):
                raise ConflictError(status, "", _error_message(resp), method, safe_url(url))
            raise GraphError.from_response(resp, method, url)

    @staticmethod
    def _json(resp) -> dict:
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------ reads
    def delta(self, delta_link: str | None = None) -> tuple[list[dict], str | None]:
        """All changes since ``delta_link`` (or the whole drive). Returns (items, new delta link)."""
        url = delta_link or f"/drives/{self.drive_id}/root/delta"
        items: list[dict] = []
        while True:
            resp = self._request("GET", url, allow=(410,))
            if resp.status_code == 410:
                raise DeltaResyncRequired(410, "resyncRequired", "delta token expired", "GET", safe_url(url))
            body = resp.json()
            items.extend(body.get("value", []))
            if body.get("@odata.nextLink"):
                url = body["@odata.nextLink"]
                continue
            return items, body.get("@odata.deltaLink")

    def get_item(self, item_id: str) -> dict | None:
        resp = self._request("GET", f"/drives/{self.drive_id}/items/{item_id}", allow=(404,))
        return None if resp.status_code == 404 else resp.json()

    def get_item_by_path(self, path: str) -> dict | None:
        path = path.strip("/")
        url = f"/drives/{self.drive_id}/root" + (f":/{quote_path(path)}" if path else "")
        resp = self._request("GET", url, allow=(404,))
        return None if resp.status_code == 404 else resp.json()

    def list_children(self, item_id: str) -> list[dict]:
        url = f"/drives/{self.drive_id}/items/{item_id}/children"
        params = {"$top": "200"}
        out: list[dict] = []
        while url:
            body = self._request("GET", url, params=params).json()
            out.extend(body.get("value", []))
            url, params = body.get("@odata.nextLink"), None
        return out

    def download(self, item_id: str, dest: Path) -> Path:
        """Stream a file to ``dest`` using the pre-authenticated download URL."""
        fresh = self.get_item(item_id) or {}
        url = fresh.get("@microsoft.graph.downloadUrl")
        if url:
            resp = self._request("GET", url, stream=True, auth=False)
        else:
            resp = self._request("GET", f"/drives/{self.drive_id}/items/{item_id}/content", stream=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
        finally:
            resp.close()
        return dest

    def read_file(self, path: str) -> tuple[bytes | None, str | None]:
        """Contents and eTag of a (small) file by path, or (None, None) if it doesn't exist."""
        item = self.get_item_by_path(path)
        if item is None:
            return None, None
        resp = self._request("GET", f"/drives/{self.drive_id}/items/{item['id']}/content")
        return resp.content, item.get("eTag")

    def list_columns(self) -> set[str]:
        """Internal names of the document library's columns (cached)."""
        if self._columns is None:
            try:
                lst = self._request("GET", f"/drives/{self.drive_id}/list", params={"$select": "id"}).json()
                body = self._request("GET", f"/sites/{self.site_id}/lists/{lst['id']}/columns").json()
                self._columns = {c.get("name") for c in body.get("value", [])}
            except GraphError as exc:
                log.warning("could not read library columns; metadata columns disabled",
                            extra={"data": {"error": str(exc)}})
                self._columns = set()
        return self._columns

    # ------------------------------------------------------------------ writes
    def ensure_folder(self, path: str) -> dict:
        """Return the folder at ``path``, creating it (and parents) if missing."""
        path = path.strip("/")
        if path in self._folder_cache:
            return self._folder_cache[path]
        existing = self.get_item_by_path(path)
        if existing is not None:
            self._folder_cache[path] = existing
            return existing
        if self.dry_run:
            fake = {"id": f"dry-run:{path}", "name": path.rsplit("/", 1)[-1], "webUrl": None, "dryRun": True}
            self._folder_cache[path] = fake
            return fake
        parent_path_, _, name = path.rpartition("/")
        parent = self.ensure_folder(parent_path_) if parent_path_ else self.get_item_by_path("")
        resp = self._request(
            "POST", f"/drives/{self.drive_id}/items/{parent['id']}/children",
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            allow=(409,))
        folder = self.get_item_by_path(path) if resp.status_code == 409 else resp.json()
        self._folder_cache[path] = folder
        return folder

    def move(self, item_id: str, parent_id: str, new_name: str, conflict: str = "fail") -> dict:
        """Move + rename within the drive (keeps the item ID). conflict: fail | rename | replace."""
        if self.dry_run:
            return {"id": item_id, "name": new_name, "webUrl": None, "dryRun": True}
        resp = self._request(
            "PATCH", f"/drives/{self.drive_id}/items/{item_id}",
            params={"@microsoft.graph.conflictBehavior": conflict},
            json={"parentReference": {"id": parent_id}, "name": new_name})
        return resp.json()

    def copy(self, item_id: str, parent_id: str, new_name: str, conflict: str = "fail",
             wait: bool = True, timeout_s: float = 300) -> dict | None:
        """Copy an item. Copy is async in Graph; with ``wait`` poll until the new item exists."""
        if self.dry_run:
            return {"id": f"dry-run-copy:{item_id}", "name": new_name, "webUrl": None, "dryRun": True}
        resp = self._request(
            "POST", f"/drives/{self.drive_id}/items/{item_id}/copy",
            params={"@microsoft.graph.conflictBehavior": conflict},
            json={"parentReference": {"driveId": self.drive_id, "id": parent_id}, "name": new_name})
        monitor = resp.headers.get("Location")
        if not wait or not monitor:
            return None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._request("GET", monitor, auth=False, allow=(303,)).json()
            if status.get("status") == "completed" and status.get("resourceId"):
                return self.get_item(status["resourceId"])
            if status.get("status") == "failed":
                raise GraphError(500, "copyFailed", str(status.get("error", "")), "POST", "copy")
            self._sleep(2)
        raise GraphError(504, "copyTimeout", "copy did not finish in time", "POST", "copy")

    def upload(self, path: str, content: bytes, *, if_match: str | None = None,
               create_only: bool = False) -> dict:
        """Write a file by path. ``if_match`` = eTag guard; ``create_only`` fails if it exists.

        Raises ConflictError when the guard fails so callers can re-read and retry.
        """
        if self.dry_run:
            return {"name": path.rsplit("/", 1)[-1], "dryRun": True}
        headers = {"If-Match": if_match} if if_match else {}
        conflict = "fail" if create_only else "replace"
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            resp = self._request(
                "PUT", f"/drives/{self.drive_id}/root:/{quote_path(path)}:/content",
                params={"@microsoft.graph.conflictBehavior": conflict}, data=content,
                headers={**headers, "Content-Type": "application/octet-stream"})
            return resp.json()
        session = self._request(
            "POST", f"/drives/{self.drive_id}/root:/{quote_path(path)}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}}, headers=headers).json()
        upload_url, total, result = session["uploadUrl"], len(content), {}
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = content[start:start + UPLOAD_CHUNK]
            end = start + len(chunk) - 1
            r = self._request("PUT", upload_url, auth=False, data=chunk,
                              headers={"Content-Range": f"bytes {start}-{end}/{total}"})
            result = self._json(r)
        return result

    def set_fields(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Set library metadata columns that exist; silently skip the rest. Returns what was set."""
        columns = self.list_columns()
        present = {k: v for k, v in fields.items() if k in columns and v not in (None, "")}
        if not present or self.dry_run:
            return present
        self._request("PATCH", f"/drives/{self.drive_id}/items/{item_id}/listItem/fields", json=present)
        return present


def _retry_after(resp, default: float) -> float:
    try:
        return max(1.0, float(resp.headers.get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


def _error_message(resp) -> str:
    try:
        return resp.json().get("error", {}).get("message", "")
    except Exception:
        return ""
