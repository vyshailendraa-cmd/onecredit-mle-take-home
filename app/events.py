"""Turn a raw feed message into a validated, normalised event, or explain why it can't be used.

The feed merges upstream sources and isn't validated, so everything here is defensive. Every rule below
comes from something actually seen in data/feed.jsonl.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8))
REG_PATTERN = re.compile(r"^SYN-\d{6}$")
FIELDS = {
    "companyName": "company_name",
    "natureOfBusiness": "nature_of_business",
    "industry": "industry",
    "state": "state",
}
UPSERTED = "company-upserted"
STRUCK_OFF = "company-struck-off"

# Canonical Malaysian states and federal territories. The feed mixes cases ("SELANGOR", "kuala lumpur").
STATES = {
    s.lower(): s
    for s in [
        "Johor",
        "Kedah",
        "Kelantan",
        "Melaka",
        "Negeri Sembilan",
        "Pahang",
        "Perak",
        "Perlis",
        "Pulau Pinang",
        "Sabah",
        "Sarawak",
        "Selangor",
        "Terengganu",
        "Kuala Lumpur",
        "Labuan",
        "Putrajaya",
    ]
}
STATE_ALIASES = {"penang": "Pulau Pinang", "malacca": "Melaka", "kl": "Kuala Lumpur"}


class InvalidEvent(ValueError):
    """The message can't be applied. It is quarantined, not retried: retrying won't fix the data."""


@dataclass(frozen=True)
class Event:
    registration_number: str
    type: str
    updated_at: datetime
    # Only the fields the message actually carried a usable value for. None/blank values are dropped:
    # with merged, unvalidated sources a missing value means "this source didn't know", not "erase it".
    fields: dict[str, str]


def normalise_registration(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidEvent("registrationNumber missing or not a string")
    reg = value.strip().upper()
    if not REG_PATTERN.match(reg):
        raise InvalidEvent(f"registrationNumber {value!r} doesn't match SYN-NNNNNN")
    return reg


def parse_timestamp(value: Any) -> datetime:
    """Accepts ISO 8601 with offset or Z, naive ISO (assumed Malaysia time), and epoch milliseconds."""
    if isinstance(value, bool) or value is None:
        raise InvalidEvent("updatedAt missing")
    if isinstance(value, int | float):
        # Epoch milliseconds in this feed (13 digits). Treat 10-digit values as seconds.
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidEvent(f"updatedAt {value!r} isn't a timestamp") from exc
        return ts if ts.tzinfo else ts.replace(tzinfo=MYT)
    raise InvalidEvent(f"updatedAt has unexpected type {type(value).__name__}")


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def collapse_repetition(text: str) -> str:
    """'A B C A B C A B C' -> 'A B C'. Seven descriptions in the feed are one phrase pasted 60 times."""
    words = text.split()
    for size in range(1, len(words) // 2 + 1):
        if len(words) % size == 0 and words == words[:size] * (len(words) // size):
            return " ".join(words[:size])
    return text


def normalise_state(value: str) -> str:
    key = value.lower()
    return STATES.get(key) or STATE_ALIASES.get(key) or value


def parse(body: bytes) -> Event:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidEvent(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidEvent(f"expected a JSON object, got {type(raw).__name__}")

    kind = raw.get("type")
    if kind not in (UPSERTED, STRUCK_OFF):
        raise InvalidEvent(f"unknown event type {kind!r}")
    reg = normalise_registration(raw.get("registrationNumber"))
    updated_at = parse_timestamp(raw.get("updatedAt"))

    fields: dict[str, str] = {}
    if kind == UPSERTED:
        data = raw.get("data")
        if not isinstance(data, dict):
            raise InvalidEvent(f"company-upserted without a data object (got {type(data).__name__})")
        for source_key, column in FIELDS.items():
            text = _clean_text(data.get(source_key))
            if text is not None:
                if column == "state":
                    text = normalise_state(text)
                elif column == "nature_of_business":
                    text = collapse_repetition(text)
                fields[column] = text
        if not fields:
            raise InvalidEvent("company-upserted with no usable fields")
    return Event(reg, kind, updated_at, fields)
