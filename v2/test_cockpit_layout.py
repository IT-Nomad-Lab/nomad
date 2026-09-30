"""V2c · the cockpit shell: panels that start hidden actually start hidden.

This exists because of a bug that made every other cockpit test worthless without any of them
failing. The terminal workspace is `<div id=ws class=ws-hidden>`, and the stylesheet said
`.ws-hidden{display:none}` next to `#ws{...display:flex}`. An id beats a class, so the hide never
applied: the panel covered the entire cockpit from first paint, opaque, at z-index 60. Every feature
underneath it rendered correctly and nobody could see any of them, and CLOSE did nothing either,
because all CLOSE does is add the class that was already losing.

The voice-surface tests assert the page *contains* each control, and all of them passed while the
page showed one orange bar on black. Containing a control and the operator being able to reach it
are different claims, and the tests were only making the first one.

So this checks specificity rather than presence. It is still not a browser, but the browser's
cascade is a rule, not a rendering detail, and a rule can be checked here.

Run: python3 -m pytest v2/test_cockpit_layout.py -q   (engine deps live in the container)
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

PAGE = server.COCKPIT

# `<div id=ws class=ws-hidden>` and friends: an element whose markup ships a hiding class on it.
HIDDEN_AT_REST = re.compile(r"<\w+\s+id=([A-Za-z][\w-]*)\s+class=([\w-]*hidden[\w-]*)[\s>]")
# A CSS rule, loose on purpose: the sheet is hand-written and one line per rule.
RULE = re.compile(r"([^{}\n/]+)\{([^{}]*)\}")


def specificity(selector):
    """(ids, classes) — enough for this sheet, which has no inline styles or !important."""
    return (selector.count("#"), selector.count("."))


def rules_setting_display(element_id, hide_class):
    """Every rule in the page's stylesheet that could set `display` on this one element.

    Matching is deliberately narrow: the bare id, the bare class, and the two combined. A sheet that
    grows descendant selectors for these panels will need this widened, and the test failing at that
    point is the correct outcome rather than a nuisance."""
    wanted = {f"#{element_id}", f".{hide_class}",
              f"#{element_id}.{hide_class}", f".{hide_class}#{element_id}"}
    out = []
    for sel, body in RULE.findall(PAGE):
        for one in sel.split(","):
            one = one.strip()
            if one in wanted and "display:" in body:
                value = re.search(r"display:\s*([\w-]+)", body).group(1)
                out.append((one, value, specificity(one)))
    return out


def test_the_page_has_a_panel_that_starts_hidden():
    """If this stops matching, the parametrised test below silently checks nothing."""
    assert HIDDEN_AT_REST.findall(PAGE), "no id'd element ships with a hiding class"


@pytest.mark.parametrize("element_id,hide_class", HIDDEN_AT_REST.findall(PAGE))
def test_a_panel_that_ships_hidden_is_actually_hidden(element_id, hide_class):
    """The hiding rule must outrank every rule that would show the same element.

    `.ws-hidden{display:none}` losing to `#ws{display:flex}` is not a subtle cascade question. It is
    the difference between a cockpit and a black rectangle."""
    found = rules_setting_display(element_id, hide_class)
    hides = [r for r in found if r[1] == "none"]
    shows = [r for r in found if r[1] != "none"]
    assert hides, f"#{element_id} ships with .{hide_class} and nothing sets display:none"
    best_hide = max(s for _, _, s in hides)
    for sel, value, spec in shows:
        assert best_hide >= spec, (
            f"`{sel}` sets display:{value} at specificity {spec} and the strongest rule hiding "
            f"#{element_id} is only {best_hide}, so the panel is visible at rest")


def test_the_workspace_overlay_is_the_one_this_was_written_for():
    """Named rather than generic, because the generic test above only protects what it can find,
    and a rename that breaks the regex would take this whole file quiet with it."""
    assert "<div id=ws class=ws-hidden>" in PAGE
    assert "#ws.ws-hidden{display:none}" in PAGE


def test_nothing_else_sits_on_top_of_the_cockpit_at_rest():
    """An always-on fixed full-bleed layer is the shape of the bug, not just this instance of it.
    Anything with `position:fixed;inset:0` must either ship hidden or be transparent to clicks."""
    for sel, body in RULE.findall(PAGE):
        flat = body.replace(" ", "")
        if "position:fixed" not in flat or "inset:0" not in flat:
            continue
        if "pointer-events:none" in flat:
            continue
        ids = [i for i, c in HIDDEN_AT_REST.findall(PAGE) if f"#{i}" in sel]
        assert ids, f"`{sel.strip()}` covers the whole viewport and never ships hidden"


# ── the served JavaScript is syntactically valid ────────────────────────────────────────────────

def _scan_js(src):
    """Walk the script looking for a raw newline inside a quoted string.

    JS forbids a literal newline in a '' or "" string; a backtick template may hold one. That makes
    this the precise signature of the one hazard COCKPIT has: it is a plain Python string, so `\\n`
    written in the Python source arrives at the browser as an actual newline instead of an escape,
    and the SyntaxError takes the entire script block with it rather than the line that caused it.

    Returns a list of (line number, quote character)."""
    bad, i, n, line = [], 0, len(src), 1
    state = None                                     # None, "'", '"', '`', '//', '/*'
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            if state in ("'", '"'):
                bad.append((line - 1, state))
                state = None                         # resync rather than cascade
            elif state == "//":
                state = None
            i += 1
            continue
        if state in ("'", '"', "`"):
            if c == "\\":
                i += 2
                continue
            if c == state:
                state = None
            i += 1
            continue
        if state == "//":
            i += 1
            continue
        if state == "/*":
            if src.startswith("*/", i):
                state = None
                i += 2
                continue
            i += 1
            continue
        if src.startswith("//", i):
            state, i = "//", i + 2
            continue
        if src.startswith("/*", i):
            state, i = "/*", i + 2
            continue
        if c in "'\"`":
            state = c
        i += 1
    return bad


def _inline_js():
    """The page's own script, without the vendor files it loads by src."""
    return "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", PAGE, re.S))


def test_the_scanner_catches_the_bug_it_was_written_for():
    """A checker nobody has seen fail is not a checker. This is the real line, before the fix."""
    broken = "const cut = a.truncated ? '\n\n… truncated' : '';"
    assert _scan_js(broken), "the scanner does not catch a newline inside a single-quoted string"
    assert not _scan_js("const cut = a.truncated ? '\\n\\n… truncated' : '';")
    assert not _scan_js("const t = `a\nb`;"), "a template literal may span lines"
    assert not _scan_js("// it's a comment with an apostrophe\nconst a = 1;")


def test_no_string_literal_in_the_cockpit_spans_a_line():
    """One of these took the whole cockpit down: every button dead, the feed stuck on `loading…`,
    and no error visible without opening the console. The script block is one unit, so a single bad
    escape anywhere in 30,000 characters costs all of it."""
    bad = _scan_js(_inline_js())
    assert not bad, "raw newline inside a quoted string at line(s): " + str(bad)


def test_the_page_carries_the_escape_rather_than_the_newline():
    """Named, because the general scanner above would also pass on a page that simply deleted the
    feature. The truncation notice has to still be there, and still be escaped."""
    assert "\\n\\n… truncated, open /api/knowledge?id=" in PAGE
    assert "\n\n… truncated" not in PAGE


# ── the cockpit on a phone (#946) ───────────────────────────────────────────────────────────────

def _media_blocks(page, query):
    """The bodies of every `@media <query>{...}` block, brace-matched."""
    out, start = [], 0
    head = f"@media {query}{{"
    while (i := page.find(head, start)) != -1:
        j, depth = i + len(head), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(page[j], 0)
            j += 1
        out.append(page[i + len(head):j - 1])
        start = j
    return out


PHONE = "\n".join(_media_blocks(PAGE, "(max-width:600px)"))


def test_the_cockpit_has_a_viewport_meta():
    """Without it a phone lays the page out at 980 px and zooms out, and no max-width:600px rule
    below ever applies. The run page had the tag; the cockpit never did, until #946."""
    head = PAGE[:PAGE.index("</head>")]
    assert re.search(r'<meta name=viewport content="width=device-width,initial-scale=1">', head)


def test_the_phone_rules_are_really_inside_a_phone_media_query():
    assert "#phonenav{display:grid" in PHONE
    assert "#voicebar{flex-wrap:wrap}" in PHONE
    assert "padding-bottom:calc(76px + env(safe-area-inset-bottom))" in PHONE, \
        "the bottom bar would cover the last run row"


def test_the_phone_tab_bar_is_hidden_on_the_laptop():
    """Outside the media query the bar is display:none, and nothing but the phone block shows it."""
    outside = PAGE
    for block in _media_blocks(PAGE, "(max-width:600px)"):
        outside = outside.replace(block, "")
    assert "#phonenav{display:none}" in outside
    assert "#phonenav{display:grid" not in outside


def test_every_phone_tab_points_at_a_section_that_exists():
    targets = re.findall(r"<button data-to=([\w-]+) aria-label=", PAGE)
    assert targets == ["apstrip", "projects", "pmboard", "lanebar", "chat"]
    for t in targets:
        assert re.search(rf"<(div|table) id={t}[\s>]", PAGE), t


def test_the_phone_badges_are_fed_by_the_polls():
    """A badge nobody updates is a zero forever: the runs feed and the autopilot poll both set one."""
    assert "id=nb-runs" in PAGE and "id=nb-ap" in PAGE
    assert "navBadge('nb-runs', c['awaiting-approval'])" in PAGE
    assert "navBadge('nb-ap'," in PAGE


def test_every_link_in_the_cockpit_opens_a_new_tab():
    """On the phone a link that replaces the cockpit loses its place in an ~8,000 px page, and the
    live feed with it (#946). Every link out, the run page included, opens its own tab."""
    links = re.findall(r"<a\b[^>]*\bhref=[^>]*>", PAGE)
    assert links, "no links found"
    same_tab = [a for a in links if "target=_blank" not in a or "rel=noopener" not in a]
    assert not same_tab, same_tab
