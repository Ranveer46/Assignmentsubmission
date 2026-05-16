"""
main.py — FastAPI backend for the Google Drive conversational agent

Endpoints:
  POST /chat          — send a message, get a streamed reply
  GET  /health        — cache stats + service health
  GET  /files         — list all indexed files (for debugging)
  GET  /files/{id}    — get a single file's metadata

Run:
  uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import create_agent
from drive_client import (
    get_cache_stats,
    get_file_by_id,
    get_folder_tree,
    get_recent_files,
    init_drive_cache,
    list_all_files,
)
from env_loader import load_project_dotenv

load_project_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────

app = FastAPI(
    title="Drive Agent API",
    description="Conversational AI agent for Google Drive file discovery.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# Startup — index the Drive folder
# ─────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """
    On startup:
      1. Connect to Google Drive and recursively index the shared folder.
      2. Start background refresh thread (every CACHE_REFRESH_SECS seconds).
      3. Create the LangGraph agent.
    """
    logger.info("Starting Drive Agent backend…")
    try:
        init_drive_cache()       # kicks off recursive scan + background refresh
        logger.info("Drive cache initialised successfully.")
    except Exception as exc:
        logger.error("Drive cache initialisation failed: %s", exc)
        # Don't crash the server — searches will fall back to API calls


# ─────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────

class Message(BaseModel):
    role: str       # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[Message] = []   # full conversation so far


class ChatResponse(BaseModel):
    reply: str


# ─────────────────────────────────────────────
# Agent singleton (created lazily)
# ─────────────────────────────────────────────

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Send a user message to the Drive agent and get a reply.

    The `history` field carries the full conversation so the agent
    can handle follow-up questions ("those", "narrow it to PDFs", etc.).

    Example request:
        {
          "message": "find invoices from last month",
          "history": [
            {"role": "user",      "content": "hi"},
            {"role": "assistant", "content": "Hello! How can I help with your Drive?"}
          ]
        }
    """
    agent = _get_agent()

    # Build LangGraph message list: history + new user message
    messages = []
    for m in req.history:
        content = m.content
        # Truncate huge assistant responses in history to save Tokens Per Minute (TPM)
        if m.role == "assistant" and len(content) > 800:
            content = content[:800] + "\n... [History truncated to save Groq token limits]"
        messages.append({"role": m.role, "content": content})
    messages.append({"role": "user", "content": req.message})

    try:
        result = agent.invoke({"messages": messages})
        # LangGraph returns the full message list; last message is the reply
        reply_msg = result["messages"][-1]
        reply_text = (
            reply_msg.content
            if isinstance(reply_msg.content, str)
            else str(reply_msg.content)
        )
        return ChatResponse(reply=reply_text)

    except Exception as exc:
        logger.exception("Agent invocation failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Streaming variant of /chat.
    Returns server-sent events so the frontend can show a typing effect.

    Usage in the frontend:
        const res = await fetch('/chat/stream', { method: 'POST', body: ... })
        const reader = res.body.getReader()
        ...
    """
    agent = _get_agent()

    messages = []
    for m in req.history:
        content = m.content
        if m.role == "assistant" and len(content) > 800:
            content = content[:800] + "\n... [History truncated to save Groq token limits]"
        messages.append({"role": m.role, "content": content})
    messages.append({"role": "user", "content": req.message})

    async def token_stream() -> AsyncIterator[str]:
        try:
            async for event in agent.astream_events(
                {"messages": messages}, version="v2"
            ):
                kind = event.get("event")
                # Emit only LLM text chunks
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        import json
                        encoded = json.dumps(chunk.content)
                        yield f"data: {encoded}\n\n"
        except Exception as exc:
            logger.exception("Streaming error")
            yield f"data: [ERROR] {exc}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        token_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    """
    Returns Drive cache statistics and server health.
    Useful to confirm the folder was indexed correctly.

    Example response:
        {
          "status": "ok",
          "cache": {
            "total_files": 142,
            "last_refresh": 1715000000.0,
            "refresh_interval_secs": 300,
            "mime_breakdown": { "application/pdf": 40, "image/png": 22, ... }
          }
        }
    """
    stats = get_cache_stats()
    return {"status": "ok", "cache": stats}


@app.get("/files")
async def list_files_endpoint(limit: int = 200):
    """
    List all indexed files (capped at `limit`).
    Useful for debugging — check what the agent can see.
    """
    files = list_all_files()[:limit]
    return {"total": len(list_all_files()), "returned": len(files), "files": files}


@app.get("/files/recent")
async def recent_files(n: int = 20):
    """Return the n most recently modified files."""
    return {"files": get_recent_files(n)}


@app.get("/files/tree")
async def folder_tree():
    """Return the folder hierarchy as a path → files map."""
    return {"tree": get_folder_tree()}


@app.get("/files/{file_id}")
async def get_file(file_id: str):
    """Return metadata for a single file by Drive ID."""
    f = get_file_by_id(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return f
