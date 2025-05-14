"""
omnichat.app
============
FastAPI entry-point exposing:

• /health               – uptime probe
• /metrics  (optional)  – Prometheus if wired in
• /ws                   – bidirectional chat WebSocket

Key integrations:
• Error & request-log middleware
• Redis-backed rate-limit + token budget
• Guard-rails (OpenAI moderation, off-topic cosine)
• Pinecone RAG + GPT-4o answer
"""

from __future__ import annotations

import uuid
from typing import List

import openai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

from omnichat_log import logger, RequestLogMiddleware
from middleware.error import ErrorMiddleware
from rate_limit import ip_throttle, consume_tokens
from services.pinecone import query as pc_query, max_similarity
from settings import settings
from guardrails import toxic_or_blocked, scrub

# ── FastAPI bootstrap ─────────────────────────────────────────────────────────
app = FastAPI(
    title="Omnichat – Spectraflex",
    version="1.0.0",
    docs_url="/docs" if settings.env == "dev" else None,
    redoc_url=None,
)

app.add_middleware(ErrorMiddleware)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # widget may load from any storefront domain
    allow_methods=["*"],
    allow_headers=["*"],
)

openai.api_key = settings.openai_api_key.get_secret_value()

# ── Schemas ───────────────────────────────────────────────────────────────────
class _WsIn(BaseModel):
    session: str | None = None
    message: str


class _WsOut(BaseModel):
    session: str
    answer: str


# ── Config knobs (pulling threshold from env / settings) ──────────────────────
OFF_TOPIC_THRESHOLD: float = float(
    getattr(settings, "off_topic_threshold", 0.60)   # default 0.60
)

MAX_OPENAI_TOKENS_PER_REPLY = 512

# ── Helpers ───────────────────────────────────────────────────────────────────
def _retrieve_context(q: str, k: int = 4) -> List[dict]:
    matches = pc_query(q, k=k)
    return [{**m.metadata, "score": m.score} for m in matches]


def _build_prompt(question: str, context: List[dict]) -> str:
    ctx_txt = "\n".join(
        f"- {c['title']} (/{c['handle']}) score={c['score']:.2f}" for c in context
    )
    return (
        "You are Spectraflex Gear Concierge. "
        "Answer only about Spectraflex products. "
        "If unsure, say you don't know.\n\n"
        f"Context:\n{ctx_txt or 'NO_MATCH'}\n\n"
        f"Q: {question}\nA:"
    )

# ── Routes ────────────────────────────────────────────────────────────────────
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

            # --- validation ---------------------------------------------------
            try:
                req = _WsIn.model_validate_json(raw)
            except ValidationError as e:
                await ws.send_json({"error": "Invalid payload", "details": e.errors()})
                continue

            ip = ws.client.host
            if not ip_throttle(ip):
                await ws.close(code=4008, reason="Rate limit")
                break

            if toxic_or_blocked(req.message):
                await ws.send_json(
                    _WsOut(session=session_id, answer="Sorry, can’t help with that.").model_dump()
                )
                continue

            # --- off-topic guard ---------------------------------------------
            sim = max_similarity(req.message)
            logger.debug("cosine %.3f  msg=%s", sim, req.message)

            if sim < OFF_TOPIC_THRESHOLD:
                await ws.send_json(
                    _WsOut(
                        session=session_id,
                        answer="I’m here for Spectraflex gear questions only 😊",
                    ).model_dump()
                )
                continue

            # --- RAG + OpenAI -------------------------------------------------
            context = _retrieve_context(req.message)
            prompt  = _build_prompt(req.message, context)

            chat = openai.chat.completions.create(
                model=settings.openai_model_chat,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=MAX_OPENAI_TOKENS_PER_REPLY,
            )
            answer       = chat.choices[0].message.content.strip()
            tokens_used  = chat.usage.total_tokens

            if not consume_tokens(session_id, tokens_used):
                await ws.send_json(
                    _WsOut(
                        session=session_id,
                        answer="You've reached the limit for this chat. "
                               "Start a new conversation if needed!",
                    ).model_dump()
                )
                await ws.close()
                break

            # --- send ---------------------------------------------------------
            await ws.send_json(_WsOut(session=session_id, answer=answer).model_dump())

    except WebSocketDisconnect:
        logger.info("WS disconnected [%s]", session_id)
    except Exception:                             # pylint: disable=broad-except
        logger.exception("Unexpected WS error [%s]", session_id)
        await ws.close(code=1011)  # internal error
