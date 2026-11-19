"""Settings read from environment variables, with defaults that match docker-compose.yml."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    rabbitmq_url: str
    queue: str
    embedding_model: str


def load() -> Settings:
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    mq_port = os.environ.get("RABBITMQ_PORT", "5672")
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", f"postgresql://postgres:postgres@localhost:{pg_port}/companies"
        ),
        rabbitmq_url=os.environ.get("RABBITMQ_URL", f"amqp://guest:guest@localhost:{mq_port}/"),
        queue=os.environ.get("QUEUE", "company-events"),
        # "hash" selects the dependency-free placeholder embedder (used in tests).
        embedding_model=os.environ.get(
            "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        ),
    )
