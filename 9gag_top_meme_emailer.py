#!/usr/bin/env python3
"""
9gag Top Memes of the Day -> Email (HTML digest)

Fetches posts from 9gag's "hot" feed, ranks them by upvote count, and emails
the top MAX_MEMES of them (default 30) as an HTML grid/gallery digest —
thumbnails shown inline in the email body, with the full original file
attached below (an .mp4 for gifs/videos, the image itself for photos), each
card linking back to the original 9gag post.

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
       export MAX_MEMES="30"      # optional, how many top memes to send
       export GRID_COLUMNS="3"    # optional, cards per row in the email
       export TIMEZONE="Asia/Ho_Chi_Minh"  # optional, used for the subject line's date/time

4. Run it:
       python 9gag_top_meme_emailer.py

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this daily
in the cloud without needing your own computer on.
"""

import os
import smtplib
import ssl
import sys
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

# How many feed pages to scan when building the candidate pool. The hot feed
# is ranked by a "hot score" (not pure vote count), so we gather a decent
# pool of candidates before picking the highest-voted ones out of it.
CANDIDATE_PAGES = 15

# Gmail's hard cap is ~25MB per message. Leave headroom for MIME/base64
# overhead (base64 inflates size by ~33%) and stop adding videos once we're
# getting close, falling back to thumbnail-only for the rest.
MAX_EMAIL_BYTES = 20 * 1024 * 1024


def fetch_hot_pages(max_pages=CANDIDATE_PAGES):
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


def collect_candidates():
    """Gather SFW image/gif/video post metadata (no downloads yet) from the hot feed."""
    candidates = []
    seen_ids = set()

    for posts in fetch_hot_pages():
        for post in posts:
            post_id = post.get("id")
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            if post.get("nsfw", 0):
                continue

            post_type = post.get("type")
            if post_type not in ("Photo", "Animated"):
                continue

            images = post.get("images", {})
            # Static thumbnail — always available, used for the inline grid image.
            thumb_url = (images.get("image700") or images.get("image460") or {}).get("url")
            if not thumb_url:
                continue

            # Animated posts (gifs/videos) additionally have a real video file.
            video_url = None
            if post_type == "Animated":
                video_info = images.get("image460sv")
                if video_info and video_info.get("url"):
                    video_url = video_info["url"]

            candidates.append({
                "id": post_id,
                "title": post.get("title", "Untitled meme"),
                "votes": post.get("upVoteCount", 0),
                "thumb_url": thumb_url,
                "video_url": video_url,
                "is_video": video_url is not None,
                "post_url": post.get("url") or f"https://9gag.com/gag/{post_id}",
            })

    return candidates


def download_meme(candidate, rank, running_total_bytes):
    """Download the thumbnail (always) and the full video (if present and size allows).

    Returns (meme_dict, new_running_total_bytes).
    """
    thumb_resp = requests.get(candidate["thumb_url"], headers=HEADERS, timeout=15)
    thumb_resp.raise_for_status()
    thumb_bytes = thumb_resp.content

    thumb_ext = candidate["thumb_url"].split(".")[-1].split("?")[0]
    thumb_subtype = "jpeg" if thumb_ext == "jpg" else thumb_ext
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in candidate["title"])[:50].strip()
    base_name = f"{rank:02d}_{candidate['votes']}_{safe_title or candidate['id']}"

    meme = {
        "rank": rank,
        "title": candidate["title"],
        "votes": candidate["votes"],
        "post_url": candidate["post_url"],
        "is_video": False,
        "thumb_bytes": thumb_bytes,
        "thumb_subtype": thumb_subtype,
        "thumb_filename": f"{base_name}.{thumb_ext}",
        "cid": f"meme{rank}",
        "video_bytes": None,
        "video_filename": None,
    }

    running_total_bytes += len(thumb_bytes)

    if candidate["is_video"]:
        # Only attempt the video if we still have headroom in the size budget.
        if running_total_bytes < MAX_EMAIL_BYTES:
            try:
                video_resp = requests.get(candidate["video_url"], headers=HEADERS, timeout=30)
                video_resp.raise_for_status()
                video_bytes = video_resp.content
                if running_total_bytes + len(video_bytes) < MAX_EMAIL_BYTES:
                    meme["is_video"] = True
                    meme["video_bytes"] = video_bytes
                    meme["video_filename"] = f"{base_name}.mp4"
                    running_total_bytes += len(video_bytes)
                else:
                    print(f"  #{rank}: skipping video attachment (size budget) - {candidate['title']}")
            except requests.RequestException as e:
                print(f"  #{rank}: video download failed ({e}), using thumbnail only", file=sys.stderr)
        else:
            print(f"  #{rank}: skipping video attachment (size budget) - {candidate['title']}")

    return meme, running_total_bytes


def get_top_memes(max_memes):
    """Return the top `max_memes` hot posts, ranked by upvote count, with media downloaded."""
    candidates = collect_candidates()
    if not candidates:
        return []

    candidates.sort(key=lambda c: c["votes"], reverse=True)
    top_candidates = candidates[:max_memes]

    memes = []
    running_total_bytes = 0
    for rank, candidate in enumerate(top_candidates, start=1):
        meme, running_total_bytes = download_meme(candidate, rank, running_total_bytes)
        memes.append(meme)
        tag = "video" if meme["is_video"] else "image"
        print(f"  #{rank} [{tag}]: {meme['title']} ({meme['votes']} upvotes)")

    return memes


def build_html(memes, columns):
    """Build an HTML grid/gallery digest, images referenced via cid: for inline display."""
    cards = []
    for m in memes:
        title = escape(m["title"])
        play_badge = (
            '<span style="position:absolute; top:6px; right:6px; background:rgba(0,0,0,0.65); '
            'color:#fff; font-size:12px; padding:2px 7px; border-radius:12px;">&#9654; GIF/Video</span>'
            if m["is_video"] else ""
        )
        note = (
            '<div style="font-size:11px; color:#4a90d9; margin-top:4px;">Full video attached below</div>'
            if m["is_video"] else ""
        )
        cards.append(f"""
        <td style="padding:8px; vertical-align:top; width:{100 // columns}%;">
          <a href="{escape(m['post_url'])}" style="text-decoration:none; color:inherit;">
            <div style="border:1px solid #e0e0e0; border-radius:10px; overflow:hidden; font-family:Arial,Helvetica,sans-serif;">
              <div style="position:relative;">
                <img src="cid:{m['cid']}" alt="{title}" style="display:block; width:100%; height:auto; max-height:500px; object-fit:contain; background:#f5f5f5;">
                <span style="position:absolute; top:6px; left:6px; background:rgba(0,0,0,0.65); color:#fff; font-size:12px; padding:2px 7px; border-radius:12px;">#{m['rank']}</span>
                {play_badge}
              </div>
              <div style="padding:10px;">
                <div style="font-size:13px; color:#222; line-height:1.35; max-height:52px; overflow:hidden;">{title}</div>
                <div style="font-size:12px; color:#888; margin-top:6px;">&#9650; {m['votes']:,} upvotes</div>
                {note}
              </div>
            </div>
          </a>
        </td>""")

    # group cards into rows of `columns`
    rows = []
    for i in range(0, len(cards), columns):
        row_cards = cards[i:i + columns]
        # pad the last row so columns stay aligned
        row_cards += ["<td></td>"] * (columns - len(row_cards))
        rows.append(f"<tr>{''.join(row_cards)}</tr>")

    return f"""\
<html>
  <body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
    <h2 style="color:#222;">Today's Top {len(memes)} Memes from 9gag</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:900px;">
      {''.join(rows)}
    </table>
    <p style="color:#999; font-size:12px; margin-top:20px;">Ranked by upvotes on 9gag's hot feed. Tap any card to view the original post.</p>
  </body>
</html>"""


def build_plain_text(memes):
    lines = [f"Today's top {len(memes)} memes from 9gag (ranked by upvotes):", ""]
    for m in memes:
        tag = " [video attached]" if m["is_video"] else ""
        lines.append(f"#{m['rank']} - {m['title']} ({m['votes']} upvotes){tag} - {m['post_url']}")
    return "\n".join(lines)


def send_email(subject, memes, columns, sender, app_password, recipient):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    # multipart/related holds the HTML body + its inline (cid) thumbnail images
    msg_related = MIMEMultipart("related")
    msg.attach(msg_related)

    msg_alt = MIMEMultipart("alternative")
    msg_related.attach(msg_alt)

    msg_alt.attach(MIMEText(build_plain_text(memes), "plain"))
    msg_alt.attach(MIMEText(build_html(memes, columns), "html"))

    # inline thumbnail copies (referenced by the HTML via cid:) — always images,
    # since email clients can't reliably autoplay inline <video>.
    for m in memes:
        img = MIMEImage(m["thumb_bytes"], _subtype=m["thumb_subtype"])
        img.add_header("Content-ID", f"<{m['cid']}>")
        img.add_header("Content-Disposition", "inline", filename=m["thumb_filename"])
        msg_related.attach(img)

    # regular file attachments: full mp4 for videos, static image otherwise
    for m in memes:
        if m["is_video"]:
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
    max_memes = int(os.environ.get("MAX_MEMES", "30"))
    columns = int(os.environ.get("GRID_COLUMNS", "3"))

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("MEME_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching top {max_memes} hot memes by upvotes...")
    memes = get_top_memes(max_memes)

    if not memes:
        print("No memes found — not sending an email.")
        return

    print(f"Found {len(memes)} meme(s). Emailing to {recipient}...")

    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    timestamp = now.strftime("%b %d, %Y %I:%M %p")

    send_email(
        subject=f"Top {len(memes)} memes of the day - {timestamp}",
        memes=memes,
        columns=columns,
        sender=sender,
        app_password=app_password,
        recipient=recipient,
    )
    print("Sent!")


if __name__ == "__main__":
    main()
