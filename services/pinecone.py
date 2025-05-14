"""
omnichat.services.pinecone
~~~~~~~~~~~~~~~~~~~~~~~~~~
One-stop helper for:
• initialising the Pinecone client once
• generating OpenAI embeddings with automatic back-off
• upsert/query convenience wrappers with sane defaults
"""

from __future__ import annotations

import functools
import time
from typing import Iterable, List, Sequence

import backoff
import openai
from pinecone import Pinecone, ServerlessSpec   # ← new 3.x API

from omnichat_log import logger
from settings import settings

# ── 1.  Lazy-initialise Pinecone client & index ──────────────────────────────
@functools.lru_cache(maxsize=1)
def _pc() -> Pinecone:
    pc = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
    logger.info("Pinecone client initialised")
    return pc


@functools.lru_cache(maxsize=1)
def _index():
    pc = _pc()

    name = settings.pinecone_index
    # Ensure the index exists (serverless example; tweak for classic env)
    if name not in pc.list_indexes().names():
        pc.create_index(
            name      = name,
            dimension = 1536,
            metric    = "cosine",
            spec      = ServerlessSpec(
                cloud  = "aws",
                region = settings.pinecone_region or "us-east-1"
            )
        )
        logger.info("Pinecone index %s created", name)

    return pc.Index(name)


# ── 2.  Embedding with exponential back-off ──────────────────────────────────
openai.api_key = settings.openai_api_key.get_secret_value()

@backoff.on_exception(backoff.expo, openai.OpenAIError, max_time=60, logger=logger)
def embed(text: str | Sequence[str]) -> List[List[float]]:
    """Return a list of embeddings (even when given a single string)."""
    start = time.perf_counter()
    resp = openai.embeddings.create(
        model = settings.openai_model_embed,
        input = text,
    )
    logger.debug("OpenAI embed latency %.0f ms", (time.perf_counter() - start) * 1000)
    return [d.embedding for d in resp.data]


# ── 3.  CRUD helpers ─────────────────────────────────────────────────────────
def upsert(ids_and_text: Iterable[tuple[str, str]], namespace: str = "default") -> None:
    """Upsert vectors with simple metadata."""
    ids, texts = zip(*ids_and_text)
    vecs = embed(texts)
    meta = [{"source": "shopify"}] * len(ids)
    _index().upsert(vectors=list(zip(ids, vecs, meta)), namespace=namespace)
    logger.info("Upserted %d vectors to Pinecone", len(ids))


def query(text: str, k: int = 4, namespace: str = "default"):
    """Return Pinecone query matches (metadata & score)."""
    vec = embed(text)[0]
    return _index().query(
        vector           = vec,
        top_k            = k,
        include_metadata = True,
        namespace        = namespace,
    ).matches


# ── 4.  Convenience cosine check for off-topic filter ────────────────────────
def max_similarity(text: str, namespace: str = "default") -> float:
    match = query(text, k=1, namespace=namespace)
    return match[0].score if match else 0.0
