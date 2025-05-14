"""
omnichat.app
============
FastAPI entry‑point for Spectraflex concierge – **Redis‑backed** version

Key changes vs. previous draft
──────────────────────────────
• **Redis** replaces the in‑memory `_HISTORY`, `_LAST_PID`, and token counters so
  multiple Render dynos (or a restart) keep chat state alive.
• Uses the `redis‑py` asyncio client (`redis.asyncio`) that plays nicely inside
  FastAPI WebSocket handlers.
• Helper fns `_get_hist()`, `_set_hist()` and siblings abstract the cache layer.
• Removed the temporary `os.environ["REQUESTS_CA_BUNDLE"]…` workaround – you now
  point `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` env‑vars in Render instead.
"""

from __future__ import annotations
import uuid, html, json, asyncio
from typing import List, Dict, Any

import openai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

import redis.asyncio as aioredis  # NEW – async client

from omnichat_log import logger, RequestLogMiddleware
from middleware.error import ErrorMiddleware
from rate_limit import ip_throttle, consume_tokens
from services.pinecone import query as pc_query, max_similarity
from services.shopify_cart import create_checkout
from settings import settings
from guardrails import toxic_or_blocked

# ── Redis connection ────────────────────────────────────────────────────────
redis_url = str(settings.redis_url)
redis: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)

# ── knobs ────────────────────────────────────────────────────────────────────
OFF_TOPIC_THRESHOLD: float = float(getattr(settings, "off_topic_threshold", 0.60))
MAX_OPENAI_TOKENS_PER_REPLY = 512
MAX_TURNS = 6               # per session history window
REDIS_TTL = 24 * 3600       # keep sessions for a day

openai.api_key = settings.openai_api_key.get_secret_value()

# ── FastAPI bootstrap ────────────────────────────────────────────────────────
app = FastAPI(title="Omnichat – Spectraflex", docs_url=None, redoc_url=None)
app.add_middleware(ErrorMiddleware)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Schemas ──────────────────────────────────────────────────────────────────
class _WsIn(BaseModel):
    session: str | None = None
    message: str

class _WsOut(BaseModel):
    session: str
    answer: str

# ── Redis helpers ────────────────────────────────────────────────────────────
async def _get_json(key: str, default: Any) -> Any:
    raw = await redis.get(key)
    return json.loads(raw) if raw else default

async def _set_json(key: str, value: Any, ttl: int = REDIS_TTL):
    await redis.set(key, json.dumps(value), ex=ttl)

# chat history ---------------------------------------------------------------
async def get_history(session: str) -> List[dict]:
    return await _get_json(f"hist:{session}", [])

async def set_history(session: str, hist: List[dict]):
    await _set_json(f"hist:{session}", hist)

# last recommended product ---------------------------------------------------
async def get_last_pid(session: str) -> str | None:
    return await redis.get(f"lastpid:{session}")

async def set_last_pid(session: str, pid: str):
    await redis.set(f"lastpid:{session}", pid, ex=REDIS_TTL)

# ── Pinecone helpers (unchanged) ─────────────────────────────────────────────

def _retrieve_context(q: str, k: int = 4) -> List[dict]:
    matches = pc_query(q, k=k)
    return [
        {**m.metadata, "score": m.score, "id": m.id}
        for m in matches if m.metadata
    ]

def _build_prompt(question: str, context: List[dict]) -> str:
    ctx_txt = "\n".join(
        f"- {html.unescape(c['title'])} (https://{settings.shop_url.host}/products/{c['handle']}) score={c['score']:.2f}"
        for c in context if "title" in c
    ) or "NO_MATCH"
    return (
        "You are Spectraflex Gear Concierge. Answer only about Spectraflex products. If unsure, say you don't know.\n\n"
        f"Context:\n{ctx_txt}\n\nQ: {question}\nA:"
    )

# ── routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        await redis.ping()
        return {"status": "ok"}
    except Exception:  # pragma: no cover
        return {"status": "degraded", "redis": "unreachable"}

@app.websocket("/ws")
async def chat_ws(ws: WebSocket):
    await ws.accept()
        # 0️⃣  session + greeting (runs once per connection)
    # ------------------------------------------------------------------
    session_id  = str(uuid.uuid4())

    # merchant-scoped:  ?merchant_id=frankman   →  greeting:frankman
    merchant_id = ws.query_params.get("merchant_id", "spectraflex")
    greeting    = await redis.get(f"greeting:{merchant_id}") \
                 or "🎸 Welcome to Spectraflex! Whether you’re hunting for the perfect cable, curious about custom lengths & colors, or just need quick advice on matching gear to your rig, I’ve got you covered. Tell me what you’re looking for and I’ll point you to the best fit—let’s dial in your sound!"

    # push the first bubble right away
    await ws.send_json(_WsOut(session=session_id, answer=greeting).model_dump())

    session_id = str(uuid.uuid4())

    try:
        while True:
            raw = await ws.receive_text()

            # ── validation ────────────────────────────────────────────────
            try:
                req = _WsIn.model_validate_json(raw)
            except ValidationError as e:
                await ws.send_json({"error": "Invalid payload", "details": e.errors()})
                continue

            # ── throttles & guardrails ────────────────────────────────────
            ip = ws.client.host
            if not ip_throttle(ip):
                await ws.close(code=4008, reason="Rate limit"); break

            if toxic_or_blocked(req.message):
                await ws.send_json(_WsOut(session=session_id,
                                           answer="Sorry, can’t help with that.").model_dump())
                continue

            # ── cart intent ------------------------------------------------
            text_l = req.message.lower()
            if "cart" in text_l and any(k in text_l for k in ("add", "buy", "purchase")):
                pid = await get_last_pid(session_id)
                if not pid:
                    await ws.send_json(_WsOut(session=session_id,
                                              answer="Which cable would you like to add?").model_dump())
                    continue
                checkout_url = create_checkout(pid)
                await ws.send_json(_WsOut(session=session_id,
                                          answer=f"✅ Added! Finish checkout here: {checkout_url}").model_dump())
                continue

            # ── RAG guard --------------------------------------------------
            sim = max_similarity(req.message)
            logger.debug("sim %.3f  | %s", sim, req.message)
            if sim < OFF_TOPIC_THRESHOLD:
                await ws.send_json(_WsOut(session=session_id,
                                          answer="I’m here for Spectraflex gear questions only 😊").model_dump())
                continue

            # ── context & memory ------------------------------------------
            context = _retrieve_context(req.message)
            if context:
                await set_last_pid(session_id, context[0]["id"])

            hist = await get_history(session_id)
            hist.append({"role": "user", "content": req.message})
            hist[:] = hist[-MAX_TURNS:]

            sys_prompt = _build_prompt(req.message, context)
            messages  = hist + [{"role": "system", "content": sys_prompt}]

            # ── GPT call ---------------------------------------------------
            chat = openai.chat.completions.create(
                model=settings.openai_model_chat,
                messages=messages,
                max_tokens=MAX_OPENAI_TOKENS_PER_REPLY,
            )
            answer = chat.choices[0].message.content.strip()
            hist.append({"role": "assistant", "content": answer})
            await set_history(session_id, hist)

            tokens_used = chat.usage.total_tokens
            if not consume_tokens(session_id, tokens_used):
                await ws.send_json(_WsOut(session_id, "Token budget exhausted.").model_dump())
                await ws.close(); break

            await ws.send_json(_WsOut(session=session_id, answer=answer).model_dump())

    except WebSocketDisconnect:
        logger.info("WS disconnected [%s]", session_id)
    except Exception:   # pylint: disable=broad-except
        logger.exception("WS error [%s]", session_id)
        await ws.close(code=1011)
