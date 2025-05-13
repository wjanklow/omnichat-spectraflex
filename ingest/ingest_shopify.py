"""
ingest_shopify.py
=================
Incremental product-catalog sync:

1. Pulls all products/variants from Shopify Admin GraphQL
2. Builds a short text blob per product
3. Embeds in batches via OpenAI
4. Upserts vectors + metadata into Pinecone
5. Stores last_sync timestamp in Redis so subsequent
   runs only fetch changed products (fast).

Env: relies on omnichat.settings for keys.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Generator, List

import redis
from tqdm import tqdm  # progress bar

from omnichat_log import logger
from rate_limit import rdb as _redis  # reuse same Redis instance
from services.pinecone import embed, upsert
from services.shopify import graphql
from settings import settings

# ── 1.  Config ----------------------------------------------------------------
BATCH_SIZE = 100     # embed 100 products per OpenAI call
REDIS_KEY = "sync:shopify:last_ts"

# ── 2.  GraphQL query ----------------------------------------------------------
PRODUCT_Q = """
query($cursor:String,$updatedAfter:DateTime){
  products(first:100, after:$cursor, query:$updatedAfter){
    pageInfo{hasNextPage}
    edges{
      cursor
      node{
        id title handle tags descriptionHtml updatedAt
      }
    }
  }
}
"""


def _iter_changed_products(since_iso: str | None) -> Generator[dict, None, None]:
    cursor = None
    query_param = f"updated_at:>={since_iso}" if since_iso else None
    while True:
        data = graphql(PRODUCT_Q, {"cursor": cursor, "updatedAfter": query_param})
        chunk = data["products"]
        for edge in chunk["edges"]:
            yield edge["node"]
            cursor = edge["cursor"]
        if not chunk["pageInfo"]["hasNextPage"]:
            break


# ── 3.  Transform -------------------------------------------------------------
def _product_to_text(p: dict) -> str:
    desc = p["descriptionHtml"] or ""
    tags = ", ".join(p["tags"]) if p.get("tags") else ""
    return f"{p['title']}\n{tags}\n{desc}"[:8191]  # embed endpoint char limit


def _to_upsert_payload(batch: List[dict]):
    ids = [p["id"] for p in batch]
    texts = [_product_to_text(p) for p in batch]
    vecs = embed(texts)
    meta = [{"title": p["title"], "handle": p["handle"]} for p in batch]
    return list(zip(ids, vecs, meta))


# ── 4.  Main ------------------------------------------------------------------
def main():
    last_ts = _redis.get(REDIS_KEY)
    logger.info(f"Shopify catalog ingest started (since={last_ts or '∞'})")

    changed = list(_iter_changed_products(last_ts))
    if not changed:
        logger.info("No product changes; Pinecone unchanged.")
        return

    logger.info(f"{len(changed)} product(s) updated/created; embedding…")
    for i in tqdm(range(0, len(changed), BATCH_SIZE), desc="Embedding"):
        batch = changed[i : i + BATCH_SIZE]
        upsert(_to_upsert_payload(batch))

    new_ts = datetime.now(timezone.utc).isoformat()
    _redis.set(REDIS_KEY, new_ts)
    logger.success(f"Catalog sync complete; new cursor saved ({new_ts})")


if __name__ == "__main__":
    main()
