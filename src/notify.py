"""Adaptive Cards to the Data Drop channel via a Workflows (Power Automate) webhook."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

ICONS = {"filed": "✅", "needs_review": "⚠️", "duplicate": "♻️"}
TITLES = {"filed": "Filed", "needs_review": "Needs review", "duplicate": "Duplicate"}


@dataclass
class Outcome:
    status: str                      # filed | needs_review | duplicate
    original_name: str
    new_name: str | None = None
    path: str | None = None          # folder path the file now lives in
    folder_url: str | None = None
    file_url: str | None = None
    category: str | None = None
    region: str | None = None
    dataset: str | None = None
    version: int | None = None
    variables: list[str] = field(default_factory=list)
    time_start: str | None = None
    time_end: str | None = None
    uploader: str | None = None
    confidence: float | None = None
    reasons: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    duplicate_url: str | None = None
    note: str | None = None          # e.g. "Published to the forecast maps" for a Met bulletin
    site_url: str | None = None      # opened by the card's "Open AWARE" button


def _facts(pairs) -> dict:
    return {"type": "FactSet", "facts": [{"title": k, "value": str(v)} for k, v in pairs if v not in (None, "", [])]}


def _time(o: Outcome) -> str | None:
    if o.time_start or o.time_end:
        return f"{(o.time_start or '?')[:10]} → {(o.time_end or '?')[:10]}"
    return None


def _card(body: list, actions: list) -> dict:
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body}
    if actions:
        card["actions"] = actions
    return card


def _open(title: str, url: str | None) -> list:
    return [{"type": "Action.OpenUrl", "title": title, "url": url}] if url else []


def build_card(o: Outcome) -> dict:
    header = {"type": "TextBlock", "weight": "Bolder", "size": "Medium", "wrap": True,
              "text": f"{ICONS[o.status]} {TITLES[o.status]}: {o.new_name or o.original_name}"}
    if o.status == "filed":
        facts = _facts([("Original", o.original_name), ("Folder", o.path),
                        ("Variables", ", ".join(o.variables[:8])), ("Time range", _time(o)),
                        ("Region", o.region), ("Version", f"v{o.version}" if o.version else None),
                        ("Uploaded by", o.uploader), ("Confidence", f"{o.confidence:.2f}" if o.confidence is not None else None),
                        ("AWARE", o.note)])
        return _card([header, facts], _open("Open folder", o.folder_url) + _open("Open file", o.file_url)
                     + _open("Open AWARE", o.site_url))
    if o.status == "duplicate":
        facts = _facts([("Original", o.original_name), ("Already filed as", o.duplicate_of),
                        ("Moved to", o.path), ("Uploaded by", o.uploader)])
        return _card([header, facts], _open("Open existing file", o.duplicate_url))
    facts = _facts([("Original", o.original_name), ("Why", "; ".join(o.reasons) or "unclassified"),
                    ("Best guess", " / ".join(filter(None, [o.category, o.region, o.dataset]))),
                    ("Moved to", o.path), ("Uploaded by", o.uploader)])
    return _card([header, facts], _open("Open needs-review", o.folder_url))


def build_summary_card(outcomes: list[Outcome]) -> dict:
    counts = {s: sum(1 for o in outcomes if o.status == s) for s in ICONS}
    headline = ", ".join(f"{ICONS[s]} {n} {TITLES[s].lower()}" for s, n in counts.items() if n)
    body = [{"type": "TextBlock", "weight": "Bolder", "size": "Medium", "wrap": True,
             "text": f"Data Desk processed {len(outcomes)} files"},
            {"type": "TextBlock", "text": headline, "wrap": True}]
    lines = []
    for o in outcomes[:40]:
        where = o.path or ""
        lines.append(f"{ICONS[o.status]} **{o.original_name}** → {o.new_name or o.original_name} ({where})")
    if len(outcomes) > 40:
        lines.append(f"… and {len(outcomes) - 40} more")
    body.append({"type": "TextBlock", "text": "\n\n".join(lines), "wrap": True})
    return _card(body, [])


def wrap(card: dict) -> dict:
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


class Notifier:
    def __init__(self, webhook_url: str | None, *, dry_run: bool = False, summary_threshold: int = 5,
                 session: requests.Session | None = None):
        self.webhook_url = webhook_url
        self.dry_run = dry_run
        self.summary_threshold = summary_threshold
        self.session = session or requests.Session()

    def send(self, outcomes: list[Outcome]) -> int:
        if not outcomes:
            return 0
        if len(outcomes) > self.summary_threshold:
            cards = [build_summary_card(outcomes)]
        else:
            cards = [build_card(o) for o in outcomes]
        for card in cards:
            self._post(card)
        return len(cards)

    def _post(self, card: dict) -> None:
        title = card["body"][0].get("text")
        if self.dry_run or not self.webhook_url:
            log.info("card not sent (%s)" % ("dry-run" if self.dry_run else "no TEAMS_WEBHOOK_URL"),
                     extra={"data": {"card_title": title}})
            return
        try:
            resp = self.session.post(self.webhook_url, json=wrap(card), timeout=30)
            if resp.status_code >= 400:
                log.warning("teams webhook rejected card", extra={"data": {"status": resp.status_code}})
        except requests.RequestException as exc:
            log.warning("teams webhook failed", extra={"data": {"error": type(exc).__name__}})
