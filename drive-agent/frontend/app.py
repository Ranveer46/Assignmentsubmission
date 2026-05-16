import streamlit as st
import requests
import uuid
import os
from pathlib import Path

from dotenv import load_dotenv

# .env lives next to backend/ and frontend/, not inside frontend/
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
# Drive searches can exceed 60s on large folder trees (first walk + list).
CHAT_REQUEST_TIMEOUT = int(os.getenv("CHAT_REQUEST_TIMEOUT", "180"))

st.set_page_config(
    page_title="Drive AI Agent",
    page_icon="🗂️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# Custom CSS — premium dark theme
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* Global reset */
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
}

/* Dark gradient background */
.stApp {
    background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
    min-height: 100vh;
}

/* Header area */
.main-header {
    text-align: center;
    padding: 2rem 0 1rem 0;
}
.main-header h1 {
    font-size: 2.4rem;
    font-weight: 700;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin: 0;
}
.main-header p {
    color: #8892b0;
    font-size: 0.95rem;
    margin-top: 0.4rem;
}

/* Chat container */
.stChatMessage {
    border-radius: 16px !important;
    margin-bottom: 0.5rem !important;
}

/* User message bubble */
[data-testid="chatAvatarIcon-user"] {
    background: linear-gradient(135deg, #667eea, #764ba2) !important;
    border-radius: 50% !important;
}

/* Assistant message bubble */
[data-testid="chatAvatarIcon-assistant"] {
    background: linear-gradient(135deg, #f093fb, #f5576c) !important;
    border-radius: 50% !important;
}

/* Chat input */
.stChatInput textarea {
    background: rgba(255,255,255,0.05) !important;
    border: 1px solid rgba(102,126,234,0.4) !important;
    border-radius: 12px !important;
    color: #e6e6f0 !important;
    font-family: 'Inter', sans-serif !important;
}
.stChatInput textarea:focus {
    border-color: #667eea !important;
    box-shadow: 0 0 0 2px rgba(102,126,234,0.2) !important;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background: rgba(15, 15, 30, 0.95) !important;
    border-right: 1px solid rgba(102, 126, 234, 0.2) !important;
}
[data-testid="stSidebar"] .stMarkdown h2,
[data-testid="stSidebar"] .stMarkdown h3 {
    color: #ccd6f6 !important;
}
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] .stMarkdown li {
    color: #8892b0 !important;
    font-size: 0.88rem;
}

/* Buttons */
.stButton > button {
    background: linear-gradient(135deg, #667eea, #764ba2) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 500 !important;
    transition: transform 0.2s, box-shadow 0.2s !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4) !important;
}

/* Status badge */
.status-badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-top: 4px;
}
.status-ok {
    background: rgba(0, 200, 100, 0.15);
    color: #00c864;
    border: 1px solid rgba(0, 200, 100, 0.3);
}
.status-err {
    background: rgba(255, 80, 80, 0.15);
    color: #ff5050;
    border: 1px solid rgba(255, 80, 80, 0.3);
}

/* Spinner text */
.stSpinner > div {
    color: #667eea !important;
}

/* Code blocks inside chat */
code {
    background: rgba(102, 126, 234, 0.1) !important;
    color: #a8b2ff !important;
    border-radius: 4px !important;
    padding: 1px 5px !important;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []

# ─────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🗂️ Drive AI Agent")
    st.markdown("---")

    # Backend health check
    st.markdown("### 🔌 Backend Status")
    try:
        health = requests.get(f"{BACKEND_URL}/health", timeout=3)
        if health.status_code == 200:
            st.markdown('<span class="status-badge status-ok">● Connected</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="status-badge status-err">● Error</span>', unsafe_allow_html=True)
    except Exception:
        st.markdown('<span class="status-badge status-err">● Offline</span>', unsafe_allow_html=True)
        st.caption("Start the backend: `cd backend` then `python -m uvicorn main:app --reload --port 8000`")

    st.markdown("---")
    st.markdown("### 💡 Example Queries")
    st.caption("Quick category shortcuts")

    example_queries = [
        "Show all images and pics",
        "Find all invoices",
        "List all DOCX/Word files",
        "Show QR codes",
        "Show everything in the drive",
    ]
    for i, ex in enumerate(example_queries):
        if st.button(ex, key=f"ex_cat_{i}", use_container_width=True):
            st.session_state.pending_prompt = ex

    st.markdown("### 💬 Conversational search")
    st.caption("Refine by name, type, content, or date — follow up in plain English")

    followup_examples = [
        "Find spreadsheets with budget in the name",
        "PDFs modified in the last 14 days",
        "Search my drive for files that mention onboarding in the text",
    ]
    for i, ex in enumerate(followup_examples):
        if st.button(ex, key=f"ex_follow_{i}", use_container_width=True):
            st.session_state.pending_prompt = ex

    st.markdown("---")
    st.markdown("### ⚙️ Session")
    st.caption(f"ID: `{st.session_state.session_id[:18]}...`")
    if st.button("🗑️ Clear Chat", use_container_width=True):
        try:
            requests.delete(f"{BACKEND_URL}/session/{st.session_state.session_id}", timeout=3)
        except Exception:
            pass
        st.session_state.messages = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()

    st.markdown("---")
    st.markdown("### 📎 Drive Query Cheatsheet")
    st.markdown("""
**By Name**
- `name contains 'keyword'`
- `name = 'exact_file.pdf'`

**By Type**
- `mimeType = 'application/pdf'` — PDF
- `mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'` — DOCX (Word)
- `mimeType contains 'image/'` — All raster/vector images (`image/*`)
- `mimeType = 'image/png'` — PNG
- `mimeType = 'image/jpeg'` — JPEG
- `mimeType = 'image/webp'` — WebP
- `mimeType = 'image/gif'` — GIF
- `mimeType = 'image/bmp'` — BMP

**By Content / Date**
- `fullText contains 'invoice'`
- `modifiedTime > '2026-01-01T00:00:00'`

**Combine**
- Use `and` / `or` between clauses
""")


# ─────────────────────────────────────────────
# Main area
# ─────────────────────────────────────────────
st.markdown("""
<div class="main-header">
    <h1>🗂️ Google Drive AI Agent</h1>
    <p>Chat naturally to <strong>search</strong>, <strong>filter</strong>, and <strong>discover</strong> files
    by name, type, content, or date — then refine in follow-up messages. Your conversation is remembered for this session.</p>
</div>
""", unsafe_allow_html=True)

# Render existing messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Handle example button click
pending = st.session_state.pop("pending_prompt", None)

# Chat input
user_input = st.chat_input("Ask anything about your Drive, or refine your last search…") or pending

if user_input:
    # Display user message
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Call backend
    with st.chat_message("assistant"):
        with st.spinner("🔍 Searching your Drive (large folders can take up to a few minutes the first time)…"):
            try:
                resp = requests.post(
                    f"{BACKEND_URL}/chat",
                    json={
                        "session_id": st.session_state.session_id,
                        "message": user_input,
                    },
                    timeout=CHAT_REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    reply = resp.json()["response"]
                elif resp.status_code == 429:
                    try:
                        detail = resp.json().get("detail", resp.text)
                    except Exception:
                        detail = resp.text
                    reply = (
                        "⏳ **Rate limit / quota**\n\n"
                        f"{detail}\n\n"
                        "Try again after a short wait, use Clear Chat, or adjust GROQ_MODEL / quotas "
                        "for Groq (https://console.groq.com/)."
                    )
                else:
                    reply = f"⚠️ Backend error {resp.status_code}: {resp.text}"
            except requests.exceptions.ConnectionError:
                reply = (
                    "❌ Cannot connect to the backend.\n\n"
                    "Make sure the FastAPI server is running:\n"
                    "```\ncd backend\npython -m uvicorn main:app --reload --port 8000\n```"
                )
            except Exception as e:
                reply = f"❌ Unexpected error: {str(e)}"

        st.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
