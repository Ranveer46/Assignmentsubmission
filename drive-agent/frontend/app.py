"""
app.py - DriveMind AI Dashboard
Premium Streamlit frontend for the Google Drive AI Agent.

Run:
    cd drive-agent/frontend
    python -m streamlit run app.py --server.port 8501
"""

import os
import requests
import streamlit as st

try:
    from dotenv import load_dotenv
    if not load_dotenv(".env"):
        if not load_dotenv("../.env"):
            load_dotenv("../../.env")
except ImportError:
    pass

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

# ── Page Config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="DriveMind AI",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "messages" not in st.session_state:
    st.session_state.messages = []
if "file_count" not in st.session_state:
    st.session_state.file_count = None

# ── CSS Injection ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

*, *::before, *::after { font-family: 'Inter', -apple-system, sans-serif !important; }

.stApp { background: #0c0c14 !important; }

#MainMenu, header, footer, .stDeployButton,
[data-testid="stToolbar"] { display: none !important; }

/* ── Sidebar ────────────────────────────── */
[data-testid="stSidebar"] {
    background: #111119 !important;
    border-right: 1px solid #1c1c2e !important;
    padding-top: 0.5rem !important;
}
[data-testid="stSidebar"] [data-testid="stMarkdown"] p,
[data-testid="stSidebar"] [data-testid="stMarkdown"] span,
[data-testid="stSidebar"] label {
    color: #a0a0be !important;
}
[data-testid="stSidebar"] .stButton > button {
    background: transparent !important;
    color: #a0a0be !important;
    border: 1px solid #252538 !important;
    border-radius: 10px !important;
    font-size: 0.85rem !important;
    padding: 0.55rem 0.9rem !important;
    transition: all 0.25s ease !important;
    text-align: left !important;
    justify-content: flex-start !important;
    width: 100% !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: #1a1a2e !important;
    border-color: #7c3aed !important;
    color: #e0e0f0 !important;
}

/* ── Chat Messages ──────────────────────── */
[data-testid="stChatMessage"] {
    background: #14141f !important;
    border: 1px solid #1e1e30 !important;
    border-radius: 14px !important;
    padding: 1rem 1.2rem !important;
    margin-bottom: 0.8rem !important;
}
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li { color: #d0d0e0 !important; }
[data-testid="stChatMessage"] strong { color: #e8e8f0 !important; }
[data-testid="stChatMessage"] a { color: #8b5cf6 !important; }
[data-testid="stChatMessage"] h3 {
    color: #a78bfa !important;
    font-size: 0.95rem !important;
}
[data-testid="stChatMessage"] code {
    background: #1e1e30 !important;
    color: #c084fc !important;
}

/* ── Chat Input ─────────────────────────── */
[data-testid="stChatInput"] {
    border-color: #252538 !important;
}
[data-testid="stChatInput"] textarea {
    color: #d0d0e0 !important;
}
[data-testid="stChatInputSubmitButton"] {
    color: #7c3aed !important;
}

/* ── Dividers ───────────────────────────── */
[data-testid="stSidebar"] hr {
    border-color: #1e1e30 !important;
}

/* ── Status elements ────────────────────── */
.stSuccess { background: rgba(34,197,94,0.08) !important; border: 1px solid rgba(34,197,94,0.25) !important; border-radius: 10px !important; }
.stError   { background: rgba(239,68,68,0.08) !important;  border: 1px solid rgba(239,68,68,0.25) !important;  border-radius: 10px !important; }
.stSpinner > div { color: #7c3aed !important; }

/* ── Scrollbar ──────────────────────────── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: #0c0c14; }
::-webkit-scrollbar-thumb { background: #2a2a3e; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #3a3a55; }
</style>
""", unsafe_allow_html=True)


# ── Backend Helpers ───────────────────────────────────────────────────────────

def send_to_backend(user_text: str) -> str:
    """POST to /chat, return reply string. Never raises."""
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages
        if m["role"] in ("user", "assistant")
    ]
    payload = {"message": user_text, "history": history}
    try:
        r = requests.post(f"{BACKEND_URL}/chat", json=payload, timeout=90)
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        return (
            "**Cannot reach the backend.**\n\n"
            "Make sure FastAPI is running:\n"
            "```\ncd drive-agent/backend\n"
            "python -m uvicorn main:app --reload --port 8000\n```"
        )
    except requests.exceptions.Timeout:
        return "**Request timed out.** Try a simpler query or check the backend logs."
    except requests.exceptions.HTTPError:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        return f"**Backend error {r.status_code}:** {detail}"
    except Exception as e:
        return f"**Unexpected error:** {e}"

    try:
        data = r.json()
    except Exception:
        return f"Backend returned non-JSON:\n```\n{r.text[:400]}\n```"

    reply = (
        data.get("reply") or data.get("response") or
        data.get("message") or data.get("output") or data.get("text")
    )
    if reply is None:
        return (
            "Got a response but could not find the reply field.\n\n"
            f"Raw response keys: `{list(data.keys())}`"
        )
    return str(reply)


def fetch_file_count():
    """Get indexed file count from backend /health endpoint."""
    if st.session_state.file_count is not None:
        return st.session_state.file_count
    try:
        r = requests.get(f"{BACKEND_URL}/health", timeout=5)
        if r.status_code == 200:
            count = r.json().get("cache", {}).get("total_files", 0)
            st.session_state.file_count = count
            return count
    except Exception:
        pass
    return None


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    # Logo
    st.markdown("""
    <div style="display:flex; align-items:center; gap:10px; padding:0.5rem 0 1rem 0;">
        <div style="width:32px; height:32px; background:linear-gradient(135deg,#7c3aed,#a855f7);
                    border-radius:8px; display:flex; align-items:center; justify-content:center;
                    font-size:16px;">🧠</div>
        <span style="font-size:1.15rem; font-weight:700; color:#e8e8f0;">DriveMind AI</span>
        <span style="background:linear-gradient(135deg,#7c3aed,#9333ea); color:white;
                     font-size:0.6rem; padding:2px 8px; border-radius:6px;
                     font-weight:600; letter-spacing:0.05em;">PRO</span>
    </div>
    """, unsafe_allow_html=True)

    # New Chat button
    if st.button("✨  New Search Chat", use_container_width=True, key="new_chat"):
        st.session_state.messages = []
        st.session_state.file_count = None
        st.rerun()

    st.divider()

    # Navigation
    st.markdown('<p class="sidebar-section" style="color:#6b6b85; font-size:0.7rem; letter-spacing:0.1em; text-transform:uppercase; font-weight:600;">NAVIGATIONAL</p>', unsafe_allow_html=True)

    if st.button("📊  Dashboard", use_container_width=True, key="nav_dash"):
        st.session_state.messages = []
        st.rerun()

    if st.button("📁  All Files", use_container_width=True, key="nav_all"):
        st.session_state.messages.append({"role": "user", "content": "List all files in the Drive"})
        st.rerun()



    st.divider()

    # Connection status
    st.markdown('<p style="color:#6b6b85; font-size:0.7rem; letter-spacing:0.1em; text-transform:uppercase; font-weight:600;">SYSTEM</p>', unsafe_allow_html=True)

    if st.button("🔄  Check Connection", use_container_width=True, key="health_btn"):
        try:
            r = requests.get(f"{BACKEND_URL}/health", timeout=5)
            if r.status_code == 200:
                data = r.json()
                cache = data.get("cache", {})
                total = cache.get("total_files", "?")
                st.session_state.file_count = total
                st.success(f"✅ Connected — {total} files indexed")
                breakdown = cache.get("mime_breakdown", {})
                for mime, count in sorted(breakdown.items(), key=lambda x: -x[1])[:6]:
                    short = mime.split("/")[-1][:20]
                    st.caption(f"  `{count:>4}`  {short}")
            else:
                st.error(f"Backend returned HTTP {r.status_code}")
        except requests.exceptions.ConnectionError:
            st.error(f"❌ Cannot reach {BACKEND_URL}")
        except Exception as e:
            st.error(f"Error: {e}")


# ── Main Content ──────────────────────────────────────────────────────────────

if not st.session_state.messages:
    # ── Landing Page (Getting Started) ────────────────────────────────────────

    # Top spacer
    st.markdown("<div style='height:4rem'></div>", unsafe_allow_html=True)

    # Hero heading
    st.markdown("""
    <div style="text-align:center; padding:0 1rem;">
        <h1 style="font-size:3rem; font-weight:800; color:#e8e8f0; margin-bottom:0.6rem; line-height:1.15;">
            Unlock your <span style="background:linear-gradient(135deg,#7c3aed,#a855f7,#c084fc);
            -webkit-background-clip:text; -webkit-text-fill-color:transparent;
            background-clip:text;">knowledge.</span>
        </h1>
        <p style="color:#7e7e9a; font-size:1.05rem; max-width:520px; margin:0 auto 2.5rem auto; line-height:1.7;">
            Search, synthesize, and organize your entire Google Drive with
            professional AI precision.
        </p>
    </div>
    """, unsafe_allow_html=True)



    # Status bar
    file_count = fetch_file_count()
    status_text = f"DriveMind is ready to index {file_count:,} of your files." if file_count else "DriveMind is ready. Start the backend to index your files."
    status_dot = "🟢" if file_count else "🟡"

    st.markdown(f"""
    <div style="text-align:center; margin-top:2.5rem;">
        <p style="color:#6b6b85; font-size:0.82rem;">
            {status_dot} {status_text}
        </p>
        <p style="color:#4a4a60; font-size:0.72rem; margin-top:0.5rem;">
            🔒 End-to-end encrypted &nbsp;&nbsp;·&nbsp;&nbsp; ⚡ Powered by DriveMind v2.0
        </p>
    </div>
    """, unsafe_allow_html=True)

else:
    # ── Chat View ─────────────────────────────────────────────────────────────

    # Display all chat messages
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])



    # System ready indicator
    st.markdown("""
    <div style="text-align:center; margin-top:0.3rem;">
        <span style="background:#1a1a2e; border:1px solid #252538; color:#6b6b85;
                     font-size:0.68rem; padding:4px 14px; border-radius:12px;
                     letter-spacing:0.06em;">● SYSTEM READY</span>
    </div>
    """, unsafe_allow_html=True)


# ── Handle pending quick-action messages ──────────────────────────────────────

msgs = st.session_state.messages
if msgs and msgs[-1]["role"] == "user":
    is_pending = len(msgs) == 1 or msgs[-2]["role"] != "assistant"
    if is_pending:
        with st.chat_message("assistant"):
            with st.spinner("Searching your Drive..."):
                reply = send_to_backend(msgs[-1]["content"])
            st.markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

# ── Chat Input ────────────────────────────────────────────────────────────────

if user_input := st.chat_input("Ask DriveMind anything about your workspace..."):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Searching your Drive..."):
            reply = send_to_backend(user_input)
        st.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
