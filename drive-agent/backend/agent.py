"""
agent.py — LangGraph ReAct agent with 8 Drive tools

Changes from v1:
  - drive_search_invoices_tool now covers PNG/JPEG/WebP/GIF/BMP (not just PDF+DOCX)
  - drive_search_by_date_tool added for date-range queries
  - drive_folder_explore_tool added — "what's in the invoices folder?"
  - drive_recent_files_tool added — "show me the newest 10 files"
  - drive_content_search_tool added — semantic fullText search via API
  - All tools use the new _cache_search path in drive_client (fast, no API quota)
  - fullText queries automatically fall back to Drive API via drive_client
"""

import os
from langchain.tools import tool
from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent

from drive_client import (
    extract_text,
    get_folder_tree,
    get_recent_files,
    list_all_files,
    search_files,
    search_files_in_named_folders,
)
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
    "application/vnd.google-apps.presentation": "📽️ Slides",
    "image/png": "🖼️ PNG",
    "image/jpeg": "🖼️ JPEG",
    "image/jpg": "🖼️ JPEG",
    "image/webp": "🖼️ WebP",
    "image/gif": "🖼️ GIF",
    "image/bmp": "🖼️ BMP",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "📊 Excel",
    "video/mp4": "🎥 MP4",
    "video/quicktime": "🎥 MOV",
    "application/x-sh": "📜 Shell Script",
    "text/x-shellscript": "📜 Shell Script",
    "text/plain": "📝 Text",
    "text/csv": "📊 CSV",
}

# All MIME types that can be an invoice (PDF, DOCX, or any scanned image)
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
    "Videos (video/*)",
    "PDF documents",
    "Word documents (DOCX)",
    "Excel spreadsheets",
    "Google Docs",
    "Google Sheets",
    "Google Slides",
    "Code & Scripts",
    "Other Google Drive types",
    "Other files",
]


# ─────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────

def _mime_bucket(mime: str) -> str:
    if mime.startswith("image/"):
        return "Images (image/*)"
    if mime.startswith("video/"):
        return "Videos (video/*)"
    if mime == "application/pdf":
        return "PDF documents"
    if mime == _DOCX_MIME:
        return "Word documents (DOCX)"
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return "Excel spreadsheets"
    if mime in ("application/x-sh", "text/x-shellscript", "text/x-python", "application/json"):
        return "Code & Scripts"
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
    modified = f.get("modifiedTime", "unknown")[:10]
    link = f.get("webViewLink", "")
    mime = f.get("mimeType", "unknown")
    path = f.get("folder_path", "")
    mime_label = _MIME_LABEL.get(
        mime,
        f"📁 {mime.split('/')[-1].upper()}" if "/" in mime else f"📁 {mime}",
    )
    location = f"  📂 `{path}`" if path and path != "/" else ""
    link_text = f" | [Open in Drive]({link})" if link else ""
    return (
        f"• **{f['name']}** {mime_label}\n"
        f"  Modified: {modified}{link_text}{location}"
    )


def _format_files(files: list[dict], category_label: str = "", max_files: int = 40) -> str:
    if not files:
        return f"No {category_label or 'files'} found."

    total_count = len(files)
    truncated = False
    if total_count > max_files:
        files = files[:max_files]
        truncated = True

    buckets: dict[str, list[dict]] = {}
    for f in files:
        b = _mime_bucket(f.get("mimeType", "unknown"))
        buckets.setdefault(b, []).append(f)

    sections = []
    seen = set()
    for bucket in _MIME_BUCKET_ORDER:
        group = buckets.get(bucket)
        if not group:
            continue
        seen.add(bucket)
        sections.append(
            f"### {bucket} ({len(group)})\n\n"
            + "\n\n".join(_file_entry_markdown(f) for f in group)
        )
    for bucket, group in sorted(buckets.items()):
        if bucket not in seen:
            sections.append(
                f"### {bucket} ({len(group)})\n\n"
                + "\n\n".join(_file_entry_markdown(f) for f in group)
            )

    header = f"Found **{total_count}** {category_label or 'file(s)'}"
    if truncated:
        header += f" *(showing first {max_files} to fit in token limits)*"
    header += ":\n\n"
    
    instruction = "IMPORTANT INSTRUCTION TO LLM: You MUST copy and paste the following text EXACTLY as your response. DO NOT summarize it. If you summarize it, the user will lose the links.\n\n"
    return instruction + header + "\n\n".join(sections)


def _dedupe(files: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for f in files:
        if f["id"] not in seen:
            seen.add(f["id"])
            result.append(f)
    return result


def _build_mime_filter(mimes: list[str]) -> str:
    clauses = " or ".join(f"mimeType = '{m}'" for m in mimes)
    return f"({clauses})"


# ─────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────

@tool
def drive_search_tool(query: str) -> str:
    """
    Searches the Drive index using a Drive API 'q' parameter string.
    The local cache handles name/mimeType/date queries instantly (no API quota).
    fullText queries are automatically routed to the Drive API.

    Supported patterns:
      name contains 'budget'
      name = 'report.pdf'
      mimeType = 'application/pdf'
      mimeType contains 'image/'
      fullText contains 'invoice'                   ← hits Drive API
      modifiedTime > '2024-01-01T00:00:00'
      (name contains 'party' or name contains 'menu') and mimeType = 'application/pdf'

    Drive MIME types:
      Google Docs:    application/vnd.google-apps.document
      Google Sheets:  application/vnd.google-apps.spreadsheet
      Google Slides:  application/vnd.google-apps.presentation
      PDF:            application/pdf
      DOCX:           application/vnd.openxmlformats-officedocument.wordprocessingml.document
      Any image:      mimeType contains 'image/'
    """
    files = search_files(query)
    return _format_files(files)


@tool
def drive_list_all_tool(placeholder: str = "") -> str:
    """
    Lists ALL files in the shared Drive (all subfolders included).
    Use when the user asks to 'show everything', 'list all files', etc.
    Pass an empty string as the placeholder.
    """
    files = list_all_files()
    return _format_files(files, "file(s) in your Drive")


@tool
def drive_search_images_tool(placeholder: str = "") -> str:
    """
    Finds ALL image files (PNG, JPEG, WebP, GIF, BMP, etc.) in the Drive.
    Use when the user asks for: pics, pictures, images, photos, thumbnails, snapshots.
    Pass an empty string as the placeholder.
    """
    files = search_files("mimeType contains 'image/' and trashed = false")
    return _format_files(files, "image(s)")


@tool
def drive_search_invoices_tool(placeholder: str = "") -> str:
    """
    Finds invoice files in ALL formats: PDF, DOCX, and scanned images (PNG, JPEG, WebP, GIF, BMP).
    Invoices are often scanned and saved as images — this tool catches all of them.

    Two-pass strategy:
      1. Files named 'invoice*' in any supported format.
      2. Files inside folders named 'invoice*' (e.g. an 'Invoices/' subfolder).

    Use for: invoices, bills, receipts, payment documents.
    Pass an empty string as the placeholder.
    """
    mime_filter = _build_mime_filter(_INVOICE_MIMES)

    # Pass 1 — files with 'invoice' in their filename
    name_results = search_files(
        f"name contains 'invoice' and trashed = false and {mime_filter}"
    )

    # Pass 2 — files inside any folder named like 'invoice*'
    folder_results = search_files_in_named_folders("invoice", mime_filter)

    return _format_files(_dedupe(name_results + folder_results), "invoice(s)")


@tool
def drive_search_docx_tool(placeholder: str = "") -> str:
    """
    Finds all DOCX / Microsoft Word documents in the Drive.
    Use for: docx, word files, word documents, .docx.
    Pass an empty string as the placeholder.
    """
    files = search_files(f"mimeType = '{_DOCX_MIME}' and trashed = false")
    return _format_files(files, "DOCX / Word document(s)")


@tool
def drive_search_qrcodes_tool(placeholder: str = "") -> str:
    """
    Finds QR code images using two passes:
      1. Images inside any folder whose name contains 'qr'.
      2. Image files whose filename contains 'qr', 'QR', 'qrcode', or 'qr_code'.
    Use for: qr codes, qr images, qrcode files.
    Pass an empty string as the placeholder.
    """
    folder_results = search_files_in_named_folders("qr", "mimeType contains 'image/'")
    name_results = search_files(
        "mimeType contains 'image/' and "
        "(name contains 'qr' or name contains 'QR' or "
        "name contains 'qrcode' or name contains 'qr_code') "
        "and trashed = false"
    )
    return _format_files(_dedupe(folder_results + name_results), "QR code image(s)")


@tool
def drive_search_by_date_tool(query: str) -> str:
    """
    Finds files modified after a specific date.
    The query must be: 'YYYY-MM-DD' or 'YYYY-MM-DD|mimeTypeHint'.

    Examples:
      '2024-06-01'                → all files modified after June 1 2024
      '2024-01-01|application/pdf'  → only PDFs modified after Jan 1 2024
      '2024-01-01|image/'           → only images modified after Jan 1 2024

    Use for: recent files, files from last week/month/year, files since a date,
             newest uploads, recently modified.
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
    return _format_files(files, f"file(s) modified after {date_str}")


@tool
def drive_recent_files_tool(n_str: str = "20") -> str:
    """
    Returns the N most recently modified files across the entire Drive.
    Pass the number as a string, e.g. '10' for the 10 newest files.
    Defaults to 20 if not specified.

    Use for: latest files, newest uploads, most recent changes, what was added today.
    """
    try:
        n = int(n_str)
    except ValueError:
        n = 20
    files = get_recent_files(n)
    return _format_files(files, f"{n} most recent file(s)")


@tool
def drive_folder_explore_tool(folder_name: str) -> str:
    """
    Returns all files inside a folder whose path contains folder_name (case-insensitive).
    Use when the user asks about a specific folder, e.g.:
      "what's in the invoices folder?"
      "show me everything inside the marketing folder"
      "files in the Q1 reports directory"

    Pass the folder name (or fragment) as the argument, e.g. 'invoices', 'marketing', 'q1'.
    """
    results = search_files_in_named_folders(folder_name.lower())
    if not results:
        return f"No files found inside a folder named '{folder_name}'."
    return _format_files(results, f"file(s) inside '{folder_name}' folder(s)")


@tool
def drive_content_search_tool(keyword: str) -> str:
    """
    Searches the TEXT CONTENT of files (not just filenames) using the Drive API's
    full-text index. Works for Google Docs, indexed PDFs, and Sheets.

    Use when the user says:
      "find documents that mention reinforcement learning"
      "search inside files for the word budget"
      "which file talks about the Q4 targets?"
      "find anything containing 'project alpha'"

    Pass the keyword or phrase to search for, e.g. 'reinforcement learning'.
    The query is automatically scoped to your shared folder.
    """
    q = f"fullText contains '{keyword}' and trashed = false"
    files = search_files(q)  # drive_client routes fullText to the API automatically
    return _format_files(files, f"file(s) containing '{keyword}'")


# ─────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a warm, concise Google Drive assistant. You chat naturally while
helping people search, filter, and discover files in one designated shared Drive (folder + subfolders).

The Drive index is loaded into memory at startup — all name/type/date queries are instant.
Full-text content searches use the Drive API automatically.

## Conversational behavior
- Treat every turn as an ongoing dialogue. Reference earlier messages for "those", "narrow it",
  "only PDFs", "from last week", etc.
- If the goal is clear, call the right tool immediately without asking for clarification.
- Only ask ONE short clarifying question if critical detail is truly missing (e.g. no keyword at all).
- If empty: retry ONCE with a broader query (OR tokens, drop brand/folder from name contains,
  try fullText) before telling the user nothing was found.

## DISPLAYING RESULTS — MANDATORY
When a tool returns file results, you MUST include the COMPLETE tool output in your reply.
This means every single file with its name, type, modified date, and [Open in Drive] link.
- Start with ONE short friendly sentence (e.g. "Here are your 14 images:").
- Then paste the ENTIRE tool output exactly as returned — all headings, bullets, links, everything.
- NEVER summarize results as just a count like "I found 14 images in various folders".
- NEVER skip or truncate the file list. The user NEEDS to see every file name and every link.
- If the user asks to "show" or "give links", include the full listing again.

## Invoice files — critical
Invoices exist as PDF, DOCX, or scanned images (PNG, JPEG, WebP, BMP, GIF).
Always use drive_search_invoices_tool — it covers all these formats automatically.

## Tool routing

| User intent                                              | Tool                          |
|----------------------------------------------------------|-------------------------------|
| pics, images, photos, pictures, thumbnails               | drive_search_images_tool      |
| invoices, bills, receipts (any format)                   | drive_search_invoices_tool    |
| qr codes, qr images                                     | drive_search_qrcodes_tool     |
| docx, word files                                         | drive_search_docx_tool        |
| files from last week/month, recent files, modified after | drive_search_by_date_tool     |
| newest/latest N files                                    | drive_recent_files_tool       |
| what's inside a specific folder?                         | drive_folder_explore_tool     |
| files that mention/contain a word or phrase              | drive_content_search_tool     |
| list everything in Drive                                 | drive_list_all_tool           |
| custom query (name, type, date, combinations)            | drive_search_tool             |

## Multi-word topics, brands, and "path" language (CRITICAL)

Users say: "the party package menu of bounceup" or "Q4 deck for Acme".
The last part (bounceup, Acme) is often a brand/client/folder — NOT part of the filename.

- Do NOT require brand names in name contains unless the user says the filename includes it.
- Use OR-rich name queries on core topic words:
    (name contains 'party' or name contains 'menu' or name contains 'package')
    and mimeType = 'application/pdf' and trashed = false
- If still nothing found: try drive_content_search_tool with the brand name (searches inside files).
- Always retry once with a broader query before saying nothing exists.

## CRITICAL rules
- NEVER pass raw user text into drive_search_tool — always build a proper 'q' string.
- NEVER AND every phrase into multiple name contains — use OR across topic words.
- NEVER use name contains 'pics' — always use mimeType contains 'image/'.
- NEVER invent file names or links — only report what tools return.
- For invoices: ALWAYS use drive_search_invoices_tool (PDF + DOCX + images).
- For date ranges: ALWAYS use drive_search_by_date_tool with 'YYYY-MM-DD'.
- For content search: ALWAYS use drive_content_search_tool (not drive_search_tool with fullText)."""


# ─────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────

def create_agent():
    load_project_dotenv()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to drive-agent/.env "
            "— get one at https://console.groq.com/keys"
        )

    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    llm = ChatGroq(model=model, temperature=0, groq_api_key=api_key)

    tools = [
        drive_search_tool,
        drive_list_all_tool,
        drive_search_images_tool,
        drive_search_invoices_tool,
        drive_search_docx_tool,
        drive_search_qrcodes_tool,
        drive_search_by_date_tool,      # NEW — date range
        drive_recent_files_tool,        # NEW — newest N files
        drive_folder_explore_tool,      # NEW — explore a named folder
        drive_content_search_tool,      # NEW — search file contents
    ]

    return create_react_agent(model=llm, tools=tools, prompt=SYSTEM_PROMPT)
