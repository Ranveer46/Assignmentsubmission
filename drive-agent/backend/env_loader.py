"""Load drive-agent/.env regardless of process cwd (e.g. uvicorn run from backend/)."""
from pathlib import Path

from dotenv import load_dotenv


def load_project_dotenv() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
