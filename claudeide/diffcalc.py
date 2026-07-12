"""Pure diff helpers for the review UI (no sublime imports)."""

import difflib
from typing import List, Optional


def changed_new_lines(old_text: str, new_text: str) -> List[int]:
    """0-based line numbers in *new_text* that differ from *old_text*
    (replacements and insertions; pure deletions have no new-side line)."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    changed = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            changed.extend(range(j1, j2))
    return changed


def pick_target_path(old_file_path: str, new_file_path: Optional[str]) -> str:
    """Where an accepted diff should be written."""
    return new_file_path or old_file_path
