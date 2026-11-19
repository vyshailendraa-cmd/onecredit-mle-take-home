"""The consumer's guarantees, checked against a real Postgres with the real feed."""

import hashlib
import json
import random
from pathlib import Path

from app.store import Outcome, apply_message

FEED = Path(__file__).parent.parent / "data" / "feed.jsonl"


def feed_messages() -> list[tuple[str, bytes]]:
    # Same message_id scheme as producer/publish.py.
    lines = FEED.read_text(encoding="utf-8").splitlines()
    return [(hashlib.sha256(line.encode()).hexdigest(), line.encode()) for line in lines]


def body(reg="SYN-000001", ts="2026-08-14T09:00:00+08:00", kind="company-upserted", **data) -> bytes:
    return json.dumps({"type": kind, "registrationNumber": reg, "updatedAt": ts, "data": data}).encode()


def snapshot(conn) -> list[tuple]:
    return conn.execute(
        "SELECT registration_number, company_name, nature_of_business, industry, state, status, updated_at,"
        " embedded_text FROM companies ORDER BY registration_number"
    ).fetchall()


def test_redelivery_is_a_no_op(conn, embedder):
    msg = body(companyName="A", natureOfBusiness="Bakery")
    assert apply_message(conn, embedder, "m1", msg) == Outcome.APPLIED
    assert apply_message(conn, embedder, "m1", msg) == Outcome.DUPLICATE
    assert conn.execute("SELECT count(*) FROM company_events").fetchone()[0] == 1


def test_older_event_arriving_late_doesnt_overwrite(conn, embedder):
    apply_message(conn, embedder, "new", body(ts="2026-08-14T20:00:00+08:00", natureOfBusiness="Cold chain"))
    apply_message(conn, embedder, "old", body(ts="2026-08-14T10:00:00+08:00", natureOfBusiness="Trucking"))
    row = conn.execute("SELECT nature_of_business, embedded_text FROM companies").fetchone()
    assert row == ("Cold chain", "Cold chain")


def test_bad_message_is_quarantined_not_lost(conn, embedder):
    assert apply_message(conn, embedder, "bad", b'{"type": "company-up') == Outcome.REJECTED
    assert apply_message(conn, embedder, "bad", b'{"type": "company-up') == Outcome.REJECTED
    assert conn.execute("SELECT message_id, reason FROM rejected_messages").fetchall()[0][0] == "bad"


def test_missing_message_id_still_dedupes(conn, embedder):
    msg = body(companyName="A")
    apply_message(conn, embedder, None, msg)
    assert apply_message(conn, embedder, None, msg) == Outcome.DUPLICATE


def test_full_feed_every_company_lands(conn, embedder):
    outcomes = [apply_message(conn, embedder, mid, msg) for mid, msg in feed_messages()]
    assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 2000
    assert conn.execute("SELECT count(*) FROM companies WHERE embedding IS NULL").fetchone()[0] == 0
    rejected = conn.execute("SELECT count(*) FROM rejected_messages").fetchone()[0]
    assert outcomes.count(Outcome.REJECTED) >= rejected > 0


def test_replay_and_shuffle_give_identical_state(conn, embedder):
    messages = feed_messages()
    for mid, msg in messages:
        apply_message(conn, embedder, mid, msg)
    first = snapshot(conn)

    # Replay everything (at-least-once) in a random order (no ordering guarantee).
    shuffled = messages[:]
    random.Random(7).shuffle(shuffled)
    assert all(apply_message(conn, embedder, mid, msg) != Outcome.APPLIED for mid, msg in shuffled)
    assert snapshot(conn) == first

    conn.execute("TRUNCATE company_events, rejected_messages, companies")
    conn.commit()
    for mid, msg in shuffled:
        apply_message(conn, embedder, mid, msg)
    assert snapshot(conn) == first
