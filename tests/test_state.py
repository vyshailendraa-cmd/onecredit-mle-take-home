"""The fold must give the same answer whatever order events arrive in, and however often."""

import itertools
from datetime import UTC, datetime, timedelta

from app.events import STRUCK_OFF, UPSERTED, Event
from app.state import ACTIVE, STRUCK, fold

T0 = datetime(2026, 8, 14, 1, 0, tzinfo=UTC)
REG = "SYN-000675"


def upsert(minutes: int, **fields) -> Event:
    return Event(REG, UPSERTED, T0 + timedelta(minutes=minutes), fields)


def struck(minutes: int) -> Event:
    return Event(REG, STRUCK_OFF, T0 + timedelta(minutes=minutes), {})


def test_latest_source_timestamp_wins_not_latest_delivery():
    # Real case from the feed: SYN-000675's 20:38 update arrives before its 14:49 update.
    newer = upsert(600, nature_of_business="Cold chain transport")
    older = upsert(300, nature_of_business="Trucking for construction materials")
    assert fold(REG, [newer, older]).nature_of_business == "Cold chain transport"


def test_order_and_duplicates_dont_matter():
    events = [
        upsert(0, company_name="A", nature_of_business="Bakery", state="Selangor"),
        upsert(10, industry="Food & Beverage"),
        upsert(20, nature_of_business="Cafe chain"),
        struck(30),
    ]
    expected = fold(REG, events)
    for perm in itertools.permutations(events):
        assert fold(REG, list(perm)) == expected
    assert fold(REG, events + events).nature_of_business == expected.nature_of_business


def test_missing_fields_dont_erase_known_values():
    company = fold(REG, [upsert(0, company_name="A", industry="Retail"), upsert(10, company_name="A2")])
    assert company.company_name == "A2"
    assert company.industry == "Retail"


def test_struck_off_then_newer_upsert_reactivates():
    assert fold(REG, [upsert(0, company_name="A"), struck(10)]).status == STRUCK
    assert fold(REG, [upsert(0, company_name="A"), struck(10), upsert(20, company_name="A")]).status == ACTIVE


def test_late_old_upsert_doesnt_undo_struck_off():
    assert fold(REG, [struck(10), upsert(0, company_name="A")]).status == STRUCK


def test_embedding_text_falls_back_when_description_missing():
    assert fold(REG, [upsert(0, nature_of_business="Bakery")]).embedding_text() == "Bakery"
    assert fold(REG, [upsert(0, company_name="A Bhd", industry="Retail")]).embedding_text() == "Retail A Bhd"
    assert fold(REG, [struck(0)]).embedding_text() == REG
