#!/usr/bin/env python3
"""
9gag Top Memes of the Day -> Email (HTML digest, 3 sections, hosted images)

Fetches posts from 9gag's "hot" feed and emails a digest split into three
sections — Static Images, Videos, and GIFs — each showing its own top
MEMES_PER_SECTION (default 30) posts ranked by upvote count (90 total by
default).

Images are hosted on GitHub (pushed to a public "meme-assets" branch of this
repo) and referenced by normal https:// URLs in the email, rather than
embedded as raw inline CID attachments. Gmail (and most webmail clients) is
known to be unreliable when a single email embeds a large number of raw
inline CID images sent via SMTP — some render, some silently don't, and it's
not predictable which. Hosting externally and linking by URL is the standard
way bulk-image emails (newsletters, digests) solve this.

This script runs in two phases so the workflow can push the images to GitHub
*between* them (see the accompanying GitHub Actions workflow):

    python 9gag_top_meme_emailer.py generate
        -> downloads/classifies/compresses media, saves it under ./previews/,
           and writes the composed email (subject/html/text) under ./email/

    python 9gag_top_meme_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

SETUP
-----
1. Install dependencies:
       pip install requests Pillow

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
       - Go to https://myaccount.google.com/apppasswords
       - You need 2-Step Verification turned on first.
       - Create an app password for "Mail" and copy the 16-character code.

3. Make this repo PUBLIC (Settings -> General -> Danger Zone -> Change
   visibility). This only exposes the meme image files pushed to the
   "meme-assets" branch — your Gmail credentials stay protected as encrypted
   repo secrets regardless of repo visibility.

4. Set these as environment variables:
       export GMAIL_ADDRESS="youraddress@gmail.com"
       export GMAIL_APP_PASSWORD="16-char-app-password"
       export MEME_RECIPIENT="where-to-send@example.com"
       export MEMES_PER_SECTION="30"       # optional, top N per section
       export GRID_COLUMNS="3"             # optional, cards per row
       export TIMEZONE="Asia/Ho_Chi_Minh"  # optional, for the subject line
       export GITHUB_REPOSITORY="yourname/9gag-meme-emailer"  # owner/repo,
                                            # auto-set already inside GitHub Actions
       export IMAGE_BRANCH="meme-assets"   # optional, branch images are hosted on
       export SENT_IDS_FILE="state/sent_ids.json"  # optional, dedup state file
       export SENT_ID_CAP="20000"          # optional, max tracked ids to retain

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this daily
in the cloud without needing your own computer on.
"""

import io
import json
import os
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests
from PIL import Image
from urllib.parse import quote

# 9gag's unofficial hot-feed API (no auth required for SFW content)
# Format is /group-posts/group/<GROUP>/type/<SECTION> — "default" is the
# main 9gag group, "hot" is the section (hot/fresh/trending).
NINEGAG_HOT_URL = "https://9gag.com/v1/group-posts/group/default/type/hot"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://9gag.com/hot",
}

# How many feed pages to scan when building the candidate pool. We need
# enough pages to find MEMES_PER_SECTION *new* (not-already-sent) posts in
# each of the 3 categories, and videos/gifs are much rarer than photos in
# the hot feed. Each page is ~10 posts. When running hourly with dedup
# enabled, the top of the feed is often already-sent, so this may need to
# be scanned deeper than a once-a-day run would.
MAX_CANDIDATE_PAGES = int(os.environ.get("MAX_CANDIDATE_PAGES", "60"))

# Media download reliability tuning. 9gag's CDN occasionally rate-limits or
# hotlink-blocks a fraction of rapid-fire requests, returning a small error
# response with a 200 status instead of the real file.
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = 1.5
MIN_VALID_IMAGE_BYTES = 500
REQUEST_DELAY_SECONDS = 0.3

# Static photo images are resized/recompressed before hosting — full-res
# originals aren't necessary for a small digest card.
IMAGE_MAX_WIDTH = 640
IMAGE_JPEG_QUALITY = 80

# Tuning for the animated-GIF preview generated from each video/gif post.
GIF_MAX_SECONDS = 6
GIF_WIDTH = 380
GIF_FPS = 10
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

SECTIONS = [
    ("photo", "Static Images", "\U0001F5BC"),
    ("video", "Videos", "\U0001F3A5"),
    ("gif", "GIFs", "\U0001F39E"),
]

PREVIEWS_ROOT = "previews"
EMAIL_DIR = "email"

# Every run gets its own subfolder (based on the unique GitHub Actions run
# ID) instead of overwriting the same file paths each day. raw.githubuser
# content.com (and Gmail's own image-proxy fetching it) is a CDN with edge
# caching — reusing the same path daily can serve stale/inconsistent
# results right after a fresh push, since different edges pick up the
# update at different times. A path that's never been requested before has
# nothing stale to collide with.
RUN_ID = os.environ.get("GITHUB_RUN_ID") or datetime.now().strftime("%Y%m%d%H%M%S")
PREVIEWS_DIR = f"{PREVIEWS_ROOT}/{RUN_ID}"

# Dedup state: a JSON file listing post IDs already emailed in a previous
# run, so re-running (hourly, etc.) sends only *new* memes off the hot feed
# instead of re-sending whatever's still sitting at the top. The workflow
# is responsible for fetching this file from the meme-assets branch before
# `generate` runs, and for committing the updated version back afterward —
# this script only reads/writes the local path.
SENT_IDS_FILE = os.environ.get("SENT_IDS_FILE", "state/sent_ids.json")
SENT_ID_CAP = int(os.environ.get("SENT_ID_CAP", "20000"))


def load_sent_ids(path=SENT_IDS_FILE):
    """Return (ordered_list, set) of previously-sent post IDs. Missing or
    corrupt state files are treated as "nothing sent yet" rather than a
    fatal error, so a fresh repo / first run just works.
    """
    if not os.path.exists(path):
        return [], set()
    try:
        with open(path) as f:
            data = json.load(f)
        ids = [int(i) for i in data.get("ids", [])]
        return ids, set(ids)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"  could not read {path} ({e}) — starting with empty dedup state", file=sys.stderr)
        return [], set()


def save_sent_ids(ordered_ids, path=SENT_IDS_FILE):
    """Persist the (oldest-first) list of sent post IDs, trimmed to
    SENT_ID_CAP so the file doesn't grow forever.
    """
    trimmed = ordered_ids[-SENT_ID_CAP:] if len(ordered_ids) > SENT_ID_CAP else ordered_ids
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"ids": trimmed, "updated": datetime.utcnow().isoformat() + "Z"}, f)


def fetch_media_bytes(url, expect="image", retries=DOWNLOAD_RETRIES):
    """GET a URL and return its bytes, retrying if the response doesn't look
    like real media (wrong content-type or suspiciously small — a common
    sign of a CDN rate-limit/error page returned with a 200 status).
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            content = resp.content

            looks_valid = (
                len(content) >= MIN_VALID_IMAGE_BYTES
                and (expect in content_type or content_type == "")
            )
            if looks_valid:
                return content
            last_error = f"unexpected response (content-type={content_type!r}, size={len(content)})"
        except requests.RequestException as e:
            last_error = str(e)

        if attempt < retries:
            time.sleep(DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(f"Failed to download valid {expect} from {url}: {last_error}")


def fetch_hot_pages(max_pages=MAX_CANDIDATE_PAGES):
    """Yield successive pages of the hot feed's post list."""
    next_cursor = None
    for _ in range(max_pages):
        url = NINEGAG_HOT_URL
        if next_cursor:
            url = f"{NINEGAG_HOT_URL}?{next_cursor}"

        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"9gag API returned {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        data = resp.json().get("data", {})

        posts = data.get("posts", [])
        if not posts:
            break
        yield posts

        next_cursor = data.get("nextCursor")
        if not next_cursor:
            break


def compress_image(image_bytes, max_width=IMAGE_MAX_WIDTH, quality=IMAGE_JPEG_QUALITY):
    """Resize + recompress an image for lightweight hosting.
    Returns (jpeg_bytes, 'jpg'), or (original_bytes, None) if it fails.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        if img.width > max_width:
            new_height = int(img.height * (max_width / img.width))
            img = img.resize((max_width, new_height), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "jpg"
    except Exception as e:
        print(f"  image compression failed, using original - {e}", file=sys.stderr)
        return None, None


def convert_video_to_gif(video_bytes):
    """Convert mp4 bytes to a compact animated GIF using ffmpeg's two-pass
    palette method. Returns gif bytes, or None if unavailable/failed.
    """
    if not FFMPEG_AVAILABLE:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "in.mp4")
        palette_path = os.path.join(tmp, "palette.png")
        out_path = os.path.join(tmp, "out.gif")

        with open(in_path, "wb") as f:
            f.write(video_bytes)

        vf_common = f"fps={GIF_FPS},scale={GIF_WIDTH}:-1:flags=lanczos"

        try:
            subprocess.run(
                ["ffmpeg", "-i", in_path, "-t", str(GIF_MAX_SECONDS),
                 "-vf", f"{vf_common},palettegen", "-y", palette_path],
                check=True, capture_output=True, timeout=60,
            )
            subprocess.run(
                ["ffmpeg", "-i", in_path, "-i", palette_path, "-t", str(GIF_MAX_SECONDS),
                 "-filter_complex", f"{vf_common}[x];[x][1:v]paletteuse",
                 "-y", out_path],
                check=True, capture_output=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"  ffmpeg gif conversion failed: {e}", file=sys.stderr)
            return None

        with open(out_path, "rb") as f:
            return f.read()


def classify_post(post):
    """Return 'photo', 'video', 'gif', or None (skip) for a 9gag post."""
    if post.get("nsfw", 0):
        return None

    post_type = post.get("type")
    if post_type == "Photo":
        return "photo"

    if post_type == "Animated":
        images = post.get("images", {})
        video_info = images.get("image460sv")
        if not video_info or not video_info.get("url"):
            return None
        # 9gag marks silent animations (hasAudio=0) as GIFs, with-sound ones
        # (hasAudio=1) as Videos.
        return "video" if video_info.get("hasAudio") else "gif"

    return None


def collect_candidates(per_section_target, sent_ids=frozenset()):
    """Gather SFW post metadata (no downloads yet), grouped by category.

    Pages through the hot feed until each category has at least
    `per_section_target` *new* (not already in sent_ids) candidates, or
    MAX_CANDIDATE_PAGES is reached.
    """
    buckets = {"photo": [], "video": [], "gif": []}
    seen_ids = set()
    skipped_already_sent = 0

    for posts in fetch_hot_pages():
        for post in posts:
            post_id = post.get("id")
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            if post_id in sent_ids:
                skipped_already_sent += 1
                continue

            category = classify_post(post)
            if category is None:
                continue

            images = post.get("images", {})
            thumb_url = (images.get("image700") or images.get("image460") or {}).get("url")
            if not thumb_url:
                continue

            video_url = None
            if category in ("video", "gif"):
                video_url = images["image460sv"]["url"]

            buckets[category].append({
                "id": post_id,
                "category": category,
                "title": post.get("title", "Untitled meme"),
                "votes": post.get("upVoteCount", 0),
                "thumb_url": thumb_url,
                "video_url": video_url,
                "post_url": post.get("url") or f"https://9gag.com/gag/{post_id}",
            })

        if all(len(buckets[cat]) >= per_section_target for cat in buckets):
            break

    if skipped_already_sent:
        print(f"  skipped {skipped_already_sent} already-sent post(s) from the feed")

    return buckets


def download_meme(candidate, rank, output_dir):
    """Download the media for this post, save it under output_dir, and
    return a meme dict with a `filename` (relative to output_dir) for the
    final hosted file — a compressed static image for photos, or an
    animated GIF preview (converted from the video) for video/gif posts.

    Returns None if even retries couldn't get a valid file for this post.
    """
    try:
        raw_bytes = fetch_media_bytes(candidate["thumb_url"], expect="image")
    except RuntimeError as e:
        print(f"  {candidate['category']} #{rank}: thumbnail failed, skipping - {e}", file=sys.stderr)
        return None

    safe_title = "".join(c if c.isalnum() else "_" for c in candidate["title"])
    safe_title = "_".join(filter(None, safe_title.split("_")))[:50]  # collapse repeats, trim
    base_name = f"{candidate['category']}_{rank:02d}_{candidate['votes']}_{safe_title or candidate['id']}"

    file_bytes, ext = compress_image(raw_bytes)
    if file_bytes is None:
        file_bytes, ext = raw_bytes, "jpg"

    has_gif_preview = False

    if candidate["video_url"]:
        try:
            video_bytes = fetch_media_bytes(candidate["video_url"], expect="video")
            gif_bytes = convert_video_to_gif(video_bytes)
            if gif_bytes:
                file_bytes, ext = gif_bytes, "gif"
                has_gif_preview = True
            else:
                print(f"  {candidate['category']} #{rank}: gif conversion failed, using static image")
        except RuntimeError as e:
            print(f"  {candidate['category']} #{rank}: video download failed, using static image - {e}", file=sys.stderr)

    filename = f"{base_name}.{ext}"
    with open(os.path.join(output_dir, filename), "wb") as f:
        f.write(file_bytes)

    time.sleep(REQUEST_DELAY_SECONDS)

    return {
        "id": candidate["id"],
        "rank": rank,
        "category": candidate["category"],
        "title": candidate["title"],
        "votes": candidate["votes"],
        "post_url": candidate["post_url"],
        "has_gif_preview": has_gif_preview,
        "filename": filename,
    }


def get_top_memes_by_section(per_section, output_dir, sent_ids=frozenset()):
    """Return {'photo': [...], 'video': [...], 'gif': [...]}, each the top
    `per_section` *new* posts of that category ranked by upvotes, media
    saved to output_dir. Candidates that fail to download validly (even
    after retries) are skipped in favor of the next-highest-voted one.
    Posts whose ID is already in sent_ids are excluded entirely.
    """
    buckets = collect_candidates(per_section, sent_ids=sent_ids)

    result = {}
    for category in ("photo", "gif", "video"):
        candidates = sorted(buckets[category], key=lambda c: c["votes"], reverse=True)
        memes = []
        rank = 1
        for candidate in candidates:
            if len(memes) >= per_section:
                break
            meme = download_meme(candidate, rank, output_dir)
            if meme is None:
                continue
            memes.append(meme)
            tag = "gif preview" if meme["has_gif_preview"] else "image"
            print(f"  {category} #{rank} [{tag}]: {meme['title']} ({meme['votes']} upvotes)")
            rank += 1
        result[category] = memes

    return result


def build_section_html(title, emoji, memes, columns, image_base_url):
    if not memes:
        return f"""
    <h2 style="color:#222; font-family:Arial,Helvetica,sans-serif;">{emoji} New {title}</h2>
    <p style="color:#999; font-size:13px; font-family:Arial,Helvetica,sans-serif;">No new qualifying posts since the last check.</p>"""

    cards = []
    for m in memes:
        title_esc = escape(m["title"])
        img_url = f"{image_base_url}/{quote(m['filename'])}"
        play_badge = (
            '<span style="position:absolute; top:6px; right:6px; background:rgba(0,0,0,0.65); '
            'color:#fff; font-size:12px; padding:2px 7px; border-radius:12px;">&#9654; GIF</span>'
            if m["has_gif_preview"] else ""
        )
        note = (
            '<div style="font-size:11px; color:#4a90d9; margin-top:4px;">Animated preview</div>'
            if m["has_gif_preview"] else ""
        )
        cards.append(f"""
        <td style="padding:8px; vertical-align:top; width:{100 // columns}%;">
          <a href="{escape(m['post_url'])}" style="text-decoration:none; color:inherit;">
            <div style="border:1px solid #e0e0e0; border-radius:10px; overflow:hidden; font-family:Arial,Helvetica,sans-serif;">
              <div style="position:relative;">
                <img src="{escape(img_url)}" alt="{title_esc}" style="display:block; width:100%; height:auto; max-height:500px; object-fit:contain; background:#f5f5f5;">
                <span style="position:absolute; top:6px; left:6px; background:rgba(0,0,0,0.65); color:#fff; font-size:12px; padding:2px 7px; border-radius:12px;">#{m['rank']}</span>
                {play_badge}
              </div>
              <div style="padding:10px;">
                <div style="font-size:13px; color:#222; line-height:1.35; max-height:52px; overflow:hidden;">{title_esc}</div>
                <div style="font-size:12px; color:#888; margin-top:6px;">&#9650; {m['votes']:,} upvotes</div>
                {note}
              </div>
            </div>
          </a>
        </td>""")

    rows = []
    for i in range(0, len(cards), columns):
        row_cards = cards[i:i + columns]
        row_cards += ["<td></td>"] * (columns - len(row_cards))
        rows.append(f"<tr>{''.join(row_cards)}</tr>")

    return f"""
    <h2 style="color:#222; font-family:Arial,Helvetica,sans-serif;">{emoji} {len(memes)} New {title}</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:900px;">
      {''.join(rows)}
    </table>"""


def build_html(sections, columns, image_base_url):
    body_parts = [
        build_section_html(title, emoji, sections[key], columns, image_base_url)
        for key, title, emoji in SECTIONS
    ]
    total = sum(len(sections[k]) for k, _, _ in SECTIONS)
    return f"""\
<html>
  <body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
    <h1 style="color:#222;">{total} New Memes from 9gag</h1>
    {''.join(body_parts)}
    <p style="color:#999; font-size:12px; margin-top:20px;">Ranked by upvotes within each category on 9gag's hot feed. Posts already sent in a previous run are excluded. Tap any card to view the original post. GIFs animate automatically in Gmail and most mail apps; some desktop clients like Outlook only show the first frame.</p>
  </body>
</html>"""


def all_memes(sections):
    for key, _, _ in SECTIONS:
        yield from sections[key]


def build_plain_text(sections):
    lines = []
    for key, title, emoji in SECTIONS:
        memes = sections[key]
        lines.append(f"--- {len(memes)} New {title} ---")
        for m in memes:
            tag = " [animated]" if m["has_gif_preview"] else ""
            lines.append(f"#{m['rank']} - {m['title']} ({m['votes']} upvotes){tag} - {m['post_url']}")
        lines.append("")
    return "\n".join(lines)


def resolve_image_base_url():
    branch = os.environ.get("IMAGE_BRANCH", "meme-assets")
    repo = os.environ.get("GITHUB_REPOSITORY")  # auto-set inside GitHub Actions as "owner/repo"
    if not repo:
        raise RuntimeError(
            "GITHUB_REPOSITORY is not set. Set it manually as 'owner/repo' when running "
            "outside GitHub Actions, or rely on the workflow which sets it automatically."
        )
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{PREVIEWS_DIR}"


def cmd_generate():
    per_section = int(os.environ.get("MEMES_PER_SECTION", "30"))
    columns = int(os.environ.get("GRID_COLUMNS", "3"))

    print(f"ffmpeg available: {FFMPEG_AVAILABLE}"
          + ("" if FFMPEG_AVAILABLE else " -- video/gif posts will fall back to a static thumbnail!"))

    if os.path.exists(PREVIEWS_DIR):
        shutil.rmtree(PREVIEWS_DIR)
    os.makedirs(PREVIEWS_DIR)
    if os.path.exists(EMAIL_DIR):
        shutil.rmtree(EMAIL_DIR)
    os.makedirs(EMAIL_DIR)

    sent_ids_list, sent_ids_set = load_sent_ids()
    print(f"Loaded {len(sent_ids_set)} previously-sent post ID(s) from {SENT_IDS_FILE}")

    print(f"Fetching up to {per_section} new per section (image/video/gif)...")
    sections = get_top_memes_by_section(per_section, PREVIEWS_DIR, sent_ids=sent_ids_set)

    total = sum(len(sections[k]) for k, _, _ in SECTIONS)
    if total == 0:
        print("No new memes found since last run.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"total": 0}, f)
        # Nothing new was sent, so the dedup state doesn't need updating.
        return

    animated_count = sum(1 for m in all_memes(sections) if m["has_gif_preview"])
    animatable_count = len(sections["video"]) + len(sections["gif"])
    print(f"Animated previews: {animated_count}/{animatable_count} video+gif posts converted successfully.")
    if animatable_count and animated_count < animatable_count:
        print("Some video/gif posts fell back to a static thumbnail — check ffmpeg errors above.", file=sys.stderr)

    image_base_url = resolve_image_base_url()

    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    timestamp = now.strftime("%b %d, %Y %I:%M %p")

    subject = f"{total} new memes - {timestamp}"
    html = build_html(sections, columns, image_base_url)
    text = build_plain_text(sections)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"total": total}, f)

    # Only mark posts as "sent" once they're actually written into the
    # composed email above — if `send` later fails, they'll be retried
    # next run rather than silently dropped. (This is a reasonable
    # tradeoff: worst case is an occasional repeat, not a lost meme.)
    new_ids = [m["id"] for m in all_memes(sections)]
    save_sent_ids(sent_ids_list + new_ids)
    print(f"Updated dedup state: {len(sent_ids_list) + len(new_ids)} tracked ID(s) (cap {SENT_ID_CAP}) -> {SENT_IDS_FILE}")

    print(f"Generated {total} new meme(s). Images saved to ./{PREVIEWS_DIR}/, email saved to ./{EMAIL_DIR}/")


def cmd_send():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("MEME_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("MEME_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    with open(os.path.join(EMAIL_DIR, "meta.json")) as f:
        meta = json.load(f)
    if meta.get("total", 0) == 0:
        print("No new memes were found in the generate step — not sending an email.")
        return

    with open(os.path.join(EMAIL_DIR, "subject.txt")) as f:
        subject = f.read()
    with open(os.path.join(EMAIL_DIR, "body.html")) as f:
        html = f.read()
    with open(os.path.join(EMAIL_DIR, "body.txt")) as f:
        text = f.read()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    print(f"Sent to {recipient}!")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("generate", "send"):
        print("Usage: python 9gag_top_meme_emailer.py [generate|send]", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "generate":
        cmd_generate()
    else:
        cmd_send()


if __name__ == "__main__":
    main()
