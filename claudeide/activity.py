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
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
            sid, j.get("name") or sid[:8], cwd, j.get("status", ""),
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
        outside of them)."""
        dirs = list(self._roots.values())
        for wt_norm, (_root, _label) in self._wt.items():
            if not any(wt_norm.startswith(norm(r) + os.sep) for r in self._roots.values()):
                dirs.append(self._wt_real[wt_norm])
        return sorted(set(dirs), key=str.lower)

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
        # do not add a root nested inside an existing one (keep the outer)
        for existing in list(self._roots):
            if n.startswith(existing + os.sep):
                return
            if existing.startswith(n + os.sep):
                del self._roots[existing]
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


def compact_log(path: str, keep_seconds: float, now: Optional[float] = None) -> int:
    """Drop records older than ``keep_seconds``; returns the number kept."""
    now = now or time.time()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    kept = []
    for line in lines:
        rec = parse_log_line(line)
        if rec and float(rec.get("ts", 0)) >= now - keep_seconds:
            kept.append(line if line.endswith("\n") else line + "\n")
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
        self._edits = []  # type: List[Tuple[float, str, str]]   (ts, sid, norm(path))
        self._bash = {}  # type: Dict[str, List[List[float]]]   sid -> [[start, end|None], ...]
        self._names = {}  # type: Dict[str, str]   sid -> name seen in hook log
        self._cwds = {}  # type: Dict[str, str]   sid -> cwd seen in hook log

    # -- sessions --

    def set_sessions(self, live: Dict[str, SessionInfo]) -> None:
        for s in self.sessions.values():
            s.live = False
        for sid, s in live.items():
            s.live = True
            self.sessions[sid] = s
            self._names[sid] = s.name
            self._cwds[sid] = s.cwd
            self.roots.add_root(s.cwd)

    def session_label(self, sid: Optional[str]) -> str:
        if not sid:
            return "—"
        s = self.sessions.get(sid)
        if s:
            return s.name
        return self._names.get(sid) or sid[:8]

    def session_status(self, sid: Optional[str]) -> str:
        """'busy' / 'idle' / 'ended' / '' (unattributed)."""
        if not sid:
            return ""
        s = self.sessions.get(sid)
        if s and s.live:
            return s.status or "idle"
        return "ended"

    # -- ingest --

    def ingest_hook(self, rec: Dict[str, Any]) -> None:
        ev = rec.get("ev")
        sid = rec.get("sid") or ""
        ts = float(rec.get("ts", 0))
        if rec.get("name") and sid:
            self._names[sid] = rec["name"]
        if rec.get("cwd") and sid:
            self._cwds[sid] = rec["cwd"]
            self.roots.add_root(rec["cwd"])
        if ev == "edit" and rec.get("path"):
            path = rec["path"]
            self._edits.append((ts, sid, norm(path)))
            self._record(path, ts, "edit", sid)
        elif ev == "bash_start":
            self._bash.setdefault(sid, []).append([ts, None])
        elif ev == "bash_end":
            spans = self._bash.setdefault(sid, [])
            for span in reversed(spans):
                if span[1] is None:
                    span[1] = ts
                    break
            else:
                spans.append([ts - 1.0, ts])

    def ingest_fs(self, path: str, ts: float, action: str = "modified") -> None:
        if action == "removed":
            self.changes.pop(norm(path), None)
            return
        self._record(path, ts, "fs", None)

    def _record(self, path: str, ts: float, source: str, sid: Optional[str]) -> None:
        name = os.path.basename(path)
        if is_temp_name(name):
            return
        where = self.roots.classify(path)
        if where is None:
            return
        root, wt, rel = where
        if is_pruned(rel.split(os.sep)[:-1], self.prune):
            return
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

    # -- attribution --

    def _attribute(self, key: str, root: str, ts: float) -> Optional[str]:
        # 1. an Edit/Write hook record for the same file around the same time
        best = None  # type: Optional[Tuple[float, str]]
        for ets, sid, epath in self._edits:
            if epath == key:
                d = abs(ets - ts)
                if d <= self.EDIT_MATCH_WINDOW and (best is None or d < best[0]):
                    best = (d, sid)
        if best:
            return best[1]
        # 2. a Bash call of a session working in this project, running at ts
        root_n = norm(root)
        hits = []
        for sid, spans in self._bash.items():
            cwd = self._cwds.get(sid) or ""
            cwd_n = norm(cwd) if cwd else ""
            if not cwd_n or not (cwd_n == root_n or cwd_n.startswith(root_n + os.sep)
                                 or root_n.startswith(cwd_n + os.sep)):
                continue
            for start, end in spans:
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

    def reattribute(self) -> None:
        """Late hook records may arrive after fs events; retry unattributed."""
        for ch in self.changes.values():
            if ch.sid is None:
                ch.sid = self._attribute(norm(ch.path), ch.root, ch.ts)

    def trim(self, keep_seconds: float, now: Optional[float] = None) -> None:
        now = now or time.time()
        cutoff = now - keep_seconds
        self.changes = {k: c for k, c in self.changes.items() if c.ts >= cutoff}
        self._edits = [e for e in self._edits if e[0] >= cutoff]
        for sid in list(self._bash):
            self._bash[sid] = [s for s in self._bash[sid] if (s[1] or s[0]) >= cutoff]
            if not self._bash[sid]:
                del self._bash[sid]

    # -- tree --

    def tree(self, window_seconds: float, show_code: bool = False,
             now: Optional[float] = None) -> List[Dict[str, Any]]:
        """``[{root, label, latest, count, live, sessions: [{sid, label, status,
        latest, files: [Change]}]}]`` newest first at every level."""
        now = now or time.time()
        cutoff = now - window_seconds
        by_root = {}  # type: Dict[str, Dict[str, Any]]
        for ch in self.changes.values():
            if ch.ts < cutoff:
                continue
            if ch.cls == "code" and not show_code:
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
                               if s.live and norm(s.cwd) == norm(node["root"]))
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


def render(tree: List[Dict[str, Any]], collapsed: Iterable[str], last_seen: float,
           width: int = 46, max_files: int = 20, now: Optional[float] = None,
           header: str = "") -> Tuple[str, Dict[int, Tuple[str, str]]]:
    """Render the tree to text. Returns ``(text, {line_no: (kind, target)})``
    where kind is ``'file'`` (target=absolute path) or ``'pj'`` (target=root)."""
    now = now or time.time()
    collapsed_n = {norm(c) for c in collapsed}
    lines = []  # type: List[str]
    targets = {}  # type: Dict[int, Tuple[str, str]]
    if header:
        lines.append(header)
        lines.append("")
    if not tree:
        lines.append("  (no recent changes)")
    for node in tree:
        is_collapsed = norm(node["root"]) in collapsed_n
        arrow = "▶" if is_collapsed else "▼"
        live = " ●{}".format(node["live"]) if node["live"] else ""
        title = "{} {}".format(arrow, node["label"])
        meta = "{}{}".format(node["count"], live)
        targets[len(lines)] = ("pj", node["root"])
        lines.append(title + "  " + meta)
        if is_collapsed:
            continue
        for s in node["sessions"]:
            glyph = STATUS_GLYPH.get(s["status"], "·")
            lines.append("  {} {}".format(glyph, s["label"]))
            files = s["files"]
            for ch in files[:max_files]:
                mark = "*" if ch.ts > last_seen else " "
                wt = f"  ⟨{ch.wt}⟩" if ch.wt else ""
                mult = f"  ×{ch.count}" if ch.count > 1 else ""
                rel = elide_rel(ch.rel, width - 10 - dwidth(wt) - dwidth(mult))
                targets[len(lines)] = ("file", ch.path)
                lines.append(f"  {mark} {fmt_time(ch.ts, now)} {rel}{wt}{mult}")
            if len(files) > max_files:
                lines.append(f"      … +{len(files) - max_files} more")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n", targets


def summary(tree: List[Dict[str, Any]], live_sessions: int) -> str:
    files = sum(n["count"] for n in tree)
    return f"Δ{files} · {len(tree)}PJ · {live_sessions} live"
