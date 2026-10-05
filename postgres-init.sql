CREATE DATABASE "ConfessIt_Test";
-- pgvector for confession semantic search (vector(1536) in confession_embeddings).
-- Requires the pgvector/pgvector:pg16 image; stock postgres has no vector extension.
-- The application also runs CREATE EXTENSION IF NOT EXISTS vector at boot, so this
-- is for fresh volumes only and is safe to re-run.
CREATE EXTENSION IF NOT EXISTS vector;
\c ConfessIt_Test
CREATE EXTENSION IF NOT EXISTS vector;
\c ConfessIt
CREATE EXTENSION IF NOT EXISTS vector;
