#!/usr/bin/env python3
"""
9gag Top Memes of the Day -> Email (HTML digest, 3 sections)

Fetches posts from 9gag's "hot" feed and emails a digest split into three
sections — Static Images, Videos, and GIFs — each showing its own top
MEMES_PER_SECTION (default 30) posts ranked by upvote count (90 total by
default). Everything displays inline in the email body — compressed static
images, and animated GIF previews (converted from the original video) for
gif/video posts — with no file attachments. Each card links back to the
original 9gag post.

9gag classifies "Animated" posts as either a GIF or a Video based on
whether the file has audio (hasAudio flag) — silent = GIF, with sound = Video.

SETUP
-----
1. Install dependency:
       pip install requests

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
       - Go to https://myaccount.google.com/apppasswords
       - You need 2-Step Verification turned on first.
       - Create an app password for "Mail" and copy the 16-character code.

3. Set these as environment variables (recommended, keeps secrets out of the file):
       export GMAIL_ADDRESS="youraddress@gmail.com"
       export GMAIL_APP_PASSWORD="16-char-app-password"
       export MEME_RECIPIENT="where-to-send@example.com"
       export MEMES_PER_SECTION="30"       # optional, top N per section (image/video/gif)
       export GRID_COLUMNS="3"             # optional, cards per row in the email
       export TIMEZONE="Asia/Ho_Chi_Minh"  # optional, used for the subject line's date/time

4. Run it:
       python 9gag_top_meme_emailer.py

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this daily
in the cloud without needing your own computer on.
"""

import io
import os
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests
from PIL import Image

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
# enough pages to find MEMES_PER_SECTION posts in each of the 3 categories,
# and videos/gifs are much rarer than photos in the hot feed, so this needs
# to be fairly generous. Each page is ~10 posts.
MAX_CANDIDATE_PAGES = 60

# Gmail's hard cap is ~25MB per message. Leave headroom for MIME/base64
# overhead (base64 inflates size by ~33%) and stop adding full videos once
# we're getting close, falling back to preview/thumbnail-only for the rest.
MAX_EMAIL_BYTES = 20 * 1024 * 1024

# Tuning for the inline animated-GIF preview generated from each video/gif.
# Keeps file size reasonable while still looking good in an email.
GIF_MAX_SECONDS = 6
GIF_WIDTH = 380
GIF_FPS = 10
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

# Media download reliability tuning. 9gag's CDN occasionally rate-limits or
# hotlink-blocks a fraction of rapid-fire requests, returning a small error
# response with a 200 status instead of the real file — this shows up as a
# broken image in the email. We retry those and add a small delay between
# requests to reduce how often it happens in the first place.
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = 1.5
MIN_VALID_IMAGE_BYTES = 500
REQUEST_DELAY_SECONDS = 0.3

# Static photo thumbnails are shown inline AND the original is attached
# separately below — embedding the same full-resolution file twice for up
# to 90 items is a lot of redundant weight and can make Gmail struggle to
# render everything. The inline copy is resized/recompressed to stay light;
# the full-resolution original is still what gets attached as a file.
INLINE_IMAGE_MAX_WIDTH = 480
INLINE_IMAGE_JPEG_QUALITY = 78

SECTIONS = [
    ("photo", "Static Images", "\U0001F5BC"),
    ("video", "Videos", "\U0001F3A5"),
    ("gif", "GIFs", "\U0001F39E"),
]


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


def compress_image_for_inline(image_bytes, max_width=INLINE_IMAGE_MAX_WIDTH, quality=INLINE_IMAGE_JPEG_QUALITY):
    """Resize + recompress an image for lightweight inline display.
    Returns (jpeg_bytes, 'jpeg'), or (original_bytes, None) if it fails
    (caller should fall back to the original bytes/subtype in that case).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")  # flattens transparency, normalizes mode
        if img.width > max_width:
            new_height = int(img.height * (max_width / img.width))
            img = img.resize((max_width, new_height), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "jpeg"
    except Exception as e:
        print(f"  inline compression failed, using original - {e}", file=sys.stderr)
        return None, None


def convert_video_to_gif(video_bytes):
    """Convert mp4 bytes to a compact animated GIF using ffmpeg's two-pass
    palette method (much smaller/better quality than a naive conversion).
    Returns gif bytes, or None if ffmpeg isn't available or conversion fails.
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


def collect_candidates(per_section_target):
    """Gather SFW post metadata (no downloads yet), grouped by category.

    Pages through the hot feed until each category has at least
    `per_section_target` candidates, or MAX_CANDIDATE_PAGES is reached.
    """
    buckets = {"photo": [], "video": [], "gif": []}
    seen_ids = set()

    for posts in fetch_hot_pages():
        for post in posts:
            post_id = post.get("id")
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

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

    return buckets


def download_meme(candidate, rank, running_total_bytes):
    """Download and prepare the inline image for this post — a compressed
    static image for photos, or an animated GIF preview (converted from the
    video) for video/gif posts. Nothing is kept for a file attachment;
    everything just displays in the email body.

    Returns (meme_dict_or_None, new_running_total_bytes). Returns None for
    the meme if even retries couldn't get a valid thumbnail — the caller
    should skip this candidate rather than embed a broken image.
    """
    try:
        raw_thumb_bytes = fetch_media_bytes(candidate["thumb_url"], expect="image")
    except RuntimeError as e:
        print(f"  {candidate['category']} #{rank}: thumbnail failed, skipping - {e}", file=sys.stderr)
        return None, running_total_bytes

    thumb_ext = candidate["thumb_url"].split(".")[-1].split("?")[0]
    thumb_subtype = "jpeg" if thumb_ext == "jpg" else thumb_ext
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in candidate["title"])[:50].strip()
    base_name = f"{candidate['category']}_{rank:02d}_{candidate['votes']}_{safe_title or candidate['id']}"

    # Compress the static image before embedding it inline — full-resolution
    # memes add up fast across up to 90 items.
    compressed_bytes, compressed_subtype = compress_image_for_inline(raw_thumb_bytes)
    if compressed_bytes:
        inline_bytes, inline_subtype = compressed_bytes, compressed_subtype
    else:
        inline_bytes, inline_subtype = raw_thumb_bytes, thumb_subtype

    meme = {
        "rank": rank,
        "category": candidate["category"],
        "title": candidate["title"],
        "votes": candidate["votes"],
        "post_url": candidate["post_url"],
        "has_gif_preview": False,
        "inline_bytes": inline_bytes,
        "inline_subtype": inline_subtype,
        "cid": f"{base_name}_cid",
    }

    running_total_bytes += len(inline_bytes)

    if candidate["video_url"] and running_total_bytes < MAX_EMAIL_BYTES:
        try:
            video_bytes = fetch_media_bytes(candidate["video_url"], expect="video")

            # Animated GIF preview so it plays natively inline in the email
            # (mp4 can't autoplay inline in most mail clients). The mp4 itself
            # is discarded after conversion — nothing is attached.
            gif_bytes = convert_video_to_gif(video_bytes)
            if gif_bytes:
                meme["inline_bytes"] = gif_bytes
                meme["inline_subtype"] = "gif"
                meme["has_gif_preview"] = True
                running_total_bytes += len(gif_bytes) - len(inline_bytes)
            else:
                print(f"  {candidate['category']} #{rank}: gif conversion failed, using static thumbnail")
        except RuntimeError as e:
            print(f"  {candidate['category']} #{rank}: video download failed, using static thumbnail - {e}", file=sys.stderr)
    elif candidate["video_url"]:
        print(f"  {candidate['category']} #{rank}: skipping video->gif conversion (size budget)")

    time.sleep(REQUEST_DELAY_SECONDS)
    return meme, running_total_bytes


def get_top_memes_by_section(per_section):
    """Return {'photo': [...], 'video': [...], 'gif': [...]}, each the top
    `per_section` posts of that category ranked by upvotes, media downloaded.

    Processing order is photo -> gif -> video, so if the size budget runs
    tight, it's the heaviest items (full videos) that degrade first, not
    the cheap static images. Candidates that fail to download validly (even
    after retries) are skipped in favor of the next-highest-voted one, so
    a section still fills up to `per_section` whenever enough candidates exist.
    """
    buckets = collect_candidates(per_section)

    result = {}
    running_total_bytes = 0
    for category in ("photo", "gif", "video"):
        candidates = sorted(buckets[category], key=lambda c: c["votes"], reverse=True)
        memes = []
        rank = 1
        for candidate in candidates:
            if len(memes) >= per_section:
                break
            meme, running_total_bytes = download_meme(candidate, rank, running_total_bytes)
            if meme is None:
                continue  # thumbnail failed even after retries — try the next candidate
            memes.append(meme)
            tag = "gif preview" if meme["has_gif_preview"] else "image"
            print(f"  {category} #{rank} [{tag}]: {meme['title']} ({meme['votes']} upvotes)")
            rank += 1
        result[category] = memes

    return result


def build_section_html(section_key, title, emoji, memes, columns):
    if not memes:
        return f"""
    <h2 style="color:#222; font-family:Arial,Helvetica,sans-serif;">{emoji} Top {title}</h2>
    <p style="color:#999; font-size:13px; font-family:Arial,Helvetica,sans-serif;">No qualifying posts found today.</p>"""

    cards = []
    for m in memes:
        title_esc = escape(m["title"])
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
                <img src="cid:{m['cid']}" alt="{title_esc}" style="display:block; width:100%; height:auto; max-height:500px; object-fit:contain; background:#f5f5f5;">
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
    <h2 style="color:#222; font-family:Arial,Helvetica,sans-serif;">{emoji} Top {len(memes)} {title}</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:900px;">
      {''.join(rows)}
    </table>"""


def build_html(sections, columns):
    body_parts = [
        build_section_html(key, title, emoji, sections[key], columns)
        for key, title, emoji in SECTIONS
    ]
    total = sum(len(sections[k]) for k, _, _ in SECTIONS)
    return f"""\
<html>
  <body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
    <h1 style="color:#222;">Today's Top {total} Memes from 9gag</h1>
    {''.join(body_parts)}
    <p style="color:#999; font-size:12px; margin-top:20px;">Ranked by upvotes within each category on 9gag's hot feed. Tap any card to view the original post. GIFs animate automatically in Gmail and most mail apps; some desktop clients like Outlook only show the first frame.</p>
  </body>
</html>"""


def build_plain_text(sections):
    lines = []
    for key, title, emoji in SECTIONS:
        memes = sections[key]
        lines.append(f"--- Top {len(memes)} {title} ---")
        for m in memes:
            tag = " [animated]" if m["has_gif_preview"] else ""
            lines.append(f"#{m['rank']} - {m['title']} ({m['votes']} upvotes){tag} - {m['post_url']}")
        lines.append("")
    return "\n".join(lines)


def all_memes(sections):
    for key, _, _ in SECTIONS:
        yield from sections[key]


def send_email(subject, sections, columns, sender, app_password, recipient):
    # No attachments needed anymore, so multipart/related (HTML + its inline
    # cid images) can be the top-level container.
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    msg_alt = MIMEMultipart("alternative")
    msg.attach(msg_alt)

    msg_alt.attach(MIMEText(build_plain_text(sections), "plain"))
    msg_alt.attach(MIMEText(build_html(sections, columns), "html"))

    for m in all_memes(sections):
        img = MIMEImage(m["inline_bytes"], _subtype=m["inline_subtype"])
        img.add_header("Content-ID", f"<{m['cid']}>")
        img.add_header("Content-Disposition", "inline")
        msg.attach(img)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)


def main():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("MEME_RECIPIENT")
    per_section = int(os.environ.get("MEMES_PER_SECTION", "30"))
    columns = int(os.environ.get("GRID_COLUMNS", "3"))

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("MEME_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching top {per_section} per section (image/video/gif)...")
    sections = get_top_memes_by_section(per_section)

    total = sum(len(sections[k]) for k, _, _ in SECTIONS)
    if total == 0:
        print("No memes found — not sending an email.")
        return

    print(f"Found {total} meme(s) total. Emailing to {recipient}...")

    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    timestamp = now.strftime("%b %d, %Y %I:%M %p")

    send_email(
        subject=f"Top {total} memes of the day - {timestamp}",
        sections=sections,
        columns=columns,
        sender=sender,
        app_password=app_password,
        recipient=recipient,
    )
    print("Sent!")


if __name__ == "__main__":
    main()
