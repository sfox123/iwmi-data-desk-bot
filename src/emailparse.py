"""Pair Teams Mailhook attachments with their .eml and parse the email."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

# Teams saves "<Subject>_<N>.eml" and attachments as "<originalname>_<N>.<ext>".
SUFFIX_RE = re.compile(r"_(\d{5,})(\.[^.]+)$")
MAX_BODY_CHARS = 5000


def split_suffix(name: str) -> tuple[str, str | None]:
    """'rain_973918918.csv' -> ('rain.csv', '973918918'). Names without a suffix are unchanged."""
    m = SUFFIX_RE.search(name)
    if not m:
        return name, None
    return name[:m.start()] + m.group(2), m.group(1)


@dataclass
class EmailContext:
    from_name: str = ""
    from_email: str = ""
    subject: str = ""
    date: str | None = None
    body: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EmailContext":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def parse_eml(data: bytes) -> EmailContext:
    msg = BytesParser(policy=policy.default).parsebytes(data)

    from_name = from_email = ""
    sender = msg.get("from")
    if sender is not None and getattr(sender, "addresses", None):
        addr = sender.addresses[0]
        from_name, from_email = addr.display_name or "", addr.addr_spec or ""

    date = None
    if msg.get("date"):
        try:
            date = parsedate_to_datetime(str(msg["date"])).isoformat()
        except (TypeError, ValueError):
            date = None

    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True).decode("utf-8", errors="replace")
        body = _html_to_text(content) if part.get_content_type() == "text/html" else content
    body = re.sub(r"[ \t]+", " ", body).strip()[:MAX_BODY_CHARS]

    return EmailContext(from_name=from_name, from_email=from_email,
                        subject=str(msg.get("subject") or "").strip(), date=date, body=body)
