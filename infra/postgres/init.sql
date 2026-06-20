-- Enable pgvector for hybrid RAG over party-member memories.
CREATE EXTENSION IF NOT EXISTS vector;
-- pg_trgm helps keyword/fuzzy search alongside tsvector.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
