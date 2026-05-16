import os
import threading
import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build
from google.oauth2 import service_account

from env_loader import load_project_dotenv

load_project_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_ID = os.getenv("FOLDER_ID", "your_folder_id_here")
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")

# Google HTTP timeout (seconds) — large folder trees can be slow.
_DRIVE_HTTP_TIMEOUT = int(os.getenv("DRIVE_HTTP_TIMEOUT", "120"))

# Cache folder tree so every search_files() does not re-walk the entire Drive.
_folder_cache_lock = threading.Lock()
_cached_folder_root: str | None = None
_cached_folder_ids: frozenset[str] | None = None


def get_drive_service():
    """Build and return an authenticated Google Drive API service."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    http = httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT)
    authed = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    return build("drive", "v3", http=authed, cache_discovery=False)


def get_all_folder_ids(root_id: str) -> list[str]:
    """Recursively collect all subfolder IDs starting from root_id."""
    global _cached_folder_root, _cached_folder_ids
    with _folder_cache_lock:
        if _cached_folder_ids is not None and _cached_folder_root == root_id:
            return list(_cached_folder_ids)

    service = get_drive_service()
    folder_ids = {root_id}
    queue = [root_id]

    while queue:
        current_id = queue.pop(0)
        query = f"'{current_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        page_token = None
        while True:
            results = service.files().list(
                q=query,
                fields="nextPageToken, files(id)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
            for folder in results.get("files", []):
                if folder["id"] not in folder_ids:
                    folder_ids.add(folder["id"])
                    queue.append(folder["id"])
            page_token = results.get("nextPageToken")
            if not page_token:
                break

    out = frozenset(folder_ids)
    with _folder_cache_lock:
        _cached_folder_root = root_id
        _cached_folder_ids = out
    return list(out)


def build_recursive_query(folder_ids: list[str], base_query: str = "") -> str:
    """Build a query string that checks for any of the provided folder IDs."""
    folder_clause = " or ".join([f"'{fid}' in parents" for fid in folder_ids])
    if base_query:
        return f"({folder_clause}) and ({base_query})"
    return f"({folder_clause}) and trashed = false"


def search_files(query: str) -> list[dict]:
    """
    Search files inside the shared folder and all subfolders using a Drive API 'q' query string.
    """
    service = get_drive_service()
    all_folders = get_all_folder_ids(FOLDER_ID)
    full_query = build_recursive_query(all_folders, query)
    
    all_files = []
    page_token = None
    while True:
        results = (
            service.files()
            .list(
                q=full_query,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return all_files


def list_all_files() -> list[dict]:
    """List all files in the configured shared folder and all subfolders."""
    service = get_drive_service()
    all_folders = get_all_folder_ids(FOLDER_ID)
    query = build_recursive_query(all_folders)
    
    all_files = []
    page_token = None
    while True:
        results = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return all_files


def search_files_in_named_folders(folder_name_contains: str, file_query: str = "") -> list[dict]:
    """
    Return all files inside any subfolder whose name contains `folder_name_contains`.
    Optionally restrict to files matching `file_query` (a Drive API q-string fragment).

    Example:
        search_files_in_named_folders("qr", "mimeType contains 'image/'")
        → all images inside every folder whose name contains 'qr' (e.g. 'qr codes').
    """
    service = get_drive_service()

    # Find all subfolder IDs in the shared tree first (uses cache)
    all_folder_ids = get_all_folder_ids(FOLDER_ID)
    if not all_folder_ids:
        return []

    # Search for folders whose name matches inside the shared tree
    parent_clause = " or ".join([f"'{fid}' in parents" for fid in all_folder_ids])
    folder_query = (
        f"({parent_clause}) and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"name contains '{folder_name_contains}' and trashed = false"
    )
    folder_results = service.files().list(
        q=folder_query,
        fields="files(id, name)",
        pageSize=100,
    ).execute()
    matched_folder_ids = [f["id"] for f in folder_results.get("files", [])]

    if not matched_folder_ids:
        return []

    # Retrieve all files within those matched folders
    child_parent_clause = " or ".join([f"'{fid}' in parents" for fid in matched_folder_ids])
    if file_query:
        full_query = f"({child_parent_clause}) and ({file_query}) and trashed = false"
    else:
        full_query = f"({child_parent_clause}) and trashed = false"

    all_files: list[dict] = []
    page_token = None
    while True:
        results = (
            service.files()
            .list(
                q=full_query,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return all_files
