"""What a worker asked at an approval gate, as plain text (#1175).

A task in Awaiting approval carries its question in a comment. The cockpit's Now queue shows it so
the operator can answer without opening the ticket: a short question and a few short options are
shown whole; anything longer or more structured is flagged `complex` and the operator is sent to
the ticket instead. No model is involved: this is a line parser, so it is cheap and predictable.
"""
import re

MAX_STEM = 400          # characters of question shown
MAX_OPTION = 200        # characters per option
MAX_OPTIONS = 5
MAX_PLAIN = 600         # a comment with no options, shown whole up to this
# "1." "1)" "Option 1:" "**Option A**:" "- B)" "- (2)": a key, a delimiter, then the text.
_OPT = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(?:option\s+|choice\s+)?\(?([0-9]{1,2}|[A-Ha-h])\)?(?:\*\*|__)?"
    r"\s*(?:[.):–—-]|\*\*:?)\s*(.+?)\s*$", re.I)
_MD = re.compile(r"(\*\*|__|`|~~)")


def _clean(s):
    s = _MD.sub("", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)      # [text](url) -> text
    return re.sub(r"\s+", " ", s).strip()


def parse_question(raw):
    """-> {"stem": str, "options": [{"key","text"}], "complex": bool}"""
    raw = (raw or "").replace("\r", "")
    lines = raw.split("\n")
    first = next((i for i, ln in enumerate(lines) if _OPT.match(ln)), None)
    opts = []
    if first is not None:
        for ln in lines[first:]:
            m = _OPT.match(ln)
            if m:
                opts.append({"key": m.group(1).upper(), "text": _clean(m.group(2))})
            elif ln.strip() and opts:
                # a wrapped line belongs to the option above it
                opts[-1]["text"] = (opts[-1]["text"] + " " + _clean(ln)).strip()
        stem = _clean(" ".join(lines[:first]))
    else:
        stem = _clean(raw)
    structured = bool(re.search(r"```|^\s*\|.*\|\s*$|^#{1,6}\s", raw, re.M))
    if len(opts) < 2:
        # not a real choice: the whole comment is the question
        whole = _clean(raw)
        plain_ok = len(whole) <= MAX_PLAIN and not structured
        return {"stem": whole[:MAX_STEM] + ("…" if len(whole) > MAX_STEM else ""),
                "options": [], "complex": not plain_ok}
    cx = (structured or len(opts) > MAX_OPTIONS or len(stem) > MAX_STEM
          or any(len(o["text"]) > MAX_OPTION for o in opts))
    return {"stem": stem[:MAX_STEM], "options": opts[:MAX_OPTIONS], "complex": cx}
