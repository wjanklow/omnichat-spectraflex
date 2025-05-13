"""
Simple Redis-backed limiters
• ip_throttle()      – 20 msgs / 60 s per IP
• consume_tokens()   – 15 K OpenAI tokens per chat session
"""

from __future__ import annotations
import redis
from settings import settings
from omnichat_log import logger  # adjust import if you renamed the file

# Redis connection
rdb: redis.Redis = redis.from_url(str(settings.redis_url), decode_responses=True)

# Lua token-bucket scripts
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

# register_script returns a **callable**, we don’t need the Script class
_ip_script     = rdb.register_script(_IP_LUA)
_session_script = rdb.register_script(_SESSION_LUA)

# limits
_IP_LIMIT       = 20
_IP_WINDOW_SEC  = 60
_SESSION_BUDGET = 15_000
_SESSION_TTL    = 60 * 30  # 30 min idle timeout

def ip_throttle(ip: str) -> bool:
    """True → still allowed, False → block."""
    allowed = bool(_ip_script(keys=[f"rl:ip:{ip}"],
                              args=[_IP_LIMIT, _IP_WINDOW_SEC]))
    if not allowed:
        logger.warning(f"IP rate-limit exceeded: {ip}")
    return allowed

def consume_tokens(session_id: str, tokens: int) -> bool:
    """True while budget left; False when exhausted."""
    remain = _session_script(keys=[f"rl:tok:{session_id}"],
                             args=[_SESSION_BUDGET, tokens, _SESSION_TTL])
    if remain == -1:
        logger.warning(f"Token budget exhausted for session {session_id}")
        return False
    logger.debug(f"Session {session_id} tokens left: {remain}")
    return True
