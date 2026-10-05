## CONFESSIT README

### Hybrid search (keyword + semantic, confessions, pgvector + Azure text-embedding-3-small)

One search box runs both signals: keyword matches first, similar-in-meaning
below, deduped so a row never appears twice.

- Model: `text-embedding-3-small`, dimensions: `1536`, column: `vector(1536)`
  (`confession_embeddings.embedding`). Query and confession embeddings always
  use the same model/dimensions; vectors are validated as exactly 1536 finite
  numbers and never truncated, padded, or reshaped.
- Embedding input is the confession's public text only (body + optional public
  recipient name). Comments, identities, reports, moderation notes, and counts
  are never embedded. Exactly one embedding per confession, written once on
  creation; no edit regeneration, no backfill. Hard delete cascades;
  soft-removed (`removed_at`) rows are excluded in SQL on the public path and
  included-and-flagged on the staff path.
- Azure route implemented in `backend/app/services/embedding_service.py`
  (Foundry v1, no api-version):
  `POST {EMBEDDING_ENDPOINT}/openai/v1/embeddings`
  with `api-key` header and `{"model": "<deployment>", "input": [...], "dimensions": 1536}`.
  `model` is `EMBEDDING_DEPLOYMENT` when set, else `EMBEDDING_MODEL`
  (the Azure deployment name, which often equals `text-embedding-3-small`).
- Runtime configuration (environment, never committed):
  `EMBEDDING_ENDPOINT="https://confessit-model.services.ai.azure.com"`,
  `EMBEDDING_API_KEY`, `EMBEDDING_MODEL="text-embedding-3-small"`,
  `EMBEDDING_DIMENSIONS="1536"`, `EMBEDDING_SEARCH_ENABLED="true"`,
  plus hybrid knobs `EMBEDDING_MIN_SIMILARITY="0.30"` (cosine-similarity floor;
  no universal threshold, tune per corpus), `EMBEDDING_SEMANTIC_TOP_K="10"`,
  `EMBEDDING_MIN_QUERY_CHARS="3"` (shorter queries skip the provider),
  `EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE="30"` (public bucket) and
  `EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE="60"` (staff bucket, `0` disables).
- PostgreSQL requirement: the `pgvector/pgvector:pg16` image (see
  `docker-compose.yml` / `docker-compose.prod.yml` and `postgres-init.sql`
  `CREATE EXTENSION IF NOT EXISTS vector`). Keyword search reuses the existing
  pg_trgm GIN indexes on `confessions(body)` / `confessions(recipient_name)`
  (`ix_trgm_confessions_body`, `ix_trgm_confessions_recipient_name`, created at
  boot by `PostgresClient.apply_trgm_indexes`) -- no new migration; the repo
  has no Alembic, schema converges via `PostgresClient.initialize()`. The app
  also creates the vector extension at boot and a best-effort HNSW cosine
  index; exact search remains correct without the index on a small corpus.
  Managed hosts need superuser/`rds_superuser` or a pre-installed extension.
- Database init: `PostgresClient.initialize()` creates `confession_embeddings`
  (FK `confessions.id` CASCADE, UNIQUE `confession_id`) via the existing
  `create_all` + additive-DDL mechanism. A disposable dev database may be
  recreated; no worker is required (synchronous generation with graceful
  degradation when Azure is unavailable).
- Search APIs (both return `{keyword: {items, total}, semantic: {items, status}}`
  with `status` in `ok | disabled | unavailable | rate_limited | skipped_short`;
  blank/over-long queries are still 422):
  `POST /confessions/search` with `{"query": "...", "limit": 10}` (public shape,
  removed excluded, never matches identity).
  Example: `curl -X POST $API/confessions/search -H "Authorization: Bearer <jwt>"`
  `-H "Content-Type: application/json" -d '{"query":"homesick night thoughts","limit":5}'`.
  `GET /admin/confessions/search?q=...&filter=all&sort=recent&skip=0&limit=10`
  (staff shape, removed included-and-flagged, sender-aware lexical kept).
  Keyword ranks by trigram similarity then recency; semantic ranks by pgvector
  cosine distance (`<=>`) ascending (`1 - distance` is cosine similarity, not a
  probability). Shared pipeline lives in
  `backend/app/services/semantic_candidates.py` (embed + vector query +
  threshold + dedupe); per-bucket budgets in
  `backend/app/services/semantic_rate_limit.py`.
- Frontend: `ConfessionsPage` "Search confessions" (debounced, stale-cancelling)
  renders "Keyword matches" then "Similar in meaning" with a non-blocking notice
  when `semantic.status` is not `ok`; `ConfessionsAdmin` review search renders
  the same two sections with removed flags and admin columns.
  `GET /admin/confessions/reports` is unchanged.
- Tests: `backend/tests/test_hybrid_search.py` (10 tests: dedupe, semantic-only,
  removed visibility, identity rules, degradation, buckets, threshold, short
  queries, 422s, admin filter/sort) plus `backend/tests/test_semantic_search.py`
  (24 tests, endpoint assertions updated to the hybrid shape). Provider calls are
  mocked; vector ordering/threshold run against real pgvector. Run:
  `docker compose exec backend python -m pytest tests/test_hybrid_search.py tests/test_semantic_search.py -v`.
  Frontend: `npm test -- --run tests/confessionsHybridSearch.test.tsx tests/confessionsAdminPaging.test.tsx`.