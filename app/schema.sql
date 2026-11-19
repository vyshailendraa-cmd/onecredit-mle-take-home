-- Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS vector;

-- Every applied event, keyed by AMQP message_id. The primary key is what makes redelivery a no-op,
-- and keeping the history lets us rebuild a company from its events in any order.
CREATE TABLE IF NOT EXISTS company_events (
    message_id          text PRIMARY KEY,
    registration_number text NOT NULL,
    event_type          text NOT NULL,
    updated_at          timestamptz NOT NULL,
    fields              jsonb NOT NULL,
    received_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_events_registration ON company_events (registration_number);

-- Messages we couldn't use. The queue can't take extra arguments (no dead-letter exchange), so the
-- quarantine lives here, where someone can inspect and replay it.
CREATE TABLE IF NOT EXISTS rejected_messages (
    message_id  text PRIMARY KEY,
    reason      text NOT NULL,
    body        text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

-- Current state per company: what the API reads.
CREATE TABLE IF NOT EXISTS companies (
    registration_number text PRIMARY KEY,
    company_name        text,
    nature_of_business  text,
    industry            text,
    state               text,
    status              text NOT NULL,
    updated_at          timestamptz NOT NULL,
    embedded_text       text NOT NULL,
    embedding_model     text NOT NULL,
    embedding           vector(384) NOT NULL,
    refreshed_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS companies_embedding_hnsw ON companies USING hnsw (embedding vector_cosine_ops);
