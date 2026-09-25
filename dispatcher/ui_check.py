#!/usr/bin/env python3
"""NOMAD UI check — the dev crew's eyes.

Loads a page in headless Chromium (Playwright) and PROVES it actually rendered:
non-blank body text, no uncaught JS errors, no failed same-origin requests, and
(optionally) that specific text is present or absent. Exits 0 on pass, 1 on a
failed check, 2 on a bad/unsafe invocation — so a caller can gate on the exit code
alone without parsing JSON.

SAFETY (enforced HERE, not just by the dispatcher's guard_command regex):
  - Only http://localhost:* and http://127.0.0.1:* targets are allowed. Anything
    else is refused before Chromium ever launches.
  - Always headless, always a fresh throwaway browser context, no persistent
    profile, no downloads, no extensions.
  - Read-only: this script never POSTs, clicks, or submits forms. It loads a URL
    and inspects the result. (Assertions of interactive behavior can be added
    later with the same localhost guard; keep them as explicit new flags, not a
    loosening of this one.)

Usage:
  ui_check.py URL [--wait-for CSS_SELECTOR] [--expect-text STR]...
              [--deny-text STR]... [--min-text-len N] [--screenshot PATH]
              [--timeout-ms N]

Examples:
  ui_check.py http://localhost:1701 --expect-text "MISSION MONITOR"
  ui_check.py http://localhost:8099 --expect-text "NOMAD" --min-text-len 40
"""
import argparse
import json
import sys
from urllib.parse import urlparse

ALLOWED_HOSTS = {"localhost", "127.0.0.1"}


def _check_url(url: str) -> str | None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return f"Refused: scheme '{p.scheme}' is not http/https."
    if p.hostname not in ALLOWED_HOSTS:
        return f"Refused: host '{p.hostname}' is not localhost/127.0.0.1."
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--wait-for", default=None, help="CSS selector to wait for (visible) before checking")
    ap.add_argument("--expect-text", action="append", default=[], help="substring that MUST appear in body text (repeatable)")
    ap.add_argument("--deny-text", action="append", default=[], help="substring that must NOT appear in body text (repeatable)")
    ap.add_argument("--min-text-len", type=int, default=20, help="minimum visible body text length (default 20)")
    ap.add_argument("--screenshot", default=None, help="save a PNG screenshot to this path on completion")
    ap.add_argument("--timeout-ms", type=int, default=15000)
    args = ap.parse_args()

    bad = _check_url(args.url)
    if bad:
        print(json.dumps({"ok": False, "error": bad}))
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"ok": False, "error": "playwright not installed in this interpreter. "
                           "Use dispatcher/.venv-playwright/bin/python3, or run "
                           "dispatcher/ui_check.sh which points at it."}))
        return 2

    console_errors = []
    page_errors = []
    http_errors = []

    result = {"ok": False, "url": args.url}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=False)
        page = context.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        def _on_response(resp):
            try:
                if resp.status >= 400 and resp.url.split("//", 1)[-1].split("/", 1)[0].startswith(("localhost", "127.0.0.1")):
                    http_errors.append(f"{resp.status} {resp.url}")
            except Exception:
                pass
        page.on("response", _on_response)

        checks = []
        try:
            page.goto(args.url, wait_until="load", timeout=args.timeout_ms)
        except Exception as e:
            browser.close()
            result["error"] = f"navigation failed: {e}"
            print(json.dumps(result))
            return 1

        if args.wait_for:
            try:
                page.wait_for_selector(args.wait_for, state="visible", timeout=args.timeout_ms)
                checks.append({"check": f"selector visible: {args.wait_for}", "pass": True})
            except Exception as e:
                checks.append({"check": f"selector visible: {args.wait_for}", "pass": False, "detail": str(e)[:200]})

        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception as e:
            checks.append({"check": "body readable", "pass": False, "detail": str(e)[:200]})

        text_len = len(body_text.strip())
        checks.append({"check": f"visible text length >= {args.min_text_len}", "pass": text_len >= args.min_text_len,
                        "detail": f"got {text_len}"})

        for needle in args.expect_text:
            checks.append({"check": f"contains: {needle!r}", "pass": needle in body_text})
        for needle in args.deny_text:
            checks.append({"check": f"does NOT contain: {needle!r}", "pass": needle not in body_text})

        checks.append({"check": "no uncaught JS exceptions", "pass": len(page_errors) == 0,
                        "detail": page_errors[:5]})
        checks.append({"check": "no console.error() calls", "pass": len(console_errors) == 0,
                        "detail": console_errors[:5]})
        checks.append({"check": "no failed same-origin requests (>=400)", "pass": len(http_errors) == 0,
                        "detail": http_errors[:5]})

        if args.screenshot:
            try:
                page.screenshot(path=args.screenshot, full_page=True)
            except Exception:
                pass

        result["title"] = page.title()
        result["body_text_len"] = text_len
        browser.close()

    result["checks"] = checks
    result["ok"] = all(c["pass"] for c in checks)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
