"""
Load drive-agent/.env regardless of how/where the process was started.

Tries four candidate paths in priority order so this works under:
  - uvicorn --reload (watcher subprocess can alter __file__ resolution)
  - direct `python main.py` from backend/
  - direct `python main.py` from project root
  - any other cwd
"""
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def load_project_dotenv() -> None:
    candidates = [
        # 1. Relative to this file — most reliable in normal runs
        Path(__file__).resolve().parent.parent / ".env",
        # 2. Two levels up from cwd (e.g. cwd = backend/)
        Path.cwd().parent / ".env",
        # 3. Directly in cwd (e.g. cwd = drive-agent/)
        Path.cwd() / ".env",
        # 4. Script entry-point directory (works with uvicorn reload worker)
        Path(os.path.abspath(os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "..", ".env"))),
    ]

    loaded = False
    for candidate in candidates:
        if candidate.exists():
            load_dotenv(candidate, override=True)
            loaded = True
            # Belt-and-suspenders: explicitly push each value into os.environ
            # in case load_dotenv's override is suppressed by the reloader.
            for key, val in dotenv_values(candidate).items():
                if val is not None:
                    os.environ.setdefault(key, val)
                    os.environ[key] = val  # force-override
            break

    if not loaded:
        # Last resort: let python-dotenv search up the directory tree
        load_dotenv(override=True)
