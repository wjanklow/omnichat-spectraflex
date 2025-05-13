"""
omnichat.services.pinecone
~~~~~~~~~~~~~~~~~~~~~~~~~~
One-stop helper for:
• ensuring the Pinecone client is initialised once
• generating OpenAI embeddings with automatic back-off
• upsert/query convenience wrappers with sane defaults
"""

from __future__ import annotations

import functools
import time
from typing import Iterable, List, Sequence

import backoff
import openai
import pinecone

from omnichat_log import logger
from settings import settings

# ── 1.  Lazy-initialise Pinecone client & index ───────────────────────────────
@functools.lru_cache(maxsize=1)
def _index() -> pinecone.Index:
    pinecone.init(api_key=settings.pinecone_api_key.get_secret_value(),
                  environment=settings.pinecone_env)
    logger.info(f"Pinecone client initialised [{settings.pinecone_env}]")
    return pinecone.Index(settings.pinecone_index)


# ── 2.  Embedding with exponential back-off ───────────────────────────────────
openai.api_key = settings.openai_api_key.get_secret_value()

@backoff.on_exception(backoff.expo, openai.OpenAIError, max_time=60, logger=logger)
def embed(text: str | Sequence[str]) -> List[List[float]]:
    """
    Returns a list of embeddings (even when given a single string).
    """
    start = time.perf_counter()
    resp = openai.embeddings.create(
        model=settings.openai_model_embed,
        input=text,
    )
    duration = (time.perf_counter() - start) * 1000
    logger.debug(f"OpenAI embed latency {duration:.0f} ms")
    return [d.embedding for d in resp.data]


# ── 3.  CRUD helpers ──────────────────────────────────────────────────────────
def upsert(ids_and_text: Iterable[tuple[str, str]], namespace: str = "default") -> None:
    """
    ids_and_text : iterable of (id, text) tuples.
    Calculates embeddings and writes them to Pinecone with same metadata structure.
    """
    ids, texts = zip(*ids_and_text)
    vecs = embed(texts)
    meta = [{"source": "shopify"}] * len(ids)  # simple metadata; extend as needed
    _index().upsert(vectors=list(zip(ids, vecs, meta)), namespace=namespace)
    logger.info(f"Upserted {len(ids)} vectors to Pinecone")


def query(text: str, k: int = 4, namespace: str = "default"):
    """
    Returns Pinecone query matches (metadata, score).
    """
    vec = embed(text)[0]
    res = _index().query(vector=vec, top_k=k, include_metadata=True, namespace=namespace)
    return res.matches


# ── 4.  Convenience cosine check for off-topic filter ─────────────────────────
def max_similarity(text: str, namespace: str = "default") -> float:
    match = query(text, k=1, namespace=namespace)
    return match[0].score if match else 0.0
