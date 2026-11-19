# Design

```
company-events (RabbitMQ) ──> consumer ──> Postgres ──> API ──> portal
                               │            ├─ company_events     (every applied event, keyed by message_id)
                               │            ├─ companies          (current state + 384-d embedding, HNSW index)
                               └──────────> └─ rejected_messages  (quarantine)
```

## What the feed actually contains

I profiled `data/feed.jsonl` before writing code. Every rule in `app/events.py` comes from this table.

| Finding | Count | Handling |
|---|---|---|
| Messages / distinct bodies | 2,518 / 2,450 | Exact redeliveries share a `message_id`; deduplicated by primary key. |
| Same event from two sources (extra `source` / `ingestedBy` key) | ~75 | Different `message_id`, same content: harmless because state is a fold over events. |
| Companies whose events arrive out of `updatedAt` order | 66 | State is ordered by `updatedAt`, not arrival. E.g. SYN-000675's 20:38 update arrives before its 14:49 one. |
| `updatedAt` as epoch ms / with `Z` / naive / missing | 30 / 28 / 14 / 7 | All normalised to UTC; naive assumed Malaysia time (+08:00); missing is rejected. |
| Registration numbers lowercase or space-padded | 49 | Trimmed and upper-cased; anything not `SYN-NNNNNN` is rejected. |
| No registration number, or a bare integer (`119`) with placeholder data (`"X"`, `"Trading"`) | 7 + 3 | Rejected. Guessing `119` means `SYN-000119` would attach junk to a real company. |
| Truncated JSON / `null` body | 5 / 6 | Rejected. |
| `data` missing or a string (`"companyName=Foo; state=Perak"`) | 9 | Rejected. |
| Description that is one phrase pasted 60 times (up to 3,359 characters) | 7 | Collapsed to the phrase. Found by reading the quality report's examples, not by the first profiling pass. |
| Null or blank fields (industry, state, nature of business) | 224 | Ignored: a null means "this source didn't know", not "erase". |
| State in mixed case (`SELANGOR`, `kuala lumpur`) | 388 | Mapped to the canonical name. |
| `company-struck-off` events | 8 | Status `struck_off` (see below). |
| Descriptions in Malay | ~28% | Drives the embedding model choice. |
| Distinct descriptions across 2,000 companies | 174 | Many exact ties in similarity; drives the tie-break. |

After one pass of the feed: **2,000 companies, 2,418 events applied, 37 messages quarantined (32 distinct), queue
empty.** Publishing the feed again changes nothing (checked by hashing the `companies` table before and after).

## Decisions

### State is a fold over stored events, not a "last write wins" upsert
The consumer stores each valid event in `company_events` (primary key `message_id`) and rebuilds the company from
all of its events, sorted by `updatedAt`, in the same transaction (`app/state.py`).

- **At-least-once** is handled twice over: a redelivered message hits the primary key and is a no-op, and even a
  duplicate with a new `message_id` folds to the same state.
- **Out-of-order** delivery can't regress a company, because order comes from `updatedAt`, not arrival.
- **Partial updates merge correctly.** An event with `industry: null` doesn't erase a known industry, and an older
  event arriving late can still fill a field that newer events never had. A conditional upsert
  (`WHERE excluded.updated_at > companies.updated_at`) can't do that: it either drops the late event or overwrites.
- **The tests prove it**: every permutation of a company's events folds to the same state, and replaying the
  whole feed shuffled gives byte-identical state.

The cost is a read of the company's events per message. Companies have 1–3 events here; see 10x below.

### Delivery and failure handling (`app/consumer.py`)
- **Ack after commit.** A crash between commit and ack causes a redelivery, which is a no-op. Nothing is acked
  before it's stored, so nothing is lost.
- **Bad data is quarantined, not retried.** The queue must stay "durable, no extra arguments", so there's no
  dead-letter exchange. `rejected_messages` is the dead-letter queue instead: body, reason and `message_id`,
  queryable and replayable. Retrying bad JSON forever would block the queue.
- **Postgres down → requeue with back-off**, so good data is never quarantined because of an outage.
- **Unexpected exceptions** (a bug, not bad data) are requeued once; on the second failure the message is
  quarantined with the error, so a poison message can't loop forever and can't be silently dropped.
- **Several consumers can run at once.** A per-company advisory lock serialises the fold for one company, and
  the event insert and state update share one transaction.

### Embeddings
- **Model: `paraphrase-multilingual-MiniLM-L12-v2`** (384 dimensions, CPU, via fastembed). The brief suggests
  `bge-small-en-v1.5`, but 28% of descriptions are Malay ("Hotel butik di Seremban", "Perunding pengurusan untuk
  PKS"). An English-only model can't match "Hotel butik" with "Boutique hotel", which quietly splits the catalogue
  into a Malay half and an English half. The deep dive below measures this. Any 384-d model can be swapped in
  with `EMBEDDING_MODEL`, and the consumer re-embeds a company when the model name changes.
- **What's embedded: nature of business only**, as the brief asks. For the 39 companies that never had a
  description, it falls back to industry plus name, so they still get a vector and still appear in other
  companies' panels ("no lost companies").
- **Re-embed only when the text changes.** The stored text and model name are compared first.
- **Placeholder** (`EMBEDDING_MODEL=hash`): hashed words and character trigrams. Used by the tests so they run
  offline; not used for the real service.

### Struck-off companies
A struck-off company keeps its own page (with `status: struck_off`), but is excluded from other companies'
panels: the panel powers lead generation, and recommending a dissolved company is a bad lead. This is in mild
tension with "every company should appear in others' recommendations", so it's a deliberate, one-line policy
(`status = 'active'` in the query) that's easy to reverse. A newer `company-upserted` after a strike-off
reactivates the company; an older one arriving late doesn't.

### Retrieval
- Cosine distance with an **HNSW index**. The target's vector is passed as a parameter rather than a subquery, so
  Postgres uses the index (checked with `EXPLAIN`).
- **Deterministic ties.** 169 distinct descriptions cover 2,000 companies, so many neighbours are exact ties.
  Without a tie-break the panel can reshuffle between page loads. The query takes a candidate pool from the index,
  then sorts by distance, then same industry as the target, then registration number.
- Measured locally: **p50 1.6 ms, p95 2.2 ms** over 200 random companies, end to end through the API.

### API (`app/api.py`)
- One call per page: `GET /companies/{reg}/similar` returns the page header and the cards, so the portal doesn't
  make two round trips. Each card has exactly what the mock-up draws, plus `registration_number` for the click.
- The API never loads the model; it only reads stored vectors, so it starts instantly and stays small.
- Errors share one JSON shape; 503 tells the portal to hide the panel instead of failing the page.

## Assumptions

| Where the brief is open | Assumption |
|---|---|
| Naive `updatedAt` | Malaysia time (+08:00), the offset every other local timestamp in the feed uses. |
| Integer `updatedAt` | Epoch milliseconds (13 digits); 10-digit values would be seconds. |
| Null / blank field in an upsert | "Unknown in this source", not "cleared". There's no evidence in the feed of intentional clearing. |
| Equal `updatedAt`, different content | Never happens in this feed; ties are broken deterministically anyway. |
| Struck-off | Excluded from others' recommendations, still has a page (above). |
| Unknown event types | Quarantined, so a new upstream event type is visible rather than silently ignored. |
| Similarity | Nature of business only, per the brief. Industry is used only to break exact ties. |

## 10x the volume

25,000 messages and 20,000 companies is still small for Postgres; the work per message is what grows.

1. **Embedding is the bottleneck.** Batch messages (prefetch ~100, embed once per batch), and cache vectors by
   text hash: with 169 distinct descriptions most embeddings are cache hits. Beyond that, move embedding to a
   separate worker pool so the consumer only does database work.
2. **More consumers.** Already safe (per-company lock, idempotent writes). Raise prefetch per consumer and use a
   connection pool.
3. **Event history growth.** The fold reads all of a company's events. Snapshot the folded state and fold only
   newer events, or prune events older than the latest snapshot that no longer change the result.
4. **Index.** HNSW stays fast into the millions. Upgrade to `iterative_scan` (pgvector 0.8, already in
   docker-compose) so the `status = 'active'` filter can never return fewer than `limit` results, and tune
   `ef_search` against measured recall.
5. **Reads.** Cache responses keyed by `(registration_number, max(updated_at))`, add a read replica, and put
   PgBouncer in front if the portal fans out.

## What's left

- **Real-model numbers in the deep dive table below** (`just evaluate` on a laptop with the model downloaded).
- **Metrics and alerting**: consumer lag (queue depth), quarantine rate by reason, embedding latency, API p95
  and 5xx rate, and a daily "companies with no vector" check (should be 0).
- **Quarantine tooling**: a command to replay `rejected_messages` after a parser fix.
- **Container images** for the consumer and API, and CI running `just lint` and `just test` against Postgres.
- **Migrations tool** (Alembic) instead of an idempotent `schema.sql`.
- **102 companies have no industry and 49 no state**: show "—" on the card for now; could be inferred from the
  description later.

## Deep dive: recommendation quality

There are no click labels yet, so `scripts/evaluate.py` uses two proxies on the 1,953 active companies with a
description, at k = 4 (the panel size):

- **industry@4**: share of neighbours in the same industry.
- **cross-language@4**: for Malay descriptions, the share of neighbours that are English, and vice versa. Same
  industry in a different language is a correct match the portal user would want to see.

| Model | industry@4 | Malay→English@4 | English→Malay@4 |
|---|---|---|---|
| `hash` (placeholder) | 0.955 | 0.000 | 0.000 |
| `BAAI/bge-small-en-v1.5` | TODO | TODO | TODO |
| `paraphrase-multilingual-MiniLM-L12-v2` (default) | TODO | TODO | TODO |

**What the placeholder row already shows:** industry@4 can look excellent while the catalogue is split by
language. The hashing embedder never pairs a Malay description with an English one, yet scores 0.955 on
industry, because each half is internally coherent. That's why I report both numbers: industry@4 alone would
have hidden the problem the multilingual model is there to fix.

**Next steps on quality**: label a small set of pairs by hand to calibrate the proxies; try embedding
`industry + nature of business` (weighed against the brief's "nature of business" scope); add a light
re-rank by state, since a Selangor café is probably more useful to a Selangor user than one in Sabah.

## AI tools

TODO (Azryl): state exactly which tools you used and for what. Draft, edit to match what you actually did:

> I used Claude (Anthropic) to profile the feed, draft the code and tests, and draft this document. I reviewed
> every file, ran everything locally, ran the model comparison myself, and made or confirmed each decision
> above. I can walk through any line of it.
