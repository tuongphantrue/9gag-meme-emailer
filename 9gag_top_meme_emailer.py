#!/usr/bin/env python3
"""
9gag Top Memes of the Day -> Email (HTML digest)

Fetches posts from 9gag's "hot" feed, ranks them by upvote count, and emails
the top MAX_MEMES of them (default 30) as an HTML grid/gallery digest —
images shown inline in the email body AND attached as files, each linking
back to the original 9gag post.

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
    """Gather SFW image/gif post metadata (no image download yet) from the hot feed."""
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
            if post.get("type") not in ("Photo", "Animated"):
                continue

            candidates.append({
                "id": post_id,
                "title": post.get("title", "Untitled meme"),
                "votes": post.get("upVoteCount", 0),
                "image_url": post["images"]["image700"]["url"],
                "post_url": post.get("url") or f"https://9gag.com/gag/{post_id}",
            })

    return candidates


def download_meme(candidate, rank):
    """Download the image and return a dict with everything needed for the email."""
    img_resp = requests.get(candidate["image_url"], headers=HEADERS, timeout=15)
    img_resp.raise_for_status()

    ext = candidate["image_url"].split(".")[-1].split("?")[0]
    subtype = "jpeg" if ext == "jpg" else ext
    safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in candidate["title"])[:50].strip()
    filename = f"{rank:02d}_{candidate['votes']}_{safe_title or candidate['id']}.{ext}"

    return {
        "rank": rank,
        "title": candidate["title"],
        "votes": candidate["votes"],
        "post_url": candidate["post_url"],
        "image_bytes": img_resp.content,
        "subtype": subtype,
        "filename": filename,
        "cid": f"meme{rank}",
    }


def get_top_memes(max_memes):
    """Return the top `max_memes` hot posts, ranked by upvote count, with images downloaded."""
    candidates = collect_candidates()
    if not candidates:
        return []

    candidates.sort(key=lambda c: c["votes"], reverse=True)
    top_candidates = candidates[:max_memes]

    memes = []
    for rank, candidate in enumerate(top_candidates, start=1):
        meme = download_meme(candidate, rank)
        memes.append(meme)
        print(f"  #{rank}: {meme['title']} ({meme['votes']} upvotes)")

    return memes


def build_html(memes, columns):
    """Build an HTML grid/gallery digest, images referenced via cid: for inline display."""
    cards = []
    for m in memes:
        title = escape(m["title"])
        cards.append(f"""
        <td style="padding:8px; vertical-align:top; width:{100 // columns}%;">
          <a href="{escape(m['post_url'])}" style="text-decoration:none; color:inherit;">
            <div style="border:1px solid #e0e0e0; border-radius:10px; overflow:hidden; font-family:Arial,Helvetica,sans-serif;">
              <div style="position:relative;">
                <img src="cid:{m['cid']}" alt="{title}" style="display:block; width:100%; height:180px; object-fit:cover;">
                <span style="position:absolute; top:6px; left:6px; background:rgba(0,0,0,0.65); color:#fff; font-size:12px; padding:2px 7px; border-radius:12px;">#{m['rank']}</span>
              </div>
              <div style="padding:10px;">
                <div style="font-size:13px; color:#222; line-height:1.35; max-height:52px; overflow:hidden;">{title}</div>
                <div style="font-size:12px; color:#888; margin-top:6px;">&#9650; {m['votes']:,} upvotes</div>
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
        lines.append(f"#{m['rank']} - {m['title']} ({m['votes']} upvotes) - {m['post_url']}")
    return "\n".join(lines)


def send_email(subject, memes, columns, sender, app_password, recipient):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    # multipart/related holds the HTML body + its inline (cid) images
    msg_related = MIMEMultipart("related")
    msg.attach(msg_related)

    msg_alt = MIMEMultipart("alternative")
    msg_related.attach(msg_alt)

    msg_alt.attach(MIMEText(build_plain_text(memes), "plain"))
    msg_alt.attach(MIMEText(build_html(memes, columns), "html"))

    # inline copies (referenced by the HTML via cid:)
    for m in memes:
        img = MIMEImage(m["image_bytes"], _subtype=m["subtype"])
        img.add_header("Content-ID", f"<{m['cid']}>")
        img.add_header("Content-Disposition", "inline", filename=m["filename"])
        msg_related.attach(img)

    # regular file attachments (same images, downloadable)
    for m in memes:
        img = MIMEImage(m["image_bytes"], _subtype=m["subtype"])
        img.add_header("Content-Disposition", "attachment", filename=m["filename"])
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
    send_email(
        subject=f"Top {len(memes)} memes of the day",
        memes=memes,
        columns=columns,
        sender=sender,
        app_password=app_password,
        recipient=recipient,
    )
    print("Sent!")


if __name__ == "__main__":
    main()
