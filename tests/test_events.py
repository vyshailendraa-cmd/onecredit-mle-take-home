"""Parsing rules, one test per kind of problem seen in data/feed.jsonl."""

import json
from datetime import UTC, datetime

import pytest

from app.events import STRUCK_OFF, InvalidEvent, parse


def message(**overrides) -> bytes:
    body = {
        "type": "company-upserted",
        "registrationNumber": "SYN-000001",
        "updatedAt": "2026-08-14T09:00:00+08:00",
        "data": {
            "companyName": "Sinar Teguh Bakery Sdn. Bhd.",
            "natureOfBusiness": "Operating cafes in the Klang Valley",
            "state": "Selangor",
            "industry": "Food & Beverage",
        },
    }
    body.update(overrides)
    return json.dumps(body).encode()


def test_valid_upsert():
    event = parse(message())
    assert event.registration_number == "SYN-000001"
    assert event.updated_at == datetime(2026, 8, 14, 1, 0, tzinfo=UTC)
    assert event.fields["industry"] == "Food & Beverage"


@pytest.mark.parametrize("reg", ["syn-000001", " SYN-000001 ", "Syn-000001\n"])
def test_registration_number_is_normalised(reg):
    assert parse(message(registrationNumber=reg)).registration_number == "SYN-000001"


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-14T09:00:00+08:00",
        "2026-08-14T01:00:00Z",
        "2026-08-14T09:00:00",  # naive: assumed Malaysia time
        1786669200000,  # epoch milliseconds
    ],
)
def test_timestamp_formats_agree(value):
    assert parse(message(updatedAt=value)).updated_at == datetime(2026, 8, 14, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    [
        b'{"type": "company-upserted", "registrationNumber": "SYN-0',  # truncated JSON
        b"null",
        message(data="companyName=Foo; state=Perak"),
        message(data=None),
        message(registrationNumber=None),
        message(registrationNumber="ABC-123"),
        message(updatedAt=None),
        message(updatedAt="yesterday"),
        message(type="company-merged"),
        message(data={"companyName": None, "state": "  "}),
    ],
)
def test_unusable_messages_are_rejected(body):
    with pytest.raises(InvalidEvent):
        parse(body)


def test_missing_registration_key_is_rejected():
    body = json.loads(message())
    del body["registrationNumber"]
    with pytest.raises(InvalidEvent):
        parse(json.dumps(body).encode())


def test_nulls_are_dropped_not_kept():
    event = parse(message(data={"companyName": "X Sdn. Bhd.", "industry": None, "natureOfBusiness": ""}))
    assert event.fields == {"company_name": "X Sdn. Bhd."}


def test_state_and_whitespace_are_normalised():
    event = parse(message(data={"companyName": "  X   Sdn. Bhd. ", "state": "KUALA LUMPUR"}))
    assert event.fields == {"company_name": "X Sdn. Bhd.", "state": "Kuala Lumpur"}


def test_struck_off_needs_no_data():
    body = {"type": STRUCK_OFF, "registrationNumber": "SYN-000001", "updatedAt": "2026-08-14T16:00:00+08:00"}
    event = parse(json.dumps(body).encode())
    assert event.type == STRUCK_OFF and event.fields == {}


def test_unknown_extra_keys_are_ignored():
    assert parse(message(source="ssm-batch", ingestedBy="partner-api")).registration_number == "SYN-000001"


def test_repeated_description_is_collapsed():
    pasted = " ".join(["Software development for small business accounting"] * 60)
    event = parse(message(data={"natureOfBusiness": pasted}))
    assert event.fields["nature_of_business"] == "Software development for small business accounting"
    # A phrase that merely contains a repeated word is left alone.
    assert parse(message(data={"natureOfBusiness": "Kerja-kerja kerja awam"})).fields["nature_of_business"] == (
        "Kerja-kerja kerja awam"
    )
