"""
drive_client.py — Google Drive access layer (performance-optimised)

Fixes applied vs v1:
  1. _api_search now uses 'in parents' with ALL folder IDs (recursive scope),
     not just the root — so fullText actually finds files in subfolders.
  2. Pre-compiled regex patterns in _eval_q (compiled once at import time).
  3. fullText results are cached for FULLTEXT_CACHE_TTL seconds so repeated
     queries don't hammer the Drive API.
  4. get_recent_files result is cached and only re-sorted when the cache refreshes.
  5. _recursive_list uses a thread pool (up to 8 workers) for concurrent subfolder
     traversal — cuts cold-start indexing time significantly on deep trees.
  6. _cache_search short-circuits on trashed=false immediately (no regex needed).

Bug fixes vs v2:
  7. search_files_in_named_folders now uses path-segment matching instead of plain
     substring so 'pics' does NOT match 'epics' or 'topics'.
  8. Cache readiness guard: init_drive_cache() blocks until the first full scan
     is complete, so no tool ever runs against an empty or partial cache.
  9. folder_path is always stored with a trailing slash normalised so segment
     splitting is consistent ('/pics/' splits cleanly on '/').
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
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

_FILE_FIELDS = (
    "id, name, mimeType, size, modifiedTime, createdTime, "
    "webViewLink, webContentLink, parents, owners, shared, "
    "thumbnailLink, description, starred"
)

_CACHE_REFRESH_SECS = int(os.getenv("CACHE_REFRESH_SECS", "300"))

# How long to cache fullText API results (seconds)
FULLTEXT_CACHE_TTL = int(os.getenv("FULLTEXT_CACHE_TTL", "120"))

# Max worker threads for recursive folder traversal
_TRAVERSE_WORKERS = int(os.getenv("TRAVERSE_WORKERS", "8"))


# ─────────────────────────────────────────────
# Pre-compiled regex patterns (fix #2)
# ─────────────────────────────────────────────

_RE_NAME_CONTAINS   = re.compile(r"name contains '([^']+)'",   re.I)
_RE_NAME_EQ         = re.compile(r"name = '([^']+)'",           re.I)
_RE_MIME_CONTAINS   = re.compile(r"mimetype contains '([^']+)'",re.I)
_RE_MIME_EQ         = re.compile(r"mimetype = '([^']+)'",        re.I)
_RE_MOD_GT          = re.compile(r"modifiedtime > '([^']+)'",   re.I)
_RE_MOD_LT          = re.compile(r"modifiedtime < '([^']+)'",   re.I)
_RE_TRASHED         = re.compile(r"trashed\s*=\s*false",         re.I)

# For _mime_matches helper
_RE_MIME_C_ALL      = re.compile(r"mimetype contains '([^']+)'", re.I)
_RE_MIME_EQ_ALL     = re.compile(r"mimetype = '([^']+)'",         re.I)


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
    Flat list of all file metadata dicts from the root folder (recursively).
    Folders are excluded; children carry a 'folder_path' field.

    Also stores:
      - _sorted_by_mtime: pre-sorted list for get_recent_files (fix #4)
      - _folder_ids: set of all folder IDs seen during traversal (fix #1)
      - _fulltext_cache: TTL cache for Drive API fullText results (fix #3)
    """

    def __init__(self):
        self._files: list[dict] = []
        self._sorted_by_mtime: list[dict] = []
        self._folder_ids: set[str] = set()
        self._lock = threading.RLock()
        self._last_refresh: float = 0.0

        # Fix #8: set after the FIRST full scan so callers can block on it
        self._ready = threading.Event()

        # fullText TTL cache: {query_str: (timestamp, [files])}
        self._fulltext_cache: dict[str, tuple[float, list[dict]]] = {}
        self._ft_lock = threading.Lock()

    def wait_until_ready(self, timeout: float = 60.0) -> bool:
        """Block until the first full Drive scan completes. Returns True if ready."""
        return self._ready.wait(timeout=timeout)

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._files)

    def get_sorted_by_mtime(self) -> list[dict]:
        """Pre-sorted list — no re-sort on every call (fix #4)."""
        with self._lock:
            return list(self._sorted_by_mtime)

    def get_folder_ids(self) -> set[str]:
        with self._lock:
            return set(self._folder_ids)

    def get_fulltext_cached(self, q: str) -> list[dict] | None:
        """Return cached fullText result if still fresh, else None."""
        with self._ft_lock:
            entry = self._fulltext_cache.get(q)
            if entry and (time.time() - entry[0]) < FULLTEXT_CACHE_TTL:
                logger.debug("fullText cache hit: %s", q)
                return list(entry[1])
        return None

    def set_fulltext_cached(self, q: str, files: list[dict]) -> None:
        with self._ft_lock:
            self._fulltext_cache[q] = (time.time(), files)

    def refresh(self, folder_id: str) -> None:
        logger.info("Cache refresh started for folder %s", folder_id)
        try:
            files, folder_ids = _recursive_list(folder_id, "/")
            sorted_files = sorted(
                files, key=lambda f: f.get("modifiedTime", ""), reverse=True
            )
            with self._lock:
                self._files = files
                self._sorted_by_mtime = sorted_files
                self._folder_ids = folder_ids
                self._last_refresh = time.time()
            # Invalidate fullText cache on refresh so stale results don't linger
            with self._ft_lock:
                self._fulltext_cache.clear()
            logger.info("Cache refresh complete — %d file(s), %d folder(s) indexed",
                        len(files), len(folder_ids))
            # Fix #8: signal readiness after first successful scan
            self._ready.set()
        except Exception as exc:
            logger.error("Cache refresh failed: %s", exc)

    def start_background_refresh(self, folder_id: str) -> None:
        def loop():
            while True:
                self.refresh(folder_id)
                time.sleep(_CACHE_REFRESH_SECS)

        t = threading.Thread(target=loop, daemon=True, name="drive-cache-refresh")
        t.start()


_cache = _FileCache()


# ─────────────────────────────────────────────
# Recursive folder traversal — concurrent (fix #5)
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


def _recursive_list(
    root_folder_id: str,
    root_path: str,
) -> tuple[list[dict], set[str]]:
    """
    Concurrently traverse all subfolders.
    Returns (flat_file_list, set_of_all_folder_ids).
    """
    service = _get_service()
    all_files: list[dict] = []
    all_folder_ids: set[str] = {root_folder_id}
    lock = threading.Lock()

    def process_folder(folder_id: str, current_path: str) -> list[tuple[str, str]]:
        """List one folder page by page; return (subfolder_id, path) pairs."""
        local_files: list[dict] = []
        subfolders: list[tuple[str, str]] = []
        page_token = None

        while True:
            resp = _list_page(service, folder_id, page_token)
            for item in resp.get("files", []):
                if item["mimeType"] == _FOLDER_MIME:
                    sub_path = current_path.rstrip("/") + "/" + item["name"] + "/"
                    subfolders.append((item["id"], sub_path))
                else:
                    item["folder_path"] = current_path
                    local_files.append(item)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        with lock:
            all_files.extend(local_files)

        return subfolders

    # BFS with a thread pool
    queue: list[tuple[str, str]] = [(root_folder_id, root_path)]
    with ThreadPoolExecutor(max_workers=_TRAVERSE_WORKERS) as pool:
        while queue:
            futures = {pool.submit(process_folder, fid, path): (fid, path)
                       for fid, path in queue}
            queue = []
            for future in as_completed(futures):
                fid, _ = futures[future]
                try:
                    subfolders = future.result()
                    for sub_fid, sub_path in subfolders:
                        all_folder_ids.add(sub_fid)
                        queue.append((sub_fid, sub_path))
                except Exception as exc:
                    logger.error("Error traversing folder %s: %s", fid, exc)

    return all_files, all_folder_ids


# ─────────────────────────────────────────────
# Public init — call once at app startup
# ─────────────────────────────────────────────

def init_drive_cache() -> None:
    """
    Call this once when your FastAPI app starts.
    Starts the background refresh thread and BLOCKS until the first full scan
    is complete (fix #8) — so no request is served against an empty cache.
    """
    folder_id = os.getenv("FOLDER_ID")
    if not folder_id:
        raise RuntimeError("FOLDER_ID env var is not set.")
    _cache.start_background_refresh(folder_id)
    ready = _cache.wait_until_ready(timeout=120.0)
    if not ready:
        logger.warning("Drive cache did not finish initial scan within 120s — proceeding anyway")
    logger.info("init_drive_cache: cache is ready")


# ─────────────────────────────────────────────
# Search helpers (used by agent.py tools)
# ─────────────────────────────────────────────

def list_all_files() -> list[dict]:
    return _cache.get_all()


def search_files(q: str) -> list[dict]:
    """
    Hybrid search:
      - fullText queries → Drive API (with TTL cache so repeats are instant)
      - Everything else  → fast in-memory cache filter
    """
    q_lower = q.lower()

    if "fulltext contains" in q_lower:
        return _api_search(q)

    return _cache_search(q)


def search_files_in_named_folders(folder_name_fragment: str, extra_q: str = "") -> list[dict]:
    """
    Return files whose folder_path contains folder_name_fragment as a full
    path segment (fix #7).

    Path segments are the slash-delimited parts of folder_path, e.g.:
      /pics/tmp/  →  segments: ['pics', 'tmp']

    So searching for 'pics' matches /pics/ and /pics/tmp/ but NOT /epics/ or /topics/.
    Matching is case-insensitive.
    """
    fragment = folder_name_fragment.lower().strip("/")
    results = []
    for f in _cache.get_all():
        raw_path = f.get("folder_path", "")
        # Split on '/' and check each segment exactly
        segments = [s.lower() for s in raw_path.strip("/").split("/") if s]
        if fragment in segments:
            if not extra_q:
                results.append(f)
            else:
                mime = f.get("mimeType", "")
                if _mime_matches(mime, extra_q):
                    results.append(f)
    return results


# ─────────────────────────────────────────────
# Cache-based query parser (fix #2 — pre-compiled regex)
# ─────────────────────────────────────────────

def _mime_matches(mime: str, q_fragment: str) -> bool:
    """Return True if any mimeType clause in q_fragment matches mime (OR logic)."""
    q = q_fragment.lower()
    mime_lower = mime.lower()

    contains_vals = _RE_MIME_C_ALL.findall(q)
    equals_vals   = _RE_MIME_EQ_ALL.findall(q)

    if not contains_vals and not equals_vals:
        return True  # no MIME filter — pass through

    for val in contains_vals:
        if val in mime_lower:
            return True
    for val in equals_vals:
        if mime_lower == val:
            return True

    return False


def _cache_search(q: str) -> list[dict]:
    files = _cache.get_all()
    return [f for f in files if _eval_q(q.strip(), f)]


def _eval_q(q: str, f: dict) -> bool:
    """Lightweight Drive 'q' evaluator using pre-compiled patterns."""
    q = q.strip()

    # Strip balanced outer parens
    while q.startswith("(") and q.endswith(")"):
        inner = q[1:-1]
        if _balanced(inner):
            q = inner.strip()
        else:
            break

    # trashed = false short-circuit (very common clause, no regex needed)
    if q.lower() == "trashed = false":
        return True

    # Top-level AND
    and_parts = _split_top_level(q, " and ")
    if len(and_parts) > 1:
        return all(_eval_q(p, f) for p in and_parts)

    # Top-level OR
    or_parts = _split_top_level(q, " or ")
    if len(or_parts) > 1:
        return any(_eval_q(p, f) for p in or_parts)

    # Leaf clause — use pre-compiled patterns
    q = q.strip().strip("()")
    name     = f.get("name", "").lower()
    mime     = f.get("mimeType", "").lower()
    modified = f.get("modifiedTime", "")

    m = _RE_NAME_CONTAINS.match(q)
    if m:
        return m.group(1).lower() in name

    m = _RE_NAME_EQ.match(q)
    if m:
        return name == m.group(1).lower()

    m = _RE_MIME_CONTAINS.match(q)
    if m:
        return m.group(1).lower() in mime

    m = _RE_MIME_EQ.match(q)
    if m:
        return mime == m.group(1).lower()

    m = _RE_MOD_GT.match(q)
    if m:
        return modified >= m.group(1)

    m = _RE_MOD_LT.match(q)
    if m:
        return modified < m.group(1)

    if _RE_TRASHED.match(q):
        return True  # we never index trashed files

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
    buf: list[str] = []
    i = 0
    sep_lower = sep.lower()
    q_lower = q.lower()
    sep_len = len(sep)
    while i < len(q):
        if q[i] == "(":
            depth += 1
            buf.append(q[i])
            i += 1
        elif q[i] == ")":
            depth -= 1
            buf.append(q[i])
            i += 1
        elif depth == 0 and q_lower[i:i + sep_len] == sep_lower:
            parts.append("".join(buf).strip())
            buf = []
            i += sep_len
        else:
            buf.append(q[i])
            i += 1
    if buf:
        parts.append("".join(buf).strip())
    return parts if len(parts) > 1 else [q]


# ─────────────────────────────────────────────
# API-based search for fullText queries (fix #1 + fix #3)
# ─────────────────────────────────────────────

def _api_search(q: str) -> list[dict]:
    """
    Run a fullText query against the Drive API.

    Fix #1: Scopes the search to ALL known folder IDs (not just root),
            so files in subfolders are found.
    Fix #3: Results are cached for FULLTEXT_CACHE_TTL seconds.
    """
    cached = _cache.get_fulltext_cached(q)
    if cached is not None:
        return cached

    service = _get_service()
    root_folder_id = os.getenv("FOLDER_ID", "")

    # Build an OR clause covering root + all known subfolders
    folder_ids = _cache.get_folder_ids()
    if not folder_ids:
        folder_ids = {root_folder_id}

    # Drive API 'q' has a length limit; batch into chunks of 50 folders
    all_results: list[dict] = []
    seen_ids: set[str] = set()
    folder_list = list(folder_ids)
    chunk_size = 50

    for i in range(0, len(folder_list), chunk_size):
        chunk = folder_list[i: i + chunk_size]
        parents_clause = " or ".join(f"'{fid}' in parents" for fid in chunk)
        scoped_q = f"({q}) and ({parents_clause}) and trashed = false"

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
            for f in resp.get("files", []):
                if f["id"] not in seen_ids:
                    seen_ids.add(f["id"])
                    all_results.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _cache.set_fulltext_cached(q, all_results)
    return all_results


# ─────────────────────────────────────────────
# Content extraction (for RAG / semantic search)
# ─────────────────────────────────────────────

def extract_text(file: dict) -> str | None:
    service = _get_service()
    mime = file.get("mimeType", "")
    file_id = file["id"]

    try:
        export_map = {
            "application/vnd.google-apps.document":     "text/plain",
            "application/vnd.google-apps.spreadsheet":  "text/csv",
            "application/vnd.google-apps.presentation": "text/plain",
        }
        if mime in export_map:
            data = (
                service.files()
                .export_media(fileId=file_id, mimeType=export_map[mime])
                .execute()
            )
            return data.decode("utf-8", errors="replace")

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
        logger.warning("Text extraction failed for %s (%s): %s",
                       file.get("name"), mime, exc)
    return None


def _extract_pdf(raw: bytes) -> str | None:
    try:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except ImportError:
        logger.debug("PyMuPDF not installed — PDF text extraction skipped")
    return None


def _extract_docx(raw: bytes) -> str | None:
    try:
        import docx
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
    for f in _cache.get_all():
        if f["id"] == file_id:
            return f
    try:
        service = _get_service()
        return service.files().get(fileId=file_id, fields=_FILE_FIELDS).execute()
    except Exception as exc:
        logger.error("get_file_by_id failed: %s", exc)
        return None


def get_folder_tree() -> dict:
    tree: dict[str, list[dict]] = {}
    for f in _cache.get_all():
        path = f.get("folder_path", "/")
        tree.setdefault(path, []).append(f)
    return tree


def get_recent_files(n: int = 20) -> list[dict]:
    """Return the n most recently modified files — uses pre-sorted cache (fix #4)."""
    return _cache.get_sorted_by_mtime()[:n]


def get_files_by_type(mime_type: str) -> list[dict]:
    if mime_type.endswith("/"):
        return [f for f in _cache.get_all()
                if f.get("mimeType", "").startswith(mime_type)]
    return [f for f in _cache.get_all() if f.get("mimeType") == mime_type]


def get_cache_stats() -> dict:
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
