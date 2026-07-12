"""Discovery lock file management (``~/.claude/ide/<port>.lock``).

Claude Code's ``/ide`` command and the ``CLAUDE_CODE_SSE_PORT`` auto-connect
path both read this file to find the server and its auth token.
"""

import json
import os
import secrets
from typing import List, Optional


def generate_token() -> str:
    """128-bit CSPRNG token as 32 lowercase hex chars (never math.random)."""
    return secrets.token_hex(16)


def default_lock_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "ide")


def lock_path(port: int, lock_dir: Optional[str] = None) -> str:
    return os.path.join(lock_dir or default_lock_dir(), f"{port}.lock")


def write_lock(
    port: int,
    pid: int,
    workspace_folders: List[str],
    auth_token: str,
    ide_name: str = "Sublime Text",
    lock_dir: Optional[str] = None,
) -> str:
    """Write the lock file atomically (tmp + os.replace). Returns its path."""
    directory = lock_dir or default_lock_dir()
    os.makedirs(directory, exist_ok=True)
    path = lock_path(port, directory)
    payload = {
        "pid": pid,
        "workspaceFolders": workspace_folders,
        "ideName": ide_name,
        "transport": "ws",
        "authToken": auth_token,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def remove_lock(port: int, lock_dir: Optional[str] = None) -> None:
    try:
        os.remove(lock_path(port, lock_dir))
    except OSError:
        pass
