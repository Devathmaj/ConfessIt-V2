## CONFESSIT README

### Semantic search (confessions, pgvector + Azure text-embedding-3-small)

Confession semantic search ranks eligible confessions by meaning using
PostgreSQL pgvector and a single Azure-hosted embedding model.

- Model: `text-embedding-3-small`, dimensions: `1536`, column: `vector(1536)`
  (`confession_embeddings.embedding`). Query and confession embeddings always
  use the same model/dimensions; vectors are validated as exactly 1536 finite
  numbers and never truncated, padded, or reshaped.
- Embedding input is the confession's public text only (body + optional public
  recipient name). Comments, identities, reports, moderation notes, and counts
  are never embedded. Exactly one embedding per confession, written once on
  creation; no edit regeneration, no backfill. Hard delete cascades;
  soft-removed (`removed_at`) rows are excluded in SQL.
- Azure route implemented in `backend/app/services/embedding_service.py`
  (Foundry v1, no api-version):
  `POST {EMBEDDING_ENDPOINT}/openai/v1/embeddings`
  with `api-key` header and `{"model": "<deployment>", "input": [...], "dimensions": 1536}`.
  `model` is `EMBEDDING_DEPLOYMENT` when set, else `EMBEDDING_MODEL`
  (the Azure deployment name, which often equals `text-embedding-3-small`).
- Runtime configuration (environment, never committed):
  `EMBEDDING_ENDPOINT="https://confessit-model.services.ai.azure.com"`,
  `EMBEDDING_API_KEY`, `EMBEDDING_MODEL="text-embedding-3-small"`,
  `EMBEDDING_DIMENSIONS="1536"`, `EMBEDDING_SEARCH_ENABLED="true"`.
  Disabled search makes no provider calls and answers 503.
- PostgreSQL requirement: the `pgvector/pgvector:pg16` image (see
  `docker-compose.yml` / `docker-compose.prod.yml` and `postgres-init.sql`
  `CREATE EXTENSION IF NOT EXISTS vector`). The app also creates the extension
  at boot (`PostgresClient.apply_vector_extension`) and a best-effort HNSW
  cosine index; exact search remains correct without the index on a small corpus.
  Managed hosts need superuser/`rds_superuser` or a pre-installed extension.
- Database init: `PostgresClient.initialize()` creates `confession_embeddings`
  (FK `confessions.id` CASCADE, UNIQUE `confession_id`) via the existing
  `create_all` + additive-DDL mechanism. A disposable dev database may be
  recreated; no worker is required (synchronous generation with graceful
  degradation when Azure is unavailable).
- Search API: `POST /confessions/search` with `{"query": "...", "limit": 10}`.
  Example: `curl -X POST $API/confessions/search -H "Authorization: Bearer <jwt>"`
  `-H "Content-Type: application/json" -d '{"query":"homesick night thoughts","limit":5}'`.
  Ranking is pgvector cosine distance (`<=>`) ascending (`1 - distance` is cosine
  similarity, not a probability). Returns the existing public confession shape.
- Frontend: `ConfessionsPage` has a "Search by meaning" field reusing confession
  cards, with loading, empty-results, error, clear-search, and disabled states.
- Tests: `backend/tests/test_semantic_search.py` (mocked provider; real pgvector
  for ordering and `vector(1536)` checks). Run:
  `docker compose exec backend python -m pytest tests/test_semantic_search.py -v`.