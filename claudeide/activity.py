"""Recent Activity model: what changed on disk, and which Claude session did it.

Two append-only JSONL logs feed the model (both under ``~/.claude/logs``):

* ``file-activity.jsonl`` — written by ``scripts/activity_hook.py`` from
  Claude Code's PreToolUse/PostToolUse hooks. Carries *who*: exact paths for
  Edit/Write tools, and start/end intervals for Bash calls (whose file
  effects are unknown to the hook).
* ``file-activity-fs.jsonl`` — written by the Sublime side from a
  filesystem watcher over live session roots. Carries *what*: every file
  that actually changed, whichever tool (or human) changed it.

This module joins the two and builds the grouped tree ``project → session →
files`` that the panel renders. Pure Python 3.8, never imports ``sublime``.
"""

import json
import os
import re
import time
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# ---------- classification ----------

DOC_EXTS = {
    ".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".pdf", ".rtf",
    ".csv", ".tsv",
    ".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pptx", ".ppt",
}
MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".flac",
}
# Sublime cannot display these; the panel hands them to the OS default app.
OS_OPEN_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx",
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".flac",
    ".zip", ".7z", ".rar", ".psd", ".ai", ".fig",
}

DEFAULT_PRUNE = [
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".nuxt",
    "dist", "build", "coverage", ".ruff_cache", ".pytest_cache", ".mypy_cache",
    ".cache", ".turbo", ".playwright-cli", "site-packages", ".claude",
    # inside ~/.claude itself (when a session's cwd is ~/.claude)
    "projects", "session-archive", "shell-snapshots", "file-history",
    "image-cache", "paste-cache", "cache", "logs", "sessions", "tasks", "teams",
    "jobs", "daemon", "tmp", "uploads", "backups", "statsig", "ide",
]

TEMP_NAME_RE = re.compile(r"(^~\$|^\.~lock|\.tmp$|\.temp$|\.crdownload$|\.part$|\.swp$|~$)", re.I)
WORKTREE_DIR_RE = re.compile(r"^\.?wt-", re.I)

# Segments that mean "machine/system churn, never a project document": the
# prune list plus the OS user-data roots. Used to keep the persisted fs log
# lean (see is_fs_loggable). normcase-lowercased for Windows comparison.
NOISE_SEGMENTS = {s.lower() for s in DEFAULT_PRUNE} | {"appdata"}


def norm(path: str) -> str:
    """Case-insensitive, separator-normalised key for Windows paths."""
    return os.path.normcase(os.path.normpath(path))


def ext_class(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in DOC_EXTS:
        return "doc"
    if ext in MEDIA_EXTS:
        return "media"
    return "code"


def is_temp_name(name: str) -> bool:
    return bool(TEMP_NAME_RE.search(name))


def is_pruned(rel_parts: Iterable[str], prune: Iterable[str]) -> bool:
    prune_set = set(prune)
    return any(part in prune_set for part in rel_parts)


def is_fs_loggable(path: str) -> bool:
    """Whether a filesystem change is worth *persisting* to the fs log.

    The fs log is the panel's default-view cache across Sublime restarts, so it
    holds only what that view shows: a document or media file (not code, not a
    temp/lock file) that lives in a project, not in system/tooling directories
    (AppData, caches, .git, node_modules, .claude internals, build output). The
    live in-memory model still ingests everything — this filter bounds the file
    on disk, which is otherwise ~90% machine churn."""
    if is_temp_name(os.path.basename(path)):
        return False
    if ext_class(path) == "code":
        return False
    return not any(seg in NOISE_SEGMENTS for seg in norm(path).split(os.sep))


# ---------- burst gate ----------

# A git checkout / rebase / stash that rewrites hundreds of files in a second
# is not "a session changed these files". Measured on a real machine: a
# worktree rebase touches ~830 documents inside one 10 s bucket, while the
# busiest genuine session output was 39 in 10 s.
BURST_WINDOW = 10.0  # seconds a burst is measured over
BURST_THRESHOLD = 100  # more events than this per key and window = a storm
BURST_QUIET = 5.0  # a storm ends after this much silence


def burst_key(path: str, roots: "RootIndex") -> str:
    """The unit a storm is counted in: the project root plus worktree label,
    or (path not under any root) its first four segments."""
    where = roots.classify(path)
    if where is not None:
        return norm(where[0]) + "|" + where[1].lower()
    return os.sep.join(norm(path).split(os.sep)[:4])


class BurstGate:
    """Live storm detector for the watcher's flush batches. ``batch(key, n,
    now)`` says whether a batch of ``n`` events for ``key`` passes: a batch
    over the threshold opens a storm for that key and is dropped whole, and
    every further batch is dropped until ``quiet`` seconds pass without one.
    Time-injected and pure so it is testable."""

    def __init__(self, threshold: int = BURST_THRESHOLD, quiet: float = BURST_QUIET) -> None:
        self.threshold = threshold
        self.quiet = quiet
        self._storm_until = {}  # type: Dict[str, float]
        self.dropped = {}  # type: Dict[str, int]

    def batch(self, key: str, n: int, now: float) -> bool:
        until = self._storm_until.get(key)
        if until is not None and now < until:
            self._storm_until[key] = now + self.quiet
            self.dropped[key] = self.dropped.get(key, 0) + n
            return False
        if n > self.threshold:
            self._storm_until[key] = now + self.quiet
            self.dropped[key] = self.dropped.get(key, 0) + n
            return False
        self._storm_until.pop(key, None)
        return True

    def total_dropped(self) -> int:
        return sum(self.dropped.values())


def storm_buckets(records: Iterable[Dict[str, Any]], key_of: Callable[[str], str],
                  window: float = BURST_WINDOW,
                  threshold: int = BURST_THRESHOLD) -> Set[Tuple[str, int]]:
    """Offline twin of BurstGate for replaying a log: the ``(key, bucket)``
    pairs whose event count exceeds ``threshold`` within one ``window``-sized
    time bucket. A record is a storm record when
    ``(key_of(path), int(ts // window))`` is in the result."""
    counts = {}  # type: Dict[Tuple[str, int], int]
    for rec in records:
        path = rec.get("path")
        if not path:
            continue
        k = (key_of(path), int(float(rec.get("ts", 0)) // window))
        counts[k] = counts.get(k, 0) + 1
    return {k for k, n in counts.items() if n > threshold}


# ---------- sessions ----------


class SessionInfo:
    __slots__ = ("sid", "name", "cwd", "status", "kind", "started_at", "live")

    def __init__(self, sid: str, name: str, cwd: str, status: str = "",
                 kind: str = "", started_at: float = 0.0, live: bool = True) -> None:
        self.sid = sid
        self.name = name
        self.cwd = cwd
        self.status = status
        self.kind = kind
        self.started_at = started_at
        self.live = live


def fallback_label(sid: str, cwd: str) -> str:
    """Label for a session without a name: ``<project>-<id4>`` reads better
    than a bare hex id and matches Claude Code's own derived names."""
    base = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    return f"{base}-{sid[:4]}" if base else sid[:8]


def load_sessions(sessions_dir: str) -> Dict[str, SessionInfo]:
    """Read ``~/.claude/sessions/<pid>.json`` (one per live Claude process)."""
    out = {}  # type: Dict[str, SessionInfo]
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, fn), encoding="utf-8") as fh:
                j = json.load(fh)
        except (OSError, ValueError):
            continue
        sid = j.get("sessionId")
        cwd = j.get("cwd")
        if not sid or not cwd:
            continue
        out[sid] = SessionInfo(
            sid, j.get("name") or fallback_label(sid, cwd), cwd, j.get("status", ""),
            j.get("kind", ""), float(j.get("startedAt", 0)) / 1000.0, True,
        )
    return out


# ---------- roots / worktrees ----------


def worktree_main_root(wt_dir: str) -> Optional[str]:
    """For a git worktree checkout, return the main repository's root by
    reading its ``.git`` *file* (``gitdir: <main>/.git/worktrees/<name>``)."""
    dotgit = os.path.join(wt_dir, ".git")
    try:
        if not os.path.isfile(dotgit):
            return None
        with open(dotgit, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first.lower().startswith("gitdir:"):
        return None
    gitdir = first.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(wt_dir, gitdir)
    gitdir = os.path.normpath(gitdir)
    parts = gitdir.split(os.sep)
    # .../<main>/.git/worktrees/<name>
    if len(parts) >= 3 and parts[-2].lower() == "worktrees" and parts[-3].lower() == ".git":
        return os.sep.join(parts[:-3])
    return None


def is_watchable_root(root: str) -> bool:
    """False for the home directory, its parents and drive roots: watching
    them recursively would flood the panel with AppData/system noise."""
    n = norm(root)
    home = norm(os.path.expanduser("~"))
    if n == home or home.startswith(n + os.sep):
        return False
    drive, tail = os.path.splitdrive(n)
    return bool(tail.strip(os.sep))


def is_hidden(root: str, patterns: Iterable[str]) -> bool:
    """True when ``root`` matches one of the user's hide patterns, by exact
    path or by directory basename (``"demo"`` hides a project named demo).

    Matching is deliberately NOT by path-prefix: hiding a project must never
    also hide the distinct projects nested under it — e.g. hiding a container
    like ``…\\01_works`` must not sweep away ``…\\01_works\\gg_ds`` and its
    siblings, which are their own projects the user can hide on their own."""
    rn = norm(root)
    base = os.path.basename(os.path.normpath(root)).lower()
    for p in patterns:
        p = (p or "").strip()
        if not p:
            continue
        if base == p.lower() or rn == norm(p):
            return True
    return False


def sibling_worktrees(root: str) -> List[str]:
    """``wt-*`` / ``.wt-*`` directories next to ``root`` (this repo's
    worktree naming convention)."""
    parent = os.path.dirname(root)
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    out = []
    for n in names:
        if WORKTREE_DIR_RE.match(n):
            p = os.path.join(parent, n)
            if os.path.isdir(p):
                out.append(p)
    return out


class RootIndex:
    """Maps any path to ``(project_root, worktree_label, relative_path)``.

    Project roots are live session cwds (plus roots remembered from the
    hook log). Worktrees are attached to their main repo when the ``.git``
    file says so, otherwise they stand as their own root.
    """

    def __init__(self) -> None:
        self._roots = {}  # type: Dict[str, str]   norm -> real
        self._wt = {}  # type: Dict[str, Tuple[str, str]]   norm(wt) -> (root, label)
        self._wt_real = {}  # type: Dict[str, str]   norm(wt) -> real path

    def roots(self) -> List[str]:
        return sorted(self._roots.values(), key=str.lower)

    def watch_dirs(self) -> List[str]:
        """Directories the filesystem watcher should cover (roots + worktrees
        outside of them). Roots too broad to watch (home, drive roots) are
        left to hook records only."""
        dirs = [r for r in self._roots.values() if is_watchable_root(r)]
        for wt_norm, (_root, _label) in self._wt.items():
            if not any(wt_norm.startswith(norm(r) + os.sep) for r in self._roots.values()):
                dirs.append(self._wt_real[wt_norm])
        # a recursive watch on the outer directory already covers nested ones
        outer = [d for d in dirs
                 if not any(norm(d).startswith(norm(o) + os.sep) for o in dirs if o != d)]
        return sorted(set(outer), key=str.lower)

    def add_root(self, root: str) -> None:
        if not root:
            return
        root = os.path.normpath(root)
        n = norm(root)
        # a session started *inside* a worktree: fold into the main repo
        main = worktree_main_root(root)
        if main:
            self.add_root(main)
            self._register_wt(root, main)
            return
        if n in self._roots:
            return
        # nested roots are kept: classify() picks the most specific one, so a
        # session started in the home directory never swallows the projects
        # below it into one group
        self._roots[n] = root
        for wt in sibling_worktrees(root):
            main = worktree_main_root(wt)
            if main and norm(main) == n:
                self._register_wt(wt, root)

    def _register_wt(self, wt: str, root: str) -> None:
        wt = os.path.normpath(wt)
        self._wt[norm(wt)] = (root, os.path.basename(wt))
        self._wt_real[norm(wt)] = wt

    def classify(self, path: str) -> Optional[Tuple[str, str, str]]:
        n = norm(path)
        best = None  # type: Optional[Tuple[int, str, str, str]]
        for wt_norm, (root, label) in self._wt.items():
            if n.startswith(wt_norm + os.sep):
                rel = path[len(wt_norm) + 1:]
                cand = (len(wt_norm), root, label, rel)
                if best is None or cand[0] > best[0]:
                    best = cand
        for root_norm, root in self._roots.items():
            if n.startswith(root_norm + os.sep):
                rel = path[len(root_norm) + 1:]
                cand = (len(root_norm), root, "", rel)
                if best is None or cand[0] > best[0]:
                    best = cand
        if best is None:
            return None
        rel = best[3]
        # inner worktrees created by subagents: <root>/.claude/worktrees/<name>/...
        parts = rel.split(os.sep)
        if len(parts) > 3 and parts[0].lower() == ".claude" and parts[1].lower() == "worktrees":
            return best[1], parts[2], os.sep.join(parts[3:])
        return best[1], best[2], rel


# ---------- log records ----------


def parse_log_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        j = json.loads(line)
    except ValueError:
        return None
    if not isinstance(j, dict) or "ts" not in j or "ev" not in j:
        return None
    return j


def read_log_tail(path: str, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Read complete lines appended after ``offset``. Returns (records, new_offset).
    A shrunken file (rotation/compaction) restarts from 0."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    records = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], offset
        data = data[: cut + 1]
    for raw in data.split(b"\n"):
        rec = parse_log_line(raw.decode("utf-8", "replace"))
        if rec:
            records.append(rec)
    return records, offset + len(data)


def compact_log(path: str, keep_seconds: float, now: Optional[float] = None,
                max_records: Optional[int] = None,
                keep_pred: Optional[Callable[[Dict[str, Any]], bool]] = None) -> int:
    """Rewrite ``path`` keeping only records that are recent enough (within
    ``keep_seconds``), pass ``keep_pred`` if given, and — after that — number at
    most ``max_records`` (newest kept). Bounds the file by BOTH age and count so
    a burst cannot grow it without limit. Returns the number of records kept."""
    now = now or time.time()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    kept = []
    for line in lines:
        rec = parse_log_line(line)
        if rec is None or float(rec.get("ts", 0)) < now - keep_seconds:
            continue
        if keep_pred is not None and not keep_pred(rec):
            continue
        kept.append(line if line.endswith("\n") else line + "\n")
    if max_records is not None and len(kept) > max_records:
        kept = kept[-max_records:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(kept)
    os.replace(tmp, path)
    return len(kept)


# ---------- model ----------


class Change:
    __slots__ = ("path", "ts", "count", "sid", "session", "root", "wt", "rel", "cls", "source")

    def __init__(self, path: str, ts: float, root: str, wt: str, rel: str, source: str) -> None:
        self.path = path
        self.ts = ts
        self.count = 1
        self.sid = None  # type: Optional[str]
        self.session = None  # type: Optional[str]
        self.root = root
        self.wt = wt
        self.rel = rel
        self.cls = ext_class(path)
        self.source = source


class ActivityModel:
    EDIT_MATCH_WINDOW = 5.0  # seconds between an Edit hook record and the fs event
    BASH_SLACK = 2.0
    BASH_OPEN_CAP = 180.0  # an unmatched bash_start is assumed to last at most this

    def __init__(self, prune: Optional[Iterable[str]] = None) -> None:
        self.prune = list(prune) if prune is not None else list(DEFAULT_PRUNE)
        self.roots = RootIndex()
        self.sessions = {}  # type: Dict[str, SessionInfo]
        self.changes = {}  # type: Dict[str, Change]   norm(path) -> latest
        self._edits = {}  # type: Dict[str, List[Tuple[float, str]]]   norm(path) -> [(ts, sid)]
        self._bash = {}  # type: Dict[str, List[List[float]]]   sid -> [[start, end|None], ...]
        # sids whose cwd relates to a project root, cached per root_n; cleared
        # whenever an anchor changes (see _anchors_changed)
        self._root_sids = {}  # type: Dict[str, List[str]]
        self._anchor_sig = None  # type: Optional[frozenset]
        self._longest_bash = 0.0  # longest closed Bash span seen (bounds the walk-back)
        self._names = {}  # type: Dict[str, str]   sid -> name seen in hook log
        self._cwds = {}  # type: Dict[str, str]   sid -> anchor cwd (the open dir)
        self._authoritative = set()  # type: set   sids whose cwd came from sessions/*.json

    # -- sessions --

    def _note_cwd(self, sid: str, cwd: str) -> None:
        """Record a session's open directory. sessions/*.json is authoritative;
        for sessions seen only in the hook log, keep the *shallowest* cwd — you
        cd down into subdirectories, so the shallowest is the open dir."""
        if not sid or not cwd:
            return
        cwd = os.path.normpath(cwd)
        if sid in self._authoritative:
            return
        prev = self._cwds.get(sid)
        if prev is None or cwd.count(os.sep) < os.path.normpath(prev).count(os.sep):
            self._cwds[sid] = cwd
            self._root_sids.clear()

    def rebuild_roots(self) -> None:
        """Roots are exactly the session anchors — never a drifted tool cwd or a
        directory pulled from the fs log. Rebuilt from scratch so a corrected
        anchor drops the stale one."""
        self.roots = RootIndex()
        for cwd in self._cwds.values():
            self.roots.add_root(cwd)
        self._root_sids.clear()

    def _anchors_changed(self) -> bool:
        """True once per change of the anchor set (sid → cwd), so callers can
        skip re-deriving every change's project when nothing moved."""
        sig = frozenset(self._cwds.items())
        if sig == self._anchor_sig:
            return False
        self._anchor_sig = sig
        return True

    def set_sessions(self, live: Dict[str, SessionInfo]) -> None:
        for s in self.sessions.values():
            s.live = False
        for sid, s in live.items():
            s.live = True
            self.sessions[sid] = s
            self._names[sid] = s.name
            self._cwds[sid] = os.path.normpath(s.cwd)
            self._authoritative.add(sid)
        self.rebuild_roots()

    def session_label(self, sid: Optional[str]) -> str:
        if not sid:
            return "—"
        s = self.sessions.get(sid)
        if s:
            return s.name
        return self._names.get(sid) or fallback_label(sid, self._cwds.get(sid, ""))

    def session_status(self, sid: Optional[str]) -> str:
        """'busy' / 'idle' / 'ended' / '' (unattributed)."""
        if not sid:
            return ""
        s = self.sessions.get(sid)
        if s and s.live:
            return s.status or "idle"
        return "ended"

    def session_root(self, sid: Optional[str]) -> Optional[str]:
        """The project root a session belongs to: its open cwd, folded to the
        registered root (worktree → main repo)."""
        cwd = self._cwds.get(sid or "")
        if not cwd:
            return None
        where = self.roots.classify(os.path.join(cwd, "\x00"))
        return where[0] if where else os.path.normpath(cwd)

    # -- ingest --

    def ingest_hook(self, rec: Dict[str, Any]) -> None:
        ev = rec.get("ev")
        sid = rec.get("sid") or ""
        ts = float(rec.get("ts", 0))
        if rec.get("name") and sid:
            self._names[sid] = rec["name"]
        if rec.get("cwd") and sid:
            self._note_cwd(sid, rec["cwd"])
        if ev in ("bash_start", "bash_end") and sid not in self._bash:
            self._root_sids.clear()  # a new session joins the per-root candidates
        if ev == "edit" and rec.get("path"):
            path = rec["path"]
            self._edits.setdefault(norm(path), []).append((ts, sid))
            self._record(path, ts, "edit", sid)
        elif ev == "bash_start":
            self._bash.setdefault(sid, []).append([ts, None])
        elif ev == "bash_end":
            spans = self._bash.setdefault(sid, [])
            for span in reversed(spans):
                if span[1] is None:
                    span[1] = ts
                    self._longest_bash = max(self._longest_bash, ts - span[0])
                    break
            else:
                spans.append([ts - 1.0, ts])

    def ingest_fs(self, path: str, ts: float, action: str = "modified") -> bool:
        """Ingest a filesystem change. Returns True when it changed the model
        (a tracked file was added/updated/removed) so the caller can decide
        whether to persist and re-render."""
        if action == "removed":
            return self.changes.pop(norm(path), None) is not None
        return self._record(path, ts, "fs", None)

    def _resolve_root(self, path: str, sid: Optional[str]) -> Optional[Tuple[str, str, str]]:
        """(root, worktree_label, rel) for a change: the most specific project
        root containing it, else the attributing session's anchor (so a file a
        session wrote outside its own tree still lands under that session)."""
        where = self.roots.classify(path)
        if where is not None:
            return where
        anchor = self.session_root(sid) if sid else None
        if not anchor:
            return None
        if norm(path).startswith(norm(anchor) + os.sep):
            return anchor, "", os.path.relpath(path, anchor)
        return anchor, "", os.path.basename(path)

    def _record(self, path: str, ts: float, source: str, sid: Optional[str]) -> bool:
        name = os.path.basename(path)
        if is_temp_name(name):
            return False
        where = self._resolve_root(path, sid)
        if where is None:
            return False
        root, wt, rel = where
        if is_pruned(rel.split(os.sep)[:-1], self.prune):
            return False
        key = norm(path)
        cur = self.changes.get(key)
        if cur is None:
            cur = Change(path, ts, root, wt, rel, source)
            self.changes[key] = cur
        else:
            if ts >= cur.ts - 1.0:
                cur.ts = max(cur.ts, ts)
                cur.count += 1
                cur.source = source if source == "edit" else cur.source
        if sid:
            cur.sid = sid
        elif cur.sid is None:
            cur.sid = self._attribute(key, root, ts)
        return True

    # -- attribution --

    def _sids_for_root(self, root_n: str) -> List[str]:
        """Sessions whose anchor is this root, inside it, or above it."""
        got = self._root_sids.get(root_n)
        if got is None:
            got = []
            for sid in self._bash:
                cwd = self._cwds.get(sid) or ""
                cwd_n = norm(cwd) if cwd else ""
                if cwd_n and (cwd_n == root_n or cwd_n.startswith(root_n + os.sep)
                              or root_n.startswith(cwd_n + os.sep)):
                    got.append(sid)
            self._root_sids[root_n] = got
        return got

    def _attribute(self, key: str, root: str, ts: float) -> Optional[str]:
        # 1. an Edit/Write hook record for the same file around the same time
        best = None  # type: Optional[Tuple[float, str]]
        for ets, sid in self._edits.get(key, ()):
            d = abs(ets - ts)
            if d <= self.EDIT_MATCH_WINDOW and (best is None or d < best[0]):
                best = (d, sid)
        if best:
            return best[1]
        # 2. a Bash call of a session working in this project, running at ts.
        # Spans are appended in start order, so walk back from the newest and
        # stop once no span (open ones last at most BASH_OPEN_CAP, closed ones
        # at most the longest seen) can still reach ts.
        root_n = norm(root)
        horizon = ts - self.BASH_SLACK - max(self.BASH_OPEN_CAP, self._longest_bash)
        hits = []
        for sid in self._sids_for_root(root_n):
            for start, end in reversed(self._bash.get(sid, ())):
                if start < horizon:
                    break
                stop = end if end is not None else start + self.BASH_OPEN_CAP
                if start - self.BASH_SLACK <= ts <= stop + self.BASH_SLACK:
                    hits.append(sid)
                    break
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # prefer the one whose cwd is the exact project root; else give up
            exact = [s for s in hits if norm(self._cwds.get(s, "")) == root_n]
            if len(exact) == 1:
                return exact[0]
        return None

    # Hook records arrive within seconds of the tool call, so an fs change
    # still unattributed after this long will never find its session: retry
    # only the recent ones instead of rescanning every change each cycle.
    RETRY_WINDOW = 600.0

    def reattribute(self, now: Optional[float] = None) -> None:
        """Retry attribution for recent unattributed changes, and — only when
        an anchor moved (the shallowest cwd was learned, a session appeared) —
        rebuild roots and re-derive every change's project."""
        now = now or time.time()
        cutoff = now - self.RETRY_WINDOW
        for key, ch in self.changes.items():
            if ch.sid is None and ch.ts >= cutoff:
                ch.sid = self._attribute(key, ch.root, ch.ts)
        if not self._anchors_changed():
            return
        self.rebuild_roots()
        stale = []
        for key, ch in self.changes.items():
            where = self._resolve_root(ch.path, ch.sid)
            if where is None:
                stale.append(key)
                continue
            ch.root, ch.wt, ch.rel = where
        for key in stale:
            del self.changes[key]

    def trim(self, keep_seconds: float, now: Optional[float] = None) -> None:
        now = now or time.time()
        cutoff = now - keep_seconds
        self.changes = {k: c for k, c in self.changes.items() if c.ts >= cutoff}
        self._edits = {k: kept for k, kept in
                       ((k, [e for e in es if e[0] >= cutoff]) for k, es in self._edits.items())
                       if kept}
        for sid in list(self._bash):
            self._bash[sid] = [s for s in self._bash[sid] if (s[1] or s[0]) >= cutoff]
            if not self._bash[sid]:
                del self._bash[sid]
        # bound the per-session maps: keep only live sessions and those a
        # surviving change still refers to, so months of ended sessions don't
        # accumulate forever.
        keep = {s.sid for s in self.sessions.values() if s.live}
        keep |= {c.sid for c in self.changes.values() if c.sid}
        for sid in list(self._cwds):
            if sid not in keep:
                self._cwds.pop(sid, None)
                self._names.pop(sid, None)
                self._authoritative.discard(sid)
                self.sessions.pop(sid, None)
                self._root_sids.clear()

    # -- tree --

    def tree(self, window_seconds: float, show_code: bool = False,
             now: Optional[float] = None,
             within: Optional[Iterable[str]] = None,
             hidden: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """``[{root, label, latest, count, live, sessions: [{sid, label, status,
        latest, files: [Change]}]}]`` newest first at every level.
        ``within``: only files under one of these directories (a window's
        folders, for example); None = everything.
        ``hidden``: project roots the user has hidden (see ``is_hidden``)."""
        now = now or time.time()
        cutoff = now - window_seconds
        scope = [norm(d) + os.sep for d in within] if within is not None else None
        hide = list(hidden) if hidden else []
        hide_cache = {}  # type: Dict[str, bool]
        by_root = {}  # type: Dict[str, Dict[str, Any]]
        for ch in self.changes.values():
            if ch.ts < cutoff:
                continue
            if ch.cls == "code" and not show_code:
                continue
            if scope is not None and not any(norm(ch.path).startswith(d) for d in scope):
                continue
            if hide:
                h = hide_cache.get(ch.root)
                if h is None:
                    h = is_hidden(ch.root, hide)
                    hide_cache[ch.root] = h
                if h:
                    continue
            node = by_root.get(ch.root)
            if node is None:
                node = {"root": ch.root, "label": os.path.basename(ch.root) or ch.root,
                        "latest": 0.0, "count": 0, "live": 0, "sessions": {}}
                by_root[ch.root] = node
            node["count"] += 1
            node["latest"] = max(node["latest"], ch.ts)
            skey = ch.sid or ""
            snode = node["sessions"].get(skey)
            if snode is None:
                snode = {"sid": ch.sid, "label": self.session_label(ch.sid),
                         "status": self.session_status(ch.sid), "latest": 0.0, "files": []}
                node["sessions"][skey] = snode
            snode["latest"] = max(snode["latest"], ch.ts)
            snode["files"].append(ch)
        out = []
        for node in by_root.values():
            sessions = list(node["sessions"].values())
            for s in sessions:
                s["files"].sort(key=lambda c: c.ts, reverse=True)
            sessions.sort(key=lambda s: s["latest"], reverse=True)
            node["sessions"] = sessions
            node["live"] = sum(1 for s in self.sessions.values()
                               if s.live and norm(self.session_root(s.sid) or s.cwd)
                               == norm(node["root"]))
            out.append(node)
        out.sort(key=lambda n: n["latest"], reverse=True)
        return out


# ---------- rendering ----------

STATUS_GLYPH = {"busy": "●", "idle": "○", "ended": "◌", "": "·"}
WIDE_GLYPHS = set("▼▶●○◌·⟨⟩…×Δ")


def fmt_time(ts: float, now: Optional[float] = None) -> str:
    now = now or time.time()
    lt = time.localtime(ts)
    if time.localtime(now)[:3] == lt[:3]:
        return time.strftime("%H:%M", lt)
    return time.strftime("%m/%d %H:%M", lt)


def dwidth(text: str) -> int:
    """Display width in cells: CJK / full-width = 2, symbol glyphs that come
    from a fallback font (▼ ● ⟨ …) are counted as 2 to stay conservative."""
    w = 0
    for ch in text:
        if ch in WIDE_GLYPHS:
            w += 2
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def _cut(text: str, width: int) -> str:
    """Prefix of ``text`` that fits in ``width`` cells, with a trailing …"""
    out = []
    used = 0
    for ch in text:
        cw = dwidth(ch)
        if used + cw > width - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def elide_rel(rel: str, width: int) -> str:
    """Keep the file name whole and as many *parent* directories as fit
    (nearest first), dropping the head: ``…/ui-design/SKILL.md``."""
    rel = rel.replace(os.sep, "/")
    if dwidth(rel) <= width:
        return rel
    parts = rel.split("/")
    name = parts[-1]
    if len(parts) == 1 or dwidth(name) + 3 >= width:
        return name if dwidth(name) <= width else _cut(name, width)
    tail = []  # type: List[str]
    budget = width - dwidth(name) - 2  # "…/"
    for d in reversed(parts[:-1]):
        if dwidth(d) + 1 <= budget:
            tail.insert(0, d)
            budget -= dwidth(d) + 1
        else:
            break
    return "/".join(["…"] + tail + [name])


def plan_lines(tree: List[Dict[str, Any]], collapsed: Iterable[str], focus: Iterable[str],
               max_files: int, max_lines: Optional[int],
               header: bool = True) -> Tuple[Dict[Tuple[str, str], int], Set[str]]:
    """How many files each session shows so the whole tree fits ``max_lines``
    rows — the cross-project overview never scrolls away.

    * every project keeps its title row;
    * focused projects are filled up to ``max_files`` first;
    * the rest get their newest file one at a time, round-robin in display
      order (newest project, newest session first), so each session's latest
      activity shows before any session's second file;
    * when even the project and session rows do not fit, the oldest unfocused
      projects are auto-collapsed (a click focuses them).

    Line costs: collapsed project 1; open project 2 (title + blank) plus, per
    session, its label, its newest file and a "+N more" row when it has more;
    granting a session one more file costs 1, or 0 when that completes the
    session (the "+N more" row becomes the file row). ``max_lines`` None =
    unlimited.
    Returns ``({(norm(root), sid_key): files}, {norm(root) auto-collapsed})``.
    """
    collapsed_n = {norm(c) for c in collapsed}
    focus_n = {norm(f) for f in focus}
    budget = (max_lines if max_lines is not None else 10 ** 9) - (2 if header else 0)
    open_nodes = [n for n in tree if norm(n["root"]) not in collapsed_n]

    def cost(node: Dict[str, Any]) -> int:
        return 2 + sum(3 if len(s["files"]) > 1 else 2 for s in node["sessions"])

    fixed = (len(tree) - len(open_nodes)) + sum(cost(n) for n in open_nodes)
    auto = set()  # type: Set[str]
    for node in reversed(open_nodes):  # oldest first
        if fixed <= budget:
            break
        root_n = norm(node["root"])
        if root_n in focus_n:
            continue
        auto.add(root_n)
        fixed -= cost(node) - 1
    sessions = []  # type: List[Tuple[Tuple[str, str], int, bool]]
    for node in open_nodes:
        root_n = norm(node["root"])
        if root_n in auto:
            continue
        for s in node["sessions"]:
            sessions.append(((root_n, s["sid"] or ""), len(s["files"]), root_n in focus_n))
    quota = {k: 1 for k, _n, _f in sessions}  # the newest file is part of the base cost
    used = fixed

    def grant(key: Tuple[str, str], n: int) -> Optional[int]:
        q = quota[key]
        if q >= min(n, max_files):
            return None
        return 0 if q + 1 == n else 1

    for key, n, focused in sessions:
        while focused:
            c = grant(key, n)
            if c is None or used + c > budget:
                break
            quota[key] += 1
            used += c
    progress = True
    while progress:
        progress = False
        for key, n, focused in sessions:
            if focused:
                continue
            c = grant(key, n)
            if c is None or used + c > budget:
                continue
            quota[key] += 1
            used += c
            progress = True
    return quota, auto


def render(tree: List[Dict[str, Any]], collapsed: Iterable[str], last_seen: float,
           width: int = 46, max_files: int = 20, now: Optional[float] = None,
           header: str = "", max_lines: Optional[int] = None,
           focus: Iterable[str] = ()) -> Tuple[str, Dict[int, Tuple[str, str]]]:
    """Render the tree to text. Returns ``(text, {line_no: (kind, target)})``
    where kind is ``'file'`` (target=absolute path), ``'pj'`` (target=root;
    click toggles collapse / drops focus) or ``'pj-auto'`` (a project folded to
    fit the view; click focuses it). Arrows: ▼ open, ◆ focused, ▶ collapsed
    by hand, ▷ folded automatically."""
    now = now or time.time()
    collapsed_n = {norm(c) for c in collapsed}
    focus_n = {norm(f) for f in focus}
    quota, auto = plan_lines(tree, collapsed, focus, max_files, max_lines, header=bool(header))
    lines = []  # type: List[str]
    targets = {}  # type: Dict[int, Tuple[str, str]]
    if header:
        lines.append(header)
        lines.append("")
    if not tree:
        lines.append("  (no recent changes)")
    for node in tree:
        root_n = norm(node["root"])
        is_collapsed = root_n in collapsed_n
        is_auto = root_n in auto
        arrow = "▶" if is_collapsed else "▷" if is_auto else "◆" if root_n in focus_n else "▼"
        live = " ●{}".format(node["live"]) if node["live"] else ""
        title = "{} {}".format(arrow, node["label"])
        meta = "{}{}".format(node["count"], live)
        targets[len(lines)] = ("pj-auto" if is_auto else "pj", node["root"])
        lines.append(title + "  " + meta)
        if is_collapsed or is_auto:
            continue
        for s in node["sessions"]:
            glyph = STATUS_GLYPH.get(s["status"], "·")
            lines.append("  {} {}".format(glyph, s["label"]))
            files = s["files"]
            shown = quota.get((root_n, s["sid"] or ""), max_files)
            for ch in files[:shown]:
                mark = "*" if ch.ts > last_seen else " "
                wt = f"  ⟨{ch.wt}⟩" if ch.wt else ""
                mult = f"  ×{ch.count}" if ch.count > 1 else ""
                rel = elide_rel(ch.rel, width - 10 - dwidth(wt) - dwidth(mult))
                targets[len(lines)] = ("file", ch.path)
                lines.append(f"  {mark} {fmt_time(ch.ts, now)} {rel}{wt}{mult}")
            if len(files) > shown:
                lines.append(f"      … +{len(files) - shown} more")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n", targets


def summary(tree: List[Dict[str, Any]], live_sessions: int) -> str:
    files = sum(n["count"] for n in tree)
    return f"Δ{files} · {len(tree)}PJ · {live_sessions} live"
