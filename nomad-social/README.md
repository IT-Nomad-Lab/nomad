# nomad-social

Give it a list of Instagram links and it returns a report. It handles posts, reels, carousels, and stories (stories need cookies). Profile links get only a follower card.

For each link it does four things:
- pulls the public metadata
- downloads the media
- transcribes the speech locally
- has a vision model describe the frames

Then it writes one report across all the links.

## Use

- **From Claude Code:** hand over the links. The `analyze_instagram(urls, focus, name)` MCP tool (`mcp-local/nomad_social.py`) runs the batch and returns the report.
- **CLI:** `.venv/bin/python cli.py links.txt --focus "competitor hooks" --name fitness`. `links.txt` has one URL per line; lines starting with `#` are ignored and duplicates are merged.
- **HTTP** (127.0.0.1:8216 only):
  - `POST /analyze {"urls": [...], "focus": "", "name": ""}` returns a `job_id`.
  - `GET /jobs/{id}` returns the job's progress, then the result.
  - `GET /health`.

Each batch writes to `output/<stamp>_<name>/`:
- `report.md`
- `items.json`, the full per-item data
- `metrics.csv`
- one folder per item: media, frames, `transcript.txt`, and `raw.json`

Expect about 45-90 s per reel. One batch runs at a time.

## What it pulls

- **Metadata:** caption, hashtags, mentions, date, likes, comment count, plays/views when public, duration, location, tagged users, coauthors, the paid-partnership flag, the audio track, and alt text.
- **Account:** follower, following, and post counts. Engagement rate is (likes + comments) / followers.
- **Comments:** a sample, about 14 per reel through yt-dlp.
- **Transcript:** faster-whisper `small`, any language.
- **Per-item analysis:** summary, format, hook and its strength, beat structure, on-screen text, visual style, audio, brands, claims, CTA, audience, commercial intent, comment sentiment, why it works, reusable ideas, and red flags.

## Stack

- instaloader: anonymous metadata through `Post.from_shortcode`
- yt-dlp: comments, and a fallback downloader
- ffmpeg: frames
- faster-whisper: transcripts
- LiteLLM for the models:
  - vision: `longdoc`, then `gpt`, with gateway fallbacks OFF and the serving model recorded
  - report: `balanced`

gallery-dl was tested and dropped: it redirects to the login page.

## Limits

- **Stories and highlights need a session.** Log into a THROWAWAY account in a browser and export `cookies.txt` (Netscape format) to `nomad-social/cookies.txt`, or point `NOMAD_SOCIAL_COOKIES` at it. Then run `systemctl --user restart nomad-social`. Instagram bans accounts that scrape, so never use your main account. The file is gitignored.
- **Age-restricted (18+) posts and private accounts need the same cookies.** Logged out, Instagram serves only a login page. The failure message says which of the two it is.
- **No profile listing.** An anonymous `Profile.from_username` gets a 429. Give post URLs, not a profile.
- **Follower counts are rounded** by Instagram ("2M"), so they are marked `~`.
- **Plays and views are often missing** anonymously.
- **Frames go to Google** (Gemini via `longdoc`). For local-only vision, set `NOMAD_SOCIAL_VISION_MODELS=private`. It only works when the GPU is free: qwen3.6 needs 22 GB and times out while ComfyUI, nomad-detect, and nomad-stt hold the card.
- **The `gpt` fallback needs OpenAI credits.** As of 2026-09-22 the account had none, so `longdoc` was the only working vision model. A malformed JSON reply from it gets one text-only repair pass.

## Maintenance

Instagram breaks scrapers every few months. When fetches start failing:

    .venv/bin/pip install -U yt-dlp instaloader && systemctl --user restart nomad-social

Install: `./install.sh` (venv), then `./install_service.sh` (systemd user unit). Logs: `journalctl --user -u nomad-social -f`.
