#!/usr/bin/env python3
"""
9gag Top Memes of the Day -> Email

Fetches posts from 9gag's "hot" feed with more than VOTE_THRESHOLD upvotes
and emails them all as attachments in a single email via Gmail.

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
       export VOTE_THRESHOLD="5000"      # optional, defaults to 5000
       export MAX_MEMES="20"             # optional, cap on attachments per email

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
from email.message import EmailMessage
from urllib.parse import urlencode

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

MAX_PAGES = 10  # safety cap on how many feed pages to walk while searching


def fetch_hot_pages(max_pages=MAX_PAGES):
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


def get_top_memes(vote_threshold, max_memes):
    """Return a list of (title, votes, image_bytes, filename) above vote_threshold."""
    matches = []
    seen_ids = set()

    for posts in fetch_hot_pages():
        page_had_qualifying_post = False

        for post in posts:
            post_id = post.get("id")
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            if post.get("nsfw", 0):
                continue
            if post.get("type") not in ("Photo", "Animated"):
                continue

            votes = post.get("upVoteCount", 0)
            if votes <= vote_threshold:
                continue

            page_had_qualifying_post = True
            title = post.get("title", "Untitled meme")
            image_url = post["images"]["image700"]["url"]

            img_resp = requests.get(image_url, headers=HEADERS, timeout=15)
            img_resp.raise_for_status()

            ext = image_url.split(".")[-1].split("?")[0]
            safe_title = "".join(c if c.isalnum() or c in " -_" else "" for c in title)[:50].strip()
            filename = f"{votes}_{safe_title or post_id}.{ext}"

            matches.append((title, votes, img_resp.content, filename))
            print(f"  matched: {title} ({votes} upvotes)")

            if len(matches) >= max_memes:
                return matches

        # Since the hot feed is ranked by score (not strictly by vote count),
        # stop paging once a whole page had no posts above the threshold —
        # further pages are unlikely to have anything higher.
        if not page_had_qualifying_post:
            break

    return matches


def send_email(subject, body, attachments, sender, app_password, recipient):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    for _, _, image_bytes, filename in attachments:
        subtype = filename.split(".")[-1]
        if subtype == "jpg":
            subtype = "jpeg"
        msg.add_attachment(image_bytes, maintype="image", subtype=subtype, filename=filename)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)


def main():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("MEME_RECIPIENT")
    vote_threshold = int(os.environ.get("VOTE_THRESHOLD", "5000"))
    max_memes = int(os.environ.get("MAX_MEMES", "20"))

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("MEME_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching hot memes with more than {vote_threshold} upvotes...")
    memes = get_top_memes(vote_threshold, max_memes)

    if not memes:
        print(f"No memes found above {vote_threshold} upvotes today — not sending an email.")
        return

    print(f"Found {len(memes)} meme(s). Emailing to {recipient}...")

    lines = [f"Today's top memes from 9gag (more than {vote_threshold} upvotes):", ""]
    for title, votes, _, _ in memes:
        lines.append(f"- {title} ({votes} upvotes)")
    body = "\n".join(lines)

    send_email(
        subject=f"Top memes of the day: {len(memes)} posts over {vote_threshold} upvotes",
        body=body,
        attachments=memes,
        sender=sender,
        app_password=app_password,
        recipient=recipient,
    )
    print("Sent!")


if __name__ == "__main__":
    main()
