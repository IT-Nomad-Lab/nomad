"""Files a user drops into the chat (#1333, docs/prd/owui-inline-files.md).

The pipe runs in a container and sends the bytes as base64; the bridge writes them to a per-chat
directory on the host and tells the agent where they are. Names are sanitised, sizes capped and the
directory is private (0700); nothing is executed or opened here.
"""
import base64
import os
import re
import time

ROOT = os.environ.get("AGENT_BRIDGE_INBOX") or os.path.expanduser("~/.local/state/nomad-agent-bridge/inbox")
MAX_BYTES = int(os.environ.get("AGENT_BRIDGE_INBOX_MAX_BYTES") or 20 * 1024 * 1024)
MAX_FILES = 10
KEEP_DAYS = int(os.environ.get("AGENT_BRIDGE_INBOX_DAYS") or 30)


def _name(raw: str, taken: set) -> str:
    base = os.path.basename(str(raw or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")[:100] or "file"
    stem, ext = os.path.splitext(base)
    n, out = 1, base
    while out in taken:
        n += 1
        out = f"{stem}-{n}{ext}"
    taken.add(out)
    return out


def save(chat_id: str, items) -> tuple[list, list]:
    """Write attachments `[{name, b64}]`. Returns (saved paths, skipped notes)."""
    saved, skipped = [], []
    if not isinstance(items, list) or not items:
        return saved, skipped
    safe_chat = re.sub(r"[^A-Za-z0-9_-]", "_", str(chat_id))[:80] or "chat"
    d = os.path.join(ROOT, safe_chat)
    os.makedirs(d, mode=0o700, exist_ok=True)
    taken = set(os.listdir(d))
    for it in items[:MAX_FILES]:
        label = str((it or {}).get("name") or "file") if isinstance(it, dict) else "file"
        try:
            data = base64.b64decode((it or {}).get("b64") or "", validate=True)
        except Exception:
            skipped.append(f"{label}: not readable")
            continue
        if not data:
            skipped.append(f"{label}: empty")
        elif len(data) > MAX_BYTES:
            skipped.append(f"{label}: over {MAX_BYTES // (1024 * 1024)} MB")
        else:
            fid = re.sub(r"[^A-Za-z0-9]", "", str(it.get("id") or ""))[:8]
            if fid:                       # the chat re-sends its files every turn: one copy per file id
                path = os.path.join(d, f"{fid}-{_name(label, set())}")
                if os.path.isfile(path) and os.path.getsize(path) == len(data):
                    saved.append(path)
                    continue
            else:
                path = os.path.join(d, _name(label, taken))
            with open(path, "wb") as f:
                f.write(data)
            saved.append(path)
    if len(items) > MAX_FILES:
        skipped.append(f"{len(items) - MAX_FILES} more files over the limit of {MAX_FILES}")
    return saved, skipped


def note(saved: list, skipped: list) -> str:
    """The text appended to the user's message."""
    out = ""
    if saved:
        out += "\n\nThe user attached these files (read them from disk):\n" + "\n".join(f"- {p}" for p in saved)
    if skipped:
        out += "\n\nNot attached: " + "; ".join(skipped)
    return out


def sweep(now: float = None) -> int:
    """Delete inbox files older than KEEP_DAYS. Returns how many were removed."""
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    gone = 0
    for dp, _, names in os.walk(ROOT):
        for n in names:
            p = os.path.join(dp, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    gone += 1
            except OSError:
                pass
    return gone
