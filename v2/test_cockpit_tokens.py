"""Cockpit design tokens (#1173): colours live in :root, amber is only for attention."""
import re
import server

CSS = server.COCKPIT.split("</style>", 1)[0]
ROOT = re.search(r":root\{(.*?)\}", CSS, re.S).group(1)
TOKENS = dict(re.findall(r"--([a-z0-9-]+):([^;}]+)", ROOT))


def test_penpot_tokens_present():
    for name, value in {"bg": "#05060a", "surface": "#0c0d16", "text": "#ccddee", "brand": "#c9a45c",
                        "attention": "#ffb000", "ok": "#33dd88", "danger": "#e3554f"}.items():
        assert TOKENS[name].lower() == value


def test_brand_is_not_attention():
    assert TOKENS["brand"] != TOKENS["attention"]


def test_core_colours_are_not_literals_outside_root():
    body = CSS.replace(ROOT, "")
    for hexval in ("#05060a", "#0c0d16", "#14141f", "#1f2030", "#2a2a3a", "#56b6ff", "#33dd88", "#e3554f"):
        assert hexval not in body.lower(), hexval


def test_svg_attributes_do_not_use_var():
    assert not re.search(r'(?:fill|stroke)="var\(', server.COCKPIT)
