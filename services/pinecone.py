"""
omnichat.services.pinecone
~~~~~~~~~~~~~~~~~~~~~~~~~~
• lazy-initialises the Pinecone client once (v3.x)
• embeds with OpenAI + back-off
• upsert/query helpers that accept 3 input shapes:
    (id, text)
    (id, text, metadata)
    (id, vector, metadata)
"""

from __future__ import annotations

import functools
import time
from typing import Iterable, List, Sequence, Tuple, overload

import backoff
import openai
from pinecone import Pinecone, ServerlessSpec

from omnichat_log import logger
from settings import settings

# ── 1.  Client + index --------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _pc() -> Pinecone:
    pc = Pinecone(api_key=settings.pinecone_api_key.get_secret_value())
    logger.info("Pinecone client initialised")
    return pc


@functools.lru_cache(maxsize=1)
def _index():
    pc   = _pc()
    name = settings.pinecone_index

    if name not in pc.list_indexes().names():
        pc.create_index(
            name      = name,
            dimension = 1536,
            metric    = "cosine",
            spec      = ServerlessSpec(cloud="aws", region=settings.pinecone_region),
        )
        logger.info("Pinecone index %s created", name)

    return pc.Index(name)

# ── 2.  Embeddings ------------------------------------------------------------
openai.api_key = settings.openai_api_key.get_secret_value()

@backoff.on_exception(backoff.expo, openai.OpenAIError, max_time=60, logger=logger)
def embed(text: str | Sequence[str]) -> List[List[float]]:
    """Return list-of-embeddings (even for a single prompt)."""
    t0 = time.perf_counter()
    resp = openai.embeddings.create(
        model=settings.openai_model_embed,
        input=text,
    )
    logger.debug("OpenAI embed latency %.0f ms", (time.perf_counter() - t0) * 1000)
    return [d.embedding for d in resp.data]

# ── 3.  Flexible upsert -------------------------------------------------------
@overload
def upsert(items: Iterable[Tuple[str, str]], *, namespace: str = ...) -> None: ...
@overload
def upsert(items: Iterable[Tuple[str, str, dict]], *, namespace: str = ...) -> None: ...
@overload
def upsert(items: Iterable[Tuple[str, list, dict]], *, namespace: str = ...) -> None: ...

def upsert(items, *, namespace: str = "default") -> None:
    """
    Accepts:
        • (id, text)
        • (id, text, metadata)
        • (id, vector, metadata)
    Automatically embeds when the 2nd element is text.
    """
    first = next(iter(items))
    # put the iterator back together
    items = list(items)

    if isinstance(first[1], list):                      # already vector
        _index().upsert(vectors=items, namespace=namespace)

    elif len(first) == 2:                               # (id, text)
        ids, texts = zip(*items)
        vecs = embed(texts)
        meta = [{"source": "shopify"}] * len(ids)
        _index().upsert(vectors=list(zip(ids, vecs, meta)), namespace=namespace)

    else:                                               # (id, text, meta)
        ids, texts, meta = zip(*items)
        vecs = embed(texts)
        _index().upsert(vectors=list(zip(ids, vecs, meta)), namespace=namespace)

    logger.info("Upserted %d vectors to Pinecone [%s]", len(items), namespace)

# ── 4.  Query helpers ---------------------------------------------------------
def query(text: str, k: int = 4, *, namespace: str = "default"):
    vec = embed(text)[0]
    return _index().query(
        vector=vec,
        top_k=k,
        include_metadata=True,
        namespace=namespace,
    ).matches


def max_similarity(text: str, *, namespace: str = "default") -> float:
    match = query(text, k=1, namespace=namespace)
    return match[0].score if match else 0.0
