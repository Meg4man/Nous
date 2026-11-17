"""
nous-api — memory tools
Semantic memory via ChromaDB + structured pattern storage via pgvector.

ChromaDB: stores full interaction embeddings for semantic "what did we talk about" queries
pgvector: stores structured pattern vectors for the proactive worker's pattern analysis

Both use nomic-embed-text via Ollama for embedding — no external embedding API.
"""

import json
import os
import uuid
from datetime import UTC, datetime

import httpx
import structlog
from langchain_core.tools import tool

log = structlog.get_logger("nous.tools.memory")

CHROMA_HOST = os.getenv("CHROMA_HOST", "127.0.0.1")
CHROMA_PORT = os.getenv("CHROMA_PORT", "8000")
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "nous_memory")
CHROMA_BASE = f"http://{CHROMA_HOST}:{CHROMA_PORT}"

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "nousdb")
POSTGRES_USER = os.getenv("POSTGRES_USER", "nous")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

_collection_id: str | None = None


# ── Embedding helper ───────────────────────────────────────────────────────────
async def _embed(text: str) -> list[float]:
    """Generate an embedding via Ollama nomic-embed-text."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OLLAMA_HOST}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama /api/embed returns {"embeddings": [[...]]}
        embeddings = data.get("embeddings", [[]])
        return embeddings[0] if embeddings else []


# ── ChromaDB collection bootstrap ─────────────────────────────────────────────
async def _get_collection_id() -> str:
    """Get or create the ChromaDB collection, return its UUID."""
    global _collection_id
    if _collection_id:
        return _collection_id

    async with httpx.AsyncClient() as client:
        # Try to get existing collection
        resp = await client.get(f"{CHROMA_BASE}/api/v1/collections/{CHROMA_COLLECTION}", timeout=5.0)
        if resp.status_code == 200:
            _collection_id = resp.json()["id"]
            return _collection_id

        # Create if not found
        resp = await client.post(
            f"{CHROMA_BASE}/api/v1/collections",
            json={"name": CHROMA_COLLECTION, "metadata": {"hnsw:space": "cosine"}},
            timeout=5.0,
        )
        resp.raise_for_status()
        _collection_id = resp.json()["id"]
        log.info("memory.chroma_collection_created", collection=CHROMA_COLLECTION)
        return _collection_id


# ── Core memory functions (called directly by agent and proactive worker) ──────
async def query_memory(query: str, limit: int = 5) -> dict:
    """
    Semantic search over ChromaDB memory.
    Returns up to `limit` relevant past interactions.
    """
    try:
        embedding = await _embed(query)
        collection_id = await _get_collection_id()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{CHROMA_BASE}/api/v1/collections/{collection_id}/query",
                json={
                    "query_embeddings": [embedding],
                    "n_results": limit,
                    "include": ["documents", "metadatas", "distances"],
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

        documents = data.get("documents", [[]])[0]
        metadatas = data.get("metadatas", [[]])[0]
        distances = data.get("distances", [[]])[0]

        results = [
            {
                "content": doc,
                "metadata": meta,
                "relevance": round(1 - dist, 4),  # cosine distance → similarity
            }
            for doc, meta, dist in zip(documents, metadatas, distances, strict=False)
        ]

        log.info("memory.query", query_len=len(query), results=len(results))
        return {"results": results, "count": len(results)}

    except Exception as e:
        log.error("memory.query_failed", error=str(e))
        return {"results": [], "count": 0, "error": str(e)}


async def write_memory(content: str, metadata: dict | None = None) -> dict:
    """
    Write a memory entry to ChromaDB with its embedding.
    Also writes a structured record to pgvector for pattern analysis.
    """
    metadata = metadata or {}
    metadata.setdefault("ts", datetime.now(tz=UTC).isoformat())
    metadata.setdefault("source", "agent")

    try:
        # 1. Embed
        embedding = await _embed(content)

        # 2. Write to ChromaDB
        collection_id = await _get_collection_id()
        doc_id = str(uuid.uuid4())

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{CHROMA_BASE}/api/v1/collections/{collection_id}/add",
                json={
                    "ids": [doc_id],
                    "embeddings": [embedding],
                    "documents": [content],
                    "metadatas": [metadata],
                },
                timeout=10.0,
            )
            resp.raise_for_status()

        log.info("memory.written", doc_id=doc_id, source=metadata.get("source"))

        # 3. Also write to pgvector for proactive pattern analysis
        await _write_pgvector(doc_id=doc_id, content=content, embedding=embedding, metadata=metadata)

        return {"success": True, "doc_id": doc_id}

    except Exception as e:
        log.error("memory.write_failed", error=str(e))
        return {"success": False, "error": str(e)}


async def _write_pgvector(
    doc_id: str,
    content: str,
    embedding: list[float],
    metadata: dict,
) -> None:
    """
    Write to pgvector for structured pattern queries by the proactive worker.
    Uses psycopg2 (sync) in a threadpool to avoid blocking the event loop.
    """
    import asyncio

    import psycopg2
    from psycopg2.extras import Json

    def _sync_write():
        conn = psycopg2.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            dbname=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
        )
        try:
            with conn.cursor() as cur:
                # Ensure table exists (idempotent)
                cur.execute("""
                    CREATE EXTENSION IF NOT EXISTS vector;
                    CREATE TABLE IF NOT EXISTS nous_memory (
                        id UUID PRIMARY KEY,
                        content TEXT NOT NULL,
                        embedding vector(768),
                        metadata JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS nous_memory_embedding_idx
                        ON nous_memory USING ivfflat (embedding vector_cosine_ops);
                """)
                cur.execute(
                    "INSERT INTO nous_memory (id, content, embedding, metadata) VALUES (%s, %s, %s, %s)",
                    (doc_id, content, embedding, Json(metadata)),
                )
            conn.commit()
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_write)


# ── LangChain tool wrappers ────────────────────────────────────────────────────
@tool
async def memory_query_tool(query: str, limit: int = 5) -> str:
    """
    Search Nous's memory for relevant past interactions or facts.

    Args:
        query: Natural language query (e.g. "what did I say about the living room lights?")
        limit: Maximum number of results to return (default 5, max 20)

    Returns:
        JSON string of relevant memory entries with relevance scores.
    """
    limit = min(limit, 20)
    result = await query_memory(query=query, limit=limit)
    return json.dumps(result)


@tool
async def memory_write_tool(content: str, source: str = "conversation") -> str:
    """
    Save a fact, interaction, or observation to Nous's memory.
    Use this to remember things the user tells you, or patterns you observe.

    Args:
        content: Text to remember (keep under 500 words for best retrieval)
        source: Where this memory came from (e.g. "conversation", "sensor", "observation")

    Returns:
        JSON string confirming the write with the memory ID.
    """
    result = await write_memory(
        content=content,
        metadata={"source": source},
    )
    return json.dumps(result)
