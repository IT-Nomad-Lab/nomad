"""#782-#784 · the cockpit draws Lucide icons, not emoji.

Emoji render differently on every platform, some not at all, and a screen reader reads them out by
their Unicode name. The cockpit now carries a small inline sprite of Lucide icons (ISC) and draws each
one in the text colour. These tests keep emoji from creeping back, keep every icon reference pointing
at a symbol that exists, and keep icon-only buttons named for assistive tech.

Run: python3 -m pytest v2/test_cockpit_icons.py -q   (engine deps live in the container)
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

C = server.COCKPIT
# Typography the page uses on purpose: dashes, middle dot, ellipsis, arrows in prose, box rules in comments.
TYPOGRAPHY = set("—–·…─→‘’“”")


def _pictographs(text):
    out = []
    for ch in text:
        o = ord(ch)
        if ch in TYPOGRAPHY:
            continue
        if (0x2190 <= o <= 0x2BFF) or (0x1F000 <= o <= 0x1FAFF) or o in (0x2328, 0xFE0F):
            out.append(ch)
    return out


def test_no_emoji_or_pictographs_remain():
    assert _pictographs(C) == []


def test_no_numeric_entities_smuggle_them_back():
    for n in re.findall(r"&#(\d+);", C):
        assert _pictographs(chr(int(n))) == [], n


def test_every_icon_used_is_in_the_sprite():
    defined = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', C))
    used = set(re.findall(r'href="#i-([a-z0-9-]+)"', C)) | set(re.findall(r"ic\('([a-z0-9-]+)'", C))
    used |= set(re.findall(r"ic\(spkOn\?'([a-z0-9-]+)':'([a-z0-9-]+)'", C)[0]) if "ic(spkOn" in C else set()
    assert used, "no icons found"
    assert used <= defined, used - defined


def test_the_sprite_keeps_its_licence_notice():
    assert "Lucide" in C and "ISC License" in C


def test_icon_only_buttons_have_a_name():
    for bid in ("wake", "livevoice", "spk"):
        tag = re.search(rf"<button id={bid}[^>]*>", C).group(0)
        assert "aria-label=" in tag and "title=" in tag, bid


def test_the_speaker_toggle_says_its_state():
    assert "spoken replies: '+(spkOn?'on':'off')" in C
    assert "aria-pressed" in C
