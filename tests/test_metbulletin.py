"""Met Dept bulletins sent to Data Drop are published to AWARE Sri Lanka, then filed."""
from __future__ import annotations

import pytest

from src.emailparse import EmailContext
from src.graph import GraphClient
from src.intake import IntakeRunner
from src.metbulletin import MetPublisher, run_of, sender_allowed
from src.notify import Notifier
from tests.conftest import Env
from tests.fakegraph import FakeResponse

MAIL = "Apps/Microsoft Teams Mailhook/Data Drop/EmailMessages_10_2026"
FILED = "Data/Climate/Sri Lanka/DoM_15day_forecast/2026/20261027_00Z.pdf"
PDF = b"%PDF-1.3 fake bulletin"
SUMMARY = {"run": "20261027_00Z", "init": "00 UTC 27 Oct 2026", "days": 14, "from": "2026-10-28",
           "to": "2026-11-10", "top": [], "warnings": [], "exists": False}


def eml(sender: str) -> bytes:
    return (f"From: Met Forecast <{sender}>\r\nTo: channel@emea.teams.ms\r\nSubject: 15 day forecast\r\n"
            "Date: Tue, 27 Oct 2026 09:00:00 +0000\r\nContent-Type: text/plain\r\n\r\nPlease find attached.\r\n").encode()


class FakeAware:
    """Stands in for AWARE's /api/met/upload. ``reply(publish)`` gives (status, body)."""

    def __init__(self, reply=None):
        self.calls: list[dict] = []
        self.reply = reply or (lambda publish: (200, {**SUMMARY, "published": publish}))

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        publish = (data or {}).get("publish") == "1"
        self.calls.append({"run": data["run"], "publish": publish, "auth": headers["Authorization"],
                           "pdf": files["file"][1].read()})
        status, body = self.reply(publish)
        return FakeResponse(status, body)


class MetEnv(Env):
    def __init__(self, *a, aware=None, **kw):
        super().__init__(*a, **kw)
        self.aware = aware or FakeAware()

    def runner(self, dry_run=None, backfill=False):
        dry = self.dry_run if dry_run is None else dry_run
        graph = GraphClient(self.tokens, "D", "S", dry_run=dry, session=self.session, sleep=lambda s: None)
        notifier = Notifier(self.settings.teams_webhook_url, dry_run=dry, session=self.session,
                            summary_threshold=self.cfg.summary_card_threshold)
        met = MetPublisher(self.cfg.met.upload_url, "k" * 32, dry_run=dry, session=self.aware)
        return IntakeRunner(self.cfg, self.settings, graph, self.store, notifier, dry_run=dry,
                            backfill=backfill, met=met)


@pytest.fixture
def make(cfg, settings, tmp_path):
    def _make(aware=None):
        e = MetEnv(cfg, settings, tmp_path, aware=aware)
        e.run()
        return e
    return _make


def drop_email(env, sender="forecast@meteo.gov.lk", name="20261027_00Z", content=PDF):
    env.drive.add_file(f"{MAIL}/15 day forecast_555111222.eml", eml(sender), created_by=("SharePoint App", None))
    env.drive.add_file(f"{MAIL}/{name}_555111222.pdf", content, created_by=("SharePoint App", None))


def test_emailed_bulletin_is_published_and_filed(make):
    env = make()
    drop_email(env)
    result = env.run()

    assert [(c["run"], c["publish"]) for c in env.aware.calls] == [("20261027_00Z", False), ("20261027_00Z", True)]
    assert env.aware.calls[0]["pdf"] == PDF and env.aware.calls[0]["auth"] == "Bearer " + "k" * 32
    assert env.drive.files_under("Data") == [FILED]
    o = result.outcomes[0]
    assert (o.status, o.uploader) == ("filed", "Met Forecast")
    assert o.note.startswith("Published to the forecast maps")
    card = env.session.webhook_posts[-1]["attachments"][0]["content"]
    assert any(f["title"] == "AWARE" for f in card["body"][1]["facts"])
    assert any(a["title"] == "Open AWARE" for a in card["actions"])


def test_file_aware_rejects_is_filed_as_usual(make):
    env = make(FakeAware(lambda publish: (422, {"error": "Could not read this bulletin: not a PDF file"})))
    drop_email(env)
    result = env.run()

    assert [c["publish"] for c in env.aware.calls] == [False]
    assert result.outcomes[0].status == "needs_review"
    assert env.drive.files_under("Data") == []


def test_sender_not_allowed_is_not_sent_to_aware(make):
    env = make()
    drop_email(env, sender="someone@example.com")
    result = env.run()

    assert env.aware.calls == []
    assert result.outcomes[0].status == "needs_review"


def test_already_published_run_is_not_replaced(make):
    env = make(FakeAware(lambda publish: (200, {**SUMMARY, "exists": True, "published": False})))
    drop_email(env)
    result = env.run()

    assert [c["publish"] for c in env.aware.calls] == [False]
    assert env.drive.files_under("Data") == [FILED]
    assert result.outcomes[0].note.startswith("Already on the site")


def test_aware_down_is_retried_then_given_up(make):
    env = make(FakeAware(lambda publish: (503, {"error": "down"})))
    drop_email(env)

    first = env.run()
    assert first.errors == 1 and first.outcomes == []
    assert env.drive.files_under(MAIL) == [f"{MAIL}/20261027_00Z_555111222.pdf"]  # left in place for a retry
    env.run()
    last = env.run()
    assert [o.status for o in last.outcomes] == ["needs_review"]
    assert len(env.aware.calls) == 3


def test_dry_run_only_checks(make):
    env = make()
    drop_email(env)
    result = env.run(dry_run=True)

    assert [c["publish"] for c in env.aware.calls] == [False]
    assert result.outcomes[0].note.startswith("Checked OK")
    assert env.drive.files_under("Data") == []


def test_run_and_sender_rules():
    assert run_of("20261027_00Z.pdf") == "20261027_00Z"
    assert run_of("20261027_00Z (2).pdf") == "20261027_00Z"
    assert run_of("forecast.pdf", EmailContext(subject="Forecast 20261028_00Z")) == "20261028_00Z"
    assert run_of("report_2026.pdf") is None
    allowed = ["@meteo.gov.lk", "ops@cgiar.org"]
    assert sender_allowed(EmailContext(from_email="Fcst@Meteo.gov.lk"), True, allowed)
    assert sender_allowed(EmailContext(from_email="ops@cgiar.org"), True, allowed)
    assert not sender_allowed(EmailContext(from_email="x@meteo.gov.lk.evil.com"), True, allowed)
    assert not sender_allowed(None, True, allowed)
    assert sender_allowed(None, False, allowed)  # dropped into the channel folder by a team member
