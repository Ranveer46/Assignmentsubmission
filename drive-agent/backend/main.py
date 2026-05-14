import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from agent import create_agent
from env_loader import load_project_dotenv

load_project_dotenv()

# Cap history length so each /chat request stays smaller (token / context limits).
CHAT_HISTORY_MAX_MESSAGES = int(os.getenv("CHAT_HISTORY_MAX_MESSAGES", "28"))

# ─────────────────────────────────────────────
# App init
# ─────────────────────────────────────────────

app = FastAPI(
    title="Google Drive AI Agent API",
    description="Conversational API: multi-turn chat that searches and filters a shared Google Drive folder via natural language.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lazy-init so /health works even when GOOGLE_API_KEY is not loaded yet
_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


# In-memory session store: session_id → list of LangChain messages
sessions: dict[str, list] = {}


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    response: str
    session_id: str


def _trim_history(msgs: list) -> list:
    """Keep only the last N messages; drop leading ToolMessages to avoid a broken tool chain."""
    if len(msgs) <= CHAT_HISTORY_MAX_MESSAGES:
        return msgs
    tail = list(msgs[-CHAT_HISTORY_MAX_MESSAGES :])
    while tail and isinstance(tail[0], ToolMessage):
        tail.pop(0)
    return tail


def _is_provider_rate_limit(exc: BaseException) -> bool:
    s = str(exc).lower()
    return (
        "429" in str(exc)
        or "resource_exhausted" in s
        or ("quota" in s and "exceed" in s)
        or "rate_limit" in s
        or "rate limit" in s
    )


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "Google Drive AI Agent is running 🚀", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Send a message to the Drive agent and receive a response.
    Chat history is maintained per session_id.
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    history = sessions.setdefault(req.session_id, [])

    trimmed = _trim_history(history)
    if len(trimmed) < len(history):
        history[:] = trimmed

    # Build messages: history + new user message
    messages = history + [HumanMessage(content=req.message)]

    try:
        result = get_agent().invoke({"messages": messages})
    except Exception as e:
        if _is_provider_rate_limit(e):
            raise HTTPException(
                status_code=429,
                detail=(
                    "LLM provider rate limit or quota (often Google Gemini). Wait and retry, use "
                    "Clear Chat to shorten context, try a smaller/faster model via GEMINI_MODEL in .env, "
                    "or check quotas at https://aistudio.google.com/ and https://ai.google.dev/pricing"
                ),
            )
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")

    # Extract the final AI response from the result messages
    output_messages = result.get("messages", [])
    reply = ""
    for msg in reversed(output_messages):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break

    if not reply:
        reply = "I couldn't generate a response. Please try again."

    # Persist capped history so sessions do not grow without bound (saves Groq TPD).
    sessions[req.session_id] = _trim_history(output_messages)

    return ChatResponse(response=reply, session_id=req.session_id)


@app.delete("/session/{session_id}")
async def clear_session(session_id: str):
    """Clear the chat history for a given session."""
    sessions.pop(session_id, None)
    return {"message": f"Session {session_id} cleared."}


@app.get("/sessions")
async def list_sessions():
    """List all active session IDs and their message counts."""
    return {sid: len(msgs) for sid, msgs in sessions.items()}
