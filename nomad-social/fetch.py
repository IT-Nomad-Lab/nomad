"""Instagram link → everything we can legally see from it, saved to disk.

Two libraries, each doing what it is best at (both verified anonymous-working 2026-09-22):

  instaloader  metadata. Post.from_shortcode returns the full media node: caption, owner, location,
               usertags, collab/co-authors, paid-partnership flag, music/original-audio info,
               carousel children, preview comments, counts. Also handles image posts and carousels,
               which yt-dlp does not ("There's no video in this post").
  yt-dlp       comments (~a dozen anonymously, instaloader needs a login for any) and the fallback
               downloader when instaloader is rate-limited. Also the only path for STORIES, which
               need a logged-in session: drop a Netscape cookies.txt at NOMAD_SOCIAL_COOKIES.

gallery-dl was tested too and redirects to the login page with no cookies, so it is not used.

Everything is fail-open per source: one source failing records an error, the other still runs.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
COOKIES = os.environ.get("NOMAD_SOCIAL_COOKIES", str(Path(__file__).parent / "cookies.txt"))
FFMPEG_DIR = os.environ.get("NOMAD_SOCIAL_FFMPEG_DIR", "/home/linuxbrew/.linuxbrew/bin")

_POST_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{5,})")
_STORY_RE = re.compile(r"instagram\.com/stories/(?:(highlights)/(\d+)|([A-Za-z0-9_.]+)/(\d+))")
_PROFILE_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$")


@dataclass
class Item:
    url: str
    kind: str = "unknown"          # post | reel | story | highlight | profile | unknown
    id: str = ""                   # shortcode, story media id, or username
    dir: Path | None = None
    meta: dict = field(default_factory=dict)      # normalised fields the analysis uses
    raw: dict = field(default_factory=dict)       # every source's full JSON, saved as-is
    media: list = field(default_factory=list)     # [{type: image|video, path, index}]
    comments: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"url": self.url, "kind": self.kind, "id": self.id, "dir": str(self.dir),
                "meta": self.meta, "media": self.media, "comments": self.comments,
                "errors": self.errors}


def has_cookies() -> bool:
    return Path(COOKIES).is_file() and Path(COOKIES).stat().st_size > 0


def resolve(url: str) -> str:
    """Follow /share/ short links to the canonical post URL. Anything else is returned as-is."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    if "/share/" in url:
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
            return str(r.url)
        except Exception:
            pass
    return url


def classify(url: str) -> tuple[str, str]:
    if m := _STORY_RE.search(url):
        return ("highlight", m.group(2)) if m.group(1) else ("story", m.group(4))
    if m := _POST_RE.search(url):
        return ("reel" if m.group(1) in ("reel", "reels", "tv") else "post"), m.group(2)
    host = urlparse(url).netloc
    if "instagram.com" in host and (m := _PROFILE_RE.search(url)):
        if m.group(1) not in ("p", "reel", "reels", "stories", "explore", "accounts"):
            return "profile", m.group(1)
    return "unknown", ""


# ── instaloader ─────────────────────────────────────────────────────────────────────────────

_L = None


def _loader():
    global _L
    if _L is None:
        import instaloader

        class _ShortWait(instaloader.RateController):
            # the default backs off for minutes on a 429 and stalls the whole batch; cap it and
            # let the yt-dlp path carry the item instead
            def sleep(self, secs):
                time.sleep(min(secs, 20))

        _L = instaloader.Instaloader(quiet=True, max_connection_attempts=2, request_timeout=30,
                                     download_comments=False, save_metadata=False,
                                     rate_controller=lambda ctx: _ShortWait(ctx))
        if has_cookies():
            jar = MozillaCookieJar(COOKIES)
            jar.load(ignore_discard=True, ignore_expires=True)
            _L.context._session.cookies.update(jar)
    return _L


def _via_instaloader(item: Item) -> list[dict]:
    """Fill item.meta/raw from instaloader. Returns media descriptors [{type, url, index}]."""
    import instaloader
    from instaloader.structures import get_json_structure

    post = instaloader.Post.from_shortcode(_loader().context, item.id)
    item.raw["instaloader"] = get_json_structure(post)
    node = item.raw["instaloader"].get("node", {})

    def g(*keys, default=None):
        for k in keys:
            try:
                v = getattr(post, k)
                if v is not None:
                    return v
            except Exception:
                continue
        return default

    loc = node.get("location") or {}
    clips = node.get("clips_metadata") or {}
    music = clips.get("music_info") or {}
    music_asset = (music.get("music_asset_info") or {}) if isinstance(music, dict) else {}
    orig = clips.get("original_sound_info") or {}
    item.meta.update({
        "shortcode": post.shortcode,
        "type": post.typename,                       # GraphImage | GraphVideo | GraphSidecar
        "product_type": node.get("product_type"),    # clips = reel, feed, carousel_container
        "owner_username": post.owner_username,
        "owner_full_name": (node.get("owner") or {}).get("full_name"),
        "date_utc": post.date_utc.isoformat() + "Z",
        "caption": post.caption or "",
        "hashtags": list(post.caption_hashtags or []),
        "mentions": list(post.caption_mentions or []),
        "tagged_users": list(g("tagged_users", default=[]) or []),
        "likes": g("likes"),
        "comments_count": g("comments"),
        "video_views": g("video_view_count"),
        "video_plays": node.get("play_count") or node.get("ig_play_count") or
                       node.get("video_play_count"),
        "video_duration_s": g("video_duration"),
        "location": {k: loc.get(k) for k in ("name", "lat", "lng")} if loc else None,
        "is_sponsored": bool(g("is_sponsored", default=False)),
        "is_paid_partnership": node.get("is_paid_partnership"),
        "sponsors": [str(s) for s in (g("sponsor_users", default=[]) or [])],
        "coauthors": [c.get("username") for c in (node.get("coauthor_producers") or [])],
        "accessibility_caption": g("accessibility_caption"),
        "audio": ({"type": clips.get("audio_type"),
                   "title": music_asset.get("title") or orig.get("original_audio_title"),
                   "artist": music_asset.get("display_artist")} if clips else None),
        "carousel_count": node.get("carousel_media_count") or node.get("edge_sidecar_to_children",
                                                                        {}).get("count"),
    })
    for c in node.get("preview_comments") or []:
        item.comments.append({"author": (c.get("user") or {}).get("username"),
                              "text": c.get("text"), "likes": c.get("comment_like_count"),
                              "source": "instaloader-preview"})

    media = []
    if post.typename == "GraphSidecar":
        for i, n in enumerate(post.get_sidecar_nodes()):
            media.append({"type": "video" if n.is_video else "image",
                          "url": n.video_url if n.is_video else n.display_url, "index": i,
                          "thumb": n.display_url})
    elif post.is_video:
        media.append({"type": "video", "url": post.video_url, "index": 0, "thumb": post.url})
    else:
        media.append({"type": "image", "url": post.url, "index": 0})
    return media


# ── yt-dlp ──────────────────────────────────────────────────────────────────────────────────

def _ydl_opts(**extra) -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 30,
         "ffmpeg_location": FFMPEG_DIR, "http_headers": {"User-Agent": UA}}
    if has_cookies():
        o["cookiefile"] = COOKIES
    o.update(extra)
    return o


def _slim(info: dict) -> dict:
    """yt-dlp info minus the format tables (huge, and signed URLs that expire anyway)."""
    drop = {"formats", "requested_formats", "requested_downloads", "thumbnails", "http_headers",
            "_format_sort_fields"}
    out = {k: v for k, v in info.items() if k not in drop}
    if "entries" in out:
        out["entries"] = [_slim(e) for e in (out["entries"] or []) if e]
    return out


def _via_ytdlp_info(item: Item) -> dict | None:
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True, getcomments=True)) as y:
        info = y.extract_info(item.url, download=False)
    info = y.sanitize_info(info)
    item.raw["ytdlp"] = _slim(info)
    seen = {c.get("text") for c in item.comments}
    for c in info.get("comments") or []:
        if c.get("text") in seen:
            continue
        item.comments.append({"author": c.get("author"), "text": c.get("text"),
                              "likes": c.get("like_count"), "timestamp": c.get("timestamp"),
                              "source": "yt-dlp"})
    # yt-dlp is the only source for stories and the fallback for posts: fill gaps, never overwrite
    m = item.meta
    for k, v in {"owner_username": info.get("channel") or info.get("uploader_id"),
                 "owner_full_name": info.get("uploader"),
                 "caption": info.get("description"),
                 "likes": info.get("like_count"),
                 "comments_count": info.get("comment_count"),
                 "video_views": info.get("view_count"),
                 "video_duration_s": info.get("duration"),
                 "date_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info["timestamp"]))
                              if info.get("timestamp") else None)}.items():
        if m.get(k) in (None, "", []) and v not in (None, ""):
            m[k] = v
    return info


def _ytdlp_download(item: Item, dest: Path) -> list[dict]:
    import yt_dlp
    opts = _ydl_opts(outtmpl=str(dest / "media_%(autonumber)02d.%(ext)s"),
                     format="bv*[height<=1920]+ba/b", merge_output_format="mp4")
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([item.url])
    out = []
    for i, p in enumerate(sorted(dest.glob("media_*"))):
        t = "video" if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov") else "image"
        out.append({"type": t, "path": str(p), "index": i})
    return out


# ── plain HTTP ──────────────────────────────────────────────────────────────────────────────

def _download(url: str, path: Path) -> bool:
    try:
        with httpx.stream("GET", url, headers={"User-Agent": UA}, follow_redirects=True,
                          timeout=120) as r:
            if r.status_code != 200:
                return False
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        return path.stat().st_size > 0
    except Exception:
        return False


_OG = re.compile(r'<meta (?:property|name)="(og:[a-z:]+|description)" content="([^"]*)"')


def profile_card(username: str) -> dict:
    """Follower/following/post counts from the public profile page's og:description. One request,
    no login, no API. Used for engagement rate. Empty dict when Instagram hides it."""
    try:
        # a bare UA gets the server-rendered card; the full Chrome UA gets a JS shell without it
        r = httpx.get(f"https://www.instagram.com/{username}/", headers={"User-Agent": "Mozilla/5.0"},
                      follow_redirects=True, timeout=20)
        tags = {k: v for k, v in _OG.findall(r.text)}
        desc = tags.get("og:description") or tags.get("description") or ""
        out = {"bio_line": desc}
        for label, key in (("Followers", "followers"), ("Following", "following"),
                           ("Posts", "posts")):
            if m := re.search(r"([\d.,]+)\s*([KMB]?)\s+" + label, desc):
                num = float(m.group(1).replace(",", ""))
                num *= {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2), 1)
                out[key] = int(num)
                out[key + "_approx"] = bool(m.group(2))   # "2M" is rounded by Instagram
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def _logged_out_block(url: str) -> str | None:
    """Why Instagram refuses this post to a logged-out visitor, when the page says so."""
    try:
        html = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True,
                         timeout=20).text
    except Exception:
        return None
    m = re.search(r'"restricted_age":\s*(\d+)', html)
    if m:
        return f"age-restricted ({m.group(1)}+) by Instagram"
    if '"is_private":true' in html:
        return "private account"
    return None


# ── entry point ─────────────────────────────────────────────────────────────────────────────

def fetch(url: str, out_root: Path) -> Item:
    url = resolve(url)
    item = Item(url=url)
    item.kind, item.id = classify(url)
    item.dir = out_root / f"{item.kind}_{item.id or abs(hash(url)) % 10**8}"
    (item.dir / "media").mkdir(parents=True, exist_ok=True)

    if item.kind == "unknown":
        item.errors.append("not an Instagram post/reel/story URL")
        return item
    if item.kind == "profile":
        item.meta["owner_username"] = item.id
        item.meta["profile"] = profile_card(item.id)
        item.errors.append("profile URL: only the public profile card is analysed; pass post "
                           "links for content analysis")
        return item
    if item.kind in ("story", "highlight") and not has_cookies():
        item.errors.append(f"{item.kind} links need a logged-in session: export cookies.txt from a "
                           f"browser logged into a THROWAWAY account and save it to {COOKIES}")
        return item

    wanted: list[dict] = []
    if item.kind in ("post", "reel"):
        try:
            wanted = _via_instaloader(item)
        except Exception as e:
            item.errors.append(f"instaloader: {type(e).__name__}: {str(e)[:300]}")
    try:
        _via_ytdlp_info(item)
    except Exception as e:
        # image-only posts always land here ("There's no video in this post"), that's expected
        msg = str(e)
        if "no video" not in msg.lower():
            item.errors.append(f"yt-dlp: {msg[:300]}")

    media_dir = item.dir / "media"
    for w in wanted:
        ext = ".mp4" if w["type"] == "video" else ".jpg"
        p = media_dir / f"media_{w['index']:02d}{ext}"
        if _download(w["url"], p):
            item.media.append({"type": w["type"], "path": str(p), "index": w["index"]})
        else:
            item.errors.append(f"download failed for media #{w['index']}")
    if not item.media:
        try:
            item.media = _ytdlp_download(item, media_dir)
        except Exception as e:
            item.errors.append(f"yt-dlp download: {str(e)[:300]}")
    if not item.media and not has_cookies():
        # Both libraries report an 18+ reel as a generic "empty media response". The page itself
        # says why: a logged-out fetch carries "restricted_age":18 (seen 2026-09-22).
        why = _logged_out_block(url)
        if why:
            item.errors.insert(0, why + f": needs a logged-in session. Export cookies.txt from a "
                                     f"browser logged into a THROWAWAY account to {COOKIES}")

    if item.meta.get("owner_username"):
        item.meta["profile"] = profile_card(item.meta["owner_username"])
        f, likes = item.meta["profile"].get("followers"), item.meta.get("likes")
        if f and likes is not None:
            item.meta["engagement_rate_pct"] = round(
                100 * (likes + (item.meta.get("comments_count") or 0)) / f, 3)

    (item.dir / "raw.json").write_text(json.dumps(item.raw, indent=1, default=str,
                                                  ensure_ascii=False))
    return item
