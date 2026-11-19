# Similar companies: consumer and service

A RabbitMQ consumer that keeps every company from the registry feed in Postgres (with a pgvector embedding of
its nature of business), and an HTTP API that returns the most similar companies for the portal's
"Similar companies" panel. Design decisions are in [DESIGN.md](DESIGN.md); the original brief is in
[BRIEF.md](BRIEF.md).

## Run it

Requirements: Docker, [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just).

```bash
just setup      # install dependencies (Python 3.14 via uv)
just up         # Postgres (pgvector) and RabbitMQ
just consume    # terminal 1: consume company-events (Ctrl+C to stop)
just publish    # terminal 2: publish the feed
just serve      # terminal 3: API on http://localhost:8000 (interactive docs at /docs)
```

The consumer creates its tables on start. The first start downloads the embedding model
(`paraphrase-multilingual-MiniLM-L12-v2`, about 220 MB) and caches it. To run without a download, use the
placeholder embedder: `EMBEDDING_MODEL=hash just consume`.

Then:

```bash
curl localhost:8000/companies/SYN-001550/similar
curl "localhost:8000/companies/SYN-001550/similar?limit=8"
curl localhost:8000/companies/SYN-001550
```

```json
{
  "company": {"registration_number": "SYN-001550", "company_name": "Sinar Teguh Bakery Sdn. Bhd.",
              "industry": "Food & Beverage", "state": "Selangor",
              "nature_of_business": "Operating cafes and coffee shops in the Klang Valley", "status": "active"},
  "similar": [
    {"registration_number": "SYN-000539", "company_name": "Seri Sejahtera Food Sdn. Bhd.",
     "industry": "Food & Beverage", "state": "Johor", "similarity": 1.0}
  ]
}
```

### API

| Endpoint | Returns |
|---|---|
| `GET /companies/{reg}/similar?limit=4` | The company (page header) and up to `limit` (1–20) similar active companies, most similar first. |
| `GET /companies/{reg}` | One company. |
| `GET /health` | `{"status": "ok"}` when Postgres is reachable. |

Errors use one shape, `{"error": {"code": ..., "message": ...}}`:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_registration_number` | Not `SYN-` plus 6 digits (case and surrounding spaces are tolerated). |
| 404 | `company_not_found` | Not in the feed (yet). |
| 422 | (FastAPI validation) | `limit` outside 1–20. |
| 503 | `unavailable` | Postgres unreachable. The portal should hide the panel, not fail the page. |

## Tests and tooling

```bash
just test       # 41 tests; the integration ones need `just up` and are skipped otherwise
just lint       # ruff
just evaluate   # recommendation-quality report comparing embedding models (needs a populated database)
```

- `tests/test_events.py`: one case per kind of bad data found in the feed.
- `tests/test_state.py`: the fold gives the same result for every ordering and with duplicates.
- `tests/test_store.py`: against real Postgres and the real feed: redelivery is a no-op, a late old event doesn't
  overwrite, bad messages are quarantined, all 2,000 companies land, and replaying the feed shuffled gives
  identical state.
- `tests/test_api.py`: card fields, ordering, exclusions and every error response.

Tests use the placeholder embedder so they run offline and fast.

## Layout

```
app/events.py     parse and validate one message (all the data cleaning lives here)
app/state.py      fold a company's events into its current state (pure, order-independent)
app/store.py      apply one message to Postgres in one transaction
app/consumer.py   RabbitMQ wiring: ack after commit, quarantine, back-off
app/api.py        FastAPI service
app/embedding.py  multilingual model, plus the hashing placeholder
app/schema.sql    tables and the HNSW index
scripts/evaluate.py  offline recommendation-quality report
```
