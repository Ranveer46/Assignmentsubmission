import os
from langchain.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from drive_client import search_files, list_all_files
from env_loader import load_project_dotenv

load_project_dotenv()

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_MIME_LABEL = {
    "application/pdf": "📄 PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "📝 DOCX",
    "application/vnd.google-apps.document": "📄 Google Doc",
    "application/vnd.google-apps.spreadsheet": "📊 Google Sheet",
    "application/vnd.google-apps.presentation": "📽️ Google Slides",
    "image/png": "🖼️ PNG",
    "image/jpeg": "🖼️ JPEG",
    "image/webp": "🖼️ WebP",
    "image/gif": "🖼️ GIF",
    "image/bmp": "🖼️ BMP",
}


def _mime_bucket(mime: str) -> str:
    """Broad MIME bucket for grouped display (stable ordering)."""
    if mime.startswith("image/"):
        return "Images (image/*)"
    if mime == "application/pdf":
        return "PDF documents"
    if mime == _DOCX_MIME:
        return "Word documents (DOCX)"
    if mime == "application/vnd.google-apps.document":
        return "Google Docs"
    if mime == "application/vnd.google-apps.spreadsheet":
        return "Google Sheets"
    if mime == "application/vnd.google-apps.presentation":
        return "Google Slides"
    if mime.startswith("application/vnd.google-apps."):
        return "Other Google Drive types"
    return "Other files"


_BUCKET_ORDER = [
    "Images (image/*)",
    "PDF documents",
    "Word documents (DOCX)",
    "Google Docs",
    "Google Sheets",
    "Google Slides",
    "Other Google Drive types",
    "Other files",
]


def _file_entry_markdown(f: dict) -> str:
    modified = f.get("modifiedTime", "unknown date")[:10]
    link = f.get("webViewLink", "no link")
    mime = f.get("mimeType", "unknown")
    mime_label = _MIME_LABEL.get(
        mime,
        f"📁 {mime.split('/')[-1].upper()}" if "/" in mime else f"📁 {mime}",
    )
    return (
        f"• **{f['name']}** {mime_label}\n"
        f"  Modified: {modified} | [Open in Drive]({link})"
    )


def _format_files(files: list[dict], category_label: str = "") -> str:
    """Format a list of Drive file dicts into markdown, grouped by MIME category."""
    if not files:
        return f"No {category_label or 'files'} found."

    buckets: dict[str, list[dict]] = {}
    for f in files:
        mime = f.get("mimeType", "unknown")
        b = _mime_bucket(mime)
        buckets.setdefault(b, []).append(f)

    sections = []
    for bucket in _BUCKET_ORDER:
        group = buckets.pop(bucket, None)
        if not group:
            continue
        sections.append(
            f"### {bucket} ({len(group)})\n\n"
            + "\n\n".join(_file_entry_markdown(f) for f in group)
        )
    # Any unexpected bucket names still left
    for bucket, group in sorted(buckets.items()):
        sections.append(
            f"### {bucket} ({len(group)})\n\n"
            + "\n\n".join(_file_entry_markdown(f) for f in group)
        )

    header = f"Found **{len(files)}** {category_label or 'file(s)'}:\n\n"
    return header + "\n\n".join(sections)


# ─────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────

@tool
def drive_search_tool(query: str) -> str:
    """
    Searches Google Drive using a properly formatted 'q' parameter string.
    Use this for custom / advanced queries not covered by the dedicated tools.

    Query design (avoid empty results on real-world phrasing):
      - Do NOT AND every user word into `name contains` — filenames rarely match full sentences.
      - Phrases like "… of Brand", "… for Client", "… in the X folder" often name a **folder or
        account**, not the file. **Do not require that brand/folder token in the file name** unless
        the user explicitly says the file is named that way.
      - Prefer **OR** across distinctive topic tokens plus optional type filter, e.g.
        `(name contains 'party' or name contains 'menu' or name contains 'package') and mimeType = 'application/pdf'`
      - If the topic might be inside the document, combine or follow with `fullText contains 'keyword'`.

    Supported patterns:
      - By name:      name contains 'budget'
      - Exact name:   name = 'quarterly_report.pdf'
      - By MIME type: mimeType = 'application/pdf'
      - By content:   fullText contains 'invoice'
      - By date:      modifiedTime > '2024-01-01T00:00:00'
      - Combined:     name contains 'report' and mimeType = 'application/pdf'

    Google Drive MIME types:
      - Google Docs:        application/vnd.google-apps.document
      - Google Sheets:      application/vnd.google-apps.spreadsheet
      - Google Slides:      application/vnd.google-apps.presentation
      - PDF:                application/pdf
      - DOCX (Word):        application/vnd.openxmlformats-officedocument.wordprocessingml.document
      - Images (generic):   mimeType contains 'image/'
      - PNG:                image/png
      - JPEG:               image/jpeg
      - WebP:               image/webp
      - GIF:                image/gif
      - BMP:                image/bmp
    """
    files = search_files(query)
    return _format_files(files)


@tool
def drive_list_all_tool(placeholder: str = "") -> str:
    """
    Lists ALL files in the shared Google Drive folder and all subfolders without any filter.
    Use this when the user asks to 'show everything', 'list all files', 'what's in the drive', etc.
    Pass an empty string as the placeholder argument.
    """
    files = list_all_files()
    return _format_files(files, "file(s) in your Drive")


@tool
def drive_search_images_tool() -> str:
    """
    Finds ALL image files (PNG, JPEG, WebP, GIF, BMP, or any image/* MIME type) in the shared Drive.
    Files inside a folder named "pics" are included automatically (recursive folder scope).
    Use when the user asks for: pics, pictures, images, photos, thumbnails, snapshots, or visual files.
    No arguments.
    """
    files = search_files("mimeType contains 'image/' and trashed = false")
    return _format_files(files, "image(s) / pic(s)")


@tool
def drive_search_invoices_tool(placeholder: str = "") -> str:
    """
    Finds PDF and DOCX files whose filename contains 'invoice' (case-insensitive via Drive query).
    Use when the user asks for: invoices, bills, receipts, payment documents (as files named invoice).
    Pass an empty string as the placeholder argument.
    """
    files = search_files(
        "name contains 'invoice' and trashed = false and "
        "(mimeType = 'application/pdf' or "
        f"mimeType = '{_DOCX_MIME}')"
    )
    return _format_files(files, "invoice(s)")


@tool
def drive_search_docx_tool(placeholder: str = "") -> str:
    """
    Finds all DOCX / Microsoft Word documents in the shared Drive.
    Use this when the user asks for: docx, word files, word documents, .docx.
    Pass an empty string as the placeholder argument.
    """
    files = search_files(f"mimeType = '{_DOCX_MIME}' and trashed = false")
    return _format_files(files, "DOCX / Word document(s)")


@tool
def drive_search_qrcodes_tool(placeholder: str = "") -> str:
    """
    Finds QR code image files: any image/* file whose name contains 'qr' or 'qrcode'
    (substring match; 'qrcode' matches via 'qr'). Use for: qr codes, qr images.
    Pass an empty string as the placeholder argument.
    """
    files = search_files(
        "mimeType contains 'image/' and (name contains 'qr' or name contains 'QR') "
        "and trashed = false"
    )
    return _format_files(files, "QR code image(s)")


# ─────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a warm, concise Google Drive assistant. You chat naturally while
helping people search, filter, and discover files in one designated shared Drive (folder + subfolders).

## Conversational behavior
- Treat every turn as part of an ongoing dialogue. Use earlier messages when the user says
  "those", "same as before", "narrow it", "only PDFs", "from last week", etc.
- If the goal is clear, call the right tool immediately. If critical detail is missing (e.g. no
  keyword, no file type, no timeframe when they insist on a date), ask **one** short clarifying
  question instead of guessing.
- After tools return data: start with **one or two friendly sentences** (what you found and how
  it matches their ask), then include the **full tool output** (markdown with headings, bullets,
  and Drive links) so they can scan and click without losing information.
- If results are empty: after you have **retried once** with a broader `q` (OR topic tokens,
  dropped folder/brand from `name contains`, or `fullText`), say so plainly and suggest next steps.
- Greetings or off-topic chat: reply briefly, then steer back to how you can help with their files.

## What you can search (intent → mechanism)
| Intent | How you satisfy it |
|--------|--------------------|
| By **name** | `name contains '...'` or exact `name = '...'` via drive_search_tool |
| By **type** | Dedicated tools when they match; else `mimeType = '...'` or `mimeType contains 'image/'` |
| By **content** | `fullText contains 'keyword'` (indexed text; works for many Docs/PDFs) |
| By **date** | `modifiedTime > 'YYYY-MM-DDTHH:MM:SS'` (ISO 8601, UTC). Interpret "last week/month" relative to today when the user gives a calendar reference. |

## Multi-word topics, brands, and "path" language (CRITICAL)

Users often describe files like: *"the party package menu of bounceup"* or *"Q4 deck for Acme"*.
- The last part (**bounceup**, **Acme**) is frequently a **brand, client, or parent folder** — not
  a substring in the PDF filename. Requiring `name contains 'bounceup' AND name contains 'party package menu'`
  usually returns **nothing** even when the right files exist (e.g. `PARTY MENU.pdf` under `bounceup/party packages/`).
- **Do not** require organizational tokens in `name contains` unless the user clearly states the
  file name includes that word.
- **Do** use **one or two OR-rich name queries** on the main topic words (e.g. party, menu, package,
  packages) plus `mimeType` when they want PDFs/docs, e.g.:
  `(name contains 'party' or name contains 'menu' or name contains 'package') and mimeType = 'application/pdf' and trashed = false`
- If that still returns nothing useful, try **`fullText contains`** on the strongest noun or brand
  (indexed body text) or relax to fewer AND conditions.
- If a strict name search returns **zero** files, **retry once** with a broader `q` (OR tokens,
  drop folder/brand from `name contains`) **before** telling the user nothing exists.

**Worked example (conceptual):** *"party package menu of bounceup"* → PDFs about party/menu/packages:
`(name contains 'party' or name contains 'menu' or name contains 'package') and mimeType = 'application/pdf' and trashed = false`

## Tool Routing — always pick the RIGHT tool

| User says / wants | Tool to call |
|-------------------|--------------|
| pics, images, photos, pictures, thumbnails, snapshots | drive_search_images_tool |
| invoices, bills, receipts (PDF/DOCX with "invoice" in filename) | drive_search_invoices_tool |
| qr codes, qr images | drive_search_qrcodes_tool |
| docx, word, .docx | drive_search_docx_tool (preferred) or drive_search_tool with DOCX mimeType |
| list everything, show all files in drive | drive_list_all_tool |
| custom query (PDFs, sheets, dates, names, content, combinations) | drive_search_tool |

Files under a folder literally named "pics" are already in scope (recursive traversal of the shared root).

## Using drive_search_tool for custom queries

When you use drive_search_tool, the `query` argument must be a valid Google Drive API
'q' parameter string. Learn these patterns:

| Intent              | Query string                                                             |
|---------------------|--------------------------------------------------------------------------|
| By name keyword     | name contains 'keyword'                                                  |
| Exact filename      | name = 'exact_name.pdf'                                                  |
| PDF files           | mimeType = 'application/pdf'                                             |
| DOCX files          | mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' |
| Google Sheet        | mimeType = 'application/vnd.google-apps.spreadsheet'                    |
| Google Doc          | mimeType = 'application/vnd.google-apps.document'                       |
| Google Slides       | mimeType = 'application/vnd.google-apps.presentation'                   |
| All images          | mimeType contains 'image/'                                               |
| PNG only            | mimeType = 'image/png'                                                   |
| JPEG only           | mimeType = 'image/jpeg'                                                   |
| By content keyword  | fullText contains 'keyword'                                              |
| Modified after date | modifiedTime > '2026-01-01T00:00:00'                                     |
| Combine criteria    | name contains 'report' and mimeType = 'application/pdf'                  |
| OR on name tokens   | (name contains 'party' or name contains 'menu') and mimeType = 'application/pdf' |

## CRITICAL rules
- NEVER pass a raw user message into drive_search_tool. Always build a proper 'q' string.
- NEVER AND every user phrase into multiple `name contains` clauses — that over-filters; use OR
  across topic words and do not require folder/brand names in the filename unless stated.
- NEVER use name contains 'pics' to find images — always use mimeType contains 'image/' or drive_search_images_tool.
- For images/pics/photos always call drive_search_images_tool (no query needed).
- For invoices always call drive_search_invoices_tool (no query needed).
- For DOCX/Word always call drive_search_docx_tool (no query needed).
- For QR codes always call drive_search_qrcodes_tool (no query needed).
- Do not invent file names or links; only report what tools return."""


# ─────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────

def create_agent():
    """Create and return a LangGraph ReAct agent for Drive queries."""
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY or GEMINI_API_KEY is not set. Add your Gemini (Google AI Studio) key "
            "to drive-agent/.env — see https://aistudio.google.com/app/apikey"
        )

    # Gemini via Google AI Studio; override with GEMINI_MODEL if you prefer another variant.
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    llm = ChatGoogleGenerativeAI(
        model=model,
        temperature=0,
        google_api_key=api_key,
    )

    tools = [
        drive_search_tool,
        drive_list_all_tool,
        drive_search_images_tool,
        drive_search_invoices_tool,
        drive_search_docx_tool,
        drive_search_qrcodes_tool,
    ]

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SYSTEM_PROMPT,
    )
    return agent
