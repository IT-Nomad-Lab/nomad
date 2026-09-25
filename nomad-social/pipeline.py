"""One batch = a list of links → a folder with everything, and report.md on top.

  <out>/<batch>/
    report.md          cross-item synthesis + one section per item (open this first)
    items.json         every normalised field + analysis, machine-readable
    metrics.csv        one row per item, for a spreadsheet
    <kind>_<id>/       per item: raw.json (full instaloader + yt-dlp JSON), media/, frames/,
                       transcript.txt, analysis.json
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import analyze
import fetch
import media

OUT = Path(os.environ.get("NOMAD_SOCIAL_OUT", Path(__file__).parent / "output"))
DELAY_S = float(os.environ.get("NOMAD_SOCIAL_DELAY_S", "4"))   # politeness between items


def _dedupe(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        u = u.strip()
        if not u or u.startswith("#"):
            continue
        key = re.sub(r"[?#].*$", "", u).rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) else ("n/a" if n is None else str(n))


def _item_md(i: int, it: dict) -> str:
    m, a = it["meta"], it.get("analysis") or {}
    prof = m.get("profile") or {}
    lines = [f"### {i}. @{m.get('owner_username', '?')} · {it['kind']} · {it['url']}", ""]
    lines.append(f"Posted {m.get('date_utc') or 'n/a'} · likes {_fmt(m.get('likes'))} · "
                 f"comments {_fmt(m.get('comments_count'))} · plays "
                 f"{_fmt(m.get('video_plays') or m.get('video_views'))} · followers "
                 f"{'~' if prof.get('followers_approx') else ''}{_fmt(prof.get('followers'))} · engagement "
                 f"{m.get('engagement_rate_pct', 'n/a')}%"
                 + (f" · {m['video_duration_s']:.0f}s" if m.get("video_duration_s") else ""))
    if m.get("is_paid_partnership") or m.get("sponsors"):
        lines.append(f"**Paid partnership** {', '.join(m.get('sponsors') or [])}")
    if m.get("audio") and m["audio"].get("title"):
        lines.append(f"Audio: {m['audio'].get('title')} {m['audio'].get('artist') or ''}".strip())
    lines.append("")
    if a.get("error"):
        lines += [f"> Analysis failed: {a['error']}", ""]
    else:
        lines += [a.get("summary", ""), ""]
        h = a.get("hook") or {}
        lines.append(f"- **Format:** {a.get('content_format')} · **Topic:** {a.get('topic')} · "
                     f"**Tone:** {a.get('tone')} · **Language:** {a.get('language')}")
        lines.append(f"- **Hook ({h.get('strength_1_10')}/10, {h.get('technique')}):** "
                     f"{h.get('first_seconds_or_slide')}")
        if a.get("structure"):
            lines.append("- **Structure:** " + " → ".join(
                f"{b.get('at')}: {b.get('beat')}" for b in a["structure"]))
        if a.get("on_screen_text"):
            lines.append("- **On-screen text:** " + " | ".join(a["on_screen_text"][:12]))
        for k, label in (("spoken_summary", "Spoken"), ("visual_style", "Visual style"),
                         ("audio", "Audio role"), ("cta", "CTA"),
                         ("target_audience", "Audience"),
                         ("commercial_intent", "Commercial intent"),
                         ("comment_sentiment", "Comments"),
                         ("why_it_works_or_not", "Why it works / doesn't")):
            if a.get(k):
                lines.append(f"- **{label}:** {a[k]}")
        if a.get("brands_products"):
            lines.append("- **Brands / products / places:** " + ", ".join(a["brands_products"]))
        if a.get("claims"):
            lines.append("- **Claims:** " + "; ".join(c.get("claim", "") for c in a["claims"]))
        if a.get("reusable_ideas"):
            lines.append("- **Reusable:** " + "; ".join(a["reusable_ideas"]))
        if a.get("red_flags"):
            lines.append("- **Red flags:** " + "; ".join(a["red_flags"]))
        lines.append(f"- _analysed by `{a.get('model')}` from {a.get('images_sent')} images_")
    if m.get("caption"):
        cap = m["caption"].replace("\n", " ")
        lines += ["", f"> Caption: {cap[:600]}{'…' if len(cap) > 600 else ''}"]
    if it.get("errors"):
        lines += ["", "Retrieval notes: " + "; ".join(it["errors"])]
    lines.append(f"\nFiles: `{it['dir']}`")
    return "\n".join(lines)


def run(urls: list[str], focus: str = "", name: str = "", progress=None) -> dict:
    urls = _dedupe(urls)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (name or focus or "batch").lower()).strip("-")[:40]
    root = OUT / f"{stamp}_{slug or 'batch'}"
    root.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda s: None)

    results = []
    for n, url in enumerate(urls, 1):
        t0 = time.time()
        say(f"[{n}/{len(urls)}] fetching {url}")
        item = fetch.fetch(url, root)
        rec = item.as_dict()
        if item.media:
            say(f"[{n}/{len(urls)}] frames + transcript ({len(item.media)} media)")
            try:
                processed = media.process(item)
                rec["transcripts"] = processed["transcripts"]
                rec["videos"] = processed["videos"]
                if not item.meta.get("video_duration_s") and processed["videos"]:
                    item.meta["video_duration_s"] = round(sum(
                        v.get("duration_s") or 0 for v in processed["videos"]), 1)
                say(f"[{n}/{len(urls)}] vision analysis ({len(processed['visuals'])} images)")
                rec["analysis"] = analyze.analyse_item(item, processed)
            except Exception as e:
                rec["analysis"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        else:
            rec["analysis"] = {"error": "nothing downloaded: " + "; ".join(item.errors)}
        (item.dir / "analysis.json").write_text(json.dumps(rec["analysis"], indent=1,
                                                           ensure_ascii=False))
        rec["seconds"] = round(time.time() - t0, 1)
        results.append(rec)
        if n < len(urls):
            time.sleep(DELAY_S)
    return write(root, results, focus, name, say)


def write(root: Path, results: list[dict], focus: str, name: str, say=lambda s: None) -> dict:
    """Cross-item synthesis + report.md, items.json, metrics.csv for a batch folder."""
    ok = [r for r in results if not r["analysis"].get("error")]
    say(f"writing report ({len(ok)}/{len(results)} analysed)")
    synth, served = "", None
    if ok:
        try:
            synth, served = analyze.report(ok, focus)
        except Exception as e:
            synth = f"> Cross-item synthesis failed: {str(e)[:300]}"
    elif results:
        synth = "> No item could be analysed, see the retrieval notes below."

    md = [f"# Instagram report: {name or focus or 'batch'}",
          f"{datetime.now():%Y-%m-%d %H:%M} · {len(results)} links · {len(ok)} analysed"
          + (f" · synthesis by `{served}`" if served else "")
          + ("" if fetch.has_cookies() else " · anonymous (no cookies, stories unavailable)"),
          "", synth, "", "## Item by item", ""]
    md += [_item_md(i, r) + "\n" for i, r in enumerate(results, 1)]
    (root / "report.md").write_text("\n".join(md), encoding="utf-8")
    (root / "items.json").write_text(json.dumps(results, indent=1, ensure_ascii=False,
                                                default=str))
    cols = ["url", "kind", "account", "posted", "followers", "likes", "comments", "plays",
            "engagement_rate_pct", "duration_s", "format", "topic", "hook_strength", "language",
            "paid_partnership", "model", "errors"]
    with open(root / "metrics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            m, a = r["meta"], r["analysis"]
            w.writerow({"url": r["url"], "kind": r["kind"], "account": m.get("owner_username"),
                        "posted": m.get("date_utc"),
                        "followers": (m.get("profile") or {}).get("followers"),
                        "likes": m.get("likes"), "comments": m.get("comments_count"),
                        "plays": m.get("video_plays") or m.get("video_views"),
                        "engagement_rate_pct": m.get("engagement_rate_pct"),
                        "duration_s": m.get("video_duration_s"),
                        "format": a.get("content_format"), "topic": a.get("topic"),
                        "hook_strength": (a.get("hook") or {}).get("strength_1_10"),
                        "language": a.get("language"),
                        "paid_partnership": m.get("is_paid_partnership"),
                        "model": a.get("model"), "errors": " | ".join(r.get("errors") or [])})
    return {"dir": str(root), "report": str(root / "report.md"), "total": len(results),
            "analysed": len(ok), "failed": [{"url": r["url"], "why": r["analysis"].get("error")}
                                            for r in results if r["analysis"].get("error")]}
