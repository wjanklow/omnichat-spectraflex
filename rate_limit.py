"""
Simple Redis-backed limiters
• ip_throttle()      – 20 msgs / 60 s per IP
• consume_tokens()   – 15 K OpenAI tokens per chat session
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import redis
from settings import settings
from omnichat_log import logger          # adjust if you renamed it


# ---------------------------------------------------------------------------
#  Redis connection (optional)
# ---------------------------------------------------------------------------

def _make_redis(url: Optional[str]) -> Optional[redis.Redis]:
    if not url:                                      # env var / setting missing
        logger.warning("REDIS_URL not set – throttling disabled")
        return None
    try:
        r = redis.from_url(url, decode_responses=True)
        r.ping()                                     # connection test
        logger.info("Connected to Redis for rate-limiting")
        return r
    except redis.exceptions.RedisError as e:
        logger.warning("Redis unreachable – throttling disabled: %s", e)
        return None


rdb: Optional[redis.Redis] = _make_redis(str(settings.redis_url))  # type: ignore[arg-type]

# ---------------------------------------------------------------------------
#  Lua token-bucket scripts (only if Redis is available)
# ---------------------------------------------------------------------------

if rdb:
    _IP_LUA = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local ttl   = tonumber(ARGV[2])
    local n = redis.call('INCR', key)
    if n == 1 then redis.call('EXPIRE', key, ttl) end
    return n <= limit
    """

    _SESSION_LUA = """
    local key = KEYS[1]
    local budget = tonumber(ARGV[1])
    local used   = tonumber(ARGV[2])
    local ttl    = tonumber(ARGV[3])
    local remain = tonumber(redis.call('GET', key) or budget) - used
    if remain < 0 then return -1 end
    redis.call('SET', key, remain, 'EX', ttl)
    return remain
    """

    _ip_script      = rdb.register_script(_IP_LUA)
    _session_script = rdb.register_script(_SESSION_LUA)
else:
    _ip_script = _session_script = None   # type: ignore[assignment]

# ---------------------------------------------------------------------------
#  Limits
# ---------------------------------------------------------------------------
_IP_LIMIT       = 20
_IP_WINDOW_SEC  = 60
_SESSION_BUDGET = 15_000
_SESSION_TTL    = 60 * 30     # 30 min idle timeout


# ---------------------------------------------------------------------------
#  Public helpers
# ---------------------------------------------------------------------------
def ip_throttle(ip: str) -> bool:
    """True → still allowed, False → block.  
       When Redis is absent, always returns True."""
    if not _ip_script:
        return True

    allowed = bool(_ip_script(
        keys=[f"rl:ip:{ip}"],
        args=[_IP_LIMIT, _IP_WINDOW_SEC],
    ))
    if not allowed:
        logger.warning("IP rate-limit exceeded: %s", ip)
    return allowed


def consume_tokens(session_id: str, tokens: int) -> bool:
    """True while budget left; False when exhausted.  
       When Redis is absent, always returns True."""
    if not _session_script:
        return True

    remain = _session_script(
        keys=[f"rl:tok:{session_id}"],
        args=[_SESSION_BUDGET, tokens, _SESSION_TTL],
    )
    if remain == -1:
        logger.warning("Token budget exhausted for session %s", session_id)
        return False

    logger.debug("Session %s tokens left: %s", session_id, remain)
    return True
