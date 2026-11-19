"""Apply one message to Postgres. This is the whole consumer logic, kept free of RabbitMQ so it's easy to test."""

import hashlib
import json
import logging
from dataclasses import asdict
from enum import StrEnum
from importlib import resources

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from app.embedding import Embedder
from app.events import Event, InvalidEvent, parse
from app.state import CompanyState, fold

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


def connect(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url, connect_timeout=5)
    migrate(conn)
    register_vector(conn)
    return conn


def migrate(conn: psycopg.Connection) -> None:
    sql = resources.files("app").joinpath("schema.sql").read_text()
    with conn.transaction():
        conn.execute(sql)


def message_key(message_id: str | None, body: bytes) -> str:
    # The producer always sets message_id; fall back to a hash of the body so a missing id still dedupes.
    return message_id or "sha256:" + hashlib.sha256(body).hexdigest()


def apply_message(conn: psycopg.Connection, embedder: Embedder, message_id: str | None, body: bytes) -> Outcome:
    """Validate, record and apply one message in a single transaction. Safe to call again with the same message."""
    key = message_key(message_id, body)
    try:
        event = parse(body)
    except InvalidEvent as exc:
        quarantine(conn, key, body, str(exc))
        return Outcome.REJECTED

    with conn.transaction():
        inserted = conn.execute(
            "INSERT INTO company_events (message_id, registration_number, event_type, updated_at, fields)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING RETURNING 1",
            (key, event.registration_number, event.type, event.updated_at, json.dumps(event.fields)),
        ).fetchone()
        if inserted is None:
            return Outcome.DUPLICATE
        # Serialise work on one company across consumers, then rebuild it from all of its events.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (event.registration_number,))
        company = fold(event.registration_number, load_events(conn, event.registration_number))
        save_company(conn, embedder, company)
    return Outcome.APPLIED


def quarantine(conn: psycopg.Connection, key: str, body: bytes, reason: str) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO rejected_messages (message_id, reason, body) VALUES (%s, %s, %s)"
            " ON CONFLICT (message_id) DO NOTHING",
            (key, reason, body.decode("utf-8", errors="replace")),
        )
    log.warning("quarantined %s: %s", key, reason)


def load_events(conn: psycopg.Connection, registration_number: str) -> list[Event]:
    rows = conn.execute(
        "SELECT event_type, updated_at, fields FROM company_events WHERE registration_number = %s",
        (registration_number,),
    ).fetchall()
    return [Event(registration_number, kind, ts, fields) for kind, ts, fields in rows]


def save_company(conn: psycopg.Connection, embedder: Embedder, company: CompanyState) -> None:
    text = company.embedding_text()
    current = conn.execute(
        "SELECT embedded_text, embedding_model, embedding FROM companies WHERE registration_number = %s",
        (company.registration_number,),
    ).fetchone()
    if current and current[0] == text and current[1] == embedder.name:
        vector = current[2]  # description unchanged: skip the model
    else:
        vector = np.asarray(embedder.embed([text])[0], dtype=np.float32)
    row = asdict(company)
    row.pop("sources")
    conn.execute(
        """
        INSERT INTO companies (registration_number, company_name, nature_of_business, industry, state, status,
                               updated_at, embedded_text, embedding_model, embedding)
        VALUES (%(registration_number)s, %(company_name)s, %(nature_of_business)s, %(industry)s, %(state)s,
                %(status)s, %(updated_at)s, %(text)s, %(model)s, %(vector)s)
        ON CONFLICT (registration_number) DO UPDATE SET
            company_name = EXCLUDED.company_name, nature_of_business = EXCLUDED.nature_of_business,
            industry = EXCLUDED.industry, state = EXCLUDED.state, status = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at, embedded_text = EXCLUDED.embedded_text,
            embedding_model = EXCLUDED.embedding_model, embedding = EXCLUDED.embedding, refreshed_at = now()
        """,
        row | {"text": text, "model": embedder.name, "vector": vector},
    )
