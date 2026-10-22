"""#1178 · Chat and More: a 16 px input, the mic floating on the phone, Terminal and System in More.

Page-contract tests on the COCKPIT string; the voice code itself is pinned in test_voice_surface.py.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

C = server.COCKPIT


def _section(name):
    s = C[C.index(f"<section id=dest-{name}"):]
    return s[:s.index("</section>")]


def test_the_chat_input_is_16px_so_a_phone_does_not_zoom_on_focus():
    assert re.search(r"\.crow input\{[^}]*font-size:16px", C)


def test_the_voice_buttons_carry_no_inline_style():
    bar = C[C.index("<div id=voicebar"):C.index("<div id=chat class=chat>")]
    assert "style=" not in bar.replace('style="display:flex"', "")


def _media_blocks(query):
    out, start, head = [], 0, f"@media {query}{{"
    while (i := C.find(head, start)) != -1:
        j, depth = i + len(head), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(C[j], 0)
            j += 1
        out.append(C[i + len(head):j - 1])
        start = j
    return out


def test_the_mic_floats_above_the_tab_bar_on_the_phone_only():
    phone = "\n".join(_media_blocks("(max-width:600px)"))
    rule = phone.split("#dest-chat #mic{")[1].split("}")[0]
    assert "position:fixed" in rule and "var(--tabbar)" in rule
    rest = C
    for block in _media_blocks("(max-width:600px)"):
        rest = rest.replace(block, "")
    assert "#dest-chat #mic{" not in rest


def test_terminal_and_system_live_in_more_and_not_in_chat_or_now():
    more = _section("more")
    assert "id=termproj" in more and "id=termopen" in more and "id=telem" in more and "id=apstrip" in more
    assert "id=telem" not in _section("now") and "id=termproj" not in _section("chat")


def test_remote_mode_hides_the_terminal_card_too():
    assert "body.remote #termrow{display:none!important}" in C


def test_every_control_the_scripts_bind_still_exists_once():
    for i in ("mic", "wake", "livevoice", "spk", "vstatus", "rt-audio", "termproj", "termopen", "cin", "csend", "ccap", "clog", "telem"):
        assert len(re.findall(rf"\bid={i}\b", C)) == 1, i


# ── #1179 · the 360 px pass ────────────────────────────────────────────────────────────────────

def test_no_text_in_the_page_style_is_smaller_than_12px():
    style = C[C.index("<style>"):C.index("</style>")]
    assert not re.findall(r"font-size:\s*(?:[0-9]|1[01])(?:\.\d+)?px", style)


def test_amber_brand_is_not_used_on_voice_or_capture():
    """Amber means attention. Speak, the wake word and Capture are not asking for anything."""
    assert "#voicebar .vb.brand{background:var(--info)" in C
    assert ".crow .cap{background:var(--info)" in C
    assert not re.search(r"style\.background=[^;]*var\(--brand\)", C)


def test_the_phone_gives_voice_and_pager_buttons_a_44px_target():
    phone = "\n".join(_media_blocks("(max-width:600px)"))
    assert "#voicebar button{min-width:var(--tap)}" in phone
    assert ".rpage{min-width:var(--tap);min-height:var(--tap)}" in phone
    assert ".crow{flex-wrap:wrap} .crow input{flex:1 1 100%}" in phone
