"""
Lightweight async Redis helper
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Exposes `redis` – a lazily-initialised, singleton async client
configured from settings.redis_url.
"""

from typing import Any
from functools import lru_cache

import redis.asyncio as redis_ai

from settings import settings


@lru_cache
def _get_client() -> "redis_ai.Redis":
    return redis_ai.from_url(
        settings.redis_url,
        decode_responses=True,   # str in → str out
        health_check_interval=30
    )

# public handle you `import` elsewhere
redis = _get_client()        # type: redis_ai.Redis
