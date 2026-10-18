from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from chttr import gcal


@pytest.fixture
def feed(fixtures, monkeypatch):
    data = (fixtures / "calendar.ics").read_bytes()
    monkeypatch.setattr(gcal, "_fetch", lambda url, max_age=gcal.CACHE_SECONDS: data)
    return ["https://calendar.google.com/calendar/ical/x/basic.ics"]


def test_find_event_by_meet_code(feed):
    e = gcal.find_event(feed, "abc-defg-hij", datetime(2026, 10, 11, 21, 5, tzinfo=UTC))
    assert e is not None and e.title == "Weekly sync"
    assert e.meet_code == "abc-defg-hij"
    emails = {a.email: a for a in e.attendees}
    assert set(emails) == {"dana@example.com", "rob@example.com", "ali@example.com"}   # the room is dropped
    assert emails["dana@example.com"].organizer
    assert emails["rob@example.com"].is_me and emails["rob@example.com"].name == ""
    assert emails["ali@example.com"].status == "DECLINED"


def test_find_event_by_time_skips_cancelled(feed):
    assert gcal.find_event(feed, "", datetime(2026, 10, 11, 22, 10, tzinfo=UTC)) is None
    e = gcal.find_event(feed, "", datetime(2026, 10, 11, 23, 10, tzinfo=UTC))
    assert e is not None and e.title == "Coffee with Sam"


def test_add_url_is_private_and_deduplicated(tmp_path):
    path = tmp_path / "urls"
    gcal.add_url(str(path), "https://a/basic.ics")
    gcal.add_url(str(path), "https://a/basic.ics")
    assert gcal.read_urls(str(path)) == ["https://a/basic.ics"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0      # anyone with the URL can read the calendar


def test_you_are_recognised_by_meeting_email_in_a_feed_not_named_after_you(fixtures, monkeypatch):
    data = (fixtures / "calendar.ics").read_bytes().replace(b"X-WR-CALNAME:rob@example.com", b"X-WR-CALNAME:Work")
    monkeypatch.setattr(gcal, "_fetch", lambda url, max_age=gcal.CACHE_SECONDS: data)
    when = datetime(2026, 10, 11, 21, 5, tzinfo=UTC)
    e = gcal.find_event(["https://x/basic.ics"], "abc-defg-hij", when)
    assert e is not None and not any(a.is_me for a in e.attendees)
    e = gcal.find_event(["https://x/basic.ics"], "abc-defg-hij", when, me_email="Rob@Example.com")
    assert e is not None and [a.email for a in e.attendees if a.is_me] == ["rob@example.com"]


def test_the_feed_address_and_meeting_email_both_count_as_you(fixtures, monkeypatch):
    # A personal feed named after one address, while [meeting] email is a work address:
    # you are still yourself on the personal feed.
    data = (fixtures / "calendar.ics").read_bytes().replace(
        b"ATTENDEE;CN=Ali Khan;PARTSTAT=DECLINED:mailto:ali@example.com",
        b"ATTENDEE;CN=Rob;PARTSTAT=ACCEPTED:mailto:rob@company.com")
    monkeypatch.setattr(gcal, "_fetch", lambda url, max_age=gcal.CACHE_SECONDS: data)
    when = datetime(2026, 10, 11, 21, 5, tzinfo=UTC)
    e = gcal.find_event(["https://x/basic.ics"], "abc-defg-hij", when, me_email="rob@company.com")
    assert e is not None
    assert sorted(a.email for a in e.attendees if a.is_me) == ["rob@company.com", "rob@example.com"]
    assert not any(a.is_me for a in e.attendees if a.email == "dana@example.com")
