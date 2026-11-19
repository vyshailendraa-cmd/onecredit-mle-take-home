set shell := ["bash", "-euo", "pipefail", "-c"]

default: setup

# Install the producer's dependencies.
setup:
    uv sync --locked

# Start Postgres (with pgvector) and RabbitMQ, and wait until both are healthy.
up:
    docker compose up -d --wait

# Stop everything and delete the volumes.
down:
    docker compose down -v

# Replay the registry feed onto the company-events queue.
publish:
    RABBITMQ_URL="${RABBITMQ_URL:-amqp://guest:guest@127.0.0.1:${RABBITMQ_PORT:-5672}/}" uv run python -m producer.publish

# Consume company-events into Postgres (runs until Ctrl+C).
consume:
    uv run python -m app.consumer

# Serve the API on http://localhost:8000 (docs at /docs).
serve:
    uv run uvicorn app.api:app --port ${API_PORT:-8000}

# Unit tests, plus integration tests when Postgres is up.
test:
    EMBEDDING_MODEL=hash uv run pytest -q

# Lint and format check.
lint:
    uv run ruff check . && uv run ruff format --check .

# Recommendation-quality report (see DESIGN.md, deep dive).
evaluate:
    uv run python -m scripts.evaluate
