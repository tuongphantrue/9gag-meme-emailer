#!/usr/bin/env python3
"""
9gag Top Memes of the Day -> Email (HTML digest, 3 sections)

Fetches posts from 9gag's "hot" feed and emails a digest split into three
sections — Static Images, Videos, and GIFs — each showing its own top
MEMES_PER_SECTION (default 30) posts ranked by upvote count (90 total by
default). Thumbnails/animated previews are shown inline in the email body;
the original file is attached below each card (image, mp4, or gif).

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

import os
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests

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

SECTIONS = [
    ("photo", "Static Images", "\U0001F5BC"),
    ("video", "Videos", "\U0001F3A5"),
    ("gif", "GIFs", "\U0001F39E"),
]


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
    """Download the thumbnail (always) and, for video/gif posts, build an
    animated GIF preview + attach the full original video (size permitting).

    Returns (meme_dict, new_running_total_bytes).
    """
    thumb_resp = requests.get(candidate["thumb_url"], headers=HEADERS, timeout=15)
    thumb_resp.raise_for_status()
    thumb_bytes = thumb_resp.content

    thumb_ext = candidate["thumb_url"].split(".")[-1].split("?")[0]
    thumb_subtype = "jpeg" if thumb_ext == "jpg" else thumb_ext
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in candidate["title"])[:50].strip()
    base_name = f"{candidate['category']}_{rank:02d}_{candidate['votes']}_{safe_title or candidate['id']}"

    meme = {
        "rank": rank,
        "category": candidate["category"],
        "title": candidate["title"],
        "votes": candidate["votes"],
        "post_url": candidate["post_url"],
        "has_full_video": False,
        "has_gif_preview": False,
        "thumb_bytes": thumb_bytes,
        "thumb_subtype": thumb_subtype,
        "thumb_filename": f"{base_name}.{thumb_ext}",
        "cid": f"{base_name}_cid",
        "video_bytes": None,
        "video_filename": None,
    }

    running_total_bytes += len(thumb_bytes)

    if candidate["video_url"]:
        if running_total_bytes < MAX_EMAIL_BYTES:
            try:
                video_resp = requests.get(candidate["video_url"], headers=HEADERS, timeout=30)
                video_resp.raise_for_status()
                video_bytes = video_resp.content

                # Animated GIF preview so it plays natively inline in the email
                # (mp4 can't autoplay inline in most mail clients).
                gif_bytes = convert_video_to_gif(video_bytes)
                if gif_bytes:
                    meme["thumb_bytes"] = gif_bytes
                    meme["thumb_subtype"] = "gif"
                    meme["thumb_filename"] = f"{base_name}.gif"
                    meme["has_gif_preview"] = True
                    running_total_bytes += len(gif_bytes) - len(thumb_bytes)

                if running_total_bytes + len(video_bytes) < MAX_EMAIL_BYTES:
                    meme["has_full_video"] = True
                    meme["video_bytes"] = video_bytes
                    meme["video_filename"] = f"{base_name}.mp4"
                    running_total_bytes += len(video_bytes)
                else:
                    print(f"  {candidate['category']} #{rank}: skipping full video attachment (size budget)")
            except requests.RequestException as e:
                print(f"  {candidate['category']} #{rank}: video download failed ({e})", file=sys.stderr)
        else:
            print(f"  {candidate['category']} #{rank}: skipping video (size budget)")

    return meme, running_total_bytes


def get_top_memes_by_section(per_section):
    """Return {'photo': [...], 'video': [...], 'gif': [...]}, each the top
    `per_section` posts of that category ranked by upvotes, media downloaded.

    Processing order is photo -> gif -> video, so if the size budget runs
    tight, it's the heaviest items (full videos) that degrade first, not
    the cheap static images.
    """
    buckets = collect_candidates(per_section)

    result = {}
    running_total_bytes = 0
    for category in ("photo", "gif", "video"):
        candidates = sorted(buckets[category], key=lambda c: c["votes"], reverse=True)[:per_section]
        memes = []
        for rank, candidate in enumerate(candidates, start=1):
            meme, running_total_bytes = download_meme(candidate, rank, running_total_bytes)
            memes.append(meme)
            tag = "gif preview" if meme["has_gif_preview"] else "image"
            print(f"  {category} #{rank} [{tag}]: {meme['title']} ({meme['votes']} upvotes)")
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
        if m["has_full_video"]:
            note = '<div style="font-size:11px; color:#4a90d9; margin-top:4px;">Full-length video attached below</div>'
        elif m["has_gif_preview"]:
            note = '<div style="font-size:11px; color:#4a90d9; margin-top:4px;">Preview only (original too large to attach)</div>'
        else:
            note = ""
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
    <p style="color:#999; font-size:12px; margin-top:20px;">Ranked by upvotes within each category on 9gag's hot feed. Tap any card to view the original post. GIFs animate automatically in most mail apps (Gmail, Apple Mail); some desktop clients like Outlook only show the first frame — the full video is attached below in that case.</p>
  </body>
</html>"""


def build_plain_text(sections):
    lines = []
    for key, title, emoji in SECTIONS:
        memes = sections[key]
        lines.append(f"--- Top {len(memes)} {title} ---")
        for m in memes:
            if m["has_full_video"]:
                tag = " [animated, full video attached]"
            elif m["has_gif_preview"]:
                tag = " [animated preview only]"
            else:
                tag = ""
            lines.append(f"#{m['rank']} - {m['title']} ({m['votes']} upvotes){tag} - {m['post_url']}")
        lines.append("")
    return "\n".join(lines)


def all_memes(sections):
    for key, _, _ in SECTIONS:
        yield from sections[key]


def send_email(subject, sections, columns, sender, app_password, recipient):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    msg_related = MIMEMultipart("related")
    msg.attach(msg_related)

    msg_alt = MIMEMultipart("alternative")
    msg_related.attach(msg_alt)

    msg_alt.attach(MIMEText(build_plain_text(sections), "plain"))
    msg_alt.attach(MIMEText(build_html(sections, columns), "html"))

    for m in all_memes(sections):
        img = MIMEImage(m["thumb_bytes"], _subtype=m["thumb_subtype"])
        img.add_header("Content-ID", f"<{m['cid']}>")
        img.add_header("Content-Disposition", "inline", filename=m["thumb_filename"])
        msg_related.attach(img)

    for m in all_memes(sections):
        if m["has_full_video"]:
            video = MIMEBase("video", "mp4")
            video.set_payload(m["video_bytes"])
            encoders.encode_base64(video)
            video.add_header("Content-Disposition", "attachment", filename=m["video_filename"])
            msg.attach(video)
        else:
            img = MIMEImage(m["thumb_bytes"], _subtype=m["thumb_subtype"])
            img.add_header("Content-Disposition", "attachment", filename=m["thumb_filename"])
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
