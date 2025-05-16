"""
omnichat.app
============
FastAPI entry-point for Spectraflex concierge – **Redis-backed** version
"""

from __future__ import annotations

import uuid, html, json, re
from typing import Any, List

import openai
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

from omnichat_log import logger, RequestLogMiddleware
from middleware.error import ErrorMiddleware
from rate_limit import ip_throttle, consume_tokens
from guardrails import toxic_or_blocked
from services.pinecone import query as pc_query, max_similarity
from services.shopify_cart import create_checkout
from settings import settings
from fastapi.staticfiles import StaticFiles


# ─────────────────────────────────────────────────────────────────────────────
# Redis connection
# ─────────────────────────────────────────────────────────────────────────────
redis: aioredis.Redis = aioredis.from_url(
    str(settings.redis_url), decode_responses=True
)

# ─────────────────────────────────────────────────────────────────────────────
# Config / knobs
# ─────────────────────────────────────────────────────────────────────────────
OFF_TOPIC_THRESHOLD = 0.20        # ← loosened so amp/cab Qs pass
MAX_OPENAI_TOKENS_PER_REPLY = 512
MAX_TURNS = 6                     # history window (user ⇄ bot pairs)
REDIS_TTL = 24 * 3600             # keep sessions a day

openai.api_key = settings.openai_api_key.get_secret_value()

# ─────────────────────────────────────────────────────────────────────────────
# Base prompt (used by both strict RAG + fallback calls)
# ─────────────────────────────────────────────────────────────────────────────
BASE_PROMPT = """
You are **Spectraflex Gear Concierge**.

Product guard-rails
• You may draw on general knowledge about music gear (amps, cabs, guitars,
  pedals, mixers, DAWs, live-sound, etc.).
• When you recommend a cable or accessory, it must be **either**  
  – an explicit Spectraflex SKU **or**  
  – a generic descriptor (“a 1/4-inch TS speaker cable”) that maps to a
    Spectraflex SKU if the shopper clicks **Buy**.
• Never recommend or endorse a competitor’s cable brand.
• If Spectraflex doesn’t make a matching product, politely say so and suggest
  the closest Spectraflex alternative.

Conversation style
• **It's ok to ask the shopper questions (if you actually are unsure yourself and need to clarify, but you should generally know).** whenever the shopper’s need is
  ambiguous (e.g. ask about required length, connector type, instrument, stage /
  studio use, cable color preference, budget, etc.).  
  – Keep questions short & friendly.  
  – Stop asking once you have enough detail to recommend confidently, and if you can recommend confidently, dont ask.
• **Be proactive**: if the shopper sounds unsure, offer sizing tips,
  maintenance advice, or mention bundle options.
• Keep answers concise, upbeat and jargon-light (avoid engineering tangents, unless the shopper starts it and seems interested and its relevent to the spectraflex products.).
▪️ If the shopper seems unsure (e.g. answers “I’m not sure”, “IDK”), proactively propose the most common option and explain why.

Link & formatting rules
• 
  You may cite products like this:

- [{{title}}]({{url}})  
  – The RAG context already feeds you each product’s *title* and *handle*.
• After the link, add a one-line benefit (“Pure copper core for stage-quiet
  tone”, etc.). Bulleted lists are fine.
• If you mention multiple options, list them as separate bullets.
• After listing a product link, end the bullet with  
  “ _(reply **add to cart** if you’d like me to start checkout)_”.

"""

# ─────────────────────────────────────────────────────────────────────────────
# Guard-rail: block competitor cable brands in answers
# ─────────────────────────────────────────────────────────────────────────────
_COMPETITOR_CABLES = re.compile(
    r"\b(mogami|planet\s*waves|hosa|d['’]?addario|fender|ernie\s*ball)\b",
    re.I,
)

def scrub_competitors(txt: str) -> str:
    """If GPT ever leaks a rival cable brand, replace with brand-safe line."""
    if _COMPETITOR_CABLES.search(txt):
        return ("I’m sorry — I only recommend Spectraflex cables. "
                "Here’s the closest option we make: Classic Series 1/4-inch "
                "TS speaker cable (various lengths).")
    return txt


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI setup
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Omnichat – Spectraflex", docs_url=None, redoc_url=None)
app.add_middleware(ErrorMiddleware)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# ── expose the front-end widget files ───────────────────────────
app.mount(
    "/widget",                          # URL prefix
    StaticFiles(directory="widget", html=True),
    name="widget",
)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic I/O schemas
# ─────────────────────────────────────────────────────────────────────────────
class _WsIn(BaseModel):
    session: str | None = None
    message: str

class _WsOut(BaseModel):
    session: str
    answer: str


# ─────────────────────────────────────────────────────────────────────────────
# Redis helpers
# ─────────────────────────────────────────────────────────────────────────────
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

# last recommended product (for “add to cart” intent) ------------------------
async def get_last_pid(session: str) -> str | None:
    return await redis.get(f"lastpid:{session}")

async def set_last_pid(session: str, pid: str):
    await redis.set(f"lastpid:{session}", pid, ex=REDIS_TTL)

# ─────────────────────────────────────────────────────────────────────────────
# Pinecone helpers
# ─────────────────────────────────────────────────────────────────────────────
def _retrieve_context(q: str, k: int = 4) -> List[dict]:
    matches = pc_query(q, k=k)
    return [
        {**m.metadata, "score": m.score, "id": m.id}
        for m in matches if m.metadata
    ]

def _prompt_with_context(question: str, ctx: List[dict]) -> str:
    ctx_txt = "\n".join(
        f"- {html.unescape(c['title'])} "
        f"(https://{settings.shop_url.host}/products/{c['handle']}) "
        f"score={c['score']:.2f}"
        for c in ctx if "title" in c
    ) or "NO_MATCH"
    return (
        BASE_PROMPT +
        f"\n\nContext:\n{ctx_txt}\n\nQ: {question}\nA:"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Dual-path answer helper
# ─────────────────────────────────────────────────────────────────────────────
async def smart_answer(
    user_q: str,
    context: list[dict],
    hist: list[dict],
) -> tuple[str, int]:
    """1) strict product RAG, 2) brand-safe general fallback.
    Returns: (answer, tokens_used)
    """
    # --- strict path -------------------------------------------------------
    prompt = _prompt_with_context(user_q, context)
    chat   = openai.chat.completions.create(
        model = settings.openai_model_chat,
        messages = hist + [{"role":"system", "content": prompt}],
        max_tokens = MAX_OPENAI_TOKENS_PER_REPLY,
    )
    answer       = chat.choices[0].message.content.strip()
    tokens_spent = chat.usage.total_tokens

    # if GPT gives up, use fallback reasoning
    if answer.lower().startswith(("i don't know", "i do not know", "unsure")):
        loose_prompt = (
            BASE_PROMPT +
            "\n\n(You didn’t find an exact Spectraflex product above. "
            "Use your broad gear knowledge to advise, but still steer "
            "toward a Spectraflex cable when appropriate.)"
            f"\n\nQ: {user_q}\nA:"
        )
        chat2   = openai.chat.completions.create(
            model = settings.openai_model_chat,
            messages = hist + [{"role":"system", "content": loose_prompt}],
            max_tokens = MAX_OPENAI_TOKENS_PER_REPLY,
        )
        answer        = chat2.choices[0].message.content.strip()
        tokens_spent += chat2.usage.total_tokens

    return scrub_competitors(answer), tokens_spent


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    try:
        await redis.ping()
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded", "redis": "unreachable"}


@app.websocket("/ws")
async def chat_ws(ws: WebSocket):
    await ws.accept()

    # 0️⃣ greeting ----------------------------------------------------------
    session_id  = str(uuid.uuid4())
    merchant_id = ws.query_params.get("merchant_id", "spectraflex")
    greeting    = await redis.get(f"greeting:{merchant_id}") or (
        "🎸 Welcome to Spectraflex! Whether you’re hunting for the perfect "
        "cable, curious about custom lengths & colours, or just need quick "
        "advice on matching gear to your rig, I’ve got you covered. Tell me "
        "what you’re after and I’ll point you to the best fit—let’s dial in "
        "your sound!"
    )
    await ws.send_json(_WsOut(session=session_id, answer=greeting).model_dump())

    try:
        while True:
            raw = await ws.receive_text()

            # ── validation -------------------------------------------------
            try:
                req = _WsIn.model_validate_json(raw)
            except ValidationError as e:
                await ws.send_json({"error": "Invalid payload",
                                    "details": e.errors()})
                continue

            # ── guardrails & rate limiting ---------------------------------
            ip = ws.client.host
            if not ip_throttle(ip):
                await ws.close(code=4008, reason="Rate limit"); break

            if toxic_or_blocked(req.message):
                await ws.send_json(_WsOut(session=session_id,
                                          answer="Sorry—can’t help with that.").model_dump())
                continue

            # ── instant add-to-cart intent --------------------------------
            text_l = req.message.lower()
            if "cart" in text_l and any(k in text_l for k in ("add", "buy", "purchase",)):
                pid = await get_last_pid(session_id)
                if not pid:
                    await ws.send_json(_WsOut(session=session_id,
                                              answer="Which cable would you like to add?").model_dump())
                    continue
                checkout = create_checkout(pid)
                await ws.send_json(_WsOut(session=session_id,
                                          answer=f"✅ Added! Finish checkout here: {checkout}").model_dump())
                continue

            # ── off-topic gate (non-gear chit-chat etc.) -------------------
            if max_similarity(req.message) < OFF_TOPIC_THRESHOLD:
                await ws.send_json(_WsOut(session=session_id,
                                          answer="I’m here for Spectraflex gear questions only 😊").model_dump())
                continue

            # ── retrieve context + update memory ---------------------------
            context = _retrieve_context(req.message)
            if context:
                meta         = context[0]
                variant_gid  = meta.get("variantId") or meta["id"]
                await set_last_pid(session_id, variant_gid)

            hist = await get_history(session_id)
            hist.append({"role": "user", "content": req.message})
            hist[:] = hist[-MAX_TURNS:]

            # ── GPT answer --------------------------------------------------
            answer, spent = await smart_answer(req.message, context, hist)
            hist.append({"role": "assistant", "content": answer})
            await set_history(session_id, hist)

            if not consume_tokens(session_id, spent):
                await ws.send_json(_WsOut(session=session_id,
                                          answer="Token budget exhausted.").model_dump())
                await ws.close(); break

            await ws.send_json(_WsOut(session=session_id, answer=answer).model_dump())

    except WebSocketDisconnect:
        logger.info("WS disconnected [%s]", session_id)
    except Exception:                     # pylint: disable=broad-except
        logger.exception("WS error [%s]", session_id)
        await ws.close(code=1011)
