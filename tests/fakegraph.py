"""In-memory fake of the Graph endpoints the service uses, plugged in as a requests session."""
from __future__ import annotations

import base64
import hashlib
import itertools
import json as jsonlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

GRAPH = "https://graph.microsoft.com/v1.0"
DOWNLOAD_HOST = "https://download.fake"
WEBHOOK = "https://webhook.fake/post"


class FakeResponse:
    def __init__(self, status: int = 200, body=None, content: bytes | None = None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.content = content if content is not None else (jsonlib.dumps(body).encode() if body is not None else b"")

    def json(self):
        return self._body if self._body is not None else jsonlib.loads(self.content or b"{}")

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


def _error(status, code="error", message=""):
    return FakeResponse(status, {"error": {"code": code, "message": message}})


class FakeDrive:
    def __init__(self, drive_id="D", site_id="S", columns=()):
        self.drive_id, self.site_id = drive_id, site_id
        self.columns = set(columns)
        self._ids = (f"item{n}" for n in itertools.count(1))
        self._seq = itertools.count(1)
        self.items: dict[str, dict] = {}
        self.root = self._new(None, None, folder=True)

    # ---------------------------------------------------------------- model
    def _new(self, parent, name, folder=False, content=b"", created_by=("Alice Tester", "alice@cgiar.org"),
             modified=None):
        item_id = "root" if parent is None and name is None else next(self._ids)
        item = {"id": item_id, "name": name, "parent": parent, "folder": folder, "content": content,
                "deleted": False, "seq": next(self._seq), "created_by": created_by, "fields": {},
                "modified": modified or datetime.now(timezone.utc) - timedelta(hours=1), "etag": 1}
        self.items[item_id] = item
        return item

    def _touch(self, item):
        item["seq"] = next(self._seq)
        item["etag"] += 1

    def path_of(self, item) -> str:
        parts = []
        while item["parent"] is not None:
            parts.append(item["name"])
            item = self.items[item["parent"]]
        return "/".join(reversed(parts))

    def children(self, item_id):
        return [i for i in self.items.values() if i["parent"] == item_id and not i["deleted"]]

    def find(self, path):
        item = self.root
        for part in [p for p in path.strip("/").split("/") if p]:
            item = next((c for c in self.children(item["id"]) if c["name"].lower() == part.lower()), None)
            if item is None:
                return None
        return item

    def mkdirs(self, path):
        item = self.root
        for part in [p for p in path.strip("/").split("/") if p]:
            child = next((c for c in self.children(item["id"]) if c["name"].lower() == part.lower()), None)
            item = child or self._new(item["id"], part, folder=True)
        return item

    def add_file(self, path, content: bytes, **kw):
        folder, _, name = path.strip("/").rpartition("/")
        parent = self.mkdirs(folder)
        return self._new(parent["id"], name, content=content, **kw)["id"]

    def files_under(self, path):
        base = path.strip("/").lower()
        return sorted(self.path_of(i) for i in self.items.values()
                      if not i["folder"] and not i["deleted"] and self.path_of(i).lower().startswith(base + "/"))

    @staticmethod
    def qxh(content: bytes) -> str:
        return base64.b64encode(hashlib.sha1(content).digest()).decode()[:28]

    def to_drive_item(self, item):
        out = {"id": item["id"], "name": item["name"] or "root", "eTag": f'"{item["id"]},{item["etag"]}"',
               "webUrl": f"https://sp.fake/{self.path_of(item)}",
               "lastModifiedDateTime": item["modified"].isoformat().replace("+00:00", "Z"),
               "createdBy": {"user": {"displayName": item["created_by"][0], "email": item["created_by"][1]}}}
        if item["parent"] is not None:
            parent = self.items[item["parent"]]
            ppath = self.path_of(parent)
            out["parentReference"] = {"driveId": self.drive_id, "id": parent["id"],
                                      "path": f"/drives/{self.drive_id}/root:" + (f"/{ppath}" if ppath else "")}
        else:
            out["root"] = {}
        if item["deleted"]:
            out["deleted"] = {"state": "deleted"}
        if item["folder"]:
            out["folder"] = {"childCount": len(self.children(item["id"]))}
        else:
            out["size"] = len(item["content"])
            out["file"] = {"hashes": {"quickXorHash": self.qxh(item["content"])}}
            out["@microsoft.graph.downloadUrl"] = f"{DOWNLOAD_HOST}/{item['id']}?tempauth=secret"
        return out


class FakeSession:
    """Routes requests to a FakeDrive. Records every call in ``calls`` as (method, url)."""

    def __init__(self, drive: FakeDrive):
        self.drive = drive
        self.calls: list[tuple[str, str]] = []
        self.webhook_posts: list[dict] = []
        self.fail_next: list[int] = []        # status codes to return before routing
        self.valid_tokens: set[str] | None = None

    # notifier uses session.post
    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url))
        self.webhook_posts.append(json)
        return FakeResponse(202, {})

    def request(self, method, url, params=None, json=None, data=None, headers=None, stream=False, timeout=None):
        self.calls.append((method, url))
        headers = headers or {}
        if self.fail_next:
            return _error(self.fail_next.pop(0), "transient", "")
        auth = headers.get("Authorization")
        if self.valid_tokens is not None and url.startswith(GRAPH) and auth not in self.valid_tokens:
            return _error(401, "InvalidAuthenticationToken", "expired")
        if url.startswith(DOWNLOAD_HOST):
            item = self.drive.items.get(urlsplit(url).path.strip("/"))
            return FakeResponse(200, content=item["content"]) if item else _error(404)
        return self._route(method, url, params or {}, json, data, headers)

    def _route(self, method, url, params, body, data, headers):
        d = self.drive
        parts = urlsplit(url)
        path = unquote(parts.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query.update(params)
        prefix = f"/v1.0/drives/{d.drive_id}"
        conflict = query.get("@microsoft.graph.conflictBehavior", "fail")

        if path == f"/v1.0/sites/{d.site_id}/lists/L/columns":
            return FakeResponse(200, {"value": [{"name": c} for c in sorted(d.columns)]})
        if not path.startswith(prefix):
            return _error(400, "badRequest", path)
        rest = path[len(prefix):]

        if rest == "/root/delta":
            token = int(query.get("token", 0))
            changed = [i for i in d.items.values() if i["seq"] > token and i["parent"] is not None]
            latest = max((i["seq"] for i in d.items.values()), default=0)
            return FakeResponse(200, {"value": [d.to_drive_item(i) for i in changed],
                                      "@odata.deltaLink": f"{GRAPH}/drives/{d.drive_id}/root/delta?token={latest}"})
        if rest == "/list":
            return FakeResponse(200, {"id": "L"})
        if rest == "/root" and method == "GET":
            return FakeResponse(200, d.to_drive_item(d.root))

        m = re.fullmatch(r"/root:/(.+?):/content", rest)
        if m and method == "PUT":
            existing = d.find(m.group(1))
            if existing and conflict == "fail":
                return _error(409, "nameAlreadyExists")
            if headers.get("If-Match") and (not existing or headers["If-Match"] != d.to_drive_item(existing)["eTag"]):
                return _error(412, "preconditionFailed")
            if existing:
                existing["content"] = data
                d._touch(existing)
                return FakeResponse(200, d.to_drive_item(existing))
            return FakeResponse(201, d.to_drive_item(d.items[d.add_file(m.group(1), data)]))
        m = re.fullmatch(r"/root:/(.+)", rest)
        if m and method == "GET":
            item = d.find(m.group(1))
            return FakeResponse(200, d.to_drive_item(item)) if item else _error(404, "itemNotFound")

        m = re.fullmatch(r"/items/([^/]+)(/.*)?", rest)
        if not m:
            return _error(400, "badRequest", rest)
        item = d.items.get(m.group(1))
        sub = m.group(2) or ""
        if item is None or item["deleted"]:
            return _error(404, "itemNotFound")

        if method == "GET" and sub == "":
            return FakeResponse(200, d.to_drive_item(item))
        if method == "GET" and sub == "/children":
            return FakeResponse(200, {"value": [d.to_drive_item(c) for c in d.children(item["id"])]})
        if method == "GET" and sub == "/content":
            return FakeResponse(200, content=item["content"])
        if method == "POST" and sub == "/children":
            if any(c["name"].lower() == body["name"].lower() for c in d.children(item["id"])):
                return _error(409, "nameAlreadyExists")
            return FakeResponse(201, d.to_drive_item(d._new(item["id"], body["name"], folder=True)))
        if method == "PATCH" and sub == "":
            parent_id = body.get("parentReference", {}).get("id", item["parent"])
            name = body.get("name", item["name"])
            clash = [c for c in d.children(parent_id) if c["name"].lower() == name.lower() and c is not item]
            if clash:
                if conflict == "fail":
                    return _error(409, "nameAlreadyExists")
                stem, dot, ext = name.rpartition(".")
                n = 1
                while any(c["name"].lower() == name.lower() for c in d.children(parent_id)):
                    name = f"{stem} {n}.{ext}" if dot else f"{name} {n}"
                    n += 1
            item["parent"], item["name"] = parent_id, name
            d._touch(item)
            return FakeResponse(200, d.to_drive_item(item))
        if method == "PATCH" and sub == "/listItem/fields":
            unknown = set(body) - d.columns
            if unknown:
                return _error(400, "invalidRequest", f"Field(s) not recognized: {unknown}")
            item["fields"].update(body)
            return FakeResponse(200, item["fields"])
        return _error(400, "badRequest", f"{method} {rest}")

    def writes(self):
        return [(m, u) for m, u in self.calls if m != "GET"]


class FakeTokens:
    def __init__(self):
        self.n = 1
        self.invalidations = 0

    def get_token(self):
        return f"tok-{self.n}"

    def invalidate(self):
        self.invalidations += 1
        self.n += 1
