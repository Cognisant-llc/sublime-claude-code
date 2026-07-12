"""Path <-> file:// URI conversion, deterministic across host OSes.

The conversion inspects the path shape (Windows drive letter vs POSIX)
instead of relying on os-specific helpers like nturl2path, so the same
code and tests behave identically on Windows, macOS and Linux CI.
"""

import re
from urllib.parse import quote, unquote, urlparse

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_WIN_DRIVE = re.compile(r"^/([A-Za-z]):(/|$)")


def path_to_uri(path: str) -> str:
    if _WIN_DRIVE.match(path):
        drive = path[0].upper()
        rest = path[2:].replace("\\", "/")
        return "file:///" + quote(drive + ":" + rest, safe="/:")
    return "file://" + quote(path, safe="/")


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"not a file URI: {uri}")
    p = unquote(parsed.path)
    m = _URI_WIN_DRIVE.match(p)
    if m:
        drive = m.group(1).upper()
        return drive + ":" + p[len("/X:"):].replace("/", "\\")
    return p
