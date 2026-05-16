"""
app.py - Streamlit frontend for the Google Drive conversational agent
Place this file at: drive-agent/frontend/app.py

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
        load_dotenv("../../.env")
except ImportError:
    pass

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

st.set_page_config(
    page_title="Drive Assistant",
    page_icon="🗂️",
    layout="centered",
)

st.title("🗂️ Google Drive Assistant")
st.caption("Search, filter, and discover files in your shared Drive — just chat naturally.")

if "messages" not in st.session_state:
    st.session_state.messages = []

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Drive status")

    if st.button("🔄 Check connection", use_container_width=True):
        try:
            r = requests.get(f"{BACKEND_URL}/health", timeout=5)
            if r.status_code == 200:
                data = r.json()
                cache = data.get("cache", {})
                total = cache.get("total_files", "?")
                st.success(f"✅ Connected — {total} files indexed")
                for mime, count in sorted(
                    cache.get("mime_breakdown", {}).items(), key=lambda x: -x[1]
                ):
                    st.text(f"  {count:>4}  {mime}")
            else:
                st.error(f"Backend returned HTTP {r.status_code}")
        except requests.exceptions.ConnectionError:
            st.error(f"❌ Cannot reach {BACKEND_URL}")
        except Exception as e:
            st.error(f"Error: {e}")

    st.divider()
    st.subheader("Quick searches")

    quick = [
        ("🖼️ All images",     "Show all images and pictures"),
        ("📄 All invoices",    "Find all invoice files"),
        ("📝 Word docs",       "Show all DOCX Word files"),
        ("📊 QR codes",        "Find all QR code images"),
        ("🕐 Recent files",    "Show the 20 most recently modified files"),
        ("📁 List everything", "List all files in the Drive"),
    ]

    for label, prompt in quick:
        if st.button(label, use_container_width=True):
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.rerun()

    st.divider()
    if st.button("🗑️ Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ── Chat history ──────────────────────────────────────────────────────────────

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ── Backend call ──────────────────────────────────────────────────────────────

def send_to_backend(user_text: str) -> str:
    """POST to /chat, return reply string. Never raises — always returns a string."""
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

    # Parse JSON safely
    try:
        data = r.json()
    except Exception:
        return f"Backend returned non-JSON:\n```\n{r.text[:400]}\n```"

    # Accept whichever key the backend uses for the reply
    reply = (
        data.get("reply")
        or data.get("response")
        or data.get("message")
        or data.get("output")
        or data.get("text")
    )

    if reply is None:
        # Show the raw payload so it's easy to debug the key name
        return (
            "Got a response but could not find the reply field.\n\n"
            f"Raw response keys: `{list(data.keys())}`\n\n"
            f"```json\n{data}\n```"
        )

    return str(reply)


# ── Handle quick-action buttons ───────────────────────────────────────────────
# Quick buttons append a user message then call st.rerun().
# On the next render we detect the unpaired user message and send it.

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

# ── Chat input box ────────────────────────────────────────────────────────────

if user_input := st.chat_input("Search your Drive… e.g. 'show all images'"):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Searching your Drive..."):
            reply = send_to_backend(user_input)
        st.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
