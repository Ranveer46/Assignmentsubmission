# Google Drive AI Agent

A LangChain-powered AI agent that lets you search your Google Drive using **natural language**. Ask things like "Find all PDFs from last month" or "Show budget spreadsheets" and the agent translates it into a Drive API query.

---

## 🗂️ Project Structure

```
drive-agent/
├── backend/
│   ├── main.py              # FastAPI app (REST API + sessions)
│   ├── agent.py             # LangChain agent + tools (Gemini 1.5 Flash)
│   ├── drive_client.py      # Google Drive API wrapper (service account)
│   ├── service_account.json # ← ADD THIS (gitignored)
│   └── requirements.txt
├── frontend/
│   └── app.py               # Streamlit chat UI
├── .env                     # ← FILL IN YOUR KEYS
├── .gitignore
└── README.md
```

---

## ⚡ Quick Start

### 1. Google Cloud Setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. Enable **Google Drive API**
3. Create a **Service Account** → download JSON key → save as `backend/service_account.json`
4. Share your Drive folder with the service account's `client_email`
5. Copy your **Folder ID** from the Drive URL:
   `https://drive.google.com/drive/folders/<FOLDER_ID>`

### 2. Get a Gemini API Key

Visit [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) and create a free key.

### 3. Configure Environment

Edit the `.env` file in the project root:

```env
GOOGLE_API_KEY=your_gemini_api_key_here
FOLDER_ID=your_drive_folder_id_here
BACKEND_URL=http://localhost:8000
```

### 4. Install Dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 5. Run the Backend

```bash
cd backend
uvicorn main:app --reload --port 8000
```

API docs available at: http://localhost:8000/docs

### 6. Run the Frontend

```bash
cd frontend
streamlit run app.py
```

Opens at: http://localhost:8501

---

## 🤖 What the Agent Can Do

| You say | Drive query built by the LLM |
|---|---|
| "Find budget spreadsheets" | `name contains 'budget' and mimeType = 'application/vnd.google-apps.spreadsheet'` |
| "Show PDFs from last week" | `mimeType = 'application/pdf' and modifiedTime > '2025-05-07T00:00:00'` |
| "Find docs mentioning invoice" | `fullText contains 'invoice'` |
| "Any images in the folder?" | `mimeType contains 'image/'` |
| "List everything" | Lists all files (no filter) |

---

## 🚀 Deployment

### Backend (Render / Railway)

1. Push to GitHub (ensure `service_account.json` is **not** committed)
2. Set environment variables: `GOOGLE_API_KEY`, `FOLDER_ID`
3. Upload `service_account.json` as a **secret file** at `/app/backend/service_account.json`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port 8000`

### Frontend (Streamlit Cloud)

1. Connect your GitHub repo at [share.streamlit.io](https://share.streamlit.io)
2. Set secrets: `BACKEND_URL = "https://your-backend-url.onrender.com"`
3. Main file path: `frontend/app.py`

---

## 🔒 Security Notes

- **Never commit** `service_account.json` or your `.env` file
- Both are already listed in `.gitignore`
- On production, inject secrets via environment variables only
