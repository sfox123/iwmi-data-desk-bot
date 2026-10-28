"""Main loop: delta poll -> filter -> download -> extract + classify -> move/rename -> catalog -> notify."""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from dateutil.parser import isoparse

from .catalog import Catalog
from .classify import Classification, classify
from .config import Config, Settings
from .emailparse import EmailContext, parse_eml, split_suffix
from .extract import FileMetadata, extract, kind_for
from .graph import DeltaResyncRequired, GraphClient, is_under, parent_path
from .metbulletin import MetPublisher, run_of, sender_allowed
from .naming import build_stem, file_ext, next_version
from .notify import Notifier, Outcome
from .state import State

log = logging.getLogger("datadesk.intake")

SHAPEFILE_PARTS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fix", ".shp.xml", ".qmd"}
SHAPEFILE_REQUIRED = {".shp", ".shx", ".dbf"}
TEMP_RE = re.compile(r"^~\$|\.tmp$|^\.~lock\.|\.partial$|\.crdownload$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Unit:
    """One logical file to file — a single item, or a shapefile and its sidecars."""
    primary: dict
    members: list[dict]
    original_names: dict[str, str]           # item_id -> name with the mailhook suffix removed
    email: EmailContext | None = None

    @property
    def original_name(self) -> str:
        return self.original_names[self.primary["id"]]

    @property
    def ids(self) -> list[str]:
        return [m["id"] for m in self.members]


@dataclass
class RunResult:
    outcomes: list[Outcome] = field(default_factory=list)
    first_run: bool = False
    recorded: int = 0
    pending: int = 0
    errors: int = 0


class IntakeRunner:
    def __init__(self, cfg: Config, settings: Settings, graph: GraphClient, store, notifier: Notifier, *,
                 dry_run: bool = False, backfill: bool = False, reset_delta: bool = False,
                 now: Callable[[], datetime] = _now, met: MetPublisher | None = None):
        self.cfg = cfg
        self.settings = settings
        self.graph = graph
        self.store = store
        self.notifier = notifier
        self.dry_run = dry_run
        self.backfill = backfill
        self.reset_delta = reset_delta
        self.now = now
        self.met = met if cfg.met else None
        self.catalog = Catalog(graph, cfg.catalog_folder)
        self._ignore = [re.compile(p, re.I) for p in cfg.ignore_patterns]
        self._versions: dict[str, int] = {}

    # ------------------------------------------------------------------ filters
    def _in_intake(self, item: dict) -> bool:
        return any(is_under(parent_path(item), base) for base in self.cfg.intake_paths)

    def _in_mailhook(self, item: dict) -> bool:
        return is_under(parent_path(item), self.cfg.mailhook_path)

    def _is_intake_file(self, item: dict) -> bool:
        return "file" in item and not item.get("deleted") and self._in_intake(item)

    def _original_name(self, item: dict) -> tuple[str, str | None]:
        if self._in_mailhook(item):
            return split_suffix(item["name"])
        return item["name"], None

    def _settling(self, item: dict, prev: dict | None) -> bool:
        """True if the file may still be uploading (recently modified and size not yet seen stable)."""
        modified = item.get("lastModifiedDateTime")
        if not modified:
            return False
        recent = self.now() - isoparse(modified) < timedelta(minutes=self.cfg.upload_settle_minutes)
        if not recent:
            return False
        return not (prev and prev.get("reason") == "settling" and prev.get("size") == item.get("size"))

    # ------------------------------------------------------------------ run
    def run_once(self) -> RunResult:
        result = RunResult()
        state = self.store.load()
        if self.reset_delta:
            state.delta_link, state.pending = None, {}
            self.reset_delta = False
        first_run = not state.delta_link
        backfill = self.backfill
        self.backfill = False

        try:
            changes, delta_link = self.graph.delta(state.delta_link)
        except DeltaResyncRequired:
            log.warning("delta token expired; resyncing and processing anything not seen before")
            changes, delta_link = self.graph.delta(None)
            backfill = True

        candidates: dict[str, dict] = {}
        for item in changes:
            if item.get("deleted"):
                state.pending.pop(item["id"], None)
            elif self._is_intake_file(item):
                candidates[item["id"]] = item

        if first_run and not backfill:
            for item_id in candidates:
                state.processed[item_id] = {"status": "preexisting", "at": self.now().isoformat()}
            state.delta_link = delta_link
            result.first_run, result.recorded = True, len(candidates)
            log.info("first run: recorded existing intake files without processing (use --backfill to process)",
                     extra={"data": {"recorded": len(candidates)}})
            self._save(state)
            return result

        # Items waiting from earlier cycles aren't in this delta page: re-fetch them.
        for item_id in list(state.pending):
            if item_id in candidates:
                continue
            item = self.graph.get_item(item_id)
            if item is None or not self._is_intake_file(item):
                state.pending.pop(item_id, None)
            else:
                candidates[item_id] = item

        self.catalog.load()
        todo = [it for it in candidates.values() if it["id"] not in state.processed]
        ready: list[dict] = []
        for item in todo:
            name, _ = self._original_name(item)
            if TEMP_RE.search(item["name"]):
                continue
            if any(p.search(name) for p in self._ignore):
                state.processed[item["id"]] = {"status": "ignored", "at": self.now().isoformat()}
                state.pending.pop(item["id"], None)
                continue
            if self._settling(item, state.pending.get(item["id"])):
                state.pending[item["id"]] = {**state.pending.get(item["id"], {}), "reason": "settling",
                                             "size": item.get("size")}
                continue
            ready.append(item)

        # Emails first, so attachments in the same run can use their context.
        for item in [i for i in ready if self._in_mailhook(i) and i["name"].lower().endswith(".eml")]:
            ready.remove(item)
            try:
                self._handle_eml(item, state)
            except Exception:
                log.exception("failed to process email", extra={"data": {"item_id": item["id"], "name": item["name"]}})
                self._record_failure(state, [item["id"]], result)

        units = self._build_units(ready, state)
        for unit in units:
            try:
                outcome = self._process_unit(unit)
            except Exception as exc:
                log.exception("failed to process file",
                              extra={"data": {"item_id": unit.primary["id"], "name": unit.original_name}})
                if self._record_failure(state, unit.ids, result):
                    result.outcomes.append(Outcome(status="needs_review", original_name=unit.original_name,
                                                   reasons=[f"processing error: {type(exc).__name__}"],
                                                   uploader=self._uploader(unit)[0]))
                continue
            for item_id in unit.ids:
                state.processed[item_id] = {"status": outcome.status, "at": self.now().isoformat()}
                state.pending.pop(item_id, None)
            result.outcomes.append(outcome)

        self.catalog.commit()
        state.delta_link = delta_link
        self._prune_emails(state)
        result.pending = len(state.pending)
        self._save(state)
        self.notifier.send(result.outcomes)
        log.info("run complete", extra={"data": {
            "processed": len(result.outcomes), "pending": result.pending, "errors": result.errors,
            "dry_run": self.dry_run}})
        return result

    def _save(self, state: State) -> None:
        if not self.dry_run:
            self.store.save(state)

    def _record_failure(self, state: State, ids: list[str], result: RunResult) -> bool:
        """Count a failed attempt. Returns True once the file has used up its attempts."""
        result.errors += 1
        attempts = max(state.pending.get(i, {}).get("attempts", 0) for i in ids) + 1
        if attempts >= self.cfg.max_attempts:
            for i in ids:
                state.processed[i] = {"status": "error", "at": self.now().isoformat()}
                state.pending.pop(i, None)
            return True
        for i in ids:
            state.pending[i] = {**state.pending.get(i, {}), "reason": "error", "attempts": attempts}
        return False

    def _prune_emails(self, state: State, keep_days: int = 31) -> None:
        cutoff = (self.now() - timedelta(days=keep_days)).isoformat()
        for key, ctx in list(state.emails.items()):
            if (ctx.get("seen_at") or "") < cutoff:
                state.emails.pop(key)

    # ------------------------------------------------------------------ emails
    def _handle_eml(self, item: dict, state: State) -> None:
        _, key = split_suffix(item["name"])
        with tempfile.TemporaryDirectory(prefix="datadesk-") as tmp:
            path = self.graph.download(item["id"], Path(tmp) / "message.eml")
            ctx = parse_eml(path.read_bytes())
        if key:
            state.emails[key] = {**ctx.to_dict(), "seen_at": self.now().isoformat()}
        if self.cfg.emails_folder:
            folder = self.graph.ensure_folder(self.cfg.emails_folder)
            self.graph.move(item["id"], folder["id"], item["name"], conflict="rename")
        state.processed[item["id"]] = {"status": "email", "at": self.now().isoformat()}
        state.pending.pop(item["id"], None)
        log.info("email parsed", extra={"data": {"item_id": item["id"], "subject": ctx.subject,
                                                 "from": ctx.from_email, "suffix": key}})

    # ------------------------------------------------------------------ grouping
    def _build_units(self, items: list[dict], state: State) -> list[Unit]:
        groups: dict[tuple, list[dict]] = {}
        singles: list[dict] = []
        names: dict[str, str] = {}
        keys: dict[str, str | None] = {}
        for item in items:
            name, key = self._original_name(item)
            names[item["id"]], keys[item["id"]] = name, key
            ext = file_ext(name)
            if ext in SHAPEFILE_PARTS:
                stem = name[: len(name) - len(ext)].lower()
                groups.setdefault((parent_path(item), stem, key), []).append(item)
            else:
                singles.append(item)

        units: list[Unit] = []
        for members in groups.values():
            exts = {file_ext(names[m["id"]]) for m in members}
            if not SHAPEFILE_REQUIRED <= exts and not self._waited_enough(members, state, "shapefile_incomplete"):
                continue
            primary = next((m for m in members if file_ext(names[m["id"]]) == ".shp"), members[0])
            units.append(Unit(primary, members, {m["id"]: names[m["id"]] for m in members}))
        for item in singles:
            units.append(Unit(item, [item], {item["id"]: names[item["id"]]}))

        ready: list[Unit] = []
        for unit in units:
            key = keys[unit.primary["id"]]
            if self._in_mailhook(unit.primary) and key:
                ctx = state.emails.get(key)
                if ctx is None and not self._waited_enough(unit.members, state, "awaiting_email"):
                    continue
                unit.email = EmailContext.from_dict(ctx) if ctx else None
            ready.append(unit)
        return ready

    def _waited_enough(self, members: list[dict], state: State, reason: str) -> bool:
        """Park items for up to ``email_wait_cycles`` cycles; True once they've waited long enough."""
        cycles = max(state.pending.get(m["id"], {}).get("cycles", 0) for m in members)
        if cycles >= self.cfg.email_wait_cycles:
            return True
        for m in members:
            state.pending[m["id"]] = {**state.pending.get(m["id"], {}), "reason": reason,
                                      "cycles": cycles + 1, "size": m.get("size")}
        return False

    # ------------------------------------------------------------------ processing
    def _uploader(self, unit: Unit) -> tuple[str | None, str | None]:
        if unit.email and (unit.email.from_name or unit.email.from_email):
            return unit.email.from_name or unit.email.from_email, unit.email.from_email or None
        user = (unit.primary.get("createdBy") or {}).get("user") or {}
        return user.get("displayName"), user.get("email")

    def _inspect(self, unit: Unit) -> FileMetadata:
        total = sum(int(m.get("size") or 0) for m in unit.members)
        if total > self.settings.max_download_mb * 1024 * 1024:
            log.info("file above MAX_DOWNLOAD_MB; classifying by name/email only",
                     extra={"data": {"name": unit.original_name, "size_bytes": total}})
            kind = kind_for(unit.original_name)
            return FileMetadata(kind="unknown" if kind == "zip" else kind)
        with tempfile.TemporaryDirectory(prefix="datadesk-") as tmp:
            for m in unit.members:
                self.graph.download(m["id"], Path(tmp) / unit.original_names[m["id"]])
            return extract(Path(tmp) / unit.original_name)

    def _process_unit(self, unit: Unit) -> Outcome:
        quick_hash = ((unit.primary.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        dup = self.catalog.find_by_hash(quick_hash)
        if dup:
            return self._file_duplicate(unit, dup, quick_hash)

        if self.met and file_ext(unit.original_name) == ".pdf":
            outcome = self._file_met_bulletin(unit, quick_hash)
            if outcome:
                return outcome

        meta = self._inspect(unit)
        cls = classify(meta, unit.original_name, unit.email, self.cfg)
        if cls.status != "filed":
            return self._file_needs_review(unit, meta, cls, quick_hash)
        return self._file_dataset(unit, meta, cls, quick_hash)

    def _relocate(self, item: dict, folder_id: str, name: str, conflict: str) -> dict:
        if self.cfg.move_mode == "copy":
            return self.graph.copy(item["id"], folder_id, name, conflict=conflict) or {"id": item["id"], "name": name}
        return self.graph.move(item["id"], folder_id, name, conflict=conflict)

    def _next_version(self, folder_path: str) -> int:
        existing = self.graph.get_item_by_path(folder_path)
        names = [c["name"] for c in self.graph.list_children(existing["id"])] if existing and "folder" in existing else []
        version = max(next_version(names), self._versions.get(folder_path, 0) + 1)
        self._versions[folder_path] = version
        return version

    def _file_dataset(self, unit: Unit, meta: FileMetadata, cls: Classification, quick_hash) -> Outcome:
        dataset_path = f"{self.cfg.data_root}/{_seg(cls.category)}/{_seg(cls.region)}/{_seg(cls.dataset)}"
        version = self._next_version(dataset_path)
        target_path = f"{dataset_path}/v{version}"
        stem = build_stem(cls.variable, cls.source, cls.region, cls.time_start, cls.time_end, version)
        folder = self.graph.ensure_folder(target_path)

        moved_primary = None
        for m in unit.members:
            new_name = stem + file_ext(unit.original_names[m["id"]])
            moved = self._relocate(m, folder["id"], new_name, conflict="fail")
            if m is unit.primary:
                moved_primary = moved
        new_name = moved_primary.get("name") or stem + file_ext(unit.original_name)
        uploader, uploader_email = self._uploader(unit)

        if self.cfg.metadata_columns and moved_primary.get("id"):
            self.graph.set_fields(moved_primary["id"], {
                "Category": cls.category, "Region": cls.region, "Variable": cls.variable,
                "Source": cls.source, "TimeStart": cls.time_start, "TimeEnd": cls.time_end,
                "Uploader": uploader, "DatasetVersion": version})
        if self.cfg.latest_copy and moved_primary.get("id"):
            latest = self.graph.ensure_folder(f"{dataset_path}/latest")
            latest_name = re.sub(r"_v\d+(?=\.)", "", new_name)
            self.graph.copy(moved_primary["id"], latest["id"], latest_name, conflict="replace", wait=False)

        self._catalog_row(moved_primary, unit, meta, cls, quick_hash, "filed", target_path, new_name,
                          version, uploader, uploader_email)
        self._log_decision(unit, "filed", cls.confidence, f"{target_path}/{new_name}", cls)
        return Outcome(status="filed", original_name=unit.original_name, new_name=new_name, path=target_path,
                       folder_url=folder.get("webUrl"), file_url=moved_primary.get("webUrl"),
                       category=cls.category, region=cls.region, dataset=cls.dataset, version=version,
                       variables=_variable_names(meta), time_start=cls.time_start, time_end=cls.time_end,
                       uploader=uploader, confidence=cls.confidence)

    def _file_met_bulletin(self, unit: Unit, quick_hash) -> Outcome | None:
        """Publish a Met Dept bulletin to AWARE and file it. None if it isn't one (normal filing follows)."""
        run = run_of(unit.original_name, unit.email)
        if not run:
            return None
        sender = unit.email.from_email if unit.email else None
        if not sender_allowed(unit.email, self._in_mailhook(unit.primary), self.cfg.met.allowed_senders):
            log.info("met bulletin: sender not allowed, filing as usual",
                     extra={"data": {"name": unit.original_name, "from": sender}})
            return None
        with tempfile.TemporaryDirectory(prefix="datadesk-") as tmp:
            path = self.graph.download(unit.primary["id"], Path(tmp) / f"{run}.pdf")
            res = self.met.publish(path, run)
        if res.status == "rejected":
            log.info("met bulletin: not accepted by AWARE, filing as usual",
                     extra={"data": {"name": unit.original_name, "run": run, "error": res.error}})
            return None

        s = res.summary
        note = {"published": "Published to the forecast maps",
                "checked": "Checked OK (dry-run: not published)",
                "exists": "Already on the site, left unchanged (replace it from /upload if needed)"}[res.status]
        note += f" · {s.get('days')} daily maps, {s.get('from')} → {s.get('to')}"
        target_path = f"{self.cfg.met.folder}/{run[:4]}"
        folder = self.graph.ensure_folder(target_path)
        moved = self._relocate(unit.primary, folder["id"], f"{run}.pdf", conflict="rename")
        new_name = moved.get("name") or f"{run}.pdf"
        uploader, uploader_email = self._uploader(unit)
        self.catalog.upsert({
            "item_id": moved.get("id") or unit.primary["id"], "name": new_name,
            "original_name": unit.original_name, "web_url": moved.get("webUrl"),
            "path": f"{target_path}/{new_name}", "category": "Climate", "region": "Sri Lanka",
            "dataset": "DoM_15day_forecast", "variables": ["rainfall_24h", "tmax_2m"],
            "time_start": s.get("from"), "time_end": s.get("to"),
            "size_bytes": unit.primary.get("size"), "quick_xor_hash": quick_hash, "uploader": uploader,
            "uploader_email": uploader_email, "email_subject": unit.email.subject if unit.email else None,
            "filed_at": self.now().isoformat(timespec="seconds"), "confidence": 1.0, "status": "filed",
        })
        self._log_decision(unit, "met_bulletin", 1.0, f"{target_path}/{new_name}", None,
                           run=run, aware=res.status, warnings=s.get("warnings"))
        return Outcome(status="filed", original_name=unit.original_name, new_name=new_name, path=target_path,
                       folder_url=folder.get("webUrl"), file_url=moved.get("webUrl"), category="Climate",
                       region="Sri Lanka", dataset="DoM_15day_forecast", time_start=s.get("from"),
                       time_end=s.get("to"), uploader=uploader, confidence=1.0,
                       note=f"{note} · run {s.get('init') or run}", site_url=self.cfg.met.site_url)

    def _file_needs_review(self, unit: Unit, meta: FileMetadata, cls: Classification, quick_hash) -> Outcome:
        folder = self.graph.ensure_folder(self.cfg.needs_review)
        moved_primary = None
        for m in unit.members:
            moved = self._relocate(m, folder["id"], unit.original_names[m["id"]], conflict="rename")
            if m is unit.primary:
                moved_primary = moved
        new_name = moved_primary.get("name") or unit.original_name
        uploader, uploader_email = self._uploader(unit)
        self._catalog_row(moved_primary, unit, meta, cls, quick_hash, "needs_review", self.cfg.needs_review,
                          new_name, None, uploader, uploader_email)
        self._log_decision(unit, "needs_review", cls.confidence, f"{self.cfg.needs_review}/{new_name}", cls)
        return Outcome(status="needs_review", original_name=unit.original_name, new_name=new_name,
                       path=self.cfg.needs_review, folder_url=folder.get("webUrl"),
                       file_url=moved_primary.get("webUrl"), category=cls.category, region=cls.region,
                       dataset=cls.dataset, uploader=uploader, confidence=cls.confidence, reasons=cls.reasons)

    def _file_duplicate(self, unit: Unit, dup: dict, quick_hash) -> Outcome:
        folder = self.graph.ensure_folder(self.cfg.duplicates)
        moved_primary = None
        for m in unit.members:
            moved = self._relocate(m, folder["id"], unit.original_names[m["id"]], conflict="rename")
            if m is unit.primary:
                moved_primary = moved
        new_name = moved_primary.get("name") or unit.original_name
        uploader, uploader_email = self._uploader(unit)
        self.catalog.upsert({
            "item_id": moved_primary.get("id") or unit.primary["id"], "name": new_name,
            "original_name": unit.original_name, "web_url": moved_primary.get("webUrl"),
            "path": f"{self.cfg.duplicates}/{new_name}", "category": dup.get("category"),
            "region": dup.get("region"), "dataset": dup.get("dataset"), "version": dup.get("version"),
            "size_bytes": unit.primary.get("size"), "quick_xor_hash": quick_hash, "uploader": uploader,
            "uploader_email": uploader_email, "email_subject": unit.email.subject if unit.email else None,
            "filed_at": self.now().isoformat(timespec="seconds"), "confidence": 1.0, "status": "duplicate",
        })
        self._log_decision(unit, "duplicate", 1.0, f"{self.cfg.duplicates}/{new_name}", None,
                           duplicate_of=dup.get("path"))
        return Outcome(status="duplicate", original_name=unit.original_name, new_name=new_name,
                       path=self.cfg.duplicates, folder_url=folder.get("webUrl"), uploader=uploader,
                       duplicate_of=dup.get("path") or dup.get("name"), duplicate_url=dup.get("web_url"))

    def _catalog_row(self, moved: dict, unit: Unit, meta: FileMetadata, cls: Classification, quick_hash,
                     status: str, folder_path: str, new_name: str, version, uploader, uploader_email) -> None:
        self.catalog.upsert({
            "item_id": moved.get("id") or unit.primary["id"],
            "name": new_name,
            "original_name": unit.original_name,
            "web_url": moved.get("webUrl"),
            "path": f"{folder_path}/{new_name}",
            "category": cls.category,
            "region": cls.region,
            "dataset": cls.dataset,
            "version": version,
            "variables": _variable_names(meta),
            "units": [v.units or "" for v in meta.variables],
            "standard_names": [v.standard_name or "" for v in meta.variables],
            "time_start": cls.time_start,
            "time_end": cls.time_end,
            "bbox": list(meta.bbox) if meta.bbox else None,
            "crs": meta.crs,
            "resolution": meta.resolution,
            "size_bytes": sum(int(m.get("size") or 0) for m in unit.members),
            "quick_xor_hash": quick_hash,
            "uploader": uploader,
            "uploader_email": uploader_email,
            "email_subject": unit.email.subject if unit.email else None,
            "filed_at": self.now().isoformat(timespec="seconds"),
            "confidence": cls.confidence,
            "status": status,
        })

    def _log_decision(self, unit: Unit, decision: str, confidence, target: str, cls: Classification | None,
                      **extra) -> None:
        data = {"item_id": unit.primary["id"], "original_name": unit.original_name, "decision": decision,
                "confidence": confidence, "target": target, "dry_run": self.dry_run, **extra}
        if cls:
            data.update({"category": cls.category, "region": cls.region, "region_method": cls.region_method,
                         "dataset": cls.dataset, "variable": cls.variable, "scores": cls.scores,
                         "reasons": cls.reasons})
        log.info("file decision", extra={"data": data})


def _seg(value: str | None) -> str:
    from .naming import ascii_clean
    return ascii_clean(value or "Unknown") or "Unknown"


def _variable_names(meta: FileMetadata) -> list[str]:
    return [v.name for v in meta.variables] or meta.fields[:30]
