"""
ingest_shopify.py
=================
Incremental Shopify-catalog sync

1. Pull changed products via Admin GraphQL
2. Build a text blob + metadata per product
3. services.pinecone.upsert()  → embeds & stores vectors
4. Save cursor in Redis (if available)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Generator, List

from tqdm import tqdm

from omnichat_log import logger
from rate_limit import rdb as _redis
from services.pinecone import upsert
from services.shopify import graphql
from settings import settings

# fallback when Redis isn't running locally
class _NullRedis:
    def get(self, *_): return None
    def set(self, *_): pass

_redis = _redis or _NullRedis()

BATCH_SIZE = 100
REDIS_KEY  = "sync:shopify:last_ts"

PRODUCT_Q = """
query($cursor:String,$updatedAfter:String){
  products(first:100, after:$cursor, query:$updatedAfter){
    pageInfo { hasNextPage }
    edges{
      cursor
      node{
        id
        title
        handle
        tags
        descriptionHtml
        updatedAt
      }
    }
  }
}
"""

# --------------------------------------------------------------------------- #
def _iter_changed_products(since_iso: str | None) -> Generator[dict, None, None]:
    cursor = None
    query_param = f"updated_at:>={since_iso}" if since_iso else None
    while True:
        data  = graphql(PRODUCT_Q, {"cursor": cursor, "updatedAfter": query_param})
        chunk = data["products"]
        for edge in chunk["edges"]:
            yield edge["node"]
            cursor = edge["cursor"]
        if not chunk["pageInfo"]["hasNextPage"]:
            break

def _product_to_text(p: dict) -> str:
    desc = p.get("descriptionHtml") or ""
    tags = ", ".join(p.get("tags") or [])
    return f"{p['title']}\n{tags}\n{desc}"[:8191]

def _payload(batch: List[dict]):
    return [
        (
            p["id"],
            _product_to_text(p),
            {"title": p["title"], "handle": p["handle"], "source": "shopify"},
        )
        for p in batch
    ]

# --------------------------------------------------------------------------- #
def main():
    last_ts = _redis.get(REDIS_KEY)
    logger.info("Shopify ingest started (since=%s)", last_ts or "first run")

    changed = list(_iter_changed_products(last_ts))
    if not changed:
        logger.info("No product changes; Pinecone unchanged.")
        return

    logger.info("%d product(s) changed; embedding + upserting …", len(changed))
    for i in tqdm(range(0, len(changed), BATCH_SIZE), desc="Sync"):
        upsert(_payload(changed[i : i + BATCH_SIZE]))

    new_ts = datetime.now(timezone.utc).isoformat()
    _redis.set(REDIS_KEY, new_ts)
    logger.success("Catalog sync complete; new cursor → %s", new_ts)

if __name__ == "__main__":
    main()
