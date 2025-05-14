"""
omnichat.app
============
FastAPI entry-point for Spectraflex concierge
"""
from __future__ import annotations
import uuid, html
from typing import List, Dict

import openai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

from omnichat_log import logger, RequestLogMiddleware
from middleware.error import ErrorMiddleware
from rate_limit import ip_throttle, consume_tokens
from services.pinecone import query as pc_query, max_similarity
from services.shopify_cart import create_checkout         # NEW
from settings import settings
from guardrails import toxic_or_blocked

import os, certifi
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# ── short-term memory (per websocket) ────────────────────────────────────────
_HISTORY: Dict[str, List[dict]] = {}      # session_id → [ {role, content}, … ]
MAX_TURNS = 6

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

openai.api_key = settings.openai_api_key.get_secret_value()

# ── Schemas ──────────────────────────────────────────────────────────────────
class _WsIn(BaseModel):
    session: str | None = None
    message: str

class _WsOut(BaseModel):
    session: str
    answer: str

# ── knobs ────────────────────────────────────────────────────────────────────
OFF_TOPIC_THRESHOLD = float(getattr(settings, "off_topic_threshold", 0.60))
MAX_OPENAI_TOKENS_PER_REPLY = 512

# ── helpers ──────────────────────────────────────────────────────────────────
def _retrieve_context(q: str, k: int = 4) -> List[dict]:
    matches = pc_query(q, k=k)            # metadata already has title/handle
    return [{**m.metadata, "score": m.score, "id": m.id} for m in matches]

def _build_prompt(question: str, context: List[dict]) -> str:
    ctx_txt = "\n".join(
        f"- {html.unescape(c['title'])} "
        f"(https://{settings.shop_url.host}/products/{c['handle']}) "
        f"score={c['score']:.2f}"
        for c in context if "title" in c
    ) or "NO_MATCH"
    return (
        "You are Spectraflex Gear Concierge. Answer only about Spectraflex "
        "products. If unsure, say you don't know.\n\n"
        f"Context:\n{ctx_txt}\n\n"
        f"Q: {question}\nA:"
    )

# last recommended product id per session
_LAST_PID: Dict[str, str] = {}

# ── routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}

@app.websocket("/ws")
async def chat_ws(ws: WebSocket):
    await ws.accept()
    session_id = str(uuid.uuid4())

    try:
        while True:
            raw = await ws.receive_text()

            # validation ------------------------------------------------------
            try:
                req = _WsIn.model_validate_json(raw)
            except ValidationError as e:
                await ws.send_json({"error": "Invalid payload", "details": e.errors()})
                continue

            # simple throttles & guardrails -----------------------------------
            ip = ws.client.host
            if not ip_throttle(ip):
                await ws.close(code=4008, reason="Rate limit"); break

            if toxic_or_blocked(req.message):
                await ws.send_json(_WsOut(session=session_id,
                                          answer="Sorry, can’t help with that.").model_dump())
                continue

            # intent: add to cart ---------------------------------------------
            text_l = req.message.lower()
            if "cart" in text_l and any(k in text_l for k in ("add", "buy", "purchase")):
                pid = _LAST_PID.get(session_id)
                if not pid:
                    await ws.send_json(_WsOut(session=session_id,
                                              answer="Which cable would you like to add?").model_dump())
                    continue
                checkout_url = create_checkout(pid)
                await ws.send_json(_WsOut(session=session_id,
                                          answer=f"✅ Added! Finish checkout here: {checkout_url}").model_dump())
                continue

            # RAG guard --------------------------------------------------------
            sim = max_similarity(req.message)
            logger.debug("sim %.3f  | %s", sim, req.message)
            if sim < OFF_TOPIC_THRESHOLD:
                await ws.send_json(_WsOut(session=session_id,
                                          answer="I’m here for Spectraflex gear questions only 😊").model_dump())
                continue

            # build context & memory ------------------------------------------
            context = _retrieve_context(req.message)
            if context:
                _LAST_PID[session_id] = context[0]["id"]          # remember

            hist = _HISTORY.setdefault(session_id, [])
            hist.append({"role": "user", "content": req.message})
            hist[:] = hist[-MAX_TURNS:]

            sys_prompt = _build_prompt(req.message, context)
            messages  = hist + [{"role": "system", "content": sys_prompt}]

            # GPT call ---------------------------------------------------------
            chat = openai.chat.completions.create(
                model=settings.openai_model_chat,
                messages=messages,
                max_tokens=MAX_OPENAI_TOKENS_PER_REPLY,
            )
            answer = chat.choices[0].message.content.strip()
            hist.append({"role": "assistant", "content": answer})

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
