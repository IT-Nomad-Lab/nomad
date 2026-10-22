"""#1387 · the ask card (owui/ask_card.py).

Pure tests (escaping, payload JSON) run with the owui command:
    uv run -q --no-project --with pytest --with aiohttp --with pydantic \
        python -m pytest owui -q -p no:cacheprovider
The browser tests need Playwright and are skipped when it is missing. Run everything, and write the
screenshots to docs/research/1387-card/, with the Playwright venv (no pytest needed):
    ~/nomad/dispatcher/.venv-playwright/bin/python3 owui/test_ask_card.py
"""
import html
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ask_card", HERE / "ask_card.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

SHOTS = HERE.parent / "docs" / "research" / "1387-card"
EVIL = '</script><img src=x onerror=alert(1)> & "q" \'s\' <!--  '


def embedded(card: str) -> dict:
    m = re.search(r'<script id=ask type=application/json>(.*?)</script>', card, re.S)
    assert m, "ask data element missing"
    return json.loads(m.group(1))


def _evil_ask():
    return {"id": "e1", "kind": "questions", "questions": [
        {"header": EVIL, "question": EVIL, "multiSelect": False,
         "options": [{"label": EVIL, "description": EVIL}]}]}


# ---------------------------------------------------------------- pure tests

def test_script_breakout_is_impossible():
    card = C.build_card(_evil_ask())
    # exactly the three script elements' worth of closers: data, code (2 closing tags)
    assert card.count("</script>") == 2
    assert "<img src=x" not in card and "<!--" not in card
    assert " " not in card
    q = embedded(card)["questions"][0]
    assert q["question"] == EVIL and q["header"] == EVIL            # round-trips unchanged
    assert q["options"][0] == {"label": EVIL, "description": EVIL}


def test_every_string_field_is_escaped_in_approve_and_answered():
    ask = {"id": "x", "kind": "approve", "tool": EVIL, "summary": EVIL, "reason": EVIL}
    card = C.build_card(ask, answered={"decision": "deny", "note": EVIL})
    assert card.count("</script>") == 2 and "<img" not in card
    d = embedded(card)
    assert d["tool"] == EVIL and d["answered"]["note"] == EVIL


def test_html_escape_of_card_survives_srcdoc():
    card = C.build_card(_evil_ask())
    assert html.unescape(html.escape(card, quote=True)) == card


def test_payload_shape_and_kind_normalisation():
    d = embedded(C.build_card({"id": 7, "kind": "weird", "questions": [{"question": "q"}]}))
    assert d["id"] == "7" and d["kind"] == "questions"
    assert d["questions"] == [{"header": "", "question": "q", "multiSelect": False, "options": []}]
    d = embedded(C.build_card({"id": "a", "kind": "approve", "tool": "Bash"}))
    assert d["kind"] == "approve" and "answered" not in d


def test_answered_flag_and_theme():
    card = C.build_card(C.SAMPLE_Q, answered={"answers": []}, theme="dark")
    assert embedded(card)["answered"] == {"answers": []}
    assert '<html data-theme="dark">' in card
    assert "data-theme" not in C.build_card(C.SAMPLE_Q).split("<style>")[0]
    assert "data-theme" not in C.build_card(C.SAMPLE_Q, theme="bogus").split("<style>")[0]


def test_self_contained():
    card = C.build_card(C.SAMPLE_4TABS)
    assert not re.search(r'(src|href)\s*=\s*["\']?https?:', card) and "@import" not in card
    assert "fetch(" not in card and "XMLHttpRequest" not in card


# ---------------------------------------------------------------- browser tests

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pytest under uv: browser tests skip
    sync_playwright = None


def host(card, width=520, height=520):
    return ("<body style='margin:0'><script>window.msgs=[];addEventListener('message',e=>msgs.push(e.data))</script>"
            f"<iframe id=f sandbox='allow-scripts allow-popups' style='width:{width}px;height:{height}px;border:0' "
            f"srcdoc=\"{html.escape(card, quote=True)}\"></iframe>")


def msgs(pg, typ="input:prompt:submit"):
    return [m for m in pg.evaluate("msgs") if m.get("type") == typ]


def answer(pg):
    m = msgs(pg)
    assert len(m) == 1, m
    pre, aid, js = m[0]["text"].split(" ", 2)
    assert pre == "answer"
    return aid, json.loads(js)


def open_card(b, ask, **kw):
    scheme = kw.pop("scheme", "dark")
    w = kw.pop("width", 520)
    pg = b.new_page(viewport={"width": w + 40, "height": 800}, color_scheme=scheme)
    pg.set_content(host(C.build_card(ask, **kw), width=w))
    pg.wait_for_selector("#f")
    return pg, pg.frame_locator("#f")


def _need_browser():
    if sync_playwright is None:
        import pytest
        pytest.skip("playwright not installed")


def _browser_checks(p):
    b = p.chromium.launch()
    out = {}
    SHOTS.mkdir(parents=True, exist_ok=True)

    # --- multi-question: tabs, recommended first, auto-advance, multi, free text, Other, submit
    pg, f = open_card(b, C.SAMPLE_4TABS)
    assert f.locator(".tab").all_inner_texts() == ["Breakpoints", "Copy work", "Notes", "Launch date"]
    first = f.locator(".panel").nth(0).locator(".opt b").all_inner_texts()
    assert first[0] == "Approve as built (Recommended)" and first[-1] == "Other", first
    assert f.locator("#sub").is_disabled() and "0/4" in f.locator("#sub").inner_text()
    f.locator(".panel").nth(0).locator("input").first.check()                 # recommended
    assert f.locator(".tab.on").inner_text().startswith("Copy work")          # auto-advance
    p1 = f.locator(".panel").nth(1)
    p1.locator("input").nth(0).check(); p1.locator("input").nth(1).check()
    f.locator(".tab", has_text="Notes").click()
    f.locator(".panel").nth(2).locator("textarea").fill("ship it")
    f.locator(".tab", has_text="Launch date").click()
    p3 = f.locator(".panel").nth(3)
    p3.locator("input:not([type=text])").last.check()                                           # Other
    assert f.locator(".tab.on").inner_text().startswith("Launch")              # Other does not advance
    assert f.locator("#sub").is_disabled() and "3/4" in f.locator("#sub").inner_text()   # Other empty = unanswered
    p3.locator("input[type=text]").fill("Dec 1")
    assert f.locator("#sub").is_enabled() and "4/4" in f.locator("#sub").inner_text()
    assert f.locator(".tab.done").count() == 4
    pg.screenshot(path=str(SHOTS / "tabs-4-dark-520.png"))
    f.locator("#sub").click(); pg.wait_for_timeout(150)
    aid, pay = answer(pg)
    assert aid == "f4"
    assert pay == {"answers": [
        {"question": "#1314: approve the derived breakpoints?", "answer": "Approve as built (Recommended)"},
        {"question": "Which pages get new copy?", "answer": ["Home", "About"]},
        {"question": "Anything else I should know?", "answer": "ship it"},
        {"question": "When should this ship?", "answer": "Dec 1"}]}, pay
    assert f.locator("#sub").is_disabled() and "Answered" in f.locator("#foot").inner_text()
    assert all(f.locator("input,textarea").nth(i).is_disabled() for i in range(f.locator("input,textarea").count()))
    pg.keyboard.press("Escape"); pg.click("body"); pg.wait_for_timeout(100)
    assert len(msgs(pg)) == 1                                                  # locked: no second answer
    out["multi"] = pay
    assert msgs(pg, "iframe:height"), "no iframe:height"
    pg.close()

    # --- single select (Other as the answer), multi select with Other, free text
    pg, f = open_card(b, C.SAMPLE_SINGLE)
    assert f.locator(".tab").count() == 0 and f.locator("#sub").is_disabled()
    f.locator("input:not([type=text])").last.check(); f.locator("input[type=text]").fill("640/1024")
    f.locator("#sub").click(); pg.wait_for_timeout(100)
    assert answer(pg)[1] == {"answers": [{"question": "#1314: approve the derived breakpoints?", "answer": "640/1024"}]}
    pg.close()
    pg, f = open_card(b, C.SAMPLE_MULTI)
    f.locator("input").nth(1).check(); f.locator("input:not([type=text])").last.check(); f.locator("input[type=text]").fill("Pricing")
    f.locator("#sub").click(); pg.wait_for_timeout(100)
    assert answer(pg)[1]["answers"][0]["answer"] == ["About", "Pricing"]
    pg.close()
    pg, f = open_card(b, C.SAMPLE_TEXT)
    assert f.locator("input").count() == 0 and f.locator("#sub").is_disabled()
    f.locator("textarea").fill("  hello  "); f.locator("#sub").click(); pg.wait_for_timeout(100)
    out["text"] = answer(pg)[1]
    assert out["text"]["answers"][0]["answer"] == "hello"
    pg.close()

    # --- approve / deny (+ note), Esc and close deny, collapse
    for sel, want in (("#ap", {"decision": "approve"}), ("#dn", {"decision": "deny"})):
        pg, f = open_card(b, C.SAMPLE_APPROVE)
        assert "Approve Bash?" in f.locator(".title").inner_text()
        assert "git push" in f.locator(".sum").inner_text() and "Red tier" in f.locator(".hint").first.inner_text()
        f.locator(sel).click(); pg.wait_for_timeout(100)
        assert answer(pg)[1] == want
        out[want["decision"]] = want
        pg.close()
    pg, f = open_card(b, C.SAMPLE_APPROVE)
    f.locator("#note").fill("not now"); f.locator("#dn").click(); pg.wait_for_timeout(100)
    assert answer(pg)[1] == {"decision": "deny", "note": "not now"}
    pg.close()
    for ask in (C.SAMPLE_APPROVE, C.SAMPLE_4TABS):
        pg, f = open_card(b, ask)
        f.locator(".title, .tab").first.click(); pg.keyboard.press("Escape"); pg.wait_for_timeout(100)
        assert answer(pg)[1] == {"decision": "deny", "note": "cancelled"}       # Esc never approves
        pg.close()
        pg, f = open_card(b, ask)
        f.locator("#close").click(); pg.wait_for_timeout(100)
        assert answer(pg)[1] == {"decision": "deny", "note": "cancelled"}
        pg.close()
    pg, f = open_card(b, C.SAMPLE_Q)
    f.locator("#col").click(); pg.wait_for_timeout(150)
    assert not f.locator(".content").is_visible() and not msgs(pg)
    hs = [m["height"] for m in msgs(pg, "iframe:height")]
    assert hs[-1] < hs[0], hs
    f.locator("#col").click(); assert f.locator(".content").is_visible()
    pg.close()

    # --- hostile strings in a real browser: nothing executes, text is shown literally
    pg, f = open_card(b, _evil_ask())
    pg.on("dialog", lambda d: (_ for _ in ()).throw(AssertionError("alert fired")))
    assert f.locator(".q").inner_text() == EVIL.replace(" ", " ") or EVIL[:20] in f.locator(".q").inner_text()
    assert f.locator("img").count() == 0
    pg.close()

    # --- answered mode: no enabled controls, shows the choice, never posts
    for ask, ans in ((C.SAMPLE_4TABS, {"answers": [
            {"question": "q", "answer": "Approve as built (Recommended)"}, {"question": "q", "answer": ["Home"]},
            {"question": "q", "answer": "done it"}, {"question": "q", "answer": "Dec 1"}]}),
            (C.SAMPLE_APPROVE, {"decision": "deny", "note": "not now"}),
            (C.SAMPLE_APPROVE, {"decision": "approve"})):
        pg, f = open_card(b, ask, answered=ans)
        n = f.locator("button, input, textarea, select").count()
        enabled = [f.locator("button, input, textarea, select").nth(i).is_enabled() for i in range(n)]
        assert not any(enabled), enabled
        pg.keyboard.press("Escape"); pg.wait_for_timeout(100)
        assert not msgs(pg)
        if "answers" in ans:
            assert f.locator("input:checked").count() == 3          # recommended, Home, Other
            assert f.locator("input[type=text]").last.input_value() == "Dec 1"
            assert f.locator("textarea").input_value() == "done it"
            pg.screenshot(path=str(SHOTS / "answered-dark-520.png"))
        else:
            assert ("Denied" if ans["decision"] == "deny" else "Approved") in f.locator(".res").inner_text()
        pg.close()

    # --- screenshots
    for name, ask, kw in (("approve-dark-520", C.SAMPLE_APPROVE, {}),
                          ("single-dark-520", C.SAMPLE_SINGLE, {}),
                          ("multi-dark-520", C.SAMPLE_MULTI, {}),
                          ("text-dark-520", C.SAMPLE_TEXT, {})):
        pg, f = open_card(b, ask, **kw)
        pg.screenshot(path=str(SHOTS / f"{name}.png")); pg.close()
    pg, f = open_card(b, C.SAMPLE_4TABS, width=320, scheme="light")
    pg.set_viewport_size({"width": 360, "height": 800})
    f.locator(".tab", has_text="Copy").click()
    # tabs scroll sideways instead of wrapping or overflowing the page
    ov = f.locator(".tabs").evaluate("e=>[e.scrollWidth,e.clientWidth]")
    assert ov[0] > ov[1], ov
    assert pg.evaluate("document.documentElement.scrollWidth") <= 360
    pg.screenshot(path=str(SHOTS / "tabs-4-light-360.png")); pg.close()
    pg, f = open_card(b, C.SAMPLE_APPROVE, width=320, scheme="light")
    pg.set_viewport_size({"width": 360, "height": 800})
    pg.screenshot(path=str(SHOTS / "approve-light-360.png")); pg.close()
    b.close()
    return out


def test_card_in_sandboxed_iframe():
    _need_browser()
    with sync_playwright() as p:
        _browser_checks(p)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        try:
            fn(); print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            fails += 1; print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
