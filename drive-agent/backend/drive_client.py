"""
drive_client.py — Google Drive access layer

What this module does:
  1. Recursively scans the entire shared folder (all subfolders) at startup
     and caches the file tree in memory so every search is fast.
  2. Exposes clean search helpers used by agent.py tools.
  3. Supports full-text content extraction from Google Docs, PDFs, and DOCX
     so the agent can do semantic / content-based search.
  4. Periodic background refresh keeps the in-memory cache up to date
     without restarting the server.

Environment variables (from .env):
  FOLDER_ID            — Root Google Drive folder ID
  GOOGLE_CREDS_FILE    — Path to service_account.json (default: service_account.json)
  CACHE_REFRESH_SECS   — How often to refresh the file cache (default: 300)
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from fnmatch import fnmatch
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_FOLDER_MIME = "application/vnd.google-apps.folder"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Fields fetched for every file — extend here if you need more
_FILE_FIELDS = (
    "id, name, mimeType, size, modifiedTime, createdTime, "
    "webViewLink, webContentLink, parents, owners, shared, "
    "thumbnailLink, description, starred"
)

_CACHE_REFRESH_SECS = int(os.getenv("CACHE_REFRESH_SECS", "300"))


# ─────────────────────────────────────────────
# Drive service singleton
# ─────────────────────────────────────────────

_service = None
_service_lock = threading.Lock()


def _get_service():
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is not None:
            return _service
        creds_file = os.getenv("GOOGLE_CREDS_FILE", "service_account.json")
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=SCOPES
        )
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google Drive service initialised")
    return _service


# ─────────────────────────────────────────────
# In-memory file cache
# ─────────────────────────────────────────────

class _FileCache:
    """
    Stores a flat list of all file metadata dicts from the root folder
    (recursively). Folders themselves are excluded from the list but their
    children are included with a 'folder_path' field so the agent can answer
    "which folder is this in?".
    """

    def __init__(self):
        self._files: list[dict] = []
        self._lock = threading.RLock()
        self._last_refresh: float = 0.0

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._files)

    def refresh(self, folder_id: str) -> None:
        logger.info("Cache refresh started for folder %s", folder_id)
        try:
            files = _recursive_list(folder_id, "/")
            with self._lock:
                self._files = files
                self._last_refresh = time.time()
            logger.info("Cache refresh complete — %d file(s) indexed", len(files))
        except Exception as exc:
            logger.error("Cache refresh failed: %s", exc)

    def start_background_refresh(self, folder_id: str) -> None:
        """Refresh once immediately, then keep refreshing in the background."""
        def loop():
            while True:
                self.refresh(folder_id)
                time.sleep(_CACHE_REFRESH_SECS)

        t = threading.Thread(target=loop, daemon=True, name="drive-cache-refresh")
        t.start()


_cache = _FileCache()


# ─────────────────────────────────────────────
# Recursive folder traversal
# ─────────────────────────────────────────────

def _list_page(service, folder_id: str, page_token: str | None) -> dict:
    return (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields=f"nextPageToken, files({_FILE_FIELDS})",
            pageSize=1000,
            pageToken=page_token,
        )
        .execute()
    )


def _recursive_list(folder_id: str, current_path: str) -> list[dict]:
    """Return every non-folder file under folder_id, recursively."""
    service = _get_service()
    all_files: list[dict] = []
    page_token = None

    while True:
        resp = _list_page(service, folder_id, page_token)
        for item in resp.get("files", []):
            if item["mimeType"] == _FOLDER_MIME:
                # Recurse into subfolder
                sub_path = current_path.rstrip("/") + "/" + item["name"] + "/"
                all_files.extend(_recursive_list(item["id"], sub_path))
            else:
                item["folder_path"] = current_path  # inject path metadata
                all_files.append(item)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return all_files


# ─────────────────────────────────────────────
# Public init — call once at app startup
# ─────────────────────────────────────────────

def init_drive_cache() -> None:
    """
    Call this once when your FastAPI app starts.
    Begins recursive indexing of the Drive folder and starts the
    background refresh thread.

    Example in main.py:
        from drive_client import init_drive_cache

        @app.on_event("startup")
        async def startup():
            init_drive_cache()
    """
    folder_id = os.getenv("FOLDER_ID")
    if not folder_id:
        raise RuntimeError("FOLDER_ID env var is not set.")
    _cache.start_background_refresh(folder_id)


# ─────────────────────────────────────────────
# Search helpers (used by agent.py tools)
# ─────────────────────────────────────────────

def list_all_files() -> list[dict]:
    """Return every file in the cached index."""
    return _cache.get_all()


def search_files(q: str) -> list[dict]:
    """
    Run a Google Drive API 'q' query PLUS filter the local cache.

    Strategy:
      - For simple queries, filter the in-memory cache (fast, no API quota).
      - For fullText queries or very complex queries, hit the API directly
        because Drive's full-text index is server-side only.

    This hybrid approach gives you:
      ✅ Speed for name/mimeType queries (cache)
      ✅ Accuracy for fullText queries (API)
      ✅ Freshness for the most recently modified files
    """
    q_lower = q.lower()

    # fullText queries must go to the API — we don't have file contents locally
    if "fulltext contains" in q_lower:
        return _api_search(q)

    # Everything else: filter the local cache
    return _cache_search(q)


def search_files_in_named_folders(folder_name_fragment: str, extra_q: str = "") -> list[dict]:
    """
    Return files whose folder_path contains folder_name_fragment (case-insensitive).
    Optionally filter by an additional mimeType clause from extra_q.

    E.g. search_files_in_named_folders("invoice", "mimeType contains 'image/'")
    """
    fragment = folder_name_fragment.lower()
    results = []
    for f in _cache.get_all():
        path = f.get("folder_path", "").lower()
        if fragment in path:
            if not extra_q:
                results.append(f)
            else:
                mime = f.get("mimeType", "")
                if _mime_matches(mime, extra_q):
                    results.append(f)
    return results


# ─────────────────────────────────────────────
# Cache-based query parser
# ─────────────────────────────────────────────

def _mime_matches(mime: str, q_fragment: str) -> bool:
    """Check if a MIME type satisfies any mimeType clause in q_fragment.

    Handles multiple OR'd clauses like:
        (mimeType = 'application/pdf' or mimeType = 'image/png' or ...)
    Returns True if ANY clause matches.
    """
    import re

    q = q_fragment.lower()
    mime_lower = mime.lower()

    # Collect ALL mimeType contains '...' values
    contains_vals = re.findall(r"mimetype contains '([^']+)'", q)
    # Collect ALL mimeType = '...' values
    equals_vals = re.findall(r"mimetype = '([^']+)'", q)

    if not contains_vals and not equals_vals:
        return True  # no MIME filter at all — pass through

    # OR logic: return True if ANY clause matches
    for val in contains_vals:
        if val in mime_lower:
            return True
    for val in equals_vals:
        if mime_lower == val:
            return True

    return False


def _cache_search(q: str) -> list[dict]:
    """
    Parse a subset of Drive 'q' syntax and filter the in-memory cache.
    Supports: name contains, name =, mimeType =, mimeType contains,
              modifiedTime >, AND, OR (flat, no nested parens).
    """
    import re

    files = _cache.get_all()
    results = []

    # Normalise: lowercase operators for parsing
    q_norm = q.strip()

    for f in files:
        if _eval_q(q_norm, f):
            results.append(f)

    return results


def _eval_q(q: str, f: dict) -> bool:
    """Very lightweight Drive 'q' evaluator for cache filtering."""
    import re

    q = q.strip()

    # AND — split on ' and ' (case-insensitive), all must be true
    # OR  — split on ' or '  (case-insensitive), any must be true
    # Simple approach: handle OR groups inside parens first, then AND

    # Remove outer parentheses wrapping the whole expression
    while q.startswith("(") and q.endswith(")"):
        inner = q[1:-1]
        # Make sure the parens are balanced before stripping
        if _balanced(inner):
            q = inner.strip()
        else:
            break

    # Top-level AND split
    and_parts = _split_top_level(q, " and ")
    if len(and_parts) > 1:
        return all(_eval_q(p, f) for p in and_parts)

    # Top-level OR split
    or_parts = _split_top_level(q, " or ")
    if len(or_parts) > 1:
        return any(_eval_q(p, f) for p in or_parts)

    # Leaf clause
    q = q.strip().strip("()")
    name = f.get("name", "").lower()
    mime = f.get("mimeType", "").lower()
    modified = f.get("modifiedTime", "")

    # name contains 'x'
    m = re.match(r"name contains '([^']+)'", q, re.I)
    if m:
        return m.group(1).lower() in name

    # name = 'x'
    m = re.match(r"name = '([^']+)'", q, re.I)
    if m:
        return name == m.group(1).lower()

    # mimeType contains 'x'
    m = re.match(r"mimetype contains '([^']+)'", q, re.I)
    if m:
        return m.group(1).lower() in mime

    # mimeType = 'x'
    m = re.match(r"mimetype = '([^']+)'", q, re.I)
    if m:
        return mime == m.group(1).lower()

    # modifiedTime > 'YYYY-MM-DDTHH:MM:SS'
    m = re.match(r"modifiedtime > '([^']+)'", q, re.I)
    if m:
        return modified >= m.group(1)

    # modifiedTime < 'x'
    m = re.match(r"modifiedtime < '([^']+)'", q, re.I)
    if m:
        return modified < m.group(1)

    # trashed = false — always false in our cache (we skip trashed in recursive scan)
    if re.match(r"trashed\s*=\s*false", q, re.I):
        return True

    # Unknown clause — pass through (safe default)
    logger.debug("Unknown q clause (passing through): %s", q)
    return True


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _split_top_level(q: str, sep: str) -> list[str]:
    """Split q on sep only when not inside parentheses."""
    parts = []
    depth = 0
    buf = []
    i = 0
    sep_lower = sep.lower()
    q_lower = q.lower()
    while i < len(q):
        if q[i] == "(":
            depth += 1
            buf.append(q[i])
            i += 1
        elif q[i] == ")":
            depth -= 1
            buf.append(q[i])
            i += 1
        elif depth == 0 and q_lower[i:i+len(sep)] == sep_lower:
            parts.append("".join(buf).strip())
            buf = []
            i += len(sep)
        else:
            buf.append(q[i])
            i += 1
    if buf:
        parts.append("".join(buf).strip())
    return parts if len(parts) > 1 else [q]


# ─────────────────────────────────────────────
# API-based search (for fullText queries)
# ─────────────────────────────────────────────

def _api_search(q: str) -> list[dict]:
    """
    Run a query directly against the Drive API.
    Used when the query contains fullText contains which
    requires server-side indexing.
    """
    service = _get_service()
    folder_id = os.getenv("FOLDER_ID", "")
    # Scope the fullText search to the shared folder
    scoped_q = f"({q}) and '{folder_id}' in parents and trashed = false"

    all_files = []
    page_token = None
    while True:
        resp = (
            service.files()
            .list(
                q=scoped_q,
                fields=f"nextPageToken, files({_FILE_FIELDS})",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        all_files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return all_files


# ─────────────────────────────────────────────
# Content extraction (for RAG / semantic search)
# ─────────────────────────────────────────────

def extract_text(file: dict) -> str | None:
    """
    Extract plain text from a file. Returns None if extraction is not
    supported for this MIME type.

    Supported:
      - Google Docs  → export as text/plain
      - Google Sheets → export as text/csv
      - Google Slides → export as text/plain
      - PDF          → download + PyMuPDF (if installed)
      - DOCX         → download + python-docx (if installed)
      - Plain text   → download directly
    """
    service = _get_service()
    mime = file.get("mimeType", "")
    file_id = file["id"]

    try:
        # ── Google Workspace types ──────────────────────────────────────
        export_map = {
            "application/vnd.google-apps.document": "text/plain",
            "application/vnd.google-apps.spreadsheet": "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
        }
        if mime in export_map:
            data = (
                service.files()
                .export_media(fileId=file_id, mimeType=export_map[mime])
                .execute()
            )
            return data.decode("utf-8", errors="replace")

        # ── Binary downloads ────────────────────────────────────────────
        buf = io.BytesIO()
        request = service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        raw = buf.getvalue()

        if mime == "application/pdf":
            return _extract_pdf(raw)

        if mime == _DOCX_MIME:
            return _extract_docx(raw)

        if mime.startswith("text/"):
            return raw.decode("utf-8", errors="replace")

    except Exception as exc:
        logger.warning("Text extraction failed for %s (%s): %s", file.get("name"), mime, exc)

    return None


def _extract_pdf(raw: bytes) -> str | None:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=raw, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except ImportError:
        logger.debug("PyMuPDF not installed — PDF text extraction skipped")
    return None


def _extract_docx(raw: bytes) -> str | None:
    try:
        import docx  # python-docx
        import io as _io
        doc = docx.Document(_io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        logger.debug("python-docx not installed — DOCX text extraction skipped")
    return None


# ─────────────────────────────────────────────
# File metadata helpers
# ─────────────────────────────────────────────

def get_file_by_id(file_id: str) -> dict | None:
    """Return a single file's metadata by ID (from cache first, then API)."""
    for f in _cache.get_all():
        if f["id"] == file_id:
            return f
    # Fallback to API if not in cache
    try:
        service = _get_service()
        return service.files().get(fileId=file_id, fields=_FILE_FIELDS).execute()
    except Exception as exc:
        logger.error("get_file_by_id failed: %s", exc)
        return None


def get_folder_tree() -> dict:
    """
    Return a nested dict representing the folder hierarchy.
    Useful for "which folder contains X?" type queries.
    """
    # Build path → [files] map from cache
    tree: dict[str, list[dict]] = {}
    for f in _cache.get_all():
        path = f.get("folder_path", "/")
        tree.setdefault(path, []).append(f)
    return tree


def get_recent_files(n: int = 20) -> list[dict]:
    """Return the n most recently modified files."""
    files = _cache.get_all()
    files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return files[:n]


def get_files_by_type(mime_type: str) -> list[dict]:
    """
    Return all files matching a MIME type (exact or prefix).
    E.g. get_files_by_type('image/') returns all images.
    """
    if mime_type.endswith("/"):
        return [f for f in _cache.get_all() if f.get("mimeType", "").startswith(mime_type)]
    return [f for f in _cache.get_all() if f.get("mimeType") == mime_type]


def get_cache_stats() -> dict:
    """Return cache health info — useful for a /health endpoint."""
    files = _cache.get_all()
    mime_counts: dict[str, int] = {}
    for f in files:
        m = f.get("mimeType", "unknown")
        mime_counts[m] = mime_counts.get(m, 0) + 1
    return {
        "total_files": len(files),
        "last_refresh": _cache._last_refresh,
        "refresh_interval_secs": _CACHE_REFRESH_SECS,
        "mime_breakdown": mime_counts,
    }
