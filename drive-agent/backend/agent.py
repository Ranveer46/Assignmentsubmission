import os
from langchain.tools import tool
from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent
from drive_client import search_files, list_all_files, search_files_in_named_folders
from env_loader import load_project_dotenv

load_project_dotenv()

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_MIME_LABEL = {
    "application/pdf": "📄 PDF",
    _DOCX_MIME: "📝 DOCX",
    "application/vnd.google-apps.document": "📄 Google Doc",
    "application/vnd.google-apps.spreadsheet": "📊 Google Sheet",
    "application/vnd.google-apps.presentation": "📽️ Google Slides",
    "image/png": "🖼️ PNG",
    "image/jpeg": "🖼️ JPEG",
    "image/webp": "🖼️ WebP",
    "image/gif": "🖼️ GIF",
    "image/bmp": "🖼️ BMP",
}

# All MIME types that can contain an invoice
_INVOICE_MIMES = [
    "application/pdf",
    _DOCX_MIME,
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/bmp",
]

_MIME_BUCKET_ORDER = [
    "Images (image/*)",
    "PDF documents",
    "Word documents (DOCX)",
    "Google Docs",
    "Google Sheets",
    "Google Slides",
    "Other Google Drive types",
    "Other files",
]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _mime_bucket(mime: str) -> str:
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
    seen_buckets = set()
    for bucket in _MIME_BUCKET_ORDER:
        group = buckets.get(bucket)
        if not group:
            continue
        seen_buckets.add(bucket)
        sections.append(
            f"### {bucket} ({len(group)})\n\n"
            + "\n\n".join(_file_entry_markdown(f) for f in group)
        )
    # Any unexpected bucket names not in the order list
    for bucket, group in sorted(buckets.items()):
        if bucket not in seen_buckets:
            sections.append(
                f"### {bucket} ({len(group)})\n\n"
                + "\n\n".join(_file_entry_markdown(f) for f in group)
            )

    header = f"Found **{len(files)}** {category_label or 'file(s)'}:\n\n"
    return header + "\n\n".join(sections)


def _build_mime_filter(mimes: list[str]) -> str:
    """Build a parenthesised OR mimeType filter from a list of MIME strings."""
    clauses = " or ".join(f"mimeType = '{m}'" for m in mimes)
    return f"({clauses})"


def _dedupe(files: list[dict]) -> list[dict]:
    """Remove duplicate file entries by Drive file ID."""
    seen: set[str] = set()
    result = []
    for f in files:
        if f["id"] not in seen:
            seen.add(f["id"])
            result.append(f)
    return result


# ─────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────

@tool
def drive_search_tool(query: str) -> str:
    """
    Searches Google Drive using a properly formatted 'q' parameter string.
    Use this for custom / advanced queries not covered by the dedicated tools.

    Query design rules (important — avoid empty results):
      - Do NOT AND every user word into `name contains` clauses.
      - Prefer OR across topic tokens, e.g.:
          (name contains 'party' or name contains 'menu') and mimeType = 'application/pdf'
      - If the topic might be inside the document body, use `fullText contains 'keyword'`.
      - Never require brand/folder names in filename unless the user explicitly says so.

    Supported filter patterns:
      - By name:       name contains 'budget'
      - Exact name:    name = 'quarterly_report.pdf'
      - By MIME type:  mimeType = 'application/pdf'
      - By content:    fullText contains 'invoice'
      - By date:       modifiedTime > '2024-01-01T00:00:00'
      - Combined:      name contains 'report' and mimeType = 'application/pdf'
      - OR tokens:     (name contains 'report' or name contains 'summary') and mimeType = 'application/pdf'

    Google Drive MIME types:
      - Google Docs:    application/vnd.google-apps.document
      - Google Sheets:  application/vnd.google-apps.spreadsheet
      - Google Slides:  application/vnd.google-apps.presentation
      - PDF:            application/pdf
      - DOCX:           application/vnd.openxmlformats-officedocument.wordprocessingml.document
      - Any image:      mimeType contains 'image/'
      - PNG:            image/png
      - JPEG:           image/jpeg
      - WebP:           image/webp
    """
    files = search_files(query)
    return _format_files(files)


@tool
def drive_list_all_tool(placeholder: str = "") -> str:
    """
    Lists ALL files in the shared Google Drive folder and all subfolders without any filter.
    Use when the user asks to 'show everything', 'list all files', 'what's in the drive', etc.
    Pass an empty string as the placeholder argument.
    """
    files = list_all_files()
    return _format_files(files, "file(s) in your Drive")


@tool
def drive_search_images_tool(placeholder: str = "") -> str:
    """
    Finds ALL image files (PNG, JPEG, WebP, GIF, BMP, or any image/* MIME type) in the Drive.
    Use when the user asks for: pics, pictures, images, photos, thumbnails, snapshots, visual files.
    Pass an empty string as the placeholder argument.
    """
    files = search_files("mimeType contains 'image/' and trashed = false")
    return _format_files(files, "image(s) / pic(s)")


@tool
def drive_search_invoices_tool(placeholder: str = "") -> str:
    """
    Finds invoice files across ALL supported formats: PDF, DOCX, PNG, JPEG, WebP, GIF, BMP.
    Searches by filename containing 'invoice' across all these types, because invoices can be
    scanned images (PNG/JPEG) just as often as PDFs or Word docs.

    Two-pass strategy:
      1. Files named 'invoice*' in PDF, DOCX, or image formats.
      2. Files inside any folder named 'invoice*' (catches 'Invoices/' folders).

    Use when the user asks for: invoices, bills, receipts, payment documents.
    Pass an empty string as the placeholder argument.
    """
    mime_filter = _build_mime_filter(_INVOICE_MIMES)

    # Pass 1 — files with 'invoice' in the filename
    name_results = search_files(
        f"name contains 'invoice' and trashed = false and {mime_filter}"
    )

    # Pass 2 — files inside any folder whose name starts with 'invoice'
    folder_results = search_files_in_named_folders("invoice", mime_filter)

    all_files = _dedupe(name_results + folder_results)
    return _format_files(all_files, "invoice(s)")


@tool
def drive_search_docx_tool(placeholder: str = "") -> str:
    """
    Finds all DOCX / Microsoft Word documents in the shared Drive.
    Use when the user asks for: docx, word files, word documents, .docx.
    Pass an empty string as the placeholder argument.
    """
    files = search_files(f"mimeType = '{_DOCX_MIME}' and trashed = false")
    return _format_files(files, "DOCX / Word document(s)")


@tool
def drive_search_qrcodes_tool(placeholder: str = "") -> str:
    """
    Finds QR code images using a two-pass strategy:
      1. Returns ALL images inside any folder whose name contains 'qr'.
      2. Returns image files whose filename contains 'qr', 'QR', 'qrcode', or 'qr_code'.
    Results are deduplicated by file ID.
    Use for: qr codes, qr images, qrcode files.
    Pass an empty string as the placeholder argument.
    """
    all_files: list[dict] = []

    # Pass 1 — every image inside any folder named "qr*"
    folder_results = search_files_in_named_folders("qr", "mimeType contains 'image/'")
    all_files.extend(folder_results)

    # Pass 2 — image files with "qr" anywhere in their filename
    name_results = search_files(
        "mimeType contains 'image/' and "
        "(name contains 'qr' or name contains 'QR' or "
        "name contains 'qrcode' or name contains 'qr_code') "
        "and trashed = false"
    )
    all_files.extend(name_results)

    return _format_files(_dedupe(all_files), "QR code image(s)")


@tool
def drive_search_by_date_tool(query: str) -> str:
    """
    Searches for files modified after a specific date.
    The `query` argument must be an ISO 8601 date string in the format 'YYYY-MM-DD'.
    Optionally append a MIME filter after a '|' separator, e.g. '2024-01-01|application/pdf'.

    Examples:
      - '2024-06-01'          → all files modified after June 1 2024
      - '2024-01-01|image/'   → images modified after Jan 1 2024

    Use when the user asks for: recent files, files from last week/month/year,
    files modified after a date, newest uploads, etc.
    """
    parts = query.strip().split("|", 1)
    date_str = parts[0].strip()
    mime_hint = parts[1].strip() if len(parts) > 1 else ""

    q = f"modifiedTime > '{date_str}T00:00:00' and trashed = false"
    if mime_hint:
        if mime_hint.endswith("/"):
            q += f" and mimeType contains '{mime_hint}'"
        else:
            q += f" and mimeType = '{mime_hint}'"

    files = search_files(q)
    label = f"file(s) modified after {date_str}"
    return _format_files(files, label)


# ─────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a warm, concise Google Drive assistant. You chat naturally while
helping people search, filter, and discover files in one designated shared Drive (folder + subfolders).

## Conversational behavior
- Treat every turn as part of an ongoing dialogue. Use earlier messages when the user says
  "those", "same as before", "narrow it", "only PDFs", "from last week", etc.
- If the goal is clear, call the right tool immediately. If a critical detail is truly missing
  (e.g. no keyword at all), ask ONE short clarifying question instead of guessing.
- After tools return data: start with one or two friendly sentences (what you found and how it
  matches their ask), then include the full tool output (markdown with headings, bullets, links).
- If results are empty: retry ONCE with a broader query (OR tokens, drop brand/folder from name
  contains, try fullText) before telling the user nothing exists.
- Greetings / off-topic chat: reply briefly, then offer to help with their Drive files.

## Invoice files — important
Invoices can exist as any file type: PDF, DOCX, or scanned images (PNG, JPEG, WebP, BMP, GIF).
Always use drive_search_invoices_tool for invoice requests — it covers all these types.

## What you can search (intent → mechanism)
| Intent              | How you satisfy it                                                       |
|---------------------|--------------------------------------------------------------------------|
| By name             | name contains '...' or exact name = '...' via drive_search_tool         |
| By type             | Dedicated tools when they match; else mimeType filter via drive_search_tool |
| By content          | fullText contains 'keyword' (works for many Docs/PDFs)                  |
| By date             | drive_search_by_date_tool with 'YYYY-MM-DD' (or add |mimeType hint)     |

## Multi-word topics, brands, and path language (CRITICAL)

Users say things like "the party package menu of bounceup" or "Q4 deck for Acme".
- The last part (bounceup, Acme) is often a brand/client/folder — NOT a substring in the filename.
- Do NOT require brand names in name contains unless the user says the filename includes it.
- Use OR-rich name queries on the core topic words, e.g.:
    (name contains 'party' or name contains 'menu' or name contains 'package')
    and mimeType = 'application/pdf' and trashed = false
- If still nothing: try fullText contains on the strongest noun, or relax conditions further.
- Always retry once with a broader q before saying nothing was found.

## Tool routing

| User intent                                              | Tool                        |
|----------------------------------------------------------|-----------------------------|
| pics, images, photos, pictures, thumbnails               | drive_search_images_tool    |
| invoices, bills, receipts (any format — PDF/DOCX/image) | drive_search_invoices_tool  |
| qr codes, qr images                                     | drive_search_qrcodes_tool   |
| docx, word, .docx files                                  | drive_search_docx_tool      |
| recent files, files from last week/month, modified after | drive_search_by_date_tool   |
| list everything, show all files                          | drive_list_all_tool         |
| anything else (custom name, type, content, combo)        | drive_search_tool           |

## CRITICAL rules
- NEVER pass raw user text into drive_search_tool — always build a proper 'q' string.
- NEVER AND every phrase into multiple name contains clauses — use OR across topic words.
- NEVER use name contains 'pics' to find images — always use mimeType contains 'image/'.
- NEVER invent file names or links — only report what the tools return.
- For invoice requests, ALWAYS use drive_search_invoices_tool (covers PDF, DOCX, and images).
- For date-range requests, ALWAYS use drive_search_by_date_tool with 'YYYY-MM-DD'."""


# ─────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────

def create_agent():
    """Create and return a LangGraph ReAct agent for Drive queries."""
    load_project_dotenv()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add your Groq API key "
            "to drive-agent/.env — get one at https://console.groq.com/keys"
        )

    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    llm = ChatGroq(
        model=model,
        temperature=0,
        groq_api_key=api_key,
    )

    tools = [
        drive_search_tool,
        drive_list_all_tool,
        drive_search_images_tool,
        drive_search_invoices_tool,
        drive_search_docx_tool,
        drive_search_qrcodes_tool,
        drive_search_by_date_tool,   # NEW — date-range search
    ]

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SYSTEM_PROMPT,
    )
    return agent